import unittest

import numpy as np
import pandas as pd

from quiet_sponsor_strategy import (
    build_quiet_sponsor_signals,
    event_study,
    quiet_sponsor_summary,
)


def _fixture(days: int = 150) -> pd.DataFrame:
    """Five legal dynamic names; 1001 alone has the strongest quiet flow."""
    dates = pd.date_range("2025-01-02", periods=days, freq="B")
    rows = []
    for stock_num in range(5):
        stock_id = str(1001 + stock_num)
        for i, day in enumerate(dates):
            # Keep the 10-day return below 5%, MA rising, and leave headroom
            # for an exact prior-10 high breakout on the final signal day.
            close = 100.0 + 0.20 * i + 0.01 * stock_num
            high = close + 0.45
            low = close - 0.45
            volume = 1_000.0
            institution_net = 1.0 if stock_num == 0 else 0.05
            if i == days - 1:
                close += 0.60
                high = close + 0.45
                low = close - 0.45
                volume = 1_300.0
                institution_net = 80.0 if stock_num == 0 else 0.05
            rows.append(
                {
                    "date": day,
                    "stock_id": stock_id,
                    "name": stock_id,
                    "open": close - 0.05,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "institution_net": institution_net,
                    "dynamic_universe_flag": True,
                    "liquidity_rank": stock_num + 1,
                    "price_quarantine_flag": False,
                    # Required by reusable rank_flow_strategy.event_study.
                    "universe_rank": stock_num + 1,
                    "above_ma20": True,
                }
            )
    return pd.DataFrame(rows)


class QuietSponsorStrategyTest(unittest.TestCase):
    def test_exact_conditions_trigger_after_hard_120_observation_warmup(self):
        metrics = _fixture()
        signals = build_quiet_sponsor_signals(metrics)
        target = signals[signals.stock_id == "1001"]
        self.assertEqual(len(target), 1)
        self.assertEqual(target.iloc[0].date, metrics.date.max())
        self.assertEqual(target.iloc[0].institution_positive_days_6, 6)
        self.assertLessEqual(target.iloc[0].flow_cross_section_pct, 0.20)
        self.assertGreater(target.iloc[0].close, target.iloc[0].prior10_high)
        self.assertTrue(target.iloc[0].volume_ratio >= 1.10)
        self.assertTrue(target.iloc[0].volume_ratio <= 1.80)
        self.assertEqual(target.iloc[0].atr_history_count, 120)

    def test_under_120_prior_atr_observations_has_no_signal_and_reports_status(self):
        metrics = _fixture(days=62)
        signals = build_quiet_sponsor_signals(metrics)
        self.assertTrue(signals.empty)
        summary = quiet_sponsor_summary(metrics, signals, pd.DataFrame())
        self.assertEqual(summary["required_history"], 120)
        self.assertEqual(summary["insufficient_history_status"], "insufficient_history")

    def test_future_perturbation_does_not_change_past_signals(self):
        metrics = _fixture()
        base = build_quiet_sponsor_signals(metrics)
        future = metrics[metrics.date == metrics.date.max()].copy()
        future["date"] = pd.Timestamp("2030-01-02")
        future["close"] = [9_999, 1, 1, 1, 1]
        future["institution_net"] = [9_999, -9_999, -9_999, -9_999, -9_999]
        future["volume"] = 9_999
        extended = build_quiet_sponsor_signals(pd.concat([metrics, future], ignore_index=True))
        pd.testing.assert_frame_equal(
            base,
            extended[extended.date <= metrics.date.max()].reset_index(drop=True),
        )

    def test_event_study_uses_t_plus_1_open_and_rejects_positive_gap_above_four_percent(self):
        signal_metrics = _fixture(days=150)
        next_rows = signal_metrics[signal_metrics.date == signal_metrics.date.max()].copy()
        next_rows["date"] = signal_metrics.date.max() + pd.offsets.BDay(1)
        next_rows["open"] = next_rows["close"] - 0.05
        metrics = pd.concat([signal_metrics, next_rows], ignore_index=True)
        signals = build_quiet_sponsor_signals(signal_metrics)
        signal = signals[signals.stock_id == "1001"].iloc[[0]]
        expected_entry = next_rows.date.iloc[0]
        events = event_study(signal, metrics, horizons=(5,), max_entry_gap=0.04)
        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0].entry_date, expected_entry)
        self.assertAlmostEqual(events.iloc[0].entry_open, float(metrics.loc[
            (metrics.stock_id == "1001") & (metrics.date == expected_entry), "open"
        ].iloc[0]))

        gapped = metrics.copy()
        signal_close = float(signal.iloc[0].close)
        gapped.loc[
            (gapped.stock_id == "1001") & (gapped.date == expected_entry), "open"
        ] = signal_close * 1.041
        self.assertTrue(event_study(signal, gapped, horizons=(5,), max_entry_gap=0.04).empty)

    def test_missing_quarantine_flag_is_an_integrity_block(self):
        metrics = _fixture().drop(columns="price_quarantine_flag")
        with self.assertRaisesRegex(ValueError, "price_quarantine_flag"):
            build_quiet_sponsor_signals(metrics)

    def test_quarantine_inside_reference_window_resets_clean_history(self):
        metrics = _fixture(days=150)
        stock_mask = metrics.stock_id == "1001"
        contaminated_day = metrics.loc[stock_mask, "date"].iloc[80]
        metrics.loc[
            stock_mask & (metrics.date == contaminated_day),
            "price_quarantine_flag",
        ] = True
        signals = build_quiet_sponsor_signals(metrics)
        self.assertTrue(signals[signals.stock_id == "1001"].empty)


if __name__ == "__main__":
    unittest.main()
