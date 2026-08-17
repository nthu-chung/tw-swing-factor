# -*- coding: utf-8 -*-
"""vwap 的尺度必須與同一列的 close 一致(PRICE_SCALE_CONTRACT.md §3)。

原缺陷:`turnover` 與 `volume` **永遠是原始值**(成交金額是尺度不變量、成交量
未被還原),所以 `turnover / volume` 算出來的是**原始價空間**的 vwap;而同一列
的 `close` 是還原價。兩者直接相比會差一個因子。

實測 2327 分割前 2025-08-13:vwap = 546.50 而 close_adj = 135.53,差 4.03 倍,
於是 `close / vwap - 1` 這種因子會從 -0.09% 變成 **-75.20%** —— 不是誤差,
是把一個接近零的量變成極端值,任何用到它的排名都會被這一檔洗版。
"""
from __future__ import annotations

import unittest

import pandas as pd

from factor_engine import attach_fields
from factor_engine.operators import PanelOps


RAW_CLOSE = 546.0
ADJ_CLOSE = 135.527555
FACTOR = ADJ_CLOSE / RAW_CLOSE          # 2327 分割前的實際因子


def _frame(with_factor: bool = True) -> pd.DataFrame:
    df = pd.DataFrame({
        "date": pd.bdate_range("2025-08-11", periods=3),
        "stock_id": ["2327"] * 3,
        "open": [ADJ_CLOSE] * 3, "high": [ADJ_CLOSE] * 3,
        "low": [ADJ_CLOSE] * 3, "close": [ADJ_CLOSE] * 3,
        "volume": [2_996_695] * 3,
        "turnover": [1_637_693_545.0] * 3,
    })
    if with_factor:
        df["adj_factor_price"] = FACTOR
    return df


def _fields(df: pd.DataFrame) -> pd.DataFrame:
    return attach_fields(df, PanelOps(df["date"], df["stock_id"]))


class VwapScaleTest(unittest.TestCase):
    def test_vwap_is_converted_to_the_close_price_space(self):
        out = _fields(_frame())
        row = out.iloc[-1]
        self.assertAlmostEqual(row["close"] / row["vwap"] - 1.0, 0.0, places=2)

    def test_raw_space_vwap_is_still_available(self):
        """要跟真實成交價比較時該用 vwap_raw,所以它必須留著。"""
        out = _fields(_frame())
        self.assertAlmostEqual(float(out.iloc[-1]["vwap_raw"]), 546.50, places=1)

    def test_without_the_factor_column_behaviour_is_unchanged(self):
        """沒開自建還原時沒有因子欄,vwap 維持原本的 turnover/volume。"""
        out = _fields(_frame(with_factor=False))
        self.assertAlmostEqual(float(out.iloc[-1]["vwap"]), 546.50, places=1)
        self.assertNotIn("vwap_raw", out.columns)

    def test_the_defect_it_fixes_is_an_extreme_value_not_a_rounding_error(self):
        """釘住量級:未換算時 close/vwap-1 是 -75%,不是小數點誤差。"""
        out = _fields(_frame())
        row = out.iloc[-1]
        broken = row["close"] / row["vwap_raw"] - 1.0
        self.assertLess(broken, -0.70)

    def test_dollar_volume_stays_on_the_raw_money_scale(self):
        """成交金額是尺度不變量(那天真正換手的錢),不得被調整。"""
        out = _fields(_frame())
        self.assertAlmostEqual(float(out.iloc[-1]["dollar_volume"]),
                               1_637_693_545.0)


if __name__ == "__main__":
    unittest.main()
