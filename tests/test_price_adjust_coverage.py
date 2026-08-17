# -*- coding: utf-8 -*-
"""自建還原鏈的涵蓋度:靜默丟棄與減資缺口(修正清單第 1 項的資料側)。

兩個原缺陷:

1. `fetch_dividend_events` 用 `factor.between(FACTOR_MIN, FACTOR_MAX)` **靜默丟棄**
   超出區間的事件。實測 1808 潤隆 2024-09-26:before=119.50 / after=52.95 →
   factor=0.443096 被丟掉且不報錯,結果「已還原」序列在該日仍留著 raw 的
   119.5 → 53.5(-55.23%)。官方 TaiwanStockPriceAdj 同一天的因子階梯正是
   ×2.256652(= 1/0.443134),證明那是真事件。

2. 減資**完全不在** `TaiwanStockDividendResult` 裡,而全市場 2015~2026 共 532 筆
   減資有 139 筆(26.1%)的價格跳幅小於 `PRICE_INTEGRITY_RETURN_THRESHOLD=0.11`,
   殘留斷點掃描結構上看不到。實例:1808 2025-11-24 raw 報酬 +2.61%(遠低於門檻),
   正確報酬是 -4.87%,單日誤差 7.5 個百分點 —— 對 8% 硬停損是決定性的。
   「還原不涵蓋 + 掃描看不到」是雙盲,必須從資料源頭補。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import config
import data
from data import price_adjust

SNAP = "2026-06-22"


def _div(rows):
    return pd.DataFrame(
        [{"date": d, "before_price": b, "after_price": a} for d, b, a in rows])


def _capred(rows):
    return pd.DataFrame(
        [{"date": d, "stock_id": sid,
          "ClosingPriceonTheLastTradingDay": b,
          "PostReductionReferencePrice": a,
          "ReasonforCapitalReduction": reason}
         for d, sid, b, a, reason in rows])


class AdjustmentCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        for p in (mock.patch.object(config, "CACHE_DIR", self.cache),
                  mock.patch.object(config, "SNAPSHOT_END_DATE", SNAP)):
            p.start()
            self.addCleanup(p.stop)

    def _patch_api(self, div_rows, capred_rows):
        def _fake(dataset, data_id, start, end):
            if dataset == price_adjust.DIVIDEND_RESULT_DATASET:
                return _div(div_rows)
            if dataset == price_adjust.CAPITAL_REDUCTION_DATASET:
                return _capred(capred_rows)
            raise AssertionError(f"未預期的 dataset {dataset}")
        return mock.patch.object(data, "fetch_finmind_dataset", side_effect=_fake)

    def test_out_of_range_event_is_reported_not_silently_dropped(self):
        """1808 的 factor=0.443 過去被靜默丟掉,現在必須出現在 uncovered。"""
        with self._patch_api([("2024-09-26", 119.50, 52.95)], []):
            events, uncovered = price_adjust.fetch_adjustment_events("1808")
        self.assertTrue(events.empty, "超出區間的因子不得被套用(硬猜係數更危險)")
        self.assertEqual(len(uncovered), 1)
        self.assertAlmostEqual(float(uncovered.iloc[0]["factor"]), 0.443096,
                               places=5)

    def test_in_range_dividend_is_still_applied(self):
        with self._patch_api([("2025-07-15", 100.0, 97.0)], []):
            events, uncovered = price_adjust.fetch_adjustment_events("2330")
        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0]["source"], "dividend")
        self.assertTrue(uncovered.empty)

    def test_capital_reduction_is_merged_into_the_factor_chain(self):
        """減資不在 DividendResult 裡,必須從專屬資料集接進來。"""
        with self._patch_api(
                [], [("2024-11-11", "3041", 19.75, 32.91, "Making up losses")]):
            events, uncovered = price_adjust.fetch_adjustment_events("3041")
        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0]["source"], "capital_reduction")
        # 減資後股數變少、參考價上升 → factor > 1,回溯還原把減資前放大到今天尺度
        self.assertAlmostEqual(float(events.iloc[0]["factor"]), 32.91 / 19.75,
                               places=6)

    def test_small_capital_reduction_below_scan_threshold_is_now_covered(self):
        """跳幅 < 0.11 的減資:掃描看不到,所以只能靠資料源覆蓋。"""
        with self._patch_api([], [("2025-11-24", "1808", 100.0, 105.0, "現金減資")]):
            events, _ = price_adjust.fetch_adjustment_events("1808")
        jump = abs(105.0 / 100.0 - 1.0)
        self.assertLess(jump, config.PRICE_INTEGRITY_RETURN_THRESHOLD,
                        "這個案例的前提就是它低於殘留掃描門檻")
        self.assertEqual(len(events), 1)

    def test_capital_reduction_explains_an_out_of_range_dividend_row(self):
        with self._patch_api([("2024-11-11", 119.5, 52.95)],
                             [("2024-11-11", "1808", 119.5, 52.95, "現金減資")]):
            events, uncovered = price_adjust.fetch_adjustment_events("1808")
        self.assertTrue(uncovered.empty, "同一天有減資紀錄就算已被解釋")
        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0]["source"], "capital_reduction")

    def test_only_the_requested_stock_is_returned_from_the_market_wide_table(self):
        rows = [("2024-11-11", "3041", 19.75, 32.91, "x"),
                ("2025-08-25", "3290", 28.65, 36.64, "現金減資")]
        with self._patch_api([], rows):
            self.assertEqual(len(price_adjust.fetch_capital_reduction_events("3041")), 1)
            self.assertEqual(len(price_adjust.fetch_capital_reduction_events("3290")), 1)
            self.assertTrue(price_adjust.fetch_capital_reduction_events("2330").empty)

    def test_frame_reports_whether_the_adjustment_was_complete(self):
        price = pd.DataFrame({
            "date": pd.bdate_range("2024-09-20", periods=10),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        })
        with self._patch_api([("2024-09-26", 119.50, 52.95)], []):
            out = price_adjust.adjust_price_frame("1808", price)
        self.assertFalse(out.attrs["adjustment_complete"])
        self.assertEqual(out.attrs["adjustment_uncovered"][0][0], "2024-09-26")

        with self._patch_api([("2025-07-15", 100.0, 97.0)], []):
            clean = price_adjust.adjust_price_frame("2330", price)
        self.assertTrue(clean.attrs["adjustment_complete"])
        self.assertEqual(clean.attrs["adjustment_sources"], {"dividend": 1})

    def test_legacy_cache_without_in_range_is_refetched(self):
        """舊格式快取是**已被過濾過**的,直接沿用等於讓丟掉的事件永遠回不來。"""
        legacy = pd.DataFrame({"date": [pd.Timestamp("2020-01-01")],
                               "factor": [0.98]})
        legacy.to_pickle(self.cache / f"divresult__1808__{SNAP}.pkl")
        with self._patch_api([("2024-09-26", 119.50, 52.95)], []) as api:
            out = price_adjust.fetch_dividend_events("1808")
        self.assertTrue(api.called, "舊格式必須重抓,不能沿用被過濾過的結果")
        self.assertIn("in_range", out.columns)



class RawColumnContractTest(unittest.TestCase):
    """`PRICE_SCALE_CONTRACT.md` 鐵則一的第一步:as-traded 價格必須另存一份。

    還原價是「今日等值」單位,而台股有一整組規則看**絕對價位**:tick 價格帶、
    整張 1000 股的資金門檻、20 元最低手續費、±10% 漲跌停。實測 2327 在
    2024-06-24 買一張的真實成本是 759,000 元,用還原價算只要 147,245 元
    (5.15 倍);12 檔樣本裡有 2 檔還原後落進不同的 tick 帶。少了 `*_raw`,
    執行層只能拿還原價去判這些規則。
    """

    def _price(self):
        return pd.DataFrame({
            "date": pd.bdate_range("2025-08-01", periods=6),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 1000, "turnover": 100000.0,
        })

    def _events(self):
        return pd.DataFrame({"date": [pd.Timestamp("2025-08-06")],
                             "factor": [0.5], "source": ["dividend"]})

    def test_raw_columns_keep_the_as_traded_price(self):
        out = price_adjust.adjust_prices(self._price(), self._events())
        self.assertTrue((out["close_raw"] == 100.0).all())

    def test_anchor_latest_bar_puts_the_true_price_at_the_end(self):
        """back-adjusted:最後一根 = 今天的真實價,事件之前被縮小。"""
        with mock.patch.object(config, "PRICE_ADJUST_ANCHOR",
                               price_adjust.ANCHOR_LATEST_BAR):
            out = price_adjust.adjust_prices(self._price(), self._events())
        self.assertAlmostEqual(float(out.iloc[0]["close"]), 50.0)
        self.assertAlmostEqual(float(out.iloc[-1]["close"]), 100.0)

    def test_anchor_series_start_puts_the_true_price_at_the_beginning(self):
        """forward-adjusted(預設):起點 = 真實價,事件之後被放大。"""
        with mock.patch.object(config, "PRICE_ADJUST_ANCHOR",
                               price_adjust.ANCHOR_SERIES_START):
            out = price_adjust.adjust_prices(self._price(), self._events())
        self.assertAlmostEqual(float(out.iloc[0]["close"]), 100.0)
        self.assertAlmostEqual(float(out.iloc[-1]["close"]), 200.0)

    def test_both_anchors_give_identical_returns(self):
        """換錨只差一個常數倍率 —— 報酬必須完全相同,策略損益不受影響。"""
        rets = {}
        for anchor in (price_adjust.ANCHOR_LATEST_BAR,
                       price_adjust.ANCHOR_SERIES_START):
            with mock.patch.object(config, "PRICE_ADJUST_ANCHOR", anchor):
                out = price_adjust.adjust_prices(self._price(), self._events())
            rets[anchor] = out["close"].pct_change().dropna().round(12).tolist()
        self.assertEqual(rets[price_adjust.ANCHOR_LATEST_BAR],
                         rets[price_adjust.ANCHOR_SERIES_START])

    def test_a_new_event_never_rewrites_earlier_adjusted_values(self):
        """凍結績效可重現的唯一來源:新事件不得回頭改動舊值。"""
        later = pd.DataFrame({
            "date": [pd.Timestamp("2025-08-06"), pd.Timestamp("2025-08-08")],
            "factor": [0.5, 0.9], "source": ["dividend", "dividend"]})
        with mock.patch.object(config, "PRICE_ADJUST_ANCHOR",
                               price_adjust.ANCHOR_SERIES_START):
            before = price_adjust.adjust_prices(self._price(), self._events())
            after = price_adjust.adjust_prices(self._price(), later)
        pd.testing.assert_series_equal(
            before["close"].iloc[:5], after["close"].iloc[:5])

    def test_unknown_anchor_fails_closed(self):
        with mock.patch.object(config, "PRICE_ADJUST_ANCHOR", "yolo"):
            with self.assertRaises(ValueError):
                price_adjust.adjust_prices(self._price(), self._events())

    def test_adj_factor_price_is_the_multiplier_from_raw_to_adjusted(self):
        """契約:adjusted == raw × adj_factor_price(vwap 換算與股數換算都靠它)。"""
        out = price_adjust.adjust_prices(self._price(), self._events())
        self.assertTrue(
            ((out["close_raw"] * out["adj_factor_price"] - out["close"]).abs()
             < 1e-9).all())

    def test_volume_and_turnover_are_never_adjusted(self):
        out = price_adjust.adjust_prices(self._price(), self._events())
        self.assertTrue((out["volume"] == 1000).all())
        self.assertTrue((out["turnover"] == 100000.0).all())

    def test_price_and_share_factors_are_separate_columns(self):
        """CRSP 的 CFACPR/CFACSHR 在 spin-off/rights 時不相等,不可共用一欄。"""
        out = price_adjust.adjust_prices(self._price(), self._events())
        self.assertIn("adj_factor_price", out.columns)
        self.assertIn("adj_factor_share", out.columns)
        # 現金股利不調量 → share 因子恆為 1(CRSP / zipline 慣例)
        self.assertTrue((out["adj_factor_share"] == 1.0).all())

    def test_no_events_still_emits_the_contract_columns(self):
        out = price_adjust.adjust_prices(self._price(),
                                         pd.DataFrame(columns=["date", "factor"]))
        for col in ("close_raw", "adj_factor_price", "adj_factor_share"):
            self.assertIn(col, out.columns)
        self.assertTrue((out["close_raw"] == out["close"]).all())

if __name__ == "__main__":
    unittest.main()
