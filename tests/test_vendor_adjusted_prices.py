# -*- coding: utf-8 -*-
"""付費還原資料集必須補上 as-traded 欄位並重新錨定(PRICE_SCALE_CONTRACT.md)。

為什麼不能直接用供應商的還原檔(2026-08-16 實測):

1. 它**只調價、不調量**(LSEG 分類上的 RPO)。2327 分割前 2025-08-13,adj 檔的
   Trading_Volume / Trading_money 與原始檔逐格相同,於是 turnover/volume 算出的
   vwap = 546.50 而同列 close = 135.53,差 4.03 倍。
2. 它沒有 as-traded 欄位,執行層就只能拿還原價判 tick 帶、整張資金門檻與漲跌停
   (2327 一張 759,000 vs 147,245,差 5.15 倍)。
3. 它的錨是 latest_bar:每次除權息回頭改寫整段歷史,凍結績效無法重現。

第 3 點可解的關鍵性質:供應商的 adj_v[t] = raw[t] × F[t],而新事件只是把**所有**
F[t] 乘上同一個常數,所以 **F[t]/F[0] 對錨不變**。用這個比值重新錨定,就能拿到
供應商的還原品質(涵蓋分割、減資、面額變更 —— 自建鏈修不到那些)同時可重現。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

import config
import data
from data import price_adjust


DAYS = pd.to_datetime(["2025-08-12", "2025-08-13", "2025-08-25", "2025-08-26"])
RAW_CLOSE = [542.0, 546.0, 143.0, 138.5]        # 2327 真實成交價(含分割跳空)
# 供應商的 latest_bar 因子(乘數:adj = raw × F)。實測 2327 分割前
# close_raw/close_adj = 4.028701,所以乘數是它的倒數。
VENDOR_F = [1 / 4.028701, 1 / 4.028701, 1 / 1.007175, 1 / 1.007175]


def _raw_frame():
    return pd.DataFrame({
        "date": DAYS, "open": RAW_CLOSE, "high": RAW_CLOSE,
        "low": RAW_CLOSE, "close": RAW_CLOSE,
        "volume": [1426843, 2996695, 26586959, 25422011],
        "turnover": [772481381.0, 1637693545.0, 3792278409.0, 3536782673.0],
    })


def _vendor_adj_frame():
    adj = [c * f for c, f in zip(RAW_CLOSE, VENDOR_F)]
    return pd.DataFrame({
        "date": DAYS, "open": adj, "high": adj, "low": adj, "close": adj,
        # 供應商的量與金額**未調整**,與原始檔相同 —— 這正是問題 1。
        "volume": [1426843, 2996695, 26586959, 25422011],
        "turnover": [772481381.0, 1637693545.0, 3792278409.0, 3536782673.0],
    })


class VendorAdjustedPriceTest(unittest.TestCase):
    def _build(self, anchor=price_adjust.ANCHOR_SERIES_START):
        with (
            mock.patch.object(config, "PRICE_ADJUST_ANCHOR", anchor),
            mock.patch.object(data, "fetch_price",
                              side_effect=lambda *a, **k: _raw_frame()),
        ):
            return data._vendor_adjusted_with_raw("2327", _vendor_adj_frame())

    def test_as_traded_columns_are_attached(self):
        out = self._build()
        self.assertEqual(list(out["close_raw"]), RAW_CLOSE)

    def test_series_start_anchor_puts_the_true_price_at_the_beginning(self):
        out = self._build()
        self.assertAlmostEqual(float(out.iloc[0]["close"]), RAW_CLOSE[0], places=6)
        self.assertAlmostEqual(float(out.iloc[0]["adj_factor_price"]), 1.0, places=9)

    def test_returns_match_the_vendor_series(self):
        """重新錨定只差常數倍率 —— 報酬必須與供應商原序列相同。"""
        out = self._build()
        vendor = _vendor_adj_frame()["close"].pct_change().dropna().round(10)
        ours = out["close"].pct_change().dropna().round(10)
        self.assertEqual(list(vendor), list(ours))

    def test_split_shows_the_true_return_not_the_fake_crash(self):
        out = self._build()
        rets = out["close"].pct_change().round(4).tolist()
        raw_rets = out["close_raw"].pct_change().round(4).tolist()
        self.assertGreater(rets[2], 0.0, "還原後分割日是正報酬")
        self.assertLess(raw_rets[2], -0.70, "原始價那根是 -73.8% 的假崩盤")

    def test_factor_step_matches_the_official_split_ratio(self):
        """因子跳幅 = 那一次事件的比例(TWSE 官方 546.00/136.50 = 4.0)。"""
        out = self._build()
        f = out["adj_factor_price"]
        self.assertAlmostEqual(float(f.iloc[2] / f.iloc[1]), 4.0, places=3)

    def test_volume_and_turnover_stay_raw(self):
        out = self._build()
        self.assertEqual(list(out["volume"]), list(_raw_frame()["volume"]))
        self.assertEqual(list(out["turnover"]), list(_raw_frame()["turnover"]))

    def test_share_factor_is_not_faked_from_the_price_factor(self):
        """close 比值分不出配股與現金股利,誠實標 1.0(CFACPR != CFACSHR)。"""
        out = self._build()
        self.assertTrue((out["adj_factor_share"] == 1.0).all())

    def test_latest_bar_anchor_reproduces_the_vendor_levels(self):
        out = self._build(anchor=price_adjust.ANCHOR_LATEST_BAR)
        vendor = _vendor_adj_frame()["close"].round(6).tolist()
        self.assertEqual(out["close"].round(6).tolist(), vendor)

    def test_missing_raw_prices_fail_closed(self):
        with (
            mock.patch.object(config, "PRICE_ADJUST_ANCHOR",
                              price_adjust.ANCHOR_SERIES_START),
            mock.patch.object(data, "fetch_price",
                              side_effect=lambda *a, **k: pd.DataFrame()),
        ):
            with self.assertRaises(RuntimeError):
                data._vendor_adjusted_with_raw("2327", _vendor_adj_frame())

    def test_provenance_is_recorded_on_the_frame(self):
        out = self._build()
        self.assertEqual(out.attrs["adjustment_source"], "vendor_adj")
        self.assertEqual(out.attrs["adjustment_anchor"],
                         price_adjust.ANCHOR_SERIES_START)


if __name__ == "__main__":
    unittest.main()
