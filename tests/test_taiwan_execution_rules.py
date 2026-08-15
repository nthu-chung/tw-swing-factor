# -*- coding: utf-8 -*-
"""台股普通股交易規則的官方範例與成本回歸測試。"""
from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

import pandas as pd

import backtest
import config
from _offline_registry import common_stocks
from execution.costs import OrderSizeMode, TaiwanStockCostModel, size_long_order
from execution.taiwan_rules import (
    PriceDirection,
    is_new_listing_unlimited,
    snap_stock_price,
    stock_price_limits,
    stock_tick_size,
)
from execution.tradability import detect_limit_lock


class TaiwanStockPriceRuleTest(unittest.TestCase):
    def test_tick_ladder_boundaries(self):
        expected = {
            "9.99": "0.01",
            "10": "0.05",
            "49.95": "0.05",
            "50": "0.10",
            "99.9": "0.10",
            "100": "0.50",
            "499.5": "0.50",
            "500": "1",
            "999": "1",
            "1000": "5",
        }
        for price, tick in expected.items():
            with self.subTest(price=price):
                self.assertEqual(stock_tick_size(price), Decimal(tick))

    def test_official_4060_price_limit_example(self):
        limits = stock_price_limits("40.60")
        self.assertEqual(limits.upper, Decimal("44.65"))
        self.assertEqual(limits.lower, Decimal("36.55"))

    def test_limit_never_exceeds_ten_percent(self):
        limits = stock_price_limits("40.60")
        self.assertLessEqual(limits.upper, Decimal("40.60") * Decimal("1.10"))
        self.assertGreaterEqual(limits.lower, Decimal("40.60") * Decimal("0.90"))

    def test_sub_tick_limit_still_moves_one_tick(self):
        limits = stock_price_limits("0.05")
        self.assertEqual(limits.upper, Decimal("0.06"))
        self.assertEqual(limits.lower, Decimal("0.04"))

    def test_snap_direction(self):
        self.assertEqual(snap_stock_price("44.69", PriceDirection.DOWN), Decimal("44.65"))
        self.assertEqual(snap_stock_price("36.51", PriceDirection.UP), Decimal("36.55"))

    def test_new_listing_first_five_days(self):
        self.assertTrue(is_new_listing_unlimited(1))
        self.assertTrue(is_new_listing_unlimited(5))
        self.assertFalse(is_new_listing_unlimited(6))
        self.assertFalse(is_new_listing_unlimited(1, transferred_listing=True))


class TaiwanTradabilityTest(unittest.TestCase):
    def test_exact_one_price_limit_lock(self):
        up = pd.Series({"open": 44.65, "high": 44.65, "low": 44.65})
        down = pd.Series({"open": 36.55, "high": 36.55, "low": 36.55})
        self.assertEqual(detect_limit_lock(up, 40.60), "up")
        self.assertEqual(detect_limit_lock(down, 40.60), "down")

    def test_near_limit_is_not_mislabeled(self):
        bar = pd.Series({"open": 44.60, "high": 44.60, "low": 44.60})
        self.assertIsNone(detect_limit_lock(bar, 40.60))

    def test_explicit_official_limits_override_previous_close(self):
        bar = pd.Series({
            "open": 30.0, "high": 30.0, "low": 30.0,
            "limit_up": 30.0, "limit_down": 24.55,
        })
        self.assertEqual(detect_limit_lock(bar, 100.0), "up")

    def test_exempt_day_is_not_treated_as_limit_lock(self):
        bar = pd.Series({
            "open": 44.65, "high": 44.65, "low": 44.65,
            "price_limit_exempt": True,
        })
        self.assertIsNone(detect_limit_lock(bar, 40.60))


class TaiwanCostAndSizingTest(unittest.TestCase):
    def test_commission_is_broker_configurable(self):
        costs = TaiwanStockCostModel(minimum_commission=Decimal("20"))
        self.assertEqual(costs.commission(1000), Decimal("20"))
        self.assertEqual(costs.commission(100000), Decimal("142.500000"))

    def test_sell_has_commission_and_stock_tax(self):
        costs = TaiwanStockCostModel()
        self.assertEqual(costs.sell_proceeds(1000, 100), Decimal("99557.500000"))

    def test_regular_lot_never_returns_fractional_lot(self):
        costs = TaiwanStockCostModel()
        shares, total = size_long_order(
            250000, 100, mode=OrderSizeMode.REGULAR_LOT, costs=costs)
        self.assertEqual(shares, 2000)
        self.assertLessEqual(total, 250000)

    def test_odd_lot_proxy_returns_integer_shares(self):
        costs = TaiwanStockCostModel()
        shares, total = size_long_order(
            10000, 73.5, mode=OrderSizeMode.ODD_LOT_PROXY, costs=costs)
        self.assertEqual(shares, int(shares))
        self.assertLessEqual(total, 10000)

    def test_research_mode_is_explicitly_fractional(self):
        costs = TaiwanStockCostModel()
        shares, total = size_long_order(
            10000, 73.5, mode=OrderSizeMode.RESEARCH_FRACTIONAL, costs=costs)
        self.assertNotEqual(shares, int(shares))
        self.assertLessEqual(total, 10000 + 1e-8)


