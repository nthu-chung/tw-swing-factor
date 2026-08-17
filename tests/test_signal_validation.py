# -*- coding: utf-8 -*-
"""SignalFrame validator 的責任測試(研究規格 §5.6 / §6)。

repo 策略與外部序列化訊號**走同一支 validator**。每一條檢查都對應一個會安靜
產生假結果的失敗:key 不唯一 → 選股取決於列順序;rank 不連續 → entry buffer
的名次語意壞掉;排名母體對不上 → 非成員混進 cs 排名;快照不完整卻宣稱完整 →
未出現的持股會被誤判成掉出榜外而賣掉;越過 as-of → 用還沒發生的資料決策。
"""
from __future__ import annotations

import unittest

import pandas as pd

from research.signal_validation import (
    SignalValidationError,
    validate_signal_frame,
)


def _frame(n_days: int = 2, n_names: int = 3, **over) -> pd.DataFrame:
    days = pd.bdate_range("2026-01-05", periods=n_days)
    rows = []
    for d in days:
        for i in range(n_names):
            rows.append({
                "date": d, "stock_id": f"S{i:02d}", "eligible": True,
                "raw_score": 1.0 - i * 0.01, "rank": i + 1,
                "ranking_universe_count": n_names, "snapshot_complete": True,
                "strategy_id": "probe", "strategy_version": "1.0.0",
            })
    df = pd.DataFrame(rows)
    for k, v in over.items():
        df[k] = v
    return df


class ValidatorTest(unittest.TestCase):
    def test_valid_frame_passes_and_is_sorted(self):
        res = validate_signal_frame(_frame(), who="probe")
        self.assertEqual(res.n_days, 2)
        self.assertTrue(res.formal_evidence_eligible)
        self.assertTrue(all(res.checks.values()))

    def test_duplicate_key_fails_closed(self):
        df = pd.concat([_frame(1, 1), _frame(1, 1)], ignore_index=True)
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(df, who="probe")

    def test_non_dense_rank_fails_closed(self):
        df = _frame(1, 3)
        df.loc[df.index[-1], "rank"] = 9
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(df, who="probe")

    def test_ranking_universe_count_mismatch_fails_closed(self):
        df = _frame(1, 3)
        df["ranking_universe_count"] = 99
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(df, who="probe")

    def test_ineligible_rows_fail_closed(self):
        df = _frame(1, 2)
        df.loc[df.index[0], "eligible"] = False
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(df, who="probe")

    def test_nan_score_fails_closed(self):
        df = _frame(1, 2)
        df.loc[df.index[0], "raw_score"] = float("nan")
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(df, who="probe")

    def test_rows_beyond_as_of_fail_closed(self):
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(_frame(3), who="probe",
                                  as_of_max=pd.Timestamp("2026-01-05"))

    def test_missing_provenance_is_debuggable_but_not_formal_evidence(self):
        """外部 frame 缺 provenance:可 debug,不得產生正式證據(§5.6)。"""
        df = _frame().drop(columns=["strategy_id", "strategy_version"])
        res = validate_signal_frame(df, who="external")
        self.assertFalse(res.formal_evidence_eligible)
        self.assertIn("provenance", res.evidence_note)

    def test_incomplete_snapshot_is_warned_not_silently_accepted(self):
        res = validate_signal_frame(_frame(snapshot_complete=False), who="probe")
        self.assertTrue(any("snapshot_complete" in w for w in res.warnings))

    def test_derivable_columns_are_filled_and_reported(self):
        res = validate_signal_frame(_frame(), who="probe")
        for col in ("alpha_score", "rank_pct", "thesis_ok", "hard_exit"):
            self.assertIn(col, res.frame.columns)
        self.assertTrue(res.warnings)

    def test_empty_frame_fails_closed(self):
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(pd.DataFrame(), who="probe")


if __name__ == "__main__":
    unittest.main()
