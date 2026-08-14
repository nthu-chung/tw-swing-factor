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


def load_disposition_days(all_dates) -> Dict[str, set]:
    """合併上市與上櫃處置期間；啟用後缺任一市場資料即拒絕回測。"""
    if not getattr(config, "BT_MODEL_DISPOSITION", False):
        return {}
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"
    sources = {
        "上市(TWSE,推導)": config.CACHE_DIR / f"disposition__ALL__{snap}.pkl",
        "上櫃(TPEx,真實)": config.CACHE_DIR / f"disposition_tpex__ALL__{snap}.pkl",
    }
    frames, loaded, missing = [], [], []
    for label, path in sources.items():
        if not path.exists():
            missing.append(f"{label}→{path.name}")
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
        import twse_disposition

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
