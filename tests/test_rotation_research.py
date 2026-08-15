"""rotation_research 的離線回歸測試。

後半段(P2)釘住的原本狀態:這支腳本只有自製的 `run_portfolio()` 迴圈能出投組
數字,而那套沒有一字漲停買不到、沒有處置期禁新倉、沒有整張/零股與券商成本
(它用小數股)。「要正式績效就把 picks 餵進正式引擎」以前只寫在註解裡 —— 沒有
可呼叫的路徑,等於每個想比較的人都得自己重寫一次轉換。現在
`formal_picks_by_date` / `formal_portfolio` / `formal_portfolio_sweep` 讓兩套引擎
吃同一份 `build_signal_table()` 輸出,而自製迴圈明確維持 research-only。

⚠ 這裡的假 summary 只驗證轉換與呼叫行為,不代表任何策略績效。
"""
import unittest
from unittest import mock

import pandas as pd

import rotation_research
from evaluation import phases


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


# ── P2:正式引擎路徑 ──────────────────────────────────────────────────────
def _signals() -> pd.DataFrame:
    return pd.DataFrame([
        {"date": pd.Timestamp("2026-03-02"), "stock_id": "1001",
         "name": "低分", "signal_score": 0.20},
        {"date": pd.Timestamp("2026-03-02"), "stock_id": "1002",
         "name": "高分", "signal_score": 0.90},
        {"date": pd.Timestamp("2026-03-03"), "stock_id": "1003",
         "name": "中分", "signal_score": 0.55},
    ])


def _fake_summary(**kwargs) -> dict:
    phase = int(kwargs.get("rebalance_phase", 0))
    return {
        "summary": {
            "sharpe": [1.0, 3.0, 2.0][phase],
            "ann_ret": 0.1,
            "max_drawdown": [-0.10, -0.50, -0.20][phase],
            "n_trades": 5,
            "win_rate": 0.5,
            "eval_audit": {"days_beyond_last_pick": 0},
            "universe": {"formal_evidence_eligible": False},
        }
    }


class FormalPicksTest(unittest.TestCase):
    def test_picks_are_grouped_by_date_and_sorted_by_score(self):
        picks = rotation_research.formal_picks_by_date(_signals())
        self.assertEqual(sorted(picks), [pd.Timestamp("2026-03-02"),
                                         pd.Timestamp("2026-03-03")])
        self.assertEqual(picks[pd.Timestamp("2026-03-02")],
                         [("1002", 0.90, "高分"), ("1001", 0.20, "低分")])

    def test_stock_id_score_and_name_stay_aligned(self):
        """排序後三個欄位必須仍屬於同一列(S19 踩過分開排序導致的錯配)。"""
        picks = rotation_research.formal_picks_by_date(_signals())
        for day, rows in picks.items():
            for sid, score, name in rows:
                src = _signals()
                match = src[(src["date"] == day) & (src["stock_id"] == sid)].iloc[0]
                self.assertAlmostEqual(score, float(match["signal_score"]))
                self.assertEqual(name, match["name"])

    def test_missing_columns_fail_closed(self):
        with self.assertRaises(ValueError):
            rotation_research.formal_picks_by_date(
                _signals().drop(columns=["signal_score"]))

    def test_empty_signals_return_empty_mapping(self):
        self.assertEqual(
            rotation_research.formal_picks_by_date(
                pd.DataFrame(columns=["date", "stock_id", "signal_score"])),
            {},
        )


