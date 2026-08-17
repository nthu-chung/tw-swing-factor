# -*- coding: utf-8 -*-
"""決策頻率的驗證與 policy 路徑的相位入口(修正清單第 5 項)。

原本的缺陷:policy 路徑的決策日 = 快照日(這是刻意的設計),但**沒有任何東西
驗證快照頻率真的是宣告的那一種**。producer 送日頻快照,policy 就會日頻換股,
而 `rules_hash` 裡仍寫著 `decision_frequency="weekly"` —— 一份宣稱週頻的規則
跑出日頻的週轉率與成本,從結果完全看不出來。

第二個缺陷:policy 路徑沒有相位評估入口。規格 §3.1 要求「正式研究仍須跑滿所有
等價 weekly phase,報中位數、最小值與最差 MaxDD」,而 AGENTS.md 陷阱 2 實測同一
訊號換相位 Sharpe 可以從 -0.09 擺到 +1.09;只報一個星期幾等於挑路徑。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

from backtest import event_backtest
from strategy_kit.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)


def _frame(dates):
    return pd.DataFrame(
        [{"date": d, "stock_id": "A", "rank": 1, "raw_score": 1.0,
          "eligible": True, "snapshot_complete": True} for d in dates]
    )


def _policy(**overrides):
    return StrategyPositionPolicy(StrategyPositionPolicySpec(**overrides))


class SnapshotFrequencyTest(unittest.TestCase):
    def test_daily_snapshots_under_weekly_declaration_fail_closed(self):
        """原缺陷:日頻快照 + weekly 宣告會靜默變成日頻換股。"""
        days = list(pd.bdate_range("2026-01-05", periods=4))   # 同一個 ISO 週
        with self.assertRaises(ValueError) as ctx:
            event_backtest._prepare_signal_snapshots(
                _frame(days), decision_frequency="weekly")
        msg = str(ctx.exception)
        self.assertIn("fail-closed", msg)
        self.assertIn("weekly", msg)

    def test_one_snapshot_per_iso_week_passes(self):
        days = list(pd.bdate_range("2026-01-05", periods=15))[::5]
        snapshots, dates = event_backtest._prepare_signal_snapshots(
            _frame(days), decision_frequency="weekly")
        self.assertEqual(len(dates), len(days))
        self.assertEqual(len(snapshots), len(days))

    def test_skipping_a_whole_week_is_allowed(self):
        """假日週(春節)整週沒有決策日是合法的,判準是「同週不得多次」。"""
        days = [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-19")]
        _, dates = event_backtest._prepare_signal_snapshots(
            _frame(days), decision_frequency="weekly")
        self.assertEqual(len(dates), 2)

    def test_daily_declaration_allows_daily_snapshots(self):
        days = list(pd.bdate_range("2026-01-05", periods=4))
        _, dates = event_backtest._prepare_signal_snapshots(
            _frame(days), decision_frequency="daily")
        self.assertEqual(len(dates), 4)

    def test_unknown_frequency_fails_closed(self):
        with self.assertRaises(ValueError):
            event_backtest._prepare_signal_snapshots(
                _frame([pd.Timestamp("2026-01-05")]),
                decision_frequency="fortnightly")

    def test_missing_declaration_skips_the_check(self):
        """沒有宣告就無從驗證;這條釘住「不驗」與「驗過了」不會被混為一談。"""
        days = list(pd.bdate_range("2026-01-05", periods=4))
        _, dates = event_backtest._prepare_signal_snapshots(_frame(days))
        self.assertEqual(len(dates), 4)


class SelectDecisionSnapshotsTest(unittest.TestCase):
    def test_each_phase_picks_one_trading_day_per_iso_week(self):
        days = list(pd.bdate_range("2026-01-05", periods=15))
        weeks = {event_backtest._iso_week(d) for d in days}
        for idx in range(event_backtest.WEEKLY_PHASES):
            picked = event_backtest.select_decision_snapshots(days, phase=idx)
            self.assertEqual(len(picked), len(weeks))
            self.assertEqual(len({event_backtest._iso_week(d) for d in picked}),
                             len(weeks))

    def test_phases_select_different_days(self):
        days = list(pd.bdate_range("2026-01-05", periods=15))
        selections = {
            idx: tuple(event_backtest.select_decision_snapshots(days, phase=idx))
            for idx in range(event_backtest.WEEKLY_PHASES)
        }
        self.assertEqual(len(set(selections.values())),
                         event_backtest.WEEKLY_PHASES)

    def test_short_week_clamps_to_last_valid_trading_day(self):
        """§3.1:假日週以該週最後一個有效交易日為決策日。"""
        days = [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
        for idx in (2, 3, 4):
            picked = event_backtest.select_decision_snapshots(days, phase=idx)
            self.assertEqual(picked, [pd.Timestamp("2026-01-06")])

    def test_daily_frequency_returns_every_day(self):
        days = list(pd.bdate_range("2026-01-05", periods=6))
        self.assertEqual(
            event_backtest.select_decision_snapshots(days, decision_frequency="daily"),
            sorted(days))

    def test_out_of_range_phase_fails_closed(self):
        with self.assertRaises(ValueError):
            event_backtest.select_decision_snapshots(
                list(pd.bdate_range("2026-01-05", periods=5)), phase=5)


class PolicyPhaseSweepTest(unittest.TestCase):
    """policy 路徑要能接上唯一那份共用相位掃描,而不是自己再寫一個迴圈。"""

    def _summary(self, **over):
        s = {"sharpe": 1.0, "cum_ret": 0.1, "ann_ret": 0.2,
             "max_drawdown": -0.05, "n_trades": 3}
        s.update(over)
        return {"summary": s}

    def test_sweep_goes_through_the_shared_implementation(self):
        days = list(pd.bdate_range("2026-01-05", periods=15))
        seen = []

        def _fake(**kwargs):
            seen.append(sorted(kwargs["signal_frame"]["date"].unique()))
            return self._summary()

        with (
            mock.patch.object(event_backtest, "backtest_portfolio", side_effect=_fake),
            mock.patch.object(event_backtest, "sweep_phases",
                              wraps=event_backtest.sweep_phases) as spy,
        ):
            sweep = event_backtest.backtest_policy_phases(
                signal_frame=_frame(days),
                strategy_position_policy=_policy(),
                symbols=["A"], sample=False)

        spy.assert_called_once()
        self.assertEqual(spy.call_args.kwargs["n_phases"],
                         event_backtest.WEEKLY_PHASES)
        self.assertEqual(len(sweep), event_backtest.WEEKLY_PHASES)
        self.assertFalse(sweep.single_phase_debug)
        # 每個相位真的餵了不同的決策日,不是同一批重複五次
        self.assertEqual(len({tuple(x) for x in seen}), event_backtest.WEEKLY_PHASES)

    def test_stats_report_median_min_and_worst_drawdown(self):
        days = list(pd.bdate_range("2026-01-05", periods=15))
        sharpes = iter([0.5, 1.5, -0.2, 2.0, 1.0])
        dds = iter([-0.05, -0.30, -0.10, -0.02, -0.08])

        def _fake(**kwargs):
            return self._summary(sharpe=next(sharpes), max_drawdown=next(dds))

        with mock.patch.object(event_backtest, "backtest_portfolio", side_effect=_fake):
            sweep = event_backtest.backtest_policy_phases(
                signal_frame=_frame(days),
                strategy_position_policy=_policy(),
                symbols=["A"], sample=False)
        stats = sweep.stats()
        self.assertAlmostEqual(stats["sharpe_median"], 1.0)
        self.assertAlmostEqual(stats["sharpe_min"], -0.2)
        self.assertAlmostEqual(stats["worst_max_drawdown"], -0.30)

    def test_already_weekly_frame_fails_closed_instead_of_faking_a_sweep(self):
        """週頻快照無法降頻 → 五個相位會是同一條路徑,必須擋下。

        比只跑一個相位更糟:中位數與最小值看起來是穩健性統計,
        實際上是同一個數字重複五次。
        """
        weekly = list(pd.bdate_range("2026-01-05", periods=15))[::5]
        with mock.patch.object(event_backtest, "backtest_portfolio",
                               side_effect=AssertionError("不該被呼叫")):
            with self.assertRaises(ValueError) as ctx:
                event_backtest.backtest_policy_phases(
                    signal_frame=_frame(weekly),
                    strategy_position_policy=_policy(),
                    symbols=["A"], sample=False)
        self.assertIn("fail-closed", str(ctx.exception))

    def test_daily_policy_runs_a_single_phase(self):
        days = list(pd.bdate_range("2026-01-05", periods=6))
        with mock.patch.object(event_backtest, "backtest_portfolio",
                               side_effect=lambda **k: self._summary()):
            sweep = event_backtest.backtest_policy_phases(
                signal_frame=_frame(days),
                strategy_position_policy=_policy(decision_frequency="daily"),
                symbols=["A"], sample=False)
        self.assertEqual(sweep.n_phases_full, 1)
        self.assertEqual(len(sweep), 1)


if __name__ == "__main__":
    unittest.main()
