# -*- coding: utf-8 -*-
"""統一 IS/OS 切割的回歸測試。

過去各腳本各自切割，曾同時存在 60/40、70/30、日曆日近似與少一日 embargo。
這些測試確保切割互斥、比例精確、固定週數不偷用更早資料，且未來標籤視窗不足時
fail-closed。
"""

from __future__ import annotations

import unittest

import pandas as pd

from evaluation.splits import build_evaluation_split


class EvaluationSplitTest(unittest.TestCase):
    def setUp(self):
        self.dates = pd.bdate_range("2024-01-01", periods=300)

    def test_ratio_has_exact_non_overlapping_embargo(self):
        sp = build_evaluation_split(
            self.dates, mode="ratio", is_ratio=0.70, embargo_days=20
        )
        self.assertEqual(sp.n_is, 196)
        self.assertEqual(sp.n_embargo, 20)
        self.assertEqual(sp.n_os, 84)
        self.assertLess(sp.is_end, sp.os_start)
        self.assertEqual(sp.is_end, self.dates[195])
        self.assertEqual(sp.os_start, self.dates[216])
        self.assertAlmostEqual(sp.n_is / (sp.n_is + sp.n_os), 0.70, places=2)

    def test_weeks_uses_only_requested_trailing_windows(self):
        sp = build_evaluation_split(
            self.dates, mode="weeks", is_weeks=20, os_weeks=8, embargo_days=5
        )
        self.assertEqual(sp.mode, "weeks")
        self.assertEqual(sp.n_embargo, 5)
        self.assertLessEqual((sp.is_end - sp.is_start).days, 20 * 7 + 7)
        self.assertLessEqual((sp.os_end - sp.os_start).days, 8 * 7 + 7)
        self.assertGreater(sp.is_start, self.dates[0])

    def test_future_label_horizon_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "未來標籤視窗"):
            build_evaluation_split(
                self.dates,
                mode="ratio",
                is_ratio=0.70,
                embargo_days=10,
                minimum_embargo_days=20,
            )

    def test_invalid_or_too_short_split_raises(self):
        with self.assertRaises(ValueError):
            build_evaluation_split(self.dates[:10], mode="ratio", is_ratio=0.9,
                                   embargo_days=2)
        with self.assertRaises(ValueError):
            build_evaluation_split(self.dates, mode="ratio", is_ratio=1.0,
                                   embargo_days=0)


if __name__ == "__main__":
    unittest.main()
