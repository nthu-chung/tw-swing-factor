# -*- coding: utf-8 -*-
"""S19 reference ``make_signals`` 的離線契約測試。

這支測試只釘住策略端輸出，不宣稱 S19 有 edge。完整 golden-path 驗收會由另一支
runner contract test 驗證 validator、policy、事件引擎、metrics 與 artifacts。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from factor_engine.panel_density import DENSE, tag
from strategies.s19_reference import S19ReferenceStrategy


def _panel(periods: int = 35) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=periods)
    rows = []
    for j, sid in enumerate(("1101", "1216", "1301")):
        for i, date in enumerate(dates):
            rows.append({
                "date": date,
                "stock_id": sid,
                "close": 100.0 + i * (0.3 + j * 0.05),
                "volume": 1_000_000.0 + j * 100_000 + i * 1_000,
                "foreign_net": float((j + 1) * 1_000 + i * 10),
                "trust_net": float((3 - j) * 100 + i),
                "in_dynamic_universe": True,
                "trend_ok": True,
            })
    return tag(pd.DataFrame(rows), DENSE)


class S19ReferenceMakeSignalsTest(unittest.TestCase):
    def setUp(self):
        self.strategy = S19ReferenceStrategy()

    def test_output_is_complete_unique_and_auditable(self):
        signals = self.strategy.make_signals(_panel())
        required = {
            "date", "stock_id", "eligible", "raw_score", "alpha_score",
            "rank", "rank_pct", "thesis_ok", "hard_exit", "reason_codes",
            "ranking_universe_count", "eligibility_rule_id", "snapshot_complete",
            "strategy_id", "strategy_version",
        }
        self.assertTrue(required.issubset(signals.columns))
        self.assertFalse(signals.duplicated(["date", "stock_id"]).any())
        self.assertTrue(signals["snapshot_complete"].all())
        for _, day in signals.groupby("date"):
            self.assertEqual(sorted(day["rank"].astype(int)), list(range(1, len(day) + 1)))
            self.assertTrue((day["ranking_universe_count"] == len(day)).all())

    def test_same_input_and_parameters_are_deterministic(self):
        panel = _panel()
        before = panel.copy(deep=True)
        a = self.strategy.make_signals(panel)
        b = self.strategy.make_signals(panel)
        assert_frame_equal(a, b)
        assert_frame_equal(panel, before)

    def test_appending_future_rows_does_not_change_past_signals(self):
        short = _panel(30)
        long = _panel(35)
        a = self.strategy.make_signals(short)
        b = self.strategy.make_signals(long)
        cutoff = short["date"].max()
        b = b[b["date"] <= cutoff].reset_index(drop=True)
        assert_frame_equal(a.reset_index(drop=True), b)

    def test_unknown_or_invalid_parameters_fail_closed(self):
        panel = _panel()
        with self.assertRaises(ValueError):
            self.strategy.make_signals(panel, {"secret_knob": 1})
        with self.assertRaises(ValueError):
            self.strategy.make_signals(panel, {"mom_window": 1})
        with self.assertRaises(ValueError):
            self.strategy.make_signals(
                panel, {"w_momentum": 0.8, "w_flow": 0.8})

    def test_context_window_limits_only_emitted_snapshots(self):
        panel = _panel()
        start = pd.Timestamp("2026-02-02")
        end = pd.Timestamp("2026-02-06")
        out = self.strategy.make_signals(
            panel, context={"start_date": start, "end_date": end})
        self.assertGreater(len(out), 0)
        self.assertGreaterEqual(out["date"].min(), start)
        self.assertLessEqual(out["date"].max(), end)


if __name__ == "__main__":
    unittest.main()
