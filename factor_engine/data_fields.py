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
        #
        # 尺度修正(2026-08-16,PRICE_SCALE_CONTRACT.md §3):
        # `turnover` 與 `volume` **永遠是原始值**(成交金額是尺度不變量、成交量
        # 未被還原),所以 turnover/volume 算出來的是**原始價空間**的 vwap;
        # 而同一列的 `close` 是還原價。兩者直接相比會差一個因子 ——
        # 實測 2327 分割前 2025-08-13:vwap = 546.50 而 close_adj = 135.53,
        # 差 4.03 倍,於是 `close/vwap - 1` 這種因子會直接變成 -75%。
        # 這裡把 vwap 乘上價格因子換算到與 `close` 同一個空間;沒有因子欄
        # (未開自建還原)時因子為 1,行為不變。
        vwap_raw = turnover / vol.replace(0, np.nan)
        if "adj_factor_price" in out.columns:
            factor = pd.to_numeric(out["adj_factor_price"], errors="coerce")
            out["vwap"] = vwap_raw * factor.fillna(1.0)
            # 原始空間的 vwap 也留著:要跟真實成交價比較時該用這一欄。
            out["vwap_raw"] = vwap_raw
        else:
            out["vwap"] = vwap_raw
        # 成交金額是尺度不變量(那天真正換手的錢),永遠不調整。
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
