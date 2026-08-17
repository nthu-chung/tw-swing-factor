# -*- coding: utf-8 -*-
"""
S19 上線訊號(精簡資料路徑)
============================
`backtest._prepare_panel` 會抓完整 bundle(price/inst/margin/lending/fholding)去
算全部因子,但 **S19 只用到 price 與 inst 兩個資料集**:

    訊號   ← close, volume, foreign_net, trust_net
    trend_ok        ← close(MA20/MA60)
    動態 universe   ← turnover / volume

FinMind 免費層是 600 次/小時,完整 bundle 對 300 檔要 ~1500 次(約 3 小時);
精簡路徑只要 ~600 次(約 1 小時)。這支就是為了在額度限制下仍能產生當日訊號。

⚠ 因為繞過了受測的 `_prepare_panel`,本模組**必須**通過 `verify_equivalence()`:
拿有完整資料的快照,比對這裡算出的 `trend_ok` / `in_dynamic_universe` 是否與
`_prepare_panel` 完全一致。不一致就不可使用 —— 寧可等額度也不要用沒對過的路徑
產生要下單的名單。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

import config
import data
from universes import dynamic as dynamic_universe
from factor_engine import panel_density


def _pit_name_map(panel: pd.DataFrame) -> pd.Series:
    """交易所逐日快照的**當日**公司名,index 對齊 panel。查不到回 NaN。

    為什麼優先用交易所快照而不是 `TaiwanStockInfo`:後者是**現值表**,等於拿
    「今天的名字」套到所有歷史。實測(2024-06~2026-06 的交易所逐日快照,1,955 檔)
    這段期間有 **24 檔改過名**,例如 2718 晶悅 → 全心投控(2024-12-20)、
    3171 新洲 → 炎洲流通(2024-10-07)。用現值表的話,2024 年的候選清單會出現
    一個當時還不存在的名字,對不回當年的成交紀錄。

    交易所快照的 name 是那一天的 name,而且候選池本來就要抓它
    (`load_history_cached` 有快取),不必為了顯示再打一個端點。
    """
    from universes import pit_snapshots as pit_universe

    hist = pit_universe.load_history_cached()
    if hist is None or hist.empty or "name" not in hist.columns:
        return pd.Series(np.nan, index=panel.index, dtype=object)
    names = hist[["date", "stock_id", "name"]].copy()
    names["date"] = pd.to_datetime(names["date"])
    names["stock_id"] = names["stock_id"].astype(str)
    names = names.drop_duplicates(["date", "stock_id"], keep="last")
    keyed = panel[["date", "stock_id"]].copy()
    keyed["date"] = pd.to_datetime(keyed["date"])
    keyed["stock_id"] = keyed["stock_id"].astype(str)
    merged = keyed.merge(names, on=["date", "stock_id"], how="left")
    return pd.Series(merged["name"].values, index=panel.index, dtype=object)


# 籌碼資料源。放模組層是為了讓「panel 有哪些籌碼欄位」一眼看得出來,
# 而不是散在迴圈裡。缺任何一個都不 fail —— 它們是選用欄位,策略若真的需要,
# `DataRequirements.validate_panel()` 會在自己那一關 fail-closed。
def _chip_sources():
    import data
    return (
        (data.fetch_margin, ["margin_balance", "short_balance", "margin_limit",
                             "margin_change", "short_change"]),
        (data.fetch_lending, ["lending_vol", "lending_vol_5d"]),
        (data.fetch_foreign_holding, ["foreign_ratio", "foreign_remain_ratio"]),
    )


_CHIP_SOURCES = _chip_sources()


def _attach_display_fields(panel: pd.DataFrame) -> pd.DataFrame:
    """補上**只給人看**的欄位:公司名與產業。這兩欄不參與任何計算。

    原本是 `panel["name"] = panel["stock_id"]` 的佔位符,結果人類端的候選清單
    印出「4967 4967」—— 看得到代號、看不到公司,等於還是要自己去查一次。

    名稱來源分兩層:
      1. **交易所(TWSE/TPEx)逐日快照** —— PIT 名稱,見 `_pit_name_map`。
      2. `security_type` 的證券別 registry(`TaiwanStockInfo` 現值表)補洞。
         產業別只有這一層有(交易所日報表不含產業),所以 `industry` 是**現值**,
         不是 PIT;它只用於顯示。

    兩層都查不到就留空字串,**不回填股號** —— 顯示層寧可少一個名字,也不要讓人
    以為「這家公司就叫 4967」。任一層取用失敗(離線、端點掛掉)都只是少了名字,
    不讓整個 panel 建不起來:它不是風險控制欄位。
    """
    import security_type

    ids = panel["stock_id"].astype(str)
    try:
        pit_names = _pit_name_map(panel)
    except Exception:                                       # noqa: BLE001
        pit_names = pd.Series(np.nan, index=panel.index, dtype=object)
    try:
        registry = security_type.default_registry()
    except Exception:                                       # noqa: BLE001
        registry = {}

    fallback = ids.map(lambda s: str((registry.get(s) or ("", "", ""))[2]))
    names = pit_names.astype(object).where(
        pit_names.notna() & (pit_names.astype(str).str.strip() != ""), fallback)
    panel["name"] = names.fillna("").astype(str).str.strip()
    panel["industry"] = ids.map(lambda s: str((registry.get(s) or ("", "", ""))[1]))
    return panel


def build_light_panel(symbols: List[str], verbose: bool = False,
                      apply_membership: bool = True) -> pd.DataFrame:
    """只用 price + inst 建 panel,欄位對齊 _prepare_panel 中 S19 需要的部分。"""
    rows = []
    for i, sid in enumerate(symbols, 1):
        px = data.fetch_price(sid)
        if px is None or px.empty:
            continue
        cols = ["date", "open", "high", "low", "close", "volume", "turnover"]
        # 原始(as-traded)收盤價一併帶進 panel。`close` 是還原價 —— 它是回測要的,
        # 但**不是券商螢幕上的數字**,而且用 `series_start` 錨定時,它的絕對水準
        # 還會隨抓取窗改變(報酬不變,絕對價會變)。人類端的清單只能顯示原始價,
        # 所以資料層就要把它留著;等到顯示層才想拿就已經沒有了。
        if "close_raw" in px.columns:
            cols.append("close_raw")
        d = px[cols].copy()
        d["date"] = pd.to_datetime(d["date"])
        d = d.sort_values("date").reset_index(drop=True)

        # trend_ok:與 factors.py 逐字對齊(MA20>MA60、MA60 5日斜率>0、收盤>MA60)
        ma_s = d["close"].rolling(config.MA_SHORT).mean()
        ma_l = d["close"].rolling(config.MA_LONG).mean()
        d["ma_short"] = ma_s
        d["ma_long"] = ma_l
        d["ma_long_slope"] = ma_l.diff(5)
        d["trend_ok"] = (ma_s > ma_l) & (ma_l.diff(5) > 0) & (d["close"] > ma_l)

        inst = data.fetch_institutional(sid)
        if inst is not None and not inst.empty:
            it = inst[["date", "foreign_net", "trust_net", "dealer_net"]].copy()
            it["date"] = pd.to_datetime(it["date"])
            d = d.merge(it, on="date", how="left")
        for c in ["foreign_net", "trust_net", "dealer_net"]:
            if c not in d.columns:
                d[c] = np.nan
            d[c] = d[c].fillna(0.0)     # 無申報日補 0,不向後延用(與 factors._align 一致)

        # 籌碼欄位(融資融券／借券／外資持股)。2026-08-16 加:IC 掃描顯示
        # `short_chg20`(融券增加)的 top10 報酬是所有籌碼因子最高的,而它的
        # IC 幾乎是 0 —— 只看 IC 會整個錯過。缺資料留 NaN,不補 0:
        # 「沒申報」與「餘額為零」是兩件事,補 0 會讓前者冒充後者。
        for fetch, cols in _CHIP_SOURCES:
            try:
                c = fetch(sid)
            except Exception:
                continue
            if c is None or getattr(c, "empty", True):
                continue
            keep = ["date"] + [x for x in cols if x in c.columns]
            cc = c[keep].copy()
            cc["date"] = pd.to_datetime(cc["date"])
            d = d.merge(cc, on="date", how="left")

        d["stock_id"] = sid
        rows.append(d)
        if verbose and i % 50 == 0:
            print(f"  [light] {i}/{len(symbols)}", flush=True)

    if not rows:
        return pd.DataFrame()
    panel = pd.concat(rows, ignore_index=True)
    panel = _attach_display_fields(panel)

    if not apply_membership:
        # 呼叫端要先套 PIT 候選池成員資格,再自行算動態 universe
        # (順序不能顛倒:動態 top-N 必須在當日候選池「之內」排名)
        return panel_density.tag(
            panel.sort_values(["date", "stock_id"]).reset_index(drop=True),
            panel_density.DENSE,
        )

    panel = dynamic_universe.add_membership(
        panel,
        top_n=config.DYNAMIC_UNIVERSE_TOP_N,
        lookback=config.DYNAMIC_UNIVERSE_LOOKBACK,
        min_obs=config.DYNAMIC_UNIVERSE_MIN_OBS,
        min_avg_volume_lots=config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
        min_avg_turnover=config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER,
    )
    # add_membership 只加旗標、不刪列 → 兩條路回傳的都是稠密 panel(可安全算 ts_)。
    # 明確標記,S19 的正式 PIT 路徑不經過 _prepare_panel,標籤要在這裡補上。
    return panel_density.tag(
        panel.sort_values(["date", "stock_id"]).reset_index(drop=True),
        panel_density.DENSE,
    )


def verify_equivalence(reference_panel: pd.DataFrame,
                       symbols: Optional[List[str]] = None) -> Tuple[bool, str]:
    """比對精簡路徑與 `_prepare_panel` 的 trend_ok / in_dynamic_universe。

    reference_panel 必須是同一快照、`backtest.build_research_panel()` 產生的稠密 panel。
    回傳 (是否一致, 說明)。不一致時說明會指出差異筆數。
    """
    syms = symbols or sorted(reference_panel["stock_id"].unique())
    light = build_light_panel(syms)
    if light.empty:
        return False, "精簡 panel 為空"

    ref = reference_panel[["date", "stock_id", "trend_ok", "in_dynamic_universe"]].copy()
    lit = light[["date", "stock_id", "trend_ok", "in_dynamic_universe"]].copy()
    m = ref.merge(lit, on=["date", "stock_id"], suffixes=("_ref", "_lit"))
    if m.empty:
        return False, "沒有可比對的重疊列"

    t_diff = int((m["trend_ok_ref"].fillna(False) != m["trend_ok_lit"].fillna(False)).sum())
    u_diff = int((m["in_dynamic_universe_ref"].fillna(False)
                  != m["in_dynamic_universe_lit"].fillna(False)).sum())
    ok = (t_diff == 0 and u_diff == 0)
    msg = (f"比對 {len(m)} 列:trend_ok 差異 {t_diff} 筆、"
           f"in_dynamic_universe 差異 {u_diff} 筆")
    return ok, msg
