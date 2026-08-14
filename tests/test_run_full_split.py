# -*- coding: utf-8 -*-
"""核心 CLI 回測必須分 IS/OS 並跑滿所有再平衡相位。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pandas as pd

import backtest
import config
import evaluation_split


class RunFullSplitTest(unittest.TestCase):
    def test_core_entry_uses_hard_windows_and_all_phases(self):
        dates = pd.bdate_range("2024-01-01", periods=120)
        calls = []

        def fake_portfolio(*_args, **kwargs):
            calls.append(kwargs.copy())
            start = pd.Timestamp(kwargs.get("start_date") or dates[0])
            end = pd.Timestamp(kwargs.get("end_date") or dates[-1])
            eq_dates = dates[(dates >= start) & (dates <= end)]
            phase = int(kwargs.get("rebalance_phase", 0))
            return {
                "summary": {
                    "n_trades": 10 + phase,
                    "ann_ret": 0.10 + phase / 100,
                    "sharpe": 1.0 + phase / 10,
                    "max_drawdown": -0.10,
                    "cum_ret": 0.12,
                    "data": {"integrity_bypassed": False},
                    "universe": {"survivorship_free": True},
                    "eval_audit": {"eval_window": [str(eq_dates[0].date()),
                                                     str(eq_dates[-1].date())]},
                },
                "equity_curve": pd.DataFrame({"date": eq_dates, "equity": 1.0}),
                "trades": pd.DataFrame(),
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(backtest.uni, "get_universe", return_value=["A"]),
                mock.patch.object(backtest, "backtest_portfolio", side_effect=fake_portfolio),
                mock.patch.object(backtest, "factor_ic", return_value=pd.DataFrame()),
                mock.patch.object(config, "OUTPUT_DIR", Path(tmp)),
            ):
                result, _ = backtest.run_full(
                    sample=True, top_n=1, rebalance_every=3,
                    dynamic_enabled=False,
                )

        split = evaluation_split.build_evaluation_split(
            dates, minimum_embargo_days=config.BT_IC_HORIZON
        )
        self.assertEqual(len(calls), 7)  # 1 次取日曆 + IS/OS 各 3 相位
        segment_calls = calls[1:]
        self.assertEqual({c["rebalance_phase"] for c in segment_calls}, {0, 1, 2})
        self.assertEqual(
            {(c["start_date"], c["end_date"]) for c in segment_calls},
            {split.is_window, split.os_window},
        )
        self.assertEqual(len(result["phases"]), 6)

    def test_formal_dynamic_run_uses_monthly_pit_provider_not_current_pool(self):
        """正式回測不得再從今天的 top-N 名單 bootstrap 歷史 universe。"""
        dates = pd.bdate_range("2024-01-01", periods=120)

        class FakeProvider:
            all_symbols = ["A", "B"]

        provider = FakeProvider()
        calls = []

        def fake_portfolio(*_args, **kwargs):
            calls.append(kwargs.copy())
            start = pd.Timestamp(kwargs.get("start_date") or dates[0])
            end = pd.Timestamp(kwargs.get("end_date") or dates[-1])
            eq_dates = dates[(dates >= start) & (dates <= end)]
            return {
                "summary": {
                    "n_trades": 1, "ann_ret": 0.1, "sharpe": 1.0,
                    "max_drawdown": -0.1, "cum_ret": 0.1,
                    "data": {"integrity_bypassed": False},
                    "universe": {"survivorship_free": True},
                    "eval_audit": {"eval_window": [str(eq_dates[0].date()),
                                                       str(eq_dates[-1].date())]},
                },
                "equity_curve": pd.DataFrame({"date": eq_dates, "equity": 1.0}),
                "trades": pd.DataFrame(),
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    backtest.MonthlyPITUniverseProvider, "from_cache",
                    return_value=provider,
                ) as make_provider,
                mock.patch.object(
                    backtest.uni, "get_research_candidates",
                    side_effect=AssertionError("正式回測不可讀 current pool"),
                ),
                mock.patch.object(backtest, "backtest_portfolio", side_effect=fake_portfolio),
                mock.patch.object(backtest, "factor_ic", return_value=pd.DataFrame()),
                mock.patch.object(config, "OUTPUT_DIR", Path(tmp)),
            ):
                backtest.run_full(
                    sample=False, top_n=1, rebalance_every=1,
                    dynamic_enabled=True, pool=250,
                )

        make_provider.assert_called_once_with(
            top_n=250, min_obs=config.DYNAMIC_UNIVERSE_MONTHLY_MIN_OBS,
        )
        self.assertTrue(calls)
        self.assertTrue(all(c["universe_provider"] is provider for c in calls))
        self.assertTrue(all(c["symbols"] == ["A", "B"] for c in calls))


if __name__ == "__main__":
    unittest.main()