class BacktestExecutionIntegrationTest(unittest.TestCase):
    def _run(self, mode: str):
        dates = pd.bdate_range("2025-01-01", periods=12)
        prices = pd.DataFrame({
            "date": dates,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000_000,
        })
        picks = {d: [("1101", 1.0, "1101")] for d in dates[:-1]}
        with (
            # 外部 picks 路徑的證券別閘門是 fail-closed,代號要顯式宣告證券別。
            common_stocks("1101"),
            mock.patch.object(backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(backtest, "_load_disposition_days", lambda *_a, **_k: {}),
            mock.patch.object(backtest.data, "fetch_price", return_value=prices.copy()),
            mock.patch.object(config, "BT_ORDER_SIZE_MODE", mode),
            mock.patch.object(config, "BT_INITIAL_CAPITAL", 250_000.0),
            mock.patch.object(config, "BT_MIN_COMMISSION", 0.0),
            mock.patch.object(config, "BT_MAX_POSITIONS", 1),
            mock.patch.object(config, "BT_EXIT_MODE", "fixed"),
            mock.patch.object(config, "BT_HOLD_DAYS", 1),
            mock.patch.object(config, "BT_TAKE_PROFIT", 1.0),
            mock.patch.object(config, "BT_STOP_LOSS", 1.0),
        ):
            return backtest.backtest_portfolio(
                symbols=["1101"], sample=False, rebalance_every=2, top_n=1,
                picks_by_date=picks,
            )

    def test_regular_lot_reaches_event_driven_trade_log(self):
        result = self._run(OrderSizeMode.REGULAR_LOT.value)
        self.assertFalse(result["trades"].empty)
        self.assertEqual(result["trades"].iloc[0]["shares"], 2000)
        self.assertTrue(result["summary"]["execution"]["lot_aware"])
        self.assertFalse(
            result["summary"]["execution"]["execution_realistic"],
            "尚未接官方逐日漲跌停資料時，不得宣稱完整 execution realistic",
        )
        self.assertEqual(
            result["summary"]["execution"]["order_size_mode"], "regular_lot")

    def test_fractional_research_mode_is_labeled_not_realistic(self):
        result = self._run(OrderSizeMode.RESEARCH_FRACTIONAL.value)
        shares = result["trades"].iloc[0]["shares"]
        self.assertNotEqual(shares, int(shares))
        self.assertFalse(result["summary"]["execution"]["execution_realistic"])

    def test_official_mode_without_official_columns_fails_closed(self):
        with (
            mock.patch.object(config, "BT_PRICE_LIMIT_SOURCE", "official"),
            mock.patch.object(backtest.data, "fetch_price_limits", return_value=pd.DataFrame()),
        ):
            with self.assertRaisesRegex(RuntimeError, "TaiwanStockPriceLimit 為空"):
                self._run(OrderSizeMode.REGULAR_LOT.value)

    def test_regular_lot_with_complete_official_limits_is_price_and_lot_realistic(self):
        dates = pd.bdate_range("2025-01-01", periods=12)
        limits = pd.DataFrame({
            "date": dates,
            "reference_price": 100.0,
            "limit_up": 110.0,
            "limit_down": 90.0,
        })
        with (
            mock.patch.object(config, "BT_PRICE_LIMIT_SOURCE", "official"),
            mock.patch.object(backtest.data, "fetch_price_limits", return_value=limits),
        ):
            result = self._run(OrderSizeMode.REGULAR_LOT.value)
        self.assertTrue(result["summary"]["execution"]["price_and_lot_realistic"])
        self.assertFalse(result["summary"]["execution"]["execution_realistic"])
        self.assertEqual(result["summary"]["execution"]["price_limit_source"], "official")


if __name__ == "__main__":
    unittest.main()
