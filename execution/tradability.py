# -*- coding: utf-8 -*-
"""台股日線回測目前能可靠表達的可成交性限制。

這裡只回答「歷史訊號在當時是否可能成交」，供 backtest 引擎使用。人工操作端仍然
只接收候選清單；本模組不建立訂單、不連券商，也不代表真實盤中撮合模擬。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

import pandas as pd

import config
from .taiwan_rules import stock_price_limits


def _covering_disposition_cache(dataset: str, snapshot: str,
                                need_start, need_end):
    """找涵蓋 [need_start, need_end] 的處置快取(範圍在檔名裡)。

    2026-08-15 修:舊版直接拼 `disposition__ALL__{snapshot}.pkl` 讀「有就用」,
    而寫入端的檔名也不含查詢範圍 —— 於是一份只涵蓋近期的處置表會被套用到
    整段歷史,更早的期間全部被當成「沒被處置」而放行進場。現在讀取端必須
    自己確認涵蓋範圍;涵蓋不到就當作缺資料(fail-closed),不是靜默用半套。

    回傳 (path, why_not):找到就 (path, None),否則 (None, 說明字串)。
    """
    import data

    found = []
    for p in sorted(config.CACHE_DIR.glob(f"{dataset}__ALL__{snapshot}__w*.pkl")):
        meta = data.parse_window_scope(p)
        if meta and meta["dataset"] == dataset:
            found.append(meta)
    covering = [m for m in found
                if m["start"] <= str(pd.Timestamp(need_start).date())
                and m["end"] >= str(pd.Timestamp(need_end).date())]
    if covering:
        # 多份都涵蓋時取範圍最窄的(重抓次數最少、內容最貼近需求)。
        best = min(covering, key=lambda m: (m["end"], m["start"]))
        return best["path"], None
    legacy = config.CACHE_DIR / f"{dataset}__ALL__{snapshot}.pkl"
    if legacy.exists():
        return None, (f"{legacy.name}(舊格式,檔名不含查詢範圍 → 視為 miss,"
                      "不當成任意範圍的有效命中)")
    if found:
        ranges = ", ".join(f"{m['start']}~{m['end']}" for m in found[:3])
        return None, (f"{dataset} 快取只涵蓋 {ranges},未涵蓋回測需要的 "
                      f"{pd.Timestamp(need_start).date()}~{pd.Timestamp(need_end).date()}")
    return None, f"{dataset}__ALL__{snapshot}__w*.pkl 不存在"


def load_disposition_days(all_dates) -> Dict[str, set]:
    """合併上市與上櫃處置期間；啟用後缺任一市場資料即拒絕回測。"""
    if not getattr(config, "BT_MODEL_DISPOSITION", False):
        return {}
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"
    days = pd.DatetimeIndex(sorted(pd.to_datetime(list(all_dates))))
    if len(days) == 0:
        raise RuntimeError("BT_MODEL_DISPOSITION 已開啟但沒有交易日可比對範圍")
    sources = {
        "上市(TWSE,推導)": "disposition",
        "上櫃(TPEx,真實)": "disposition_tpex",
    }
    frames, loaded, missing = [], [], []
    for label, dataset in sources.items():
        path, why = _covering_disposition_cache(dataset, snap, days[0], days[-1])
        if path is None:
            missing.append(f"{label}→{why}")
            continue
        try:
            frames.append(pd.read_pickle(path))
            loaded.append(label)
        except Exception as exc:
            missing.append(f"{label}(載入失敗 {type(exc).__name__})")
    if not frames:
        raise RuntimeError(
            f"BT_MODEL_DISPOSITION 已開啟但無處置快取({'、'.join(missing)})；"
            "請先跑 twse_disposition.py / tpex_disposition.py"
        )
    if missing:
        raise RuntimeError(
            f"處置禁倉只有 {'、'.join(loaded)}；缺 {'、'.join(missing)}。"
            "拒絕用半套市場覆蓋回測"
        )
    try:
        from data import twse_disposition

        combined = pd.concat(frames, ignore_index=True)
        return twse_disposition.disposition_day_set(combined, all_dates)
    except Exception as exc:
        raise RuntimeError(f"處置快取合併失敗:{type(exc).__name__}") from exc


def detect_limit_lock(bar: pd.Series, prev_close: Optional[float]) -> Optional[str]:
    """依合法漲跌停價辨識一字鎖板，回傳 up/down/None。

    優先使用資料列的 `limit_up`／`limit_down`；否則以 `reference_price`，再退回前收
    推導。公司行動日若沒有官方開盤競價基準，推導值只是近似，因此資料層後續必須
    補齊 reference_price。`price_limit_exempt=True` 代表首五日等無漲跌幅情況。
    """
    if prev_close is None or prev_close <= 0:
        return None
    try:
        high = Decimal(str(bar["high"]))
        low = Decimal(str(bar["low"]))
        open_price = Decimal(str(bar["open"]))
    except (KeyError, TypeError, ValueError):
        return None
    if high != low:
        return None
    if bool(bar.get("price_limit_exempt", False)):
        return None

    try:
        upper_raw = bar.get("limit_up")
        lower_raw = bar.get("limit_down")
        if pd.notna(upper_raw) and pd.notna(lower_raw):
            upper = Decimal(str(upper_raw))
            lower = Decimal(str(lower_raw))
        else:
            reference_raw = bar.get("reference_price", prev_close)
            if reference_raw is None or pd.isna(reference_raw):
                return None
            limits = stock_price_limits(reference_raw)
            upper, lower = limits.upper, limits.lower
    except (ValueError, TypeError):
        return None

    if upper is not None and open_price == upper:
        return "up"
    if lower is not None and open_price == lower:
        return "down"
    return None
