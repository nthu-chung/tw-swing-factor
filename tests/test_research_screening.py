# -*- coding: utf-8 -*-
"""Golden Path 候選清單的離線回歸測試。"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import pandas as pd

from research.fixtures import synthetic_fixture
from research.golden_path import run_golden_path
from research.screening import build_candidate_screen, format_candidate_screen
from strategy_kit.contracts import SignalContext
from strategy_kit.registry import resolve


def _signals(*, complete: bool = True) -> pd.DataFrame:
    rows = []
    for day in pd.to_datetime(["2026-01-05", "2026-01-06"]):
        for rank, sid in enumerate(("A", "B", "C"), 1):
            rows.append({
                "date": day, "stock_id": sid, "eligible": True,
                "raw_score": 1.0 / rank, "rank": rank,
                "ranking_universe_count": 3,
                "snapshot_complete": complete,
                "strategy_id": "probe", "strategy_version": "1.0",
                "reason_codes": f"reason_{sid}",
            })
    return pd.DataFrame(rows)


class CandidateScreenTest(unittest.TestCase):
    def test_latest_complete_snapshot_is_rendered_without_reranking(self):
        panel = pd.DataFrame([
            {"date": "2026-01-06", "stock_id": "A", "name": "甲",
             "industry": "半導體", "close": 101.0},
            {"date": "2026-01-06", "stock_id": "B", "name": "乙",
             "industry": "電子", "close": 52.0},
            {"date": "2026-01-06", "stock_id": "C", "name": "丙",
             "industry": "傳產", "close": 33.0},
        ])
        screen = build_candidate_screen(_signals(), panel=panel, top_n=2)
        self.assertEqual(screen["stock_id"].tolist(), ["A", "B"])
        self.assertEqual(screen["rank"].tolist(), [1, 2])
        self.assertEqual(screen["name"].tolist(), ["甲", "乙"])
        self.assertEqual(set(screen["list_type"]),
                         {"research_candidate_not_order"})
        text = format_candidate_screen(screen)
        self.assertIn("不是交易指令", text)
        self.assertIn("甲", text)

    def test_incomplete_latest_snapshot_fails_closed(self):
        with self.assertRaises(ValueError):
            build_candidate_screen(_signals(complete=False), top_n=2)


class DisplayPriceSpaceTest(unittest.TestCase):
    """人類端看到的價格必須是**原始成交價**,而且價格空間要講明白。

    實測(6515 @ 2025-10-21):還原價 2526.24、實際成交價 2450.00,差 3.1%;
    而且還原價的絕對水準會隨抓取窗改變(兩年窗 2526.24、500 日窗 2497.85)。
    拿還原價去對券商畫面必然對不起來,所以顯示層不能只印一個 `close`。
    """

    def _panel(self, **extra):
        base = {"date": "2026-01-06", "stock_id": "A", "name": "甲",
                "industry": "半導體", "close": 2526.24}
        base.update(extra)
        return pd.DataFrame([base])

    def test_as_traded_price_is_shown_when_available(self):
        screen = build_candidate_screen(
            _signals(), panel=self._panel(close_raw=2450.0), top_n=1)
        row = screen.iloc[0]
        self.assertAlmostEqual(float(row["close_as_traded"]), 2450.0)
        self.assertAlmostEqual(float(row["close_adjusted"]), 2526.24)
        self.assertEqual(row["price_space"], "as_traded")
        text = format_candidate_screen(screen)
        self.assertIn("收盤 2450.00", text)
        self.assertNotIn("2526.24", text)

    def test_adjusted_only_is_labelled_not_silently_shown_as_close(self):
        """拿不到原始價時,不得讓還原價冒充成交價。"""
        screen = build_candidate_screen(_signals(), panel=self._panel(), top_n=1)
        row = screen.iloc[0]
        self.assertTrue(pd.isna(row["close_as_traded"]))
        self.assertEqual(row["price_space"], "adjusted_only")
        text = format_candidate_screen(screen)
        self.assertIn("還原價 2526.24", text)
        self.assertIn("非成交價", text)

    def test_no_bare_close_column_survives_into_the_output(self):
        """輸出裡不留一個沒有標明空間的 `close`,免得下游又拿去當成交價。"""
        screen = build_candidate_screen(
            _signals(), panel=self._panel(close_raw=2450.0), top_n=1)
        self.assertNotIn("close", screen.columns)
        self.assertNotIn("close_raw", screen.columns)


class CompanyNameTest(unittest.TestCase):
    """原缺陷:`panel["name"] = panel["stock_id"]`,清單印成「4967 4967」。"""

    def _panel(self):
        return pd.DataFrame({
            "date": pd.to_datetime(["2024-09-01", "2025-01-01", "2024-09-01"]),
            "stock_id": ["2718", "2718", "9999"]})

    def _run(self, pit_history):
        import live_signal
        import security_type

        previous = (security_type.default_registry()
                    if security_type._REGISTRY_CACHE is not None else None)
        security_type.set_registry({
            "2718": ("twse", "觀光餐旅", "全心投控"),
            "4967": ("twse", "電子工業", "十銓")})
        try:
            with mock.patch("universes.pit_snapshots.load_history_cached",
                            return_value=pit_history):
                return live_signal._attach_display_fields(self._panel())
        finally:
            security_type.set_registry(previous)

    def test_exchange_snapshot_gives_point_in_time_names(self):
        """交易所逐日快照的 name 是**那一天**的 name,不是今天的。

        實測 2024-06~2026-06 的交易所快照有 24 檔改過名;這裡用其中一檔的真實
        改名軌跡:2718 晶悅 → 全心投控(2024-12-20)。用現值表的話,2024-09 的
        候選清單會印出「全心投控」—— 一個當時還不存在的名字。
        """
        pit = pd.DataFrame([
            {"date": pd.Timestamp("2024-09-01"), "stock_id": "2718",
             "name": "晶悅"},
            {"date": pd.Timestamp("2025-01-01"), "stock_id": "2718",
             "name": "全心投控"},
        ])
        panel = self._run(pit)
        self.assertEqual(panel["name"].tolist()[:2], ["晶悅", "全心投控"])

    def test_registry_fills_gaps_and_unknown_stays_blank(self):
        """交易所快照沒有的列退回現值表;兩層都沒有就留空,不回填股號。"""
        panel = self._run(pd.DataFrame(columns=["date", "stock_id", "name"]))
        self.assertEqual(panel["name"].tolist(), ["全心投控", "全心投控", ""])
        self.assertNotIn("9999", panel["name"].tolist())
        self.assertEqual(panel["industry"].tolist(),
                         ["觀光餐旅", "觀光餐旅", ""])

    def test_exchange_lookup_failure_degrades_to_registry_not_crash(self):
        """公司名不是風險控制欄位:抓不到只該少一個名字,不該讓 panel 建不起來。"""
        import live_signal
        import security_type

        previous = (security_type.default_registry()
                    if security_type._REGISTRY_CACHE is not None else None)
        security_type.set_registry({"2718": ("twse", "觀光餐旅", "全心投控")})
        try:
            with mock.patch("universes.pit_snapshots.load_history_cached",
                            side_effect=RuntimeError("端點掛了")):
                panel = live_signal._attach_display_fields(self._panel())
        finally:
            security_type.set_registry(previous)
        self.assertEqual(panel["name"].tolist()[0], "全心投控")


class SweepDelegationTest(unittest.TestCase):
    """兩條入口都必須把相位掃描委派給 `evaluation.phases.sweep_phases`(§7.3)。

    這條測不是形式主義:AST 守衛擋的是「長出第四份手寫迴圈」,但擋不了「有人把
    sweep_phases 匯入了卻沒真的呼叫」。用 spy 直接觀察委派行為,兩者互補。
    """

    def _spy(self):
        import research.golden_path as gp

        calls = []
        real = gp.sweep_phases

        def _wrapped(*a, **kw):
            calls.append(kw.get("n_phases"))
            return real(*a, **kw)

        return calls, mock.patch.object(gp, "sweep_phases", side_effect=_wrapped)

    def test_registered_strategy_entry_delegates_to_shared_sweep(self):
        from research.golden_path import run_golden_path

        calls, patcher = self._spy()
        with tempfile.TemporaryDirectory() as td, patcher:
            run_golden_path(strategy_id="s19_reference_make_signals",
                            fixture_name="synthetic", output_dir=td,
                            stamp="delegation",
                            fixture_kwargs={"n_symbols": 6, "n_days": 120})
        self.assertEqual(calls, [5], "strategy 入口必須委派共用掃描,且跑滿五相位")

    def test_external_signal_frame_entry_delegates_to_shared_sweep(self):
        from backtest import event_backtest
        import research.golden_path as gp

        frame = pd.DataFrame([
            {"date": d, "stock_id": "A", "rank": 1, "raw_score": 1.0,
             "eligible": True, "snapshot_complete": True,
             "ranking_universe_count": 1}
            for d in pd.bdate_range("2026-01-05", periods=30)])
        engine_result = {
            "summary": {"n_trades": 0},
            "equity_curve": pd.DataFrame(
                {"date": pd.bdate_range("2026-01-05", periods=20),
                 "equity": [1_000_000.0] * 20}),
        }

        class _Uni:
            def backtest_kwargs(self):
                return {"symbols": ["A"], "universe_provider": object(),
                        "sample": False, "dynamic_enabled": True}

        calls, patcher = self._spy()
        with patcher, mock.patch.object(event_backtest, "backtest_portfolio",
                                        return_value=engine_result):
            gp.run_signal_frame_backtest(signal_frame=frame, universe=_Uni())
        self.assertEqual(calls, [5], "外部 SignalFrame 入口也必須委派共用掃描")


class CandidateScreenMatchesSignalsTest(unittest.TestCase):
    """人看到的候選必須**逐值**等於回測用的那份 SignalFrame(§7.7、§7.9)。

    這是整條鏈的核心承諾:如果 candidate screen 自己重算或重排,人類看到的名單
    就又變成第二套策略 —— 那正是 `screener.py` 現在的問題。
    """

    def _run(self, td, *, n_symbols: int = 8):
        from research.golden_path import run_golden_path

        return run_golden_path(
            strategy_id="s19_reference_make_signals", fixture_name="synthetic",
            output_dir=td, stamp="match",
            fixture_kwargs={"n_symbols": n_symbols, "n_days": 120})

    def test_screen_rows_are_value_identical_to_last_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._run(td)
        signals = result.tables["signals"]
        screen = result.tables["candidate_screen"]

        complete = signals[signals["snapshot_complete"].astype(bool)]
        last_day = pd.Timestamp(complete["date"].max())
        expected = complete[complete["date"].eq(last_day)].sort_values("rank")
        expected = expected[expected["rank"] <= len(screen)]

        self.assertEqual(set(pd.to_datetime(screen["date"]).unique()),
                         {last_day}, "候選清單只能來自最後一個完整快照")
        self.assertEqual(screen["stock_id"].tolist(),
                         expected["stock_id"].tolist())
        self.assertEqual(screen["rank"].tolist(), expected["rank"].tolist())
        for col in ("raw_score", "alpha_score", "rank_pct"):
            self.assertEqual(screen[col].round(12).tolist(),
                             expected[col].round(12).tolist(),
                             f"{col} 不得被重算")

    def test_top_n_comes_from_the_policy_entry_rank_not_a_second_setting(self):
        """top N 只能有一個設定來源:policy 的 `entry_rank`。

        用 14 檔(> entry_rank)跑,上限才真的會綁到;用 8 檔跑的話 8 < 10,
        測試會在「沒有套用任何上限」的情況下也通過,等於什麼都沒測。
        """
        from strategy_kit.position_policy import StrategyPositionPolicySpec

        entry_rank = int(StrategyPositionPolicySpec().entry_rank)
        with tempfile.TemporaryDirectory() as td:
            result = self._run(td, n_symbols=entry_rank + 4)
        screen = result.tables["candidate_screen"]
        self.assertEqual(len(screen), entry_rank)
        self.assertLessEqual(int(screen["rank"].max()), entry_rank)

    def test_screener_never_touches_the_legacy_factor_path(self):
        """候選 renderer 不得呼叫 legacy 九因子 —— 那會讓人看到的是另一支策略。"""
        import factor_engine.legacy_factors as factors

        with mock.patch.object(factors, "compute_factors") as cf, \
                mock.patch.object(factors, "composite_score") as cs:
            with tempfile.TemporaryDirectory() as td:
                self._run(td)
        cf.assert_not_called()
        cs.assert_not_called()

    def test_duplicate_panel_rows_fail_closed(self):
        panel = pd.DataFrame([
            {"date": "2026-01-06", "stock_id": "A", "name": "甲", "close": 10.0},
            {"date": "2026-01-06", "stock_id": "A", "name": "甲", "close": 11.0},
        ])
        with self.assertRaises(ValueError):
            build_candidate_screen(_signals(), panel=panel, top_n=2)

    def test_rows_beyond_as_of_fail_closed(self):
        with self.assertRaises(ValueError):
            build_candidate_screen(_signals(), as_of="2026-01-04", top_n=2)


class InvalidatedLeaderboardTest(unittest.TestCase):
    """作廢的 leaderboard 不得留下可被誤讀成有效的超額數字(§7.12)。

    `outputs/` 不進版控,乾淨 clone 沒有這個檔案 —— 缺檔就 skip,不是失敗。
    """

    PATH = (__import__("pathlib").Path(__file__).resolve().parents[1]
            / "outputs" / "2026-08-16" / "hypothesis_leaderboard.csv")

    @unittest.skipUnless(PATH.exists(), "本機沒有這份歷史 leaderboard")
    def test_invalidated_rows_carry_no_usable_excess(self):
        frame = pd.read_csv(self.PATH)
        self.assertTrue(
            (frame["status"] == "invalidated_benchmark_bug").all(),
            "整份 leaderboard 都是舊 benchmark 產生的,狀態必須一致標明作廢")
        for col in ("benchmark_cum_return", "excess_vs_benchmark"):
            self.assertTrue(
                frame[col].isna().all(),
                f"{col} 必須清空 —— 留著就會有人拿去引用")


class PrecomputedSignalGoldenPathTest(unittest.TestCase):
    def test_precomputed_signal_uses_the_same_full_artifact_path(self):
        fixture = synthetic_fixture(n_symbols=6, n_days=120)
        strategy = resolve("s19_reference_make_signals")
        context = SignalContext(
            as_of=pd.Timestamp(fixture.end_date),
            start_date=pd.Timestamp(fixture.start_date),
            end_date=pd.Timestamp(fixture.end_date),
            universe_provider_id=fixture.name,
            eligibility_rule_id="fixture_declared",
            mode="discovery",
        )
        signals = strategy.make_signals(
            fixture.panel, strategy.default_parameters(), context)
        with tempfile.TemporaryDirectory() as td:
            result = run_golden_path(
                strategy_id=strategy.name,
                fixture_name="synthetic",
                fixture=fixture,
                signal_frame=signals,
                output_dir=td,
                stamp="external",
            )
            names = {p.name for p in __import__("pathlib").Path(
                result.run_dir).iterdir()}
        self.assertEqual(len(result.tables["phase_results"]), 5)
        self.assertGreater(len(result.tables["candidate_screen"]), 0)
        self.assertIn("candidate_screen.csv", names)
        self.assertIn("candidate_screen.txt", names)

    def test_precomputed_signal_provenance_must_match_registry(self):
        fixture = synthetic_fixture(n_symbols=6, n_days=120)
        strategy = resolve("s19_reference_make_signals")
        context = SignalContext(
            as_of=pd.Timestamp(fixture.end_date),
            start_date=pd.Timestamp(fixture.start_date),
            end_date=pd.Timestamp(fixture.end_date),
            universe_provider_id=fixture.name,
            eligibility_rule_id="fixture_declared",
            mode="discovery",
        )
        signals = strategy.make_signals(
            fixture.panel, strategy.default_parameters(), context)
        signals["strategy_id"] = "wrong"
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                run_golden_path(
                    strategy_id=strategy.name,
                    fixture_name="synthetic",
                    fixture=fixture,
                    signal_frame=signals,
                    output_dir=td,
                )


if __name__ == "__main__":
    unittest.main()
