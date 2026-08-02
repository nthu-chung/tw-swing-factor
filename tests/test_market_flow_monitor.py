import unittest

import numpy as np
import pandas as pd

from market_flow_monitor import compute_market_flow, write_outputs


def _panels(periods=32):
    dates = pd.date_range("2026-01-02", periods=periods, freq="B")
    price_rows, flow_rows = [], []
    for stock_id, start, slope, flow in [
        ("1001", 100.0, 1.5, 120.0),
        ("1002", 90.0, 1.0, 60.0),
        ("1003", 110.0, 0.4, -80.0),
        ("1004", 80.0, 0.7, 20.0),
    ]:
        for i, day in enumerate(dates):
            price_rows.append({
                "date": day, "stock_id": stock_id, "close": start + slope * i,
                "turnover": 1_000_000 + 50_000 * i + int(stock_id),
                "volume": 100_000 + 1_000 * i,
            })
            flow_rows.append({"date": day, "stock_id": stock_id, "institution_net": flow + i})
    return pd.DataFrame(price_rows), pd.DataFrame(flow_rows)


class MarketFlowMonitorTest(unittest.TestCase):
    def test_future_rows_do_not_change_past_results(self):
        prices, flows = _panels()
        base = compute_market_flow(prices, flows, top_n=2)
        future_prices, future_flows = _panels(periods=5)
        future_prices = future_prices.groupby("stock_id", group_keys=False).tail(1).copy()
        future_flows = future_flows.groupby("stock_id", group_keys=False).tail(1).copy()
        future_prices["date"] = pd.Timestamp("2030-01-02")
        future_prices["close"] = [1.0, 9_999.0, 3.0, 4.0]
        future_prices["turnover"] = 9_999_999
        future_flows["date"] = pd.Timestamp("2030-01-02")
        future_flows["institution_net"] = [-9_999.0, 9_999.0, 0.0, 0.0]
        extended = compute_market_flow(
            pd.concat([prices, future_prices], ignore_index=True),
            pd.concat([flows, future_flows], ignore_index=True), top_n=2,
        )
        cutoff = prices["date"].max()
        for attr in ("stock_metrics", "market_breadth", "membership_events", "top_n_churn"):
            left = getattr(base, attr)
            right = getattr(extended, attr)
            left = left[left["date"] <= cutoff].reset_index(drop=True)
            right = right[right["date"] <= cutoff].reset_index(drop=True)
            pd.testing.assert_frame_equal(left, right)

    def test_rank_deltas_and_top_n_entry_exit_are_reported(self):
        prices, flows = _panels()
        last_day = prices["date"].max()
        # A contemporaneous shock is permitted to change today's rank, but not earlier ranks.
        # Stay below the integrity threshold: this test is about normal
        # contemporaneous rank movement, not an unadjusted corporate action.
        prices.loc[(prices["stock_id"] == "1003") & (prices["date"] == last_day), "close"] = 145.0
        prices.loc[(prices["stock_id"] == "1003") & (prices["date"] == last_day), "turnover"] = 20_000_000
        flows.loc[(flows["stock_id"] == "1003") & (flows["date"] == last_day), "institution_net"] = 10_000
        result = compute_market_flow(prices, flows, top_n=2)
        latest = result.stock_metrics[result.stock_metrics["date"] == last_day]
        self.assertTrue(latest["rank_delta_1d"].notna().all())
        self.assertTrue(latest["rank_delta_5d"].notna().all())
        event_stocks = set(result.membership_events.loc[result.membership_events["date"] == last_day, "stock_id"])
        self.assertIn("1003", event_stocks)
        latest_churn = result.top_n_churn.iloc[-1]
        self.assertGreater(latest_churn["top_n_churn"], 0)
        self.assertEqual(latest_churn["entrants"], latest_churn["exits"])

    def test_breadth_uses_only_available_twenty_day_history(self):
        prices, flows = _panels()
        result = compute_market_flow(prices, flows, top_n=2)
        early = result.market_breadth.iloc[0]
        latest = result.market_breadth.iloc[-1]
        self.assertEqual(early["eligible_count"], 0)
        self.assertTrue(np.isnan(early["above_ma20_pct"]))
        self.assertEqual(latest["eligible_count"], 4)
        self.assertEqual(latest["above_ma20_pct"], 1.0)
        self.assertEqual(latest["new_high_20d_pct"], 1.0)
        self.assertAlmostEqual(latest["institution_positive_pct"], 0.75)

    def test_discontinuity_is_quarantined_for_its_twenty_day_return_window(self):
        prices, flows = _panels(periods=50)
        prices.loc[prices["stock_id"] == "1001", "stock_id"] = "2380"
        anomaly_index = 25
        stock_mask = prices["stock_id"] == "2380"
        stock_dates = prices.loc[stock_mask, "date"].sort_values().to_list()
        post_action = stock_dates[anomaly_index:]
        # An upward 2380-like unadjusted corporate-action jump would otherwise
        # dominate momentum.  There is no open column, so this also exercises
        # the close-only integrity fallback.
        prices.loc[stock_mask & prices["date"].isin(post_action), "close"] *= 2
        result = compute_market_flow(prices, flows, top_n=2)
        metrics = result.stock_metrics.set_index(["stock_id", "date"])
        anomaly_day = stock_dates[anomaly_index]
        self.assertTrue(metrics.loc[("2380", anomaly_day), "price_discontinuity_flag"])
        for day in stock_dates[anomaly_index : anomaly_index + 21]:
            row = metrics.loc[("2380", day)]
            self.assertTrue(row["price_quarantine_flag"])
            self.assertTrue(pd.isna(row["momentum_z"]))
            self.assertTrue(pd.isna(row["flow_score"]))
            self.assertTrue(pd.isna(row["universe_rank"]))
            self.assertFalse(row["top_n_flag"])
        recovered = metrics.loc[("2380", stock_dates[anomaly_index + 21])]
        self.assertFalse(recovered["price_quarantine_flag"])
        self.assertTrue(pd.notna(recovered["flow_score"]))
        self.assertLess(
            result.market_breadth.loc[result.market_breadth["date"] == anomaly_day, "eligible_count"].iloc[0],
            4,
        )

    def test_dynamic_liquidity_pool_limits_ranks_and_excludes_quarantine(self):
        prices, flows = _panels(periods=32)
        latest_day = prices["date"].max()
        # 1004 is the most liquid name, but its discontinuity makes it
        # ineligible for the dynamic pool on this day.
        prices.loc[(prices["stock_id"] == "1004") & (prices["date"] == latest_day), "close"] *= 2
        result = compute_market_flow(prices, flows, top_n=2, universe_size=2)
        latest = result.stock_metrics[result.stock_metrics["date"] == latest_day].set_index("stock_id")

        self.assertTrue(latest.loc["1004", "price_quarantine_flag"])
        self.assertFalse(latest.loc["1004", "dynamic_universe_flag"])
        self.assertTrue(pd.isna(latest.loc["1004", "liquidity_rank"]))
        self.assertTrue(pd.isna(latest.loc["1004", "flow_score"]))
        self.assertTrue(pd.isna(latest.loc["1004", "universe_rank"]))

        dynamic = latest[latest["dynamic_universe_flag"]]
        self.assertEqual(len(dynamic), 2)
        self.assertTrue(dynamic["price_quarantine_flag"].eq(False).all())
        self.assertEqual(set(dynamic["liquidity_rank"].astype(int)), {1, 2})
        self.assertEqual(set(dynamic["universe_rank"].astype(int)), {1, 2})
        self.assertTrue(latest.loc[~latest["dynamic_universe_flag"], "flow_score"].isna().all())
        self.assertTrue(latest.loc[~latest["dynamic_universe_flag"], "universe_rank"].isna().all())

        latest_breadth = result.market_breadth[result.market_breadth["date"] == latest_day].iloc[0]
        self.assertEqual(latest_breadth["dynamic_universe_count"], 2)

    def test_outputs_include_integrity_audit_and_quarantine_summary(self):
        import json
        import tempfile
        from pathlib import Path

        prices, flows = _panels(periods=32)
        jump_day = prices.loc[prices["stock_id"] == "1001", "date"].iloc[-1]
        prices.loc[(prices["stock_id"] == "1001") & (prices["date"] == jump_day), "close"] *= 2
        result = compute_market_flow(prices, flows, top_n=2)
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_outputs(result, Path(tmp), jump_day, 2, universe_size=3)
            self.assertTrue(paths["integrity_audit"].exists())
            audit = pd.read_csv(paths["integrity_audit"])
            self.assertEqual(audit.loc[0, "stock_id"], 1001)
            self.assertIn("previous_close", audit)
            self.assertIn("close_return", audit)
            summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
            self.assertEqual(summary["universe_size"], 3)
            self.assertEqual(summary["latest_quarantine_count"], 1)
            self.assertGreaterEqual(summary["cumulative_quarantine_count"], 1)
            self.assertEqual(summary["quarantined_stock_count"], 1)

    def test_top_n_cannot_exceed_dynamic_universe(self):
        prices, flows = _panels()
        with self.assertRaisesRegex(ValueError, "top_n cannot exceed"):
            compute_market_flow(prices, flows, top_n=3, universe_size=2)


if __name__ == "__main__":
    unittest.main()
