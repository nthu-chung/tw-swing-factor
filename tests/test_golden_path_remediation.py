# -*- coding: utf-8 -*-
"""Golden Path 修復的十項針對性回歸測試(全離線)。

每一支都對應一個**已經存在過、而且不會報錯**的缺陷。共同特徵是「錯了不會壞,
只會安靜地給出一個看起來合理的數字」——所以斷言要釘住行為,不是釘住實作。
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from backtest import event_backtest
import provenance
import research.artifacts as artifacts
import research.golden_path as gp
from research.contracts import (
    REQUEST_OWNED_KEYS, BacktestRequest, CandidateSpec, EvaluationProtocol)
from research.fixtures import Fixture, build_fixture
from research.signal_validation import (
    SignalValidationError, validate_signal_frame)
from strategy_kit.position_policy import (
    StrategyPositionPolicy, StrategyPositionPolicySpec)

STRATEGY = "s19_reference_make_signals"
FIXTURE_KW = {"n_symbols": 6, "n_days": 120}


def _good_row(**over):
    row = {"date": pd.Timestamp("2026-01-05"), "stock_id": "A", "rank": 1,
           "raw_score": 1.0, "eligible": True, "snapshot_complete": True,
           "ranking_universe_count": 1, "strategy_id": "s", "strategy_version": "1"}
    row.update(over)
    return row


def _good_frame(**over):
    return pd.DataFrame([_good_row(**over)])


# ── 1. 不合格的外部 SignalFrame 必須在事件引擎**之前**被擋下 ────────────────
class MalformedExternalFrameTest(unittest.TestCase):
    """外部訊號不得走比 repo 內策略更寬鬆的路(§3.1)。"""

    def _assert_rejected_before_engine(self, frame, **kw):
        with mock.patch.object(event_backtest, "backtest_portfolio") as engine:
            with self.assertRaises(SignalValidationError):
                gp.run_signal_frame_backtest(signal_frame=frame, **kw)
        engine.assert_not_called()

    def test_missing_ranking_universe_count_is_rejected(self):
        frame = _good_frame().drop(columns=["ranking_universe_count"])
        self._assert_rejected_before_engine(frame)

    def test_duplicate_key_is_rejected(self):
        frame = pd.concat([_good_frame(), _good_frame()], ignore_index=True)
        self._assert_rejected_before_engine(frame)

    def test_ineligible_row_is_rejected(self):
        self._assert_rejected_before_engine(_good_frame(eligible=False))

    def test_rows_beyond_as_of_are_rejected(self):
        frame = pd.DataFrame([
            _good_row(date=pd.Timestamp("2026-01-05")),
            _good_row(date=pd.Timestamp("2026-06-30"), stock_id="B"),
        ])
        # 兩天各自 1 檔,rank 都是 1 —— 結構合法,唯一的問題是超過 as-of。
        self._assert_rejected_before_engine(frame, end_date="2026-01-31")


# ── 2. engine_kwargs 不得覆寫 runner 自己管理的欄位 ─────────────────────────
class EngineKwargsCannotOverrideRunnerFieldsTest(unittest.TestCase):
    def test_every_owned_key_is_owned_by_signature_or_by_guard(self):
        """runner 擁有一個欄位只有兩種合法方式,不得有第三種(=沒人管)。

          A. 它是 `run_signal_frame_backtest` 的具名參數 —— Python 會把它綁到
             簽章上,結構上就進不了 `**engine_kwargs`,runner 一定看得到它。
          B. 它不是具名參數 —— 那就必須被 `REQUEST_OWNED_KEYS` 的閘門擋下。

        把兩種情況分開檢查,是因為它們的失效模式不同:A 少一個參數會變成
        TypeError(吵),B 少一道閘門會**安靜地**讓呼叫端蓋掉資金情境或 PIT
        provider,而結果看起來完全正常。
        """
        import inspect
        named = set(inspect.signature(gp.run_signal_frame_backtest).parameters)
        for key in REQUEST_OWNED_KEYS:
            with self.subTest(key=key):
                if key in named:
                    continue                    # A:簽章擁有,結構上安全
                with mock.patch.object(event_backtest, "backtest_portfolio") as engine:
                    with self.assertRaises(ValueError) as ctx:
                        gp.run_signal_frame_backtest(
                            signal_frame=_good_frame(), **{key: "hijacked"})
                    self.assertIn(key, str(ctx.exception))
                engine.assert_not_called()

    def test_the_dangerous_engine_only_keys_are_actually_guarded(self):
        """釘住上面那條規則的 B 組不是空集合 —— 否則測試會空轉成永遠通過。"""
        import inspect
        named = set(inspect.signature(gp.run_signal_frame_backtest).parameters)
        guarded = [k for k in REQUEST_OWNED_KEYS if k not in named]
        self.assertTrue(
            {"symbols", "universe_provider", "initial_capital",
             "picks_by_date"}.issubset(set(guarded)),
            f"這幾個只能靠閘門擋,目前 guarded={guarded}")

    def test_capital_cannot_be_silently_replaced_by_engine_kwargs(self):
        """最要命的一個:資金情境被蓋掉,結果看起來完全正常。"""
        with mock.patch.object(event_backtest, "backtest_portfolio") as engine:
            with self.assertRaises(ValueError):
                gp.run_signal_frame_backtest(signal_frame=_good_frame(),
                                             capital="research",
                                             initial_capital=999.0)
        engine.assert_not_called()

    def test_request_object_rejects_owned_keys_too(self):
        for key in REQUEST_OWNED_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    BacktestRequest(candidate=CandidateSpec("s", "1"),
                                    protocol=EvaluationProtocol(),
                                    engine_kwargs={key: 1})


# ── 3. 五個 phase 必須用五個獨立 policy,且呼叫端狀態不得滲入 ────────────────
class PhaseIsolationTest(unittest.TestCase):
    def _spy_run(self, *, policy=None):
        seen = []
        real = event_backtest.backtest_portfolio

        def _spy(**kwargs):
            pol = kwargs["strategy_position_policy"]
            # 在引擎跑之前記下當時的狀態:事後再看會混進這個 phase 自己的變化。
            seen.append({"id": id(pol),
                         "locked_at_entry": set(pol._stop_locked),
                         "rules_hash": pol.rules_hash()})
            return real(**kwargs)

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(event_backtest, "backtest_portfolio",
                                   side_effect=_spy):
                gp.run_golden_path(strategy_id=STRATEGY, fixture_name="synthetic",
                                   output_dir=td, stamp="iso", policy=policy,
                                   fixture_kwargs=FIXTURE_KW)
        return seen

    def test_each_phase_gets_its_own_policy_instance(self):
        seen = self._spy_run()
        self.assertEqual(len(seen), event_backtest.WEEKLY_PHASES)
        self.assertEqual(len({s["id"] for s in seen}), event_backtest.WEEKLY_PHASES,
                         "五個 phase 共用一個 policy 會讓停損鎖跨相位污染")
        self.assertEqual(len({s["rules_hash"] for s in seen}), 1,
                         "五個 phase 必須是同一套規則、不同狀態")

    def test_caller_policy_state_does_not_leak_into_phases(self):
        """呼叫端傳進來的是**規則**,不是它累積到現在的狀態。"""
        poisoned = StrategyPositionPolicy(StrategyPositionPolicySpec())
        poisoned._stop_locked.add("9001")        # 假裝它先前停損過 9001
        seen = self._spy_run(policy=poisoned)
        for s in seen:
            self.assertEqual(s["locked_at_entry"], set(),
                             "phase 開始時不得帶著呼叫端的停損鎖")
        self.assertIn("9001", poisoned._stop_locked, "呼叫端自己的物件不該被改動")


# ── 4./5. 基準母體與 fail-closed ────────────────────────────────────────────
def _bench_fixture(rows) -> Fixture:
    panel = pd.DataFrame(rows)
    return Fixture(name="bench", panel=panel,
                   symbols=sorted(panel["stock_id"].unique()),
                   start_date=str(panel["date"].min().date()),
                   end_date=str(panel["date"].max().date()))


class BenchmarkPopulationTest(unittest.TestCase):
    def setUp(self):
        self.days = pd.bdate_range("2026-01-05", periods=6)
        self.equity = pd.DataFrame({"date": self.days,
                                    "equity": np.linspace(1e6, 1.1e6, 6)})

    def _rows(self, sid, closes, eligible):
        return [{"date": d, "stock_id": sid, "close": c,
                 "in_dynamic_universe": bool(e)}
                for d, c, e in zip(self.days, closes, eligible)]

    def test_non_eligible_extreme_stock_does_not_move_the_benchmark(self):
        base = self._rows("A", [100, 101, 102, 103, 104, 105], [True] * 6)
        base += self._rows("B", [50, 50.5, 51, 51.5, 52, 52.5], [True] * 6)
        before = gp._equal_weight_benchmark(_bench_fixture(base), self.equity, 1e6)

        # 一檔漲十倍、但**當日都不是成員**的股票混進稠密 panel。
        bomb = self._rows("Z", [10, 30, 90, 270, 810, 2430], [False] * 6)
        after = gp._equal_weight_benchmark(_bench_fixture(base + bomb),
                                           self.equity, 1e6)
        self.assertAlmostEqual(before["cum_return"], after["cum_return"], places=12)
        self.assertAlmostEqual(before["ann_return"], after["ann_return"], places=12)

    def test_returns_are_computed_before_membership_filter(self):
        """剛進 universe 那天的報酬要算得出來(先篩再算會變成 NaN)。"""
        rows = self._rows("A", [100, 110, 121, 133.1, 146.41, 161.051],
                          [False, False, True, True, True, True])
        bench = gp._equal_weight_benchmark(_bench_fixture(rows), self.equity, 1e6)
        # 成員日 = 第 3~6 天,共 4 天報酬,每天 +10%。
        self.assertEqual(bench["n_days"], 4)
        self.assertAlmostEqual(bench["cum_return"], 1.1 ** 4 - 1.0, places=10)

    def test_name_describes_daily_rebalance_not_buy_and_hold(self):
        rows = self._rows("A", [100, 101, 102, 103, 104, 105], [True] * 6)
        bench = gp._equal_weight_benchmark(_bench_fixture(rows), self.equity, 1e6)
        self.assertEqual(bench["method"], "daily_equal_weight_rebalanced_eligible")
        self.assertEqual(bench["rebalance"], "daily_equal_weight")

    def test_missing_membership_column_fails_closed(self):
        panel = pd.DataFrame([{"date": d, "stock_id": "A", "close": 100.0}
                              for d in self.days])
        fixture = Fixture(name="bench", panel=panel, symbols=["A"],
                          start_date="2026-01-05", end_date="2026-01-12")
        with self.assertRaises(gp.BenchmarkUnavailableError):
            gp._equal_weight_benchmark(fixture, self.equity, 1e6)

    def test_no_eligible_day_fails_closed_instead_of_returning_zero(self):
        """假的 0% 基準會讓任何正報酬策略看起來都有超額。"""
        rows = self._rows("A", [100, 101, 102, 103, 104, 105], [False] * 6)
        with self.assertRaises(gp.BenchmarkUnavailableError):
            gp._equal_weight_benchmark(_bench_fixture(rows), self.equity, 1e6)


# ── 6. manifest 的必要 provenance ──────────────────────────────────────────
class ManifestProvenanceTest(unittest.TestCase):
    def test_manifest_records_real_git_commit_and_dirty_state(self):
        man = BacktestRequest(
            candidate=CandidateSpec("s", "1", signal_params={"a": 1}),
            protocol=EvaluationProtocol(data_snapshot="2026-06-22",
                                        segment="in_sample")).manifest()
        git = provenance.git_state()
        self.assertEqual(man["git_commit"], git["git_commit"])
        self.assertNotIn(man["git_commit"], ("", None))
        self.assertIsInstance(man["git_dirty"], bool)
        self.assertEqual(man["git_dirty"], git["git_dirty"])
        self.assertEqual(man["candidate"]["signal_params"], {"a": 1})
        self.assertEqual(man["protocol"]["data_snapshot"], "2026-06-22")
        self.assertEqual(man["protocol"]["segment"], "in_sample")

    def test_git_state_key_is_git_commit_not_commit(self):
        """原缺陷:讀 `commit` 永遠拿到空字串,規則指紋從未綁到程式碼版本。"""
        self.assertIn("git_commit", provenance.git_state())
        self.assertNotIn("commit", provenance.git_state())

    def test_parameters_are_defensively_copied(self):
        params = {"a": 1}
        spec = CandidateSpec("s", "1", signal_params=params)
        before = spec.strategy_rule_hash()
        params["a"] = 999                      # 呼叫端事後改自己的 dict
        self.assertEqual(spec.rules()["signal_params"], {"a": 1})
        self.assertEqual(spec.strategy_rule_hash(), before)


# ── 7. validator 的四種靜默壞資料 ──────────────────────────────────────────
class ValidatorHardeningTest(unittest.TestCase):
    def _reject(self, frame):
        with self.assertRaises(SignalValidationError):
            validate_signal_frame(frame, who="t")

    def test_infinite_score_is_rejected(self):
        for bad in (np.inf, -np.inf):
            with self.subTest(value=bad):
                self._reject(_good_frame(raw_score=bad))

    def test_fractional_rank_is_rejected_not_truncated(self):
        frame = pd.DataFrame([_good_row(rank=1.9, ranking_universe_count=1)])
        self._reject(frame)

    def test_string_boolean_is_rejected(self):
        """`pd.Series(['False']).astype(bool)` 是 True —— 不能讓它靜默翻面。"""
        self._reject(_good_frame(snapshot_complete="False"))
        self._reject(_good_frame(eligible="False"))

    def test_blank_or_mixed_provenance_is_rejected(self):
        self._reject(_good_frame(strategy_id=""))
        mixed = pd.DataFrame([
            _good_row(stock_id="A", rank=1, ranking_universe_count=2),
            _good_row(stock_id="B", rank=2, ranking_universe_count=2,
                      strategy_id="other"),
        ])
        self._reject(mixed)

    def test_rank_above_universe_count_is_rejected(self):
        self._reject(_good_frame(rank=5, ranking_universe_count=1))

    def test_a_clean_frame_still_passes(self):
        """收緊之後,合格的 frame 仍然要過 —— 否則就只是把門焊死。"""
        res = validate_signal_frame(_good_frame(), who="t")
        self.assertEqual(res.n_rows, 1)
        self.assertTrue(res.formal_evidence_eligible)


# ── 8. 災難停損鎖的四種快照邊界 ────────────────────────────────────────────
def _sig(ranks, *, complete=True, date=None):
    rows = [{"stock_id": sid, "rank": r, "raw_score": 1.0 - r * 0.001,
             "eligible": True, "snapshot_complete": complete}
            for sid, r in ranks.items()]
    frame = pd.DataFrame(rows)
    if date is not None:
        frame["date"] = pd.Timestamp(date)
    return frame


class StopLockSnapshotBoundaryTest(unittest.TestCase):
    def _stopped_policy(self):
        pol = StrategyPositionPolicy()
        pol.decide(as_of=pd.Timestamp("2026-01-09"),
                   signals=_sig({"X": 1}),
                   holdings=pd.DataFrame([{
                       "stock_id": "X", "weight": 0.1, "entry_price": 100.0,
                       "close": 75.0, "holding_days": 20, "exit_pending": False}]),
                   equity=1e6, regime="risk_on", is_decision_day=True,
                   next_execution=pd.Timestamp("2026-01-12"))
        self.assertIn("X", pol._stop_locked)
        return pol

    def _decide(self, pol, signals, *, is_decision_day=True, date="2026-01-16"):
        return pol.decide(as_of=pd.Timestamp(date), signals=signals,
                          holdings=pd.DataFrame(), equity=1e6, regime="risk_on",
                          is_decision_day=is_decision_day,
                          next_execution=pd.Timestamp(date) + pd.offsets.BDay(1))

    def test_total_absence_from_complete_snapshot_counts_as_left_top20(self):
        """完整快照裡連出現都沒有,比「掉到第 21 名」更徹底。"""
        pol = self._stopped_policy()
        self._decide(pol, _sig({f"S{i:02d}": i for i in range(1, 11)}))
        self.assertNotIn("X", pol._stop_locked)

    def test_incomplete_snapshot_never_rearms(self):
        pol = self._stopped_policy()
        self._decide(pol, _sig({"S01": 1}, complete=False))
        self.assertIn("X", pol._stop_locked, "不完整快照的『沒看到』不是證據")

    def test_non_decision_day_never_rearms(self):
        pol = self._stopped_policy()
        self._decide(pol, _sig({"S01": 1}), is_decision_day=False)
        self.assertIn("X", pol._stop_locked)

    def test_stale_snapshot_never_rearms(self):
        """快照日期比 as_of 早 = 舊名次,不足以證明「它現在掉出去了」。"""
        pol = self._stopped_policy()
        self._decide(pol, _sig({"S01": 1}, date="2026-01-13"), date="2026-01-16")
        self.assertIn("X", pol._stop_locked)

    def test_fresh_complete_decision_day_snapshot_rearms(self):
        pol = self._stopped_policy()
        self._decide(pol, _sig({"S01": 1}, date="2026-01-16"), date="2026-01-16")
        self.assertNotIn("X", pol._stop_locked)

    def test_still_inside_top20_does_not_rearm(self):
        pol = self._stopped_policy()
        self._decide(pol, _sig({"X": 3, "S01": 1}))
        self.assertIn("X", pol._stop_locked)


# ── 9./10. 端到端:make_signals → validator → phases → 引擎 → artifacts ─────
class GoldenPathEndToEndTest(unittest.TestCase):
    def test_black_box_run_produces_complete_and_honest_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            res = gp.run_golden_path(strategy_id=STRATEGY,
                                     fixture_name="synthetic", output_dir=td,
                                     stamp="e2e", fixture_kwargs=FIXTURE_KW)
            files = {p.name for p in __import__("pathlib").Path(res.run_dir).iterdir()}
        self.assertTrue({"manifest.json", "summary.json", "audit.json",
                         "signals.csv", "phase_results.csv", "decisions.csv",
                         "orders.csv", "trades.csv",
                         "equity_curve.csv"}.issubset(files))

        checklist = res.audit["checklist"]
        # checklist 是人可以直接讀的,而且 formal_evidence_ready 只從它推導。
        for key in ("signal_validation", "pit_universe", "adjusted_price",
                    "evaluation_boundary", "all_phases", "phase_independence",
                    "benchmark", "code_identity", "os_status",
                    "performance_claim"):
            self.assertIn(key, checklist)
        self.assertEqual(checklist["all_phases"], "pass")
        self.assertEqual(checklist["phase_independence"], "pass")
        # 2026-08-16 收緊:沒有 holdout_protocol 的 run 不再叫 "not_revealed"
        # (見 OsStatusHonestyTest —— 那個字讓一次無邊界的 run 看起來合規)。
        self.assertEqual(checklist["os_status"], "unbounded_no_holdout_protocol")
        self.assertEqual(checklist["data_source"], "fail_synthetic_fixture_only")

        # 合成資料一律不得被標成正式證據,而且理由要列得出來。
        self.assertFalse(res.audit["formal_evidence_ready"])
        self.assertIn("data_source=fail_synthetic_fixture_only",
                      res.audit["formal_evidence_blockers"])
        self.assertEqual(res.audit["performance_claim"], "none")

        # 規則指紋必須真的綁到程式碼版本(原缺陷:永遠是空字串)。
        self.assertTrue(res.manifest["candidate"]["code_fingerprint"])
        self.assertEqual(res.manifest["candidate"]["code_fingerprint"],
                         provenance.git_state()["git_commit"])

    def test_formal_evidence_ready_is_derived_only_from_the_checklist(self):
        """不得有第二套 evidence 狀態機:blocker 必須逐項對得回 checklist。"""
        with tempfile.TemporaryDirectory() as td:
            res = gp.run_golden_path(strategy_id=STRATEGY,
                                     fixture_name="synthetic", output_dir=td,
                                     stamp="derive", fixture_kwargs=FIXTURE_KW)
        checklist = res.audit["checklist"]
        expected = [f"{k}={v}" for k, v in checklist.items()
                    if k not in ("os_status", "performance_claim") and v != "pass"]
        self.assertEqual(res.audit["formal_evidence_blockers"], expected)
        self.assertEqual(res.audit["formal_evidence_ready"], not expected)


class ArtifactPathSafetyTest(unittest.TestCase):
    def test_stamp_with_path_tricks_is_rejected(self):
        for bad in ("../escape", "a/b", "..", "a\\b"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    artifacts.build_run_id(strategy_id="s", run_hash="h",
                                           stamp=bad)

    def test_run_directory_rejects_escaping_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                artifacts.create_run_directory(td, "../outside")


if __name__ == "__main__":
    unittest.main()


class OsStatusHonestyTest(unittest.TestCase):
    """沒有 holdout protocol 的 run 不得宣稱 `os_status=not_revealed`。

    2026-08-16 實例:為了取 forward 區間,直接呼叫 `run_golden_path()` 而沒帶
    `holdout_protocol`。那一次在 2024-11 ~ 2026-08 的完整區間上計分,把
    `h2_inst_persistence` 的 locked OS 整段掃過去 —— 而 audit 寫著
    `os_status="not_revealed"`,看起來完全合規。

    「**沒有邊界**」和「**有邊界且沒揭露**」是兩件不同的事;共用同一個字,
    就等於讓一次無邊界的 run 冒充成受管制的 IS run。
    """

    def test_run_without_protocol_is_labelled_unbounded(self):
        with tempfile.TemporaryDirectory() as td:
            res = gp.run_golden_path(strategy_id=STRATEGY, fixture_name="synthetic",
                                     output_dir=td, stamp="nobound",
                                     fixture_kwargs=FIXTURE_KW)
        self.assertEqual(res.audit["checklist"]["os_status"],
                         "unbounded_no_holdout_protocol")
        self.assertNotIn("segment_boundary", res.audit,
                         "沒有 protocol 就不該有 segment 邊界稽核")


class MarketBenchmarkTest(unittest.TestCase):
    """加權報酬指數基準:回答「我該做這個還是買 0050」,與等權基準並存。"""

    def _equity(self):
        return pd.DataFrame({"date": pd.bdate_range("2026-01-05", periods=30),
                             "equity": np.linspace(1e6, 1.2e6, 30)})

    def test_uses_total_return_not_price_index(self):
        """必須用**報酬**指數 —— 個股走還原價(含息),比價格指數會白賺殖利率。"""
        import data as _data
        ix = pd.DataFrame({"date": pd.bdate_range("2026-01-05", periods=30),
                           "close": np.linspace(100.0, 110.0, 30)})
        with mock.patch.object(_data, "fetch_market_total_return_index",
                               return_value=ix) as tr, \
             mock.patch.object(_data, "fetch_market_index") as px:
            out = gp._market_benchmark(self._equity())
        tr.assert_called_once()
        px.assert_not_called()
        self.assertTrue(out["available"])
        self.assertAlmostEqual(out["cum_return"], 0.10, places=6)

    def test_unavailable_index_is_flagged_not_zero(self):
        """抓不到就標 unavailable —— 假的 0% 會讓任何正報酬看起來有超額。"""
        import data as _data
        with mock.patch.object(_data, "fetch_market_total_return_index",
                               side_effect=RuntimeError("端點掛了")):
            out = gp._market_benchmark(self._equity())
        self.assertFalse(out["available"])
        self.assertNotIn("cum_return", out)
        self.assertIn("端點掛了", out["reason"])


class BrokerCostConfigTest(unittest.TestCase):
    """券商實際費率必須進得了模型,而且看得出「這個數字怎麼來的」。"""

    def test_fee_is_statutory_times_discount(self):
        import config
        self.assertAlmostEqual(config.BT_FEE,
                               config.BT_FEE_STATUTORY * config.BT_FEE_DISCOUNT)
        # 拆成兩欄而非直接寫 0.000399:折扣改了要在 provenance 看得出來
        self.assertAlmostEqual(config.BT_FEE_STATUTORY, 0.001425)

    def test_round_trip_friction_matches_broker_quote(self):
        """owner 券商:0.1425% × 2.8 折 × 2 邊 + 0.3% 證交稅 = 0.3798%。"""
        import config
        self.assertAlmostEqual(config.BT_FEE * 2 + config.BT_TAX, 0.003798,
                               places=6)

    def test_minimum_commission_is_not_zero(self):
        """預設 0 等於假設「多小的單都不用錢」,會系統性低估零股成本。"""
        import config
        self.assertGreater(config.BT_MIN_COMMISSION, 0)
