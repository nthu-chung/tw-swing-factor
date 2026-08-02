import unittest

import pandas as pd

from rank_flow_strategy import build_rank_flow_signals, event_study


def _fixtures():
    dates = pd.date_range("2026-01-02", periods=12, freq="B")
    rank_a = [70, 65, 60, 55, 18, 15, 12, 11, 10, 9, 8, 7]
    rank_b = [45, 40, 35, 28, 24, 19, 18, 17, 16, 15, 14, 13]
    rows = []
    for stock_id, ranks, flow in [("1001", rank_a, 10.0), ("1002", rank_b, 20.0)]:
        for i, day in enumerate(dates):
            rows.append({
                "date": day, "stock_id": stock_id, "name": stock_id,
                "open": 100 + i, "close": 100 + i + 0.5,
                "universe_rank": ranks[i], "inst_net_5d": flow,
                "above_ma20": True, "ma20": 95 + i,
                "flow_score": 0.1 + i / 100,
                "institution_intensity": 0.01 + i / 1000,
            })
    breadth = pd.DataFrame({
        "date": dates,
        "above_ma20_pct": [0.40, 0.40, 0.40, 0.40, 0.40, 0.46, 0.47, 0.48, 0.49, 0.50, 0.51, 0.52],
        "institution_positive_pct": [0.50, 0.50, 0.50, 0.50, 0.50, 0.56, 0.56, 0.56, 0.56, 0.56, 0.56, 0.56],
    })
    return pd.DataFrame(rows), breadth


class RankFlowStrategyTest(unittest.TestCase):
    def test_exact_signal_conditions_and_reasons(self):
        metrics, breadth = _fixtures()
        signals = build_rank_flow_signals(metrics, breadth)
        dates = pd.date_range("2026-01-02", periods=12, freq="B")
        # Stock 1001 entered top20 from rank 55 yesterday (day 4) and confirms day 5.
        entrant = signals[(signals.stock_id == "1001") & (signals.signal == "confirmed_entrant")]
        self.assertEqual(entrant.iloc[0].date, dates[5])
        self.assertIn("rank 55->18", entrant.iloc[0].reason)
        # Stock 1002 has five top30 observations by day 5 and improved 45 -> 19.
        leader = signals[(signals.stock_id == "1002") & (signals.signal == "persistent_leader")]
        self.assertEqual(leader.iloc[0].date, dates[5])
        self.assertIn("rank improved 45->19", leader.iloc[0].reason)
        expansion = signals[signals.signal == "breadth_expansion"]
        self.assertTrue((expansion.breadth_change_5d_pp >= 5.0).all())
        persistence = signals[
            (signals.stock_id == "1001")
            & (signals.signal == "rank_flow_persistence")
        ]
        self.assertEqual(persistence.iloc[0].date, dates[5])
        self.assertIn("rank improved 70->15", persistence.iloc[0].reason)

    def test_event_study_enters_next_available_open(self):
        metrics, breadth = _fixtures()
        signals = build_rank_flow_signals(metrics, breadth)
        entrant = signals[(signals.stock_id == "1001") & (signals.signal == "confirmed_entrant")].iloc[[0]]
        events = event_study(entrant, metrics, horizons=(5,), deduplicate_overlaps=False)
        expected_entry = pd.Timestamp("2026-01-12")
        self.assertEqual(events.iloc[0].entry_date, expected_entry)
        self.assertEqual(events.iloc[0].entry_open, 106.0)
        self.assertEqual(events.iloc[0].h5_exit_date, pd.Timestamp("2026-01-19"))
        self.assertAlmostEqual(events.iloc[0].h5_return, 111.5 / 106.0 - 1.0)
        self.assertEqual(events.iloc[0].h5_benchmark_n, 1)
        self.assertTrue(pd.notna(events.iloc[0].h5_excess_return))

    def test_event_study_skips_large_positive_entry_gap(self):
        metrics, _ = _fixtures()
        signal = pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-01-09")],
                "stock_id": ["1001"],
                "signal": ["rank_flow_persistence"],
            }
        )
        next_day = pd.Timestamp("2026-01-12")
        metrics.loc[
            (metrics.stock_id == "1001") & (metrics.date == next_day), "open"
        ] = 120.0
        events = event_study(signal, metrics, horizons=(5,))
        self.assertTrue(events.empty)

    def test_event_path_crossing_quarantine_is_not_measured(self):
        metrics, _ = _fixtures()
        metrics["price_quarantine_flag"] = False
        signal = pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-01-09")],
                "stock_id": ["1001"],
                "signal": ["rank_flow_persistence"],
            }
        )
        quarantine_day = pd.Timestamp("2026-01-13")
        metrics.loc[
            (metrics.stock_id == "1001")
            & (metrics.date == quarantine_day),
            "price_quarantine_flag",
        ] = True
        events = event_study(signal, metrics, horizons=(5,))
        self.assertEqual(len(events), 1)
        self.assertTrue(events.iloc[0].h5_integrity_blocked)
        self.assertTrue(pd.isna(events.iloc[0].h5_return))

    def test_future_perturbation_does_not_change_past_signals(self):
        metrics, breadth = _fixtures()
        base = build_rank_flow_signals(metrics, breadth)
        future = metrics[metrics.date == metrics.date.max()].copy()
        future["date"] = pd.Timestamp("2030-01-02")
        future["universe_rank"] = [1, 999]
        future["inst_net_5d"] = [99999, -99999]
        future["close"] = [9999, 1]
        extended = build_rank_flow_signals(pd.concat([metrics, future], ignore_index=True), breadth)
        pd.testing.assert_frame_equal(base, extended[extended.date <= metrics.date.max()].reset_index(drop=True))

    def test_overlap_deduplication_keeps_first_signal_for_stock(self):
        metrics, breadth = _fixtures()
        signals = build_rank_flow_signals(metrics, breadth)
        # Use two known signals from the same hypothesis on consecutive days;
        # the 20-day default overlap window must retain only the earliest one.
        chosen = signals[
            (signals.stock_id == "1002")
            & (signals.signal == "persistent_leader")
        ].head(2)
        self.assertEqual(len(chosen), 2)
        events = event_study(chosen, metrics, horizons=(5,), deduplicate_overlaps=True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0].signal_date, chosen.iloc[0].date)

    def test_overlap_deduplication_keeps_distinct_hypotheses(self):
        metrics, breadth = _fixtures()
        signal_date = pd.Timestamp("2026-01-09")
        signals = pd.DataFrame(
            {
                "date": [signal_date, signal_date],
                "stock_id": ["1002", "1002"],
                "signal": ["breadth_expansion", "persistent_leader"],
            }
        )
        events = event_study(
            signals, metrics, horizons=(5,), deduplicate_overlaps=True
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(
            set(events["signal"]),
            {"breadth_expansion", "persistent_leader"},
        )


if __name__ == "__main__":
    unittest.main()
