# -*- coding: utf-8 -*-
"""正式月頻 universe provider 的離線契約測試。"""
from __future__ import annotations

import unittest

import pandas as pd

from universes import MonthlyPITUniverseProvider


class MonthlyPITUniverseProviderTest(unittest.TestCase):
    def _history(self):
        rows = []
        for d in pd.bdate_range("2026-01-01", "2026-02-03"):
            month = d.month
            rows += [
                {"date": d, "stock_id": "A", "turnover": 100 if month == 1 else 1},
                {"date": d, "stock_id": "B", "turnover": 50 if month == 1 else 1e9},
            ]
        return pd.DataFrame(rows)

    def test_metadata_states_previous_calendar_month_contract(self):
        provider = MonthlyPITUniverseProvider.from_history(
            self._history(), top_n=1, min_obs=5,
        )
        self.assertEqual(provider.members_on("2026-02-02"), ["A"])
        meta = provider.metadata()
        self.assertTrue(meta["candidate_membership_survivorship_free"])
        self.assertFalse(meta["survivorship_free"], "缺下市股完整價格前不可過度宣稱")
        self.assertEqual(
            meta["candidate_rule"],
            "month_M_uses_only_calendar_month_M_minus_1",
        )

    def test_candidate_mask_keeps_dense_rows_but_marks_membership(self):
        provider = MonthlyPITUniverseProvider.from_history(
            self._history(), top_n=1, min_obs=5,
        )
        panel = pd.DataFrame([
            {"date": "2026-02-02", "stock_id": "A"},
            {"date": "2026-02-02", "stock_id": "B"},
        ])
        mask = provider.candidate_mask(panel)
        self.assertEqual(mask.tolist(), [True, False])
        self.assertEqual(len(mask), len(panel), "provider 只能標記，不可刪掉稠密 panel 的列")


if __name__ == "__main__":
    unittest.main()
