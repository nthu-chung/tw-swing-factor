# -*- coding: utf-8 -*-
"""執行層必須在 as-traded 價格空間判定(PRICE_SCALE_CONTRACT.md §3)。

還原價是「今日等值」單位,而下面三件事看的是**絕對價位**、不是比例:

1. 升降單位(tick)由價格帶決定,而漲跌停價要先 tick 化 —— 在還原價空間判
   「一字漲跌停」會在價格帶邊界出錯。實測 12 檔樣本有 2 檔(17%)還原後落進
   不同的 tick 帶。
2. 整張 1000 股的資金門檻。實測 2327 在 2024-06-24 一張真實成本 759,000 元,
   還原價算只要 147,245 元(5.15 倍)—— 回測會買進當時買不起的股票,而且偏誤
   集中在高價股,正是動能策略最愛選的那一群。
3. 20 元最低手續費的觸底判定。

損益仍在還原價空間算(連續、不需要在部位上模擬公司行動);兩個空間用
`shares_adj × price_adj == shares_real × price_raw` 換算,保證花掉的錢一致。
"""
from __future__ import annotations

import unittest

import pandas as pd

from backtest import event_backtest
from execution.costs import OrderSizeMode, TaiwanStockCostModel


def _bar(**over):
    row = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
           "open_raw": 400.0, "high_raw": 404.0, "low_raw": 396.0,
           "close_raw": 400.0}
    row.update(over)
    return pd.Series(row)


class RawBarViewTest(unittest.TestCase):
    def test_view_swaps_ohlc_for_as_traded_values(self):
        view = event_backtest._raw_bar_view(_bar())
        for col in ("open", "high", "low", "close"):
            self.assertAlmostEqual(float(view[col]), float(_bar()[f"{col}_raw"]))

    def test_missing_raw_columns_fall_back_unchanged(self):
        plain = pd.Series({"open": 100.0, "high": 101.0,
                           "low": 99.0, "close": 100.0})
        view = event_backtest._raw_bar_view(plain)
        self.assertAlmostEqual(float(view["close"]), 100.0)

    def test_prev_close_prefers_the_as_traded_column(self):
        frame = pd.DataFrame({"close": [100.0, 101.0],
                              "close_raw": [400.0, 404.0]})
        self.assertAlmostEqual(event_backtest._raw_prev_close(frame, 1), 400.0)
        plain = pd.DataFrame({"close": [100.0, 101.0]})
        self.assertAlmostEqual(event_backtest._raw_prev_close(plain, 1), 100.0)


class SizeInRawSpaceTest(unittest.TestCase):
    """整張可負擔性必須用真實價格判,否則會買進當時買不起的股票。"""

    def setUp(self) -> None:
        self.costs = TaiwanStockCostModel(
            commission_rate=0.001425, minimum_commission=20.0,
            sell_tax_rate=0.003)

    def _size(self, alloc, price_adj, price_raw, mode=OrderSizeMode.REGULAR_LOT):
        return event_backtest._size_in_raw_space(
            alloc, price_adj, price_raw, mode=mode, costs=self.costs,
            regular_lot_shares=1000)

    def test_unaffordable_lot_is_rejected_even_though_adjusted_price_looks_cheap(self):
        """2327 的真實案例:20 萬買不起一張 759 元的股票。"""
        adj_only, _ = event_backtest.size_long_order(
            200_000.0, 147.25, mode=OrderSizeMode.REGULAR_LOT,
            costs=self.costs, regular_lot_shares=1000)
        self.assertGreater(adj_only, 0, "前提:只看還原價會以為買得起")

        shares_adj, cost, shares_real = self._size(200_000.0, 147.25, 759.0)
        self.assertEqual(shares_real, 0.0)
        self.assertEqual(shares_adj, 0.0)
        self.assertEqual(cost, 0.0)

    def test_affordable_lot_keeps_the_money_identity(self):
        """換算後「花掉的錢」必須一致:shares_adj × price_adj == shares_real × price_raw。"""
        shares_adj, cost, shares_real = self._size(1_000_000.0, 147.25, 759.0)
        self.assertGreater(shares_real, 0)
        self.assertAlmostEqual(shares_adj * 147.25, shares_real * 759.0, places=6)
        self.assertGreater(cost, shares_real * 759.0)      # 成本含手續費

    def test_without_raw_prices_it_matches_the_legacy_sizing(self):
        """沒有 as-traded 欄位時(price_raw == price_adj)行為與舊路徑相同。"""
        legacy_shares, legacy_cost = event_backtest.size_long_order(
            200_000.0, 147.25, mode=OrderSizeMode.REGULAR_LOT,
            costs=self.costs, regular_lot_shares=1000)
        shares_adj, cost, shares_real = self._size(200_000.0, 147.25, 147.25)
        self.assertAlmostEqual(shares_adj, legacy_shares)
        self.assertAlmostEqual(shares_real, legacy_shares)
        self.assertAlmostEqual(cost, legacy_cost)

    def test_minimum_commission_is_evaluated_on_the_real_notional(self):
        """最低手續費的觸底判定也必須在真實價格上做。"""
        _, cost, shares_real = self._size(
            50_000.0, 2.0, 10.0, mode=OrderSizeMode.ODD_LOT_PROXY)
        self.assertGreater(shares_real, 0)
        self.assertGreaterEqual(cost, shares_real * 10.0)


class LimitLockUsesRawPricesTest(unittest.TestCase):
    """一字漲跌停的判定必須在真實價格帶上做(漲跌停價會先 tick 化)。"""

    def test_lock_detection_reads_the_as_traded_view(self):
        # 真實價 400(tick 0.5 帶),前一日 400 → 一字跌停在 360
        locked = _bar(open_raw=360.0, high_raw=360.0,
                      low_raw=360.0, close_raw=360.0,
                      open=90.0, high=90.0, low=90.0, close=90.0)
        self.assertEqual(
            event_backtest._limit_lock(event_backtest._raw_bar_view(locked), 400.0), "down")


if __name__ == "__main__":
    unittest.main()
