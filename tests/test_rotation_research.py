import unittest

import pandas as pd

import rotation_research


def _panel():
    dates = pd.date_range("2026-01-01", periods=25, freq="B")
    rows = []
    for industry, prefix in [("A", "1"), ("B", "2")]:
        for j in range(5):
            sid = f"{prefix}{j:03d}"
            for i, date in enumerate(dates):
                rows.append({
                    "date": date,
                    "industry": industry,
                    "stock_id": sid,
                    "rs_excess": i / 100 + (0.1 if industry == "A" else 0),
                    "mom_ret": i / 50 + (0.1 if industry == "A" else 0),
                    "near_high": 0.98 if industry == "A" else 0.90,
                    "inst_6d": 1.0 if industry == "A" else -1.0,
                })
    return pd.DataFrame(rows)


class RotationResearchTest(unittest.TestCase):
    def test_future_rows_do_not_change_past_group_score(self):
        panel = _panel()
        base = rotation_research.attach_group_scores(panel)
        future = panel.copy()
        extra = future.iloc[-10:].copy()
        extra["date"] = pd.Timestamp("2030-01-01")
        extra["rs_excess"] = -99
        extended = rotation_research.attach_group_scores(
            pd.concat([future, extra], ignore_index=True)
        )
        cols = ["date", "industry", "stock_id", "group_combo_score", "group_rank"]
        left = base[cols].sort_values(cols[:3]).reset_index(drop=True)
        right = extended[extended["date"] < "2030-01-01"][cols]
        right = right.sort_values(cols[:3]).reset_index(drop=True)
        pd.testing.assert_frame_equal(left, right)

    def test_stronger_group_ranks_first(self):
        scored = rotation_research.attach_group_scores(_panel())
        latest = scored[scored["date"] == scored["date"].max()]
        ranks = latest.groupby("industry")["group_rank"].first().to_dict()
        self.assertLess(ranks["A"], ranks["B"])


if __name__ == "__main__":
    unittest.main()
