from __future__ import annotations

import unittest

import pandas as pd

import data
from universes.dynamic import add_membership


def _panel(rows):
    return pd.DataFrame(
        rows,
        columns=["stock_id", "date", "turnover", "volume", "close"],
    )


class DynamicUniverseTest(unittest.TestCase):
    def test_membership_rotates_by_trailing_turnover(self):
        panel = _panel([
            ("A", "2026-01-01", 100, 10_000, 10),
            ("A", "2026-01-02", 100, 10_000, 10),
            ("A", "2026-01-03", 100, 10_000, 10),
            ("A", "2026-01-04", 100, 10_000, 10),
            ("B", "2026-01-01", 50, 10_000, 10),
            ("B", "2026-01-02", 50, 10_000, 10),
            ("B", "2026-01-03", 500, 10_000, 10),
            ("B", "2026-01-04", 500, 10_000, 10),
        ])
        out = add_membership(
            panel, top_n=1, lookback=2, min_obs=2,
            min_avg_volume_lots=0,
        )
        members = {
            str(day.date()): grp.loc[grp["in_dynamic_universe"], "stock_id"].tolist()
            for day, grp in out.groupby("date")
        }
        self.assertEqual(members["2026-01-02"], ["A"])
        self.assertEqual(members["2026-01-03"], ["B"])

    def test_future_rows_do_not_change_past_membership(self):
        base = _panel([
            ("A", "2026-01-01", 100, 10_000, 10),
            ("A", "2026-01-02", 100, 10_000, 10),
            ("B", "2026-01-01", 50, 10_000, 10),
            ("B", "2026-01-02", 50, 10_000, 10),
        ])
        extended = pd.concat([
            base,
            _panel([
                ("A", "2026-01-03", 1, 10_000, 10),
                ("B", "2026-01-03", 1_000_000, 10_000, 10),
            ]),
        ], ignore_index=True)

        kwargs = dict(top_n=1, lookback=2, min_obs=2, min_avg_volume_lots=0)
        before = add_membership(base, **kwargs)
        after = add_membership(extended, **kwargs)
        cutoff = pd.Timestamp("2026-01-02")
        cols = ["stock_id", "date", "in_dynamic_universe", "universe_rank"]
        pd.testing.assert_frame_equal(
            before[cols].sort_values(["stock_id", "date"]).reset_index(drop=True),
            after.loc[after["date"] <= cutoff, cols]
            .sort_values(["stock_id", "date"]).reset_index(drop=True),
        )

    def test_daily_rank_is_limited_to_locked_candidate_pool(self):
        """非月池成員即使成交值最大，也不能擠掉當月合法候選。"""
        panel = _panel([
            ("A", "2026-01-01", 1_000_000, 10_000, 10),
            ("A", "2026-01-02", 1_000_000, 10_000, 10),
            ("B", "2026-01-01", 100, 10_000, 10),
            ("B", "2026-01-02", 100, 10_000, 10),
        ])
        candidate = panel["stock_id"].eq("B")
        out = add_membership(
            panel, top_n=1, lookback=2, min_obs=2,
            min_avg_volume_lots=0, candidate_mask=candidate,
        )
        day2 = out[out["date"] == pd.Timestamp("2026-01-02")]
        self.assertEqual(day2.loc[day2["in_dynamic_universe"], "stock_id"].tolist(), ["B"])
        self.assertFalse(bool(day2.loc[day2["stock_id"] == "A", "universe_eligible"].iloc[0]))

    def test_zero_price_placeholder_is_removed(self):
        raw = pd.DataFrame([
            {"date": "2026-01-01", "open": 10, "high": 11, "low": 9,
             "close": 10, "volume": 1000, "turnover": 10_000},
            {"date": "2026-01-02", "open": 0, "high": 0, "low": 0,
             "close": 0, "volume": 0, "turnover": 0},
        ])
        clean = data._clean_price_frame(raw)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean.iloc[0]["close"], 10)


if __name__ == "__main__":
    unittest.main()