class FormalPortfolioTest(unittest.TestCase):
    def test_picks_go_into_the_real_event_driven_engine(self):
        with mock.patch.object(rotation_research.backtest, "backtest_portfolio",
                               side_effect=_fake_summary) as engine:
            result = rotation_research.formal_portfolio(
                _signals(), ["1001", "1002", "1003"],
                start_date="2026-03-01", end_date="2026-04-30",
                max_positions=5, rebalance_every=2,
            )
        self.assertIn("summary", result)
        kwargs = engine.call_args.kwargs
        self.assertEqual(set(kwargs["picks_by_date"]),
                         {pd.Timestamp("2026-03-02"), pd.Timestamp("2026-03-03")})
        self.assertEqual(kwargs["top_n"], 5)
        self.assertEqual(kwargs["rebalance_every"], 2)

    def test_evaluation_window_bounds_are_forwarded(self):
        """只限制 picks 日期不夠:引擎沒有 end_date 會跑到資料末端(陷阱 5)。"""
        with mock.patch.object(rotation_research.backtest, "backtest_portfolio",
                               side_effect=_fake_summary) as engine:
            rotation_research.formal_portfolio(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30")
        kwargs = engine.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "2026-03-01")
        self.assertEqual(kwargs["end_date"], "2026-04-30")

    def test_formal_path_never_uses_the_research_only_loop(self):
        """正式數字不得經過自製 positions/cash/MTM 迴圈。"""
        with (
            mock.patch.object(rotation_research.backtest, "backtest_portfolio",
                              side_effect=_fake_summary),
            mock.patch.object(rotation_research, "run_portfolio") as legacy,
        ):
            rotation_research.formal_portfolio(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30")
        legacy.assert_not_called()

    def test_no_picks_means_no_engine_call(self):
        empty = pd.DataFrame(columns=["date", "stock_id", "signal_score", "name"])
        with mock.patch.object(rotation_research.backtest, "backtest_portfolio",
                               side_effect=_fake_summary) as engine:
            self.assertEqual(
                rotation_research.formal_portfolio(
                    empty, ["1001"],
                    start_date="2026-03-01", end_date="2026-04-30"),
                {},
            )
        engine.assert_not_called()


class FormalPortfolioSweepTest(unittest.TestCase):
    def test_sweep_uses_the_shared_phase_implementation(self):
        self.assertIs(rotation_research.sweep_phases, phases.sweep_phases)
        spy = mock.Mock(side_effect=phases.sweep_phases)
        with (
            mock.patch.object(rotation_research.backtest, "backtest_portfolio",
                              side_effect=_fake_summary),
            mock.patch.object(rotation_research, "sweep_phases", spy),
        ):
            sweep = rotation_research.formal_portfolio_sweep(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30",
                rebalance_every=3)
        self.assertEqual(spy.call_args.kwargs["n_phases"], 3)
        self.assertTrue(sweep.full_sweep)
        self.assertEqual(list(sweep.rows["phase"]), [0, 1, 2])

    def test_sweep_reports_median_min_and_worst_drawdown(self):
        with mock.patch.object(rotation_research.backtest, "backtest_portfolio",
                               side_effect=_fake_summary):
            sweep = rotation_research.formal_portfolio_sweep(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30",
                rebalance_every=3)
        stats = sweep.stats()
        self.assertAlmostEqual(stats["sharpe_median"], 2.0)
        self.assertAlmostEqual(stats["sharpe_min"], 1.0)
        self.assertAlmostEqual(stats["worst_max_drawdown"], -0.50)
        self.assertFalse(stats["single_phase_debug"])

    def test_rows_expose_evidence_flags_for_verification(self):
        """相位表要能在結果層面驗證評估窗與候選池,而不是只能相信參數傳對了。"""
        with mock.patch.object(rotation_research.backtest, "backtest_portfolio",
                               side_effect=_fake_summary):
            sweep = rotation_research.formal_portfolio_sweep(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30",
                rebalance_every=1)
        row = sweep.rows.iloc[0]
        self.assertEqual(row["days_beyond_last_pick"], 0)
        # rotation 的候選池是 legacy 單日排名 → 沒傳 PIT provider 就不是正式證據。
        self.assertFalse(row["formal_evidence_eligible"])


if __name__ == "__main__":
    unittest.main()
