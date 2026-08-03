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
import dynamic_universe


def build_light_panel(symbols: List[str], verbose: bool = False,
                      apply_membership: bool = True) -> pd.DataFrame:
    """只用 price + inst 建 panel,欄位對齊 _prepare_panel 中 S19 需要的部分。"""
    rows = []
    for i, sid in enumerate(symbols, 1):
        px = data.fetch_price(sid)
        if px is None or px.empty:
            continue
        d = px[["date", "open", "high", "low", "close", "volume", "turnover"]].copy()
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

        d["stock_id"] = sid
        rows.append(d)
        if verbose and i % 50 == 0:
            print(f"  [light] {i}/{len(symbols)}", flush=True)

    if not rows:
        return pd.DataFrame()
    panel = pd.concat(rows, ignore_index=True)
    panel["name"] = panel["stock_id"]

    if not apply_membership:
        # 呼叫端要先套 PIT 候選池成員資格,再自行算動態 universe
        # (順序不能顛倒:動態 top-N 必須在當日候選池「之內」排名)
        return panel.sort_values(["date", "stock_id"]).reset_index(drop=True)

    panel = dynamic_universe.add_membership(
        panel,
        top_n=config.DYNAMIC_UNIVERSE_TOP_N,
        lookback=config.DYNAMIC_UNIVERSE_LOOKBACK,
        min_obs=config.DYNAMIC_UNIVERSE_MIN_OBS,
        min_avg_volume_lots=config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS,
        min_avg_turnover=config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER,
    )
    return panel.sort_values(["date", "stock_id"]).reset_index(drop=True)


def verify_equivalence(reference_panel: pd.DataFrame,
                       symbols: Optional[List[str]] = None) -> Tuple[bool, str]:
    """比對精簡路徑與 `_prepare_panel` 的 trend_ok / in_dynamic_universe。

    reference_panel 必須是同一快照、`keep_non_members=True` 的完整 panel。
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
