# -*- coding: utf-8 -*-
"""無視窗衍生資料欄位。

field 與 operator 的分界是「是否帶有可調視窗」。這些欄位只轉換當日資料或引用
前一個已知收盤，不包含策略搜尋參數；RSI、ATR 等有視窗指標仍屬 operator。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from .operators import PanelOps


FIELD_COLUMNS = [
    "vwap",
    "returns",
    "true_range",
    "gap",
    "intraday_ret",
    "close_loc",
    "dollar_volume",
    "amihud",
]


def attach_fields(panel: pd.DataFrame, ops: "PanelOps") -> pd.DataFrame:
    """回傳加上無視窗衍生欄位的 panel，不修改輸入資料。"""
    out = panel.copy()
    need = {"open", "high", "low", "close", "volume"}
    missing = need - set(out.columns)
    if missing:
        raise ValueError(f"attach_fields 缺少欄位: {sorted(missing)}")

    vol = pd.to_numeric(out["volume"], errors="coerce")
    close = pd.to_numeric(out["close"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")
    opn = pd.to_numeric(out["open"], errors="coerce")

    if "turnover" in out.columns:
        turnover = pd.to_numeric(out["turnover"], errors="coerce")
        # FinMind 的成交金額與成交量可形成真實日 VWAP，不使用 typical price 近似。
        out["vwap"] = turnover / vol.replace(0, np.nan)
        out["dollar_volume"] = turnover
    else:
        out["vwap"] = (high + low + close) / 3.0
        out["dollar_volume"] = close * vol

    prev_close = ops.ts_delay(close, 1)
    out["returns"] = close / prev_close.replace(0, np.nan) - 1.0
    out["gap"] = opn / prev_close.replace(0, np.nan) - 1.0
    out["intraday_ret"] = close / opn.replace(0, np.nan) - 1.0

    daily_range = high - low
    out["true_range"] = pd.concat(
        [daily_range, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    out["close_loc"] = (close - low) / daily_range.replace(0, np.nan)
    out["amihud"] = out["returns"].abs() / out["dollar_volume"].replace(0, np.nan)
    return out
