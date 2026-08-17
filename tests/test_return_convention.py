# -*- coding: utf-8 -*-
"""基準與個股的報酬口徑必須一致（含息 vs 不含息）的離線回歸測試。

原本的 bug:個股序列在 `SELF_ADJUST_PRICES=1`(預設)或
`PRICE_DATASET=TaiwanStockPriceAdj` 下是**含息**的(現金股利被還原回價格),
而基準一律用 TAIEX **價格指數**(`TaiwanStockPrice / data_id=TAIEX`,不含息)。
兩把尺不同 → 差額全部被算成策略的超額報酬,而且方向永遠是「策略比較好」。

實測(FinMind level 2,repo 回測窗 2024-06-03~2026-06-20,算術年化):

    TAIEX 價格指數  年化 42.38%  Sharpe 1.677
    TAIEX 含息指數  年化 45.23%  Sharpe 1.790
                    差 2.86pp/年            差 0.113

逐年(2015~2026)差 2.41~4.81pp,沒有一年為負 —— 系統性,不是雜訊。

這裡釘住四件事:
  1. 個股口徑的判定(還原價 / 自建還原 / 純原始價)。
  2. 基準選擇跟著個股口徑走(含息 → TaiwanStockTotalReturnIndex)。
  3. 口徑不一致 **raise**,而且不會偷偷退回另一種指數頂替。
  4. `summary["return_convention"]` 存在且值正確(結果自己說得出用哪把尺)。

全部離線:`FINMIND_TOKEN` patch 成空字串、HTTP 一律 mock,誤走真實抓取會
fail-closed 報錯而不是靜默通過。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from backtest import event_backtest
import config
import data
from data import return_convention as rc

SNAP = "2026-06-22"


# ── 1. 個股序列的口徑判定 ──────────────────────────────────────────────
class StockSeriesConventionTest(unittest.TestCase):
    def test_self_adjust_makes_the_stock_series_total_return(self):
        """自建還原價 = 除息日股利再投入 = 含息(這是預設設定)。"""
        self.assertEqual(
            rc.stock_series_convention("TaiwanStockPrice", True), rc.TOTAL_RETURN)

    def test_official_adjusted_dataset_is_total_return_even_without_self_adjust(self):
        self.assertEqual(
            rc.stock_series_convention("TaiwanStockPriceAdj", False), rc.TOTAL_RETURN)

    def test_raw_prices_without_self_adjust_are_price_return(self):
        self.assertEqual(
            rc.stock_series_convention("TaiwanStockPrice", False), rc.PRICE_RETURN)

    def test_defaults_come_from_config(self):
        with mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(config, "SELF_ADJUST_PRICES", True):
            self.assertEqual(rc.stock_series_convention(), rc.TOTAL_RETURN)
        with mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(config, "SELF_ADJUST_PRICES", False):
            self.assertEqual(rc.stock_series_convention(), rc.PRICE_RETURN)


# ── 2. 基準選擇 ────────────────────────────────────────────────────────
class BenchmarkSelectionTest(unittest.TestCase):
    def test_total_return_stocks_select_the_total_return_index(self):
        self.assertEqual(
            rc.resolve_benchmark_dataset(rc.TOTAL_RETURN, "auto"),
            "TaiwanStockTotalReturnIndex")

    def test_price_return_stocks_select_the_price_index(self):
        self.assertEqual(
            rc.resolve_benchmark_dataset(rc.PRICE_RETURN, "auto"),
            "TaiwanStockPrice")

    def test_explicit_consistent_choice_is_allowed(self):
        self.assertEqual(
            rc.resolve_benchmark_dataset(rc.PRICE_RETURN, "TaiwanStockPrice"),
            "TaiwanStockPrice")

    def test_explicit_mismatch_fails_closed(self):
        """顯式把含息個股配上價格指數 = 這次要修的 bug,必須 raise。"""
        with self.assertRaises(rc.ReturnConventionMismatch):
            rc.resolve_benchmark_dataset(rc.TOTAL_RETURN, "TaiwanStockPrice")
        with self.assertRaises(rc.ReturnConventionMismatch):
            rc.resolve_benchmark_dataset(rc.PRICE_RETURN,
                                         "TaiwanStockTotalReturnIndex")

    def test_mismatch_message_carries_the_measured_magnitude(self):
        """錯誤訊息要帶實測量級,否則下一個人只會把它當成潔癖檢查繞過去。"""
        with self.assertRaisesRegex(rc.ReturnConventionMismatch, "2.86pp"):
            rc.assert_consistent(rc.TOTAL_RETURN, rc.PRICE_RETURN)

    def test_unknown_benchmark_dataset_is_never_guessed(self):
        with self.assertRaisesRegex(ValueError, "不認得基準指數資料集"):
            rc.benchmark_index_convention("TaiwanStockSomethingElse")


# ── 3. 取序列:選對資料集、抓不到就 raise（不 fallback）──────────────────
def _tr_index() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-03"]),
        "close": [30000.0, 30150.0],
    })


def _price_index() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-03"]),
        "open": [20000.0, 20100.0], "high": [20200.0, 20300.0],
        "low": [19900.0, 20000.0], "close": [20100.0, 20200.0],
        "volume": [1e9, 1.1e9], "turnover": [3e11, 3.1e11],
    })


class FetchBenchmarkIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        p = mock.patch.object(config, "BENCHMARK_INDEX_DATASET", "auto")
        p.start()
        self.addCleanup(p.stop)

    def test_total_return_stocks_never_touch_the_price_index(self):
        with mock.patch.object(config, "SELF_ADJUST_PRICES", True), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(data, "fetch_market_total_return_index",
                               return_value=_tr_index()) as tr, \
             mock.patch.object(data, "fetch_market_index",
                               side_effect=AssertionError("不得取用價格指數")) as pi:
            out = rc.fetch_benchmark_index()
        self.assertEqual(tr.call_count, 1)
        self.assertEqual(pi.call_count, 0)
        self.assertEqual(list(out.columns), ["date", "close"])
        self.assertEqual(out.attrs["return_convention"], rc.TOTAL_RETURN)
        self.assertEqual(out.attrs["benchmark_dataset"],
                         "TaiwanStockTotalReturnIndex")

    def test_price_return_stocks_use_the_price_index(self):
        with mock.patch.object(config, "SELF_ADJUST_PRICES", False), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(data, "fetch_market_index",
                               return_value=_price_index()) as pi, \
             mock.patch.object(data, "fetch_market_total_return_index",
                               side_effect=AssertionError("不得取用含息指數")):
            out = rc.fetch_benchmark_index()
        self.assertEqual(pi.call_count, 1)
        self.assertEqual(out.attrs["return_convention"], rc.PRICE_RETURN)

    def test_missing_total_return_index_never_falls_back_to_price_index(self):
        """含息指數抓不到時**不得**退回價格指數 —— 那正是 bug 本身。"""
        with mock.patch.object(config, "SELF_ADJUST_PRICES", True), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(data, "fetch_market_total_return_index",
                               return_value=pd.DataFrame()), \
             mock.patch.object(data, "fetch_market_index",
                               side_effect=AssertionError("不得退回價格指數")):
            with self.assertRaises(rc.BenchmarkUnavailable):
                rc.fetch_benchmark_index()


# ── 4. 資料層:含息指數的快取要遵守 CacheScope（P0-2 的範圍戳）───────────
class TotalReturnIndexDataLayerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        data._legacy_cache_warned.clear()
        for p in (
            mock.patch.object(config, "CACHE_DIR", self.cache),
            mock.patch.object(config, "SNAPSHOT_END_DATE", SNAP),
            # 空 token = 任何真實抓取立刻 fail-closed，測試不可能打網路。
            mock.patch.object(config, "FINMIND_TOKEN", ""),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _raw(self) -> pd.DataFrame:
        """FinMind TaiwanStockTotalReturnIndex 的實測欄位:price / stock_id / date。"""
        return pd.DataFrame({
            "date": ["2026-01-02", "2026-01-03"],
            "stock_id": ["TAIEX", "TAIEX"],
            "price": [30000.0, 30150.0],
        })

    def test_cache_key_carries_snapshot_and_range(self):
        with mock.patch.object(data, "_finmind_get", return_value=self._raw()) as g:
            df = data.fetch_market_total_return_index()
        self.assertEqual(g.call_args[0][0], "TaiwanStockTotalReturnIndex")
        self.assertEqual(g.call_args[0][1], "TAIEX")
        expect = f"market__TAIEX_TR__{SNAP}__d{config.MARKET_HISTORY_DAYS}.pkl"
        self.assertTrue((self.cache / expect).exists(),
                        f"快取檔名不對:{sorted(p.name for p in self.cache.iterdir())}")
        self.assertEqual(list(df.columns), ["date", "close"])

    def test_different_range_is_a_cache_miss(self):
        """範圍必須進 key(不變式 7):短範圍快取不得回應長範圍請求。"""
        with mock.patch.object(data, "_finmind_get", return_value=self._raw()):
            data.fetch_market_total_return_index(730)
        with self.assertRaises(data.FinMindAPIError):
            data.fetch_market_total_return_index(2000)

    def test_price_index_cache_is_not_shared_with_total_return_index(self):
        """兩條大盤序列不可共用檔名,否則含息/不含息會互相命中。"""
        tr = data.cache_scope("market", "TAIEX_TR", 730,
                              default_attr="MARKET_HISTORY_DAYS")
        px = data.cache_scope("market", "TAIEX", 730,
                              default_attr="MARKET_HISTORY_DAYS")
        self.assertNotEqual(tr.path, px.path)

    def test_unexpected_schema_fails_closed(self):
        """上游改欄位名時要 raise,不可猜一欄當指數值。"""
        raw = self._raw().rename(columns={"price": "value"})
        with mock.patch.object(data, "_finmind_get", return_value=raw):
            with self.assertRaisesRegex(data.FinMindAPIError, "price/close"):
                data.fetch_market_total_return_index()


# ── 5. summary 要自己說得出兩條序列的口徑 ───────────────────────────────
def _factor_frame(start="2026-01-01", end="2026-03-31") -> pd.DataFrame:
    dates = pd.bdate_range(start, end)
    px = 100.0 + np.arange(len(dates)) * 0.1
    return pd.DataFrame({
        "date": dates, "open": px, "high": px * 1.01, "low": px * 0.99,
        "close": px, "volume": 5_000_000.0, "turnover": 5e8,
        "avg_vol_lots": 5_000.0, "trend_ok": True,
    })


class _PanelEnv:
    """`backtest_portfolio` 需要的資料層 → 離線假資料(絕不打網路)。"""

    def __enter__(self):
        price = _factor_frame()
        self._patches = [
            mock.patch.object(event_backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(event_backtest, "_load_disposition_days", lambda *_a, **_k: {}),
            mock.patch.object(event_backtest.uni, "get_name_map", return_value={}),
            mock.patch.object(event_backtest.uni, "get_industry_map", return_value={}),
            mock.patch.object(event_backtest.data, "fetch_market_index",
                              return_value=pd.DataFrame()),
            mock.patch.object(event_backtest.data, "fetch_bundle",
                              side_effect=lambda *_a, **_k: {"price": price.copy()}),
            mock.patch.object(event_backtest.data, "fetch_price",
                              side_effect=lambda *_a, **_k: price.copy()),
            mock.patch.object(event_backtest.factors, "compute_factors",
                              side_effect=lambda *_a, **_k: price.copy()),
            mock.patch.object(event_backtest.factors, "composite_score",
                              new=lambda *_a, **_k: 80.0),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


def _run_backtest() -> dict:
    with _PanelEnv():
        res = event_backtest.backtest_portfolio(
            symbols=["2330", "2317"], sample=False, dynamic_enabled=False,
            rebalance_every=5, top_n=2, static_universe_comparator=True)
    return res["summary"]


class SummaryReturnConventionTest(unittest.TestCase):
    def test_summary_records_both_series_conventions(self):
        with mock.patch.object(config, "SELF_ADJUST_PRICES", True), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(config, "BENCHMARK_INDEX_DATASET", "auto"):
            block = _run_backtest()["return_convention"]
        self.assertEqual(block["stock_series"]["convention"], rc.TOTAL_RETURN)
        self.assertTrue(block["stock_series"]["includes_cash_dividends"])
        self.assertEqual(block["benchmark_series"]["convention"], rc.TOTAL_RETURN)
        self.assertEqual(block["benchmark_series"]["dataset"],
                         "TaiwanStockTotalReturnIndex")
        self.assertEqual(block["benchmark_series"]["data_id"], "TAIEX")
        self.assertTrue(block["consistent"])

    def test_summary_follows_the_unadjusted_setting(self):
        with mock.patch.object(config, "SELF_ADJUST_PRICES", False), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(config, "BENCHMARK_INDEX_DATASET", "auto"):
            block = _run_backtest()["return_convention"]
        self.assertEqual(block["stock_series"]["convention"], rc.PRICE_RETURN)
        self.assertEqual(block["benchmark_series"]["convention"], rc.PRICE_RETURN)
        self.assertEqual(block["benchmark_series"]["dataset"], "TaiwanStockPrice")
        self.assertFalse(block["benchmark_series"]["includes_cash_dividends"])

    def test_inconsistent_config_makes_the_backtest_fail_closed(self):
        """口徑不一致時不是「標記一下」而是**跑不出結果**。

        口徑不一致的「贏過基準」比不比更糟:它看起來像 alpha,還帶著小數點。
        """
        with mock.patch.object(config, "SELF_ADJUST_PRICES", True), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(config, "BENCHMARK_INDEX_DATASET",
                               "TaiwanStockPrice"):
            with self.assertRaises(rc.ReturnConventionMismatch):
                _run_backtest()

    def test_summary_declares_the_known_residual_in_the_factor_layer(self):
        """rs_excess 仍用價格指數當基準 —— 誠實聲明,不假裝已經全部修好。"""
        with mock.patch.object(config, "SELF_ADJUST_PRICES", True), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"), \
             mock.patch.object(config, "BENCHMARK_INDEX_DATASET", "auto"):
            block = rc.summary_block()
        residual = block["known_residuals"]["relative_strength_factor_benchmark"]
        self.assertFalse(residual["consistent_with_stock_series"])
        self.assertEqual(residual["convention"], rc.PRICE_RETURN)


# ── 6. 研究路徑不得再直接拿價格指數當基準 ────────────────────────────────
class ResearchPathsUseTheConsistentBenchmarkTest(unittest.TestCase):
    def test_rotation_research_benchmark_goes_through_return_convention(self):
        import rotation_research

        idx = pd.DataFrame({
            "date": pd.bdate_range("2026-01-01", periods=40),
            "close": 20000.0 + np.arange(40) * 5.0,
        })
        idx.attrs["return_convention"] = rc.TOTAL_RETURN
        idx.attrs["benchmark_dataset"] = "TaiwanStockTotalReturnIndex"
        with mock.patch.object(rotation_research.return_convention,
                               "fetch_benchmark_index", return_value=idx) as f, \
             mock.patch.object(rotation_research.data, "fetch_market_index",
                               side_effect=AssertionError("不得直接用價格指數")):
            out = rotation_research.benchmark_metrics("2026-01-01", "2026-02-28")
        self.assertEqual(f.call_count, 1)
        self.assertEqual(out["return_convention"], rc.TOTAL_RETURN)
        self.assertEqual(out["benchmark_dataset"], "TaiwanStockTotalReturnIndex")

    def test_equal_weight_baseline_declares_the_stock_convention(self):
        """等權基準是從個股 close 算的 → 必然同口徑,但要標出來。"""
        from strategies import s19_chip_momentum as s19

        dates = pd.bdate_range("2026-01-01", periods=40)
        rows = []
        for sid in ("A", "B"):
            px = 100.0 + np.arange(len(dates)) * (0.2 if sid == "A" else 0.1)
            rows.append(pd.DataFrame({"date": dates, "stock_id": sid, "close": px,
                                      "in_dynamic_universe": True}))
        panel = pd.concat(rows, ignore_index=True)
        with mock.patch.object(config, "SELF_ADJUST_PRICES", True), \
             mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"):
            b = s19.equal_weight_baseline(panel)
        self.assertEqual(b["return_convention"], rc.TOTAL_RETURN)
        self.assertEqual(b["benchmark_source"], "dynamic_universe_equal_weight_hold")


if __name__ == "__main__":
    unittest.main()
