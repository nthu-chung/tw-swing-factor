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

from _offline_registry import use_common_stocks
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
        with mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
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
        with mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
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
            mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
                              side_effect=_fake_summary),
            mock.patch.object(rotation_research, "run_portfolio") as legacy,
        ):
            rotation_research.formal_portfolio(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30")
        legacy.assert_not_called()

    def test_strategy_spec_is_forwarded_to_the_engine(self):
        """訊號規則的 provenance 必須有路徑傳到 summary。

        原 bug:`strategy_spec` 只存在於 `backtest_portfolio` 的簽章,rotation 的
        正式入口根本沒有這個參數 —— 於是「傳了 PIT provider 的 rotation 正式
        績效」在 summary 裡是 `formal_evidence_eligible=True` 配
        `params.strategy=None`(引擎 external picks 路徑也不算 factor weights),
        沒有任何欄位描述產生它的規則。
        """
        spec = {"name": "rotation_probe", "signal": {"window": 20}}
        with mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
                               side_effect=_fake_summary) as engine:
            rotation_research.formal_portfolio(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30",
                strategy_spec=spec)
        self.assertIs(engine.call_args.kwargs["strategy_spec"], spec)

        with mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
                               side_effect=_fake_summary) as engine:
            rotation_research.formal_portfolio_sweep(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30",
                rebalance_every=1, strategy_spec=spec)
        self.assertIs(engine.call_args.kwargs["strategy_spec"], spec)

    def test_no_picks_means_no_engine_call(self):
        empty = pd.DataFrame(columns=["date", "stock_id", "signal_score", "name"])
        with mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
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
            mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
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
        with mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
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
        with mock.patch.object(rotation_research.event_backtest, "backtest_portfolio",
                               side_effect=_fake_summary):
            sweep = rotation_research.formal_portfolio_sweep(
                _signals(), ["1001"],
                start_date="2026-03-01", end_date="2026-04-30",
                rebalance_every=1)
        row = sweep.rows.iloc[0]
        self.assertEqual(row["days_beyond_last_pick"], 0)
        # rotation 的候選池是 legacy 單日排名 → 沒傳 PIT provider 就不是正式證據。
        self.assertFalse(row["formal_evidence_eligible"])


class ResearchOnlyStampTest(unittest.TestCase):
    """探索性產出必須自帶「我不是正式績效」的欄位。

    原缺陷:`main()` 寫出的 rotation_is_oos.csv / rotation_trades.csv /
    theme_case_audit.csv 沒有任何 in-artifact 標記,落到 outputs/ 之後跟正式回測
    結果長得一模一樣;research-only 的資訊只存在原始碼 docstring 與
    STRATEGY_REGISTRY 裡。本 repo 對正式結果的標準是「結果必須自帶說得出可不可以
    當正式證據的欄位」,探索性產出沒有理由例外。
    """

    def test_stamp_adds_self_describing_columns(self):
        out = rotation_research.stamp_research_only(
            pd.DataFrame({"variant": ["a"], "sharpe": [1.0]}))
        self.assertTrue(bool(out.iloc[0]["research_only"]))
        self.assertFalse(bool(out.iloc[0]["formal_evidence_eligible"]))
        self.assertEqual(out.iloc[0]["engine"],
                         "rotation_research.run_portfolio")
        self.assertIn("非 PIT", str(out.iloc[0]["reason"]))

    def test_stamp_keeps_the_original_columns(self):
        src = pd.DataFrame({"variant": ["a"], "sharpe": [1.0]})
        out = rotation_research.stamp_research_only(src)
        for col in src.columns:
            self.assertIn(col, out.columns)
        self.assertEqual(len(out), len(src))


class FormalPortfolioEndToEndTest(unittest.TestCase):
    """不 mock 正式引擎的端到端測試。

    原缺陷:`FormalPortfolioTest` / `FormalPortfolioSweepTest` 的每一支都
    `mock.patch.object(rotation_research.event_backtest, "backtest_portfolio", ...)`,
    斷言的是 call_args 與假 summary 的聚合 —— 真引擎的參數相容性
    (picks_by_date 的 key 型別、symbols 與 picks 的一致性、start/end 是否被接受)
    完全沒被覆蓋。這正是「mock 過深,測到 mock」:把 formal_portfolio 的參數改壞,
    那批測試仍然全綠。這一支只 mock 資料層,讓真引擎跑完。
    """

    def _prices(self, ids, days):
        frames = {}
        for i, sid in enumerate(ids):
            frames[sid] = pd.DataFrame({
                "date": days,
                "open": 100.0 + i, "high": 101.0 + i,
                "low": 99.0 + i, "close": 100.0 + i,
                "volume": 1_000_000, "turnover": 1e8,
            })
        return frames

    def test_real_engine_accepts_the_picks_and_reports_honest_provenance(self):
        ids = ["1001", "1002", "1003"]
        # 證券別閘門會擋掉判不出來的代號(這是對的),所以離線測試要顯式宣告。
        use_common_stocks(self, *ids)
        days = list(pd.bdate_range("2026-03-02", periods=12))
        prices = self._prices(ids, days)
        with (
            mock.patch.object(rotation_research.event_backtest,
                              "_assert_price_integrity", lambda *a, **k: None),
            mock.patch.object(rotation_research.event_backtest,
                              "_load_disposition_days", lambda *a, **k: {}),
            mock.patch.object(rotation_research.event_backtest.data, "fetch_price",
                              side_effect=lambda sid, *a, **k: prices[sid].copy()),
        ):
            res = rotation_research.formal_portfolio(
                _signals(), ids,
                start_date="2026-03-02", end_date="2026-03-17",
                max_positions=2, rebalance_every=1)

        self.assertIn("summary", res, f"真引擎沒有產出 summary:{str(res)[:200]}")
        summary = res["summary"]
        self.assertEqual(summary["eval_audit"]["days_beyond_last_pick"], 0)
        # legacy 單日候選池 + 沒傳 PIT provider → 必須誠實標成不可作正式證據
        self.assertFalse(summary["universe"]["formal_evidence_eligible"])


if __name__ == "__main__":
    unittest.main()
