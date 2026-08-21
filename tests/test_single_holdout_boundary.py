# -*- coding: utf-8 -*-
"""`EVALUATION_DATA_BOUNDARY_SPEC.md` §8 的九項最小驗收(全離線)。

要證明的不是「CLI 跑得完」,而是**研究程序根本沒有取得 locked OS**,而且事件
引擎與 artifacts 都沒有越過當前 segment 邊界。
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import pandas as pd

from evaluation.holdout import HoldoutLedgerError, read_ledger
from research.contracts import BacktestRequest, CandidateSpec, EvaluationProtocol
from research.fixtures import build_fixture
from research.golden_path import run_golden_path
from research.holdout import (
    REVEAL_AUTHORIZATION,
    SEGMENT_IS,
    SEGMENT_OS,
    HoldoutBoundaryError,
    SingleHoldoutProtocol,
    freeze_candidate,
    freeze_from_is_manifest,
    precompute_strategy_rule_hash,
    research_run,
    reveal_locked_os,
)
from strategies.s19_reference import S19ReferenceStrategy

STRATEGY = "s19_reference_make_signals"
FIXTURE_KW = {"n_symbols": 6, "n_days": 120}


def _is_manifest(proto: "SingleHoldoutProtocol", td: str) -> dict:
    """跑一次 IS,拿到凍結要用的 manifest(含 strategy_rule_hash 與 candidate)。"""
    res = run_golden_path(
        strategy_id=STRATEGY, fixture_name="synthetic",
        capital=proto.capital_scenario, output_dir=td, stamp="probe",
        holdout_protocol=proto, segment=SEGMENT_IS, fixture_kwargs=FIXTURE_KW)
    return res.manifest


def _protocol(**over) -> SingleHoldoutProtocol:
    days = build_fixture("synthetic", **FIXTURE_KW).panel["date"].drop_duplicates()
    kw = dict(snapshot="2026-06-22", warmup_bars=20, phases=5,
              capital_scenario="research", initial_capital=1_000_000.0,
              order_size_mode="research_fractional", minimum_commission=0.0,
              mode="ratio", is_ratio=0.7, embargo_days=5)
    kw.update(over)
    return SingleHoldoutProtocol.from_dates(sorted(days), **kw)


class _Spy:
    """記錄策略實際收到的 panel(§8.1 要求證明資料沒進來,不是輸出被裁掉)。"""

    def __init__(self):
        self.seen = []
        self._real = S19ReferenceStrategy.make_signals

    def __enter__(self):
        spy = self

        def _wrapped(inner_self, panel, params=None, context=None):
            spy.seen.append(panel[["date"]].copy())
            return spy._real(inner_self, panel, params, context)

        self._patch = mock.patch.object(S19ReferenceStrategy, "make_signals",
                                        _wrapped)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    @property
    def max_date(self):
        return max(df["date"].max() for df in self.seen)

    @property
    def min_date(self):
        return min(df["date"].min() for df in self.seen)


class _DataLayerSpy:
    """釘住「OS 資料層一次都沒被呼叫」——比「有拋例外」強一級的證據。

    同時攔兩層:
      `research.golden_path.build_fixture` → OS panel 有沒有被**建立**
      `S19ReferenceStrategy.make_signals`  → OS 列有沒有進到策略

    只斷言 `assertRaises` 的測試證明不了不變式:2026-08-17 的事故裡例外確實有
    拋出來,只是拋在整段 OS 已經算完之後。例外的存在與 OS 有沒有被消耗是兩件
    獨立的事,所以要分開釘。
    """

    def __init__(self):
        self.fixture_calls = []      # build_fixture 收到的資料窗
        self.signal_panels = []      # 策略實際看到的 panel

    def __enter__(self):
        import research.golden_path as gp

        spy = self
        real_fixture = gp.build_fixture
        real_signals = S19ReferenceStrategy.make_signals

        def _fixture(name, *args, **kwargs):
            spy.fixture_calls.append(kwargs.get("window"))
            return real_fixture(name, *args, **kwargs)

        def _signals(inner_self, panel, params=None, context=None):
            spy.signal_panels.append(panel[["date"]].copy())
            return real_signals(inner_self, panel, params, context)

        self._patches = [
            mock.patch.object(gp, "build_fixture", _fixture),
            mock.patch.object(S19ReferenceStrategy, "make_signals", _signals),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False

    def assert_untouched(self, case):
        case.assertEqual(self.fixture_calls, [],
                         "OS panel 連建都不該被建 —— 閘門必須擋在載入之前")
        case.assertEqual(self.signal_panels, [], "策略不該看到任何 OS 資料")


class C1ResearchModeNeverSeesOSTest(unittest.TestCase):
    def test_strategy_input_stops_at_is_end_and_never_sees_os_rows(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td, _Spy() as spy:
            research_run(strategy_id=STRATEGY, protocol=proto, output_dir=td,
                         fixture_kwargs=FIXTURE_KW)
        self.assertLessEqual(spy.max_date, pd.Timestamp(proto.is_end))
        os_rows = [df for df in spy.seen
                   if (df["date"] >= pd.Timestamp(proto.os_start)).any()]
        self.assertEqual(os_rows, [], "OS 的列不得進入策略,而不只是輸出被裁掉")


class C2WarmupTest(unittest.TestCase):
    def test_legal_past_warmup_is_available(self):
        proto = _protocol()
        f = build_fixture("synthetic", window=proto.window(SEGMENT_IS),
                          **FIXTURE_KW)
        self.assertLessEqual(pd.Timestamp(f.panel["date"].min()),
                             pd.Timestamp(proto.is_start))

    def test_window_without_enough_history_fails_closed(self):
        proto = _protocol()
        with self.assertRaises(ValueError):
            build_fixture("synthetic", window=("2030-01-01", "2030-01-05"),
                          **FIXTURE_KW)


class C3ProtocolCannotBeOverriddenTest(unittest.TestCase):
    def test_engine_kwargs_cannot_override_protocol_owned_fields(self):
        for key in ("start_date", "end_date", "initial_capital",
                    "order_size_mode", "minimum_commission"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    BacktestRequest(
                        CandidateSpec(strategy_id=STRATEGY,
                                      strategy_version="1"),
                        EvaluationProtocol(), engine_kwargs={key: "x"})

    def test_unknown_segment_is_rejected(self):
        proto = _protocol()
        with self.assertRaises(HoldoutBoundaryError):
            proto.window("TRAIN")


class C4And7SegmentOutputsTest(unittest.TestCase):
    def _run(self, segment, td, proto, **kw):
        return run_golden_path(
            strategy_id=STRATEGY, fixture_name="synthetic",
            capital=proto.capital_scenario, output_dir=td, stamp=segment.lower(),
            holdout_protocol=proto, segment=segment,
            fixture_kwargs=FIXTURE_KW, **kw)

    def test_is_outputs_never_cross_is_end(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            res = self._run(SEGMENT_IS, td, proto)
        bounds = res.audit["segment_boundary"]
        self.assertTrue(bounds["within_segment"])
        for name, top in bounds["output_max_dates"].items():
            if top is not None:
                self.assertLessEqual(pd.Timestamp(top),
                                     pd.Timestamp(proto.is_end), name)

    def test_os_outputs_never_cross_os_end_and_run_all_phases(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            res = self._run(SEGMENT_OS, td, proto)
        bounds = res.audit["segment_boundary"]
        self.assertTrue(bounds["within_segment"])
        for name, top in bounds["output_max_dates"].items():
            if top is not None:
                self.assertLessEqual(pd.Timestamp(top),
                                     pd.Timestamp(proto.os_end), name)
        self.assertEqual(len(res.tables["phase_results"]), 5)
        self.assertIn("cum_return", res.summary["benchmark"])


class C5UnauthorizedRevealTest(unittest.TestCase):
    def test_missing_authorization_is_rejected_before_any_os_panel(self):
        proto = _protocol()
        frozen = freeze_candidate(strategy_id=STRATEGY, strategy_rule_hash="h",
                                  protocol=proto, frozen_at="2026-08-16")
        with tempfile.TemporaryDirectory() as td, _Spy() as spy:
            with self.assertRaises(HoldoutBoundaryError):
                reveal_locked_os(strategy_id=STRATEGY, protocol=proto,
                                 frozen=frozen, authorization="mode=os",
                                 output_dir=td, fixture_kwargs=FIXTURE_KW)
        self.assertEqual(spy.seen, [], "未授權時連 OS panel 都不該被建立")

    def test_unfrozen_rule_is_rejected(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(HoldoutBoundaryError):
                reveal_locked_os(strategy_id=STRATEGY, protocol=proto,
                                 frozen=None,
                                 authorization=REVEAL_AUTHORIZATION,
                                 output_dir=td, fixture_kwargs=FIXTURE_KW)

    def test_rule_hash_change_after_freeze_is_rejected(self):
        proto = _protocol()
        frozen = freeze_candidate(
            strategy_id=STRATEGY, strategy_rule_hash="not-the-real-hash",
            rules={"eligibility_rule_id": "fixture_declared"},
            protocol=proto, frozen_at="2026-08-16")
        with tempfile.TemporaryDirectory() as td, _DataLayerSpy() as spy:
            with self.assertRaises(HoldoutBoundaryError):
                reveal_locked_os(strategy_id=STRATEGY, protocol=proto,
                                 frozen=frozen,
                                 authorization=REVEAL_AUTHORIZATION,
                                 output_dir=td, fixture_kwargs=FIXTURE_KW,
                                 ledger_path=f"{td}/holdout_ledger.jsonl")
        # 舊版這裡只斷言「有拋例外」,而事故當天例外也確實有拋 —— 拋在 OS 算完
        # 之後。有沒有拋例外和 OS 有沒有被消耗是兩件事,這一行釘的是後者。
        spy.assert_untouched(self)

    def test_frozen_without_rules_cannot_reveal(self):
        """只有 hash 的舊 frozen 會逼閘門退回「先跑再擋」,所以直接 fail-closed。"""
        proto = _protocol()
        frozen = freeze_candidate(strategy_id=STRATEGY, strategy_rule_hash="h",
                                  protocol=proto, frozen_at="2026-08-16")
        with tempfile.TemporaryDirectory() as td, _DataLayerSpy() as spy:
            with self.assertRaises(HoldoutBoundaryError):
                reveal_locked_os(strategy_id=STRATEGY, protocol=proto,
                                 frozen=frozen,
                                 authorization=REVEAL_AUTHORIZATION,
                                 output_dir=td, fixture_kwargs=FIXTURE_KW,
                                 ledger_path=f"{td}/holdout_ledger.jsonl")
        spy.assert_untouched(self)


class C8HashSeparationTest(unittest.TestCase):
    def _req(self, **proto_over):
        cand = CandidateSpec(strategy_id=STRATEGY, strategy_version="1",
                             signal_params={"mom_window": 20})
        return BacktestRequest(cand, EvaluationProtocol(**proto_over))

    def test_protocol_changes_move_only_the_run_hash(self):
        base = self._req(data_snapshot="2026-06-22")
        for over in ({"data_snapshot": "2026-08-16"}, {"phases": 3},
                     {"benchmark": "taiex"}, {"minimum_commission": 20.0},
                     {"initial_capital": 500_000.0}):
            with self.subTest(over=over):
                kw = {"data_snapshot": "2026-06-22", **over}
                other = self._req(**kw)
                self.assertEqual(base.strategy_rule_hash(),
                                 other.strategy_rule_hash())
                self.assertNotEqual(base.evaluation_run_hash(),
                                    other.evaluation_run_hash())

    def test_split_change_changes_the_protocol_hash(self):
        self.assertNotEqual(_protocol().protocol_hash(),
                            _protocol(embargo_days=10).protocol_hash())


class C9PrefixInvarianceTest(unittest.TestCase):
    """§8.9:數個固定截點的因果契約測試 —— 不是逐日 runtime 沙盒。"""

    def test_registered_strategy_is_prefix_invariant(self):
        panel = build_fixture("synthetic", **FIXTURE_KW).panel
        strategy = S19ReferenceStrategy()
        full = strategy.make_signals(panel)
        days = sorted(panel["date"].unique())
        for cut in (days[59], days[79], days[99]):
            with self.subTest(cut=str(cut)[:10]):
                prefix = panel[panel["date"] <= cut]
                got = strategy.make_signals(prefix)
                want = full[full["date"] <= cut].reset_index(drop=True)
                pd.testing.assert_frame_equal(got.reset_index(drop=True), want)



class C6RevealLedgerTest(unittest.TestCase):
    """§8.6:第一次 reveal 寫揭露紀錄;第二次同一段 OS 只能是 reproduction。"""

    def test_first_reveal_writes_ledger_second_is_previously_seen(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            ledger = f"{td}/holdout_ledger.jsonl"
            manifest = _is_manifest(proto, td)
            rule_hash = manifest["strategy_rule_hash"]
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-16")

            first = reveal_locked_os(
                strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                authorization=REVEAL_AUTHORIZATION, output_dir=td,
                stamp="os1", fixture_kwargs=FIXTURE_KW, ledger_path=ledger,
                now=pd.Timestamp("2026-08-16 09:00:00").to_pydatetime())
            self.assertEqual(first.audit["os_reveal"]["strategy_hash"], rule_hash)
            self.assertFalse(first.audit["os_reveal"]["holdout_previously_seen"])

            second = reveal_locked_os(
                strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                authorization=REVEAL_AUTHORIZATION, output_dir=td,
                stamp="os2", fixture_kwargs=FIXTURE_KW, ledger_path=ledger,
                now=pd.Timestamp("2026-08-16 10:00:00").to_pydatetime())
            rec = second.audit["os_reveal"]
            self.assertTrue(rec["holdout_previously_seen"])
            self.assertFalse(rec["fresh_oos_claim_allowed"])

class C10RevealGateOrderRegressionTest(unittest.TestCase):
    """2026-08-17 `control_h4` 事故的回歸測試 —— 釘的是**順序**,不是例外。

    當天發生的事:`h4_chip_momentum` 在 IS 凍結時的 manifest 是
    `strategy_rule_hash=5245d29d14496733`(code_fingerprint `df8171fb…`、
    `signal_params` 裡沒有 `trend_guard`);揭露時實際跑出來的是
    `472a0133dbae46f1`(code_fingerprint `ea47819b…`、`signal_params` 多了
    `trend_guard: true`)。兩個差異都是宣告性的、run 之前就已知的,但
    `reveal_locked_os()` 把 hash 比對排在 `run_golden_path()` **之後** ——
    於是整段 locked OS 被載入、被算完,才拋出「規則變了」。OS 消耗不可逆,
    例外只是事後通知。揭露紀錄也因為排在更後面而沒寫成,只能人工補登
    (`outputs/holdout_ledger.jsonl` seq 3,
    `source=manual_backfill_after_rule_hash_gate_failed_post_run`)。

    所以這裡每一支都斷言兩件事:(1) 有 fail-closed,(2) 資料層**一次都沒被
    呼叫**。少了 (2) 的測試無法區分「擋在載入之前」與「先跑再擋」——
    事故前的測試就只有 (1),bug 因此活著。
    """

    def _frozen_with_drifted_fingerprint(self, proto, td):
        """模擬事故:凍結的規則本體與現在的 git commit 不同。"""
        manifest = _is_manifest(proto, td)
        drifted = dict(manifest["candidate"])
        drifted["code_fingerprint"] = "df8171fbdeadbeef"      # 凍結當時的 commit
        frozen_hash = CandidateSpec(**drifted).strategy_rule_hash()
        self.assertNotEqual(frozen_hash, manifest["strategy_rule_hash"])
        return freeze_from_is_manifest(
            manifest={**manifest, "strategy_rule_hash": frozen_hash,
                      "candidate": drifted},
            protocol=proto, frozen_at="2026-08-16")

    def test_code_fingerprint_drift_is_caught_before_any_os_panel(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            frozen = self._frozen_with_drifted_fingerprint(proto, td)
            ledger = f"{td}/holdout_ledger.jsonl"
            with _DataLayerSpy() as spy:
                with self.assertRaises(HoldoutBoundaryError) as ctx:
                    reveal_locked_os(
                        strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                        authorization=REVEAL_AUTHORIZATION, output_dir=td,
                        stamp="os", fixture_kwargs=FIXTURE_KW,
                        ledger_path=ledger)
            spy.assert_untouched(self)
            self.assertIn("strategy_rule_hash", str(ctx.exception))
            # 沒看過就不該有紀錄:擋在載入之前 = 這段 OS 仍然是 fresh。
            self.assertEqual(read_ledger(ledger), [])

    def test_signal_params_drift_is_caught_before_any_os_panel(self):
        """事故的另一半:`signal_params` 多了 `trend_guard: true`。"""
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-16")
            drifted_params = {**dict(manifest["candidate"]["signal_params"]),
                              "trend_guard": True}
            with _DataLayerSpy() as spy:
                with self.assertRaises(HoldoutBoundaryError):
                    reveal_locked_os(
                        strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                        authorization=REVEAL_AUTHORIZATION, output_dir=td,
                        stamp="os", fixture_kwargs=FIXTURE_KW,
                        params=drifted_params,
                        ledger_path=f"{td}/holdout_ledger.jsonl")
            spy.assert_untouched(self)

    def test_broken_ledger_chain_blocks_before_any_os_panel(self):
        """揭露紀錄壞掉 = 寫不進去,而「看過但沒紀錄」不可以發生 → 先擋。"""
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-16")
            ledger = f"{td}/holdout_ledger.jsonl"
            with open(ledger, "w", encoding="utf-8") as fh:   # 竄改過的鏈
                fh.write('{"seq": 1, "strategy_hash": "x", '
                         '"record_sha256": "tampered"}\n')
            with _DataLayerSpy() as spy:
                with self.assertRaises((HoldoutBoundaryError,
                                        HoldoutLedgerError)):
                    reveal_locked_os(
                        strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                        authorization=REVEAL_AUTHORIZATION, output_dir=td,
                        stamp="os", fixture_kwargs=FIXTURE_KW,
                        ledger_path=ledger)
            spy.assert_untouched(self)

    def test_unwritable_ledger_dir_blocks_before_any_os_panel(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-16")
            locked = pathlib.Path(td) / "locked"
            locked.mkdir()
            locked.chmod(0o500)
            try:
                with _DataLayerSpy() as spy:
                    with self.assertRaises(HoldoutBoundaryError):
                        reveal_locked_os(
                            strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                            authorization=REVEAL_AUTHORIZATION, output_dir=td,
                            stamp="os", fixture_kwargs=FIXTURE_KW,
                            ledger_path=str(locked / "holdout_ledger.jsonl"))
                spy.assert_untouched(self)
            finally:
                locked.chmod(0o700)

    def test_run_failure_still_leaves_a_reveal_record(self):
        """OS 一旦要被載入就等於「看過」,紀錄不能取決於 run 之後會不會出錯。

        這一支蓋掉事故的第二半:當天 hash 比對失敗時,揭露紀錄那一行根本沒跑到,
        變成「看過但沒紀錄」,只能靠人事後補登。
        """
        import research.golden_path as gp

        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-16")
            ledger = f"{td}/holdout_ledger.jsonl"
            with mock.patch.object(gp, "run_golden_path",
                                   side_effect=RuntimeError("引擎在 run 中炸掉")):
                with self.assertRaises(RuntimeError):
                    reveal_locked_os(
                        strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                        authorization=REVEAL_AUTHORIZATION, output_dir=td,
                        stamp="os", fixture_kwargs=FIXTURE_KW,
                        ledger_path=ledger,
                        now=pd.Timestamp("2026-08-17 00:45:00").to_pydatetime())
            rows = read_ledger(ledger)
            self.assertEqual(len(rows), 1, "run 失敗也必須留下揭露紀錄")
            self.assertEqual(rows[0]["strategy_hash"],
                             manifest["strategy_rule_hash"])
            self.assertEqual(rows[0]["context"]["phase"], "pre_run")

    def test_rule_hash_is_computable_without_touching_any_data(self):
        """前移之所以可行:規則身分與資料窗、fixture 都無關。"""
        with tempfile.TemporaryDirectory() as td_a, \
                tempfile.TemporaryDirectory() as td_b:
            wide = _is_manifest(_protocol(), td_a)
            narrow = _is_manifest(_protocol(is_ratio=0.5), td_b)
        self.assertEqual(wide["strategy_rule_hash"],
                         narrow["strategy_rule_hash"],
                         "換資料窗不得改變 strategy_rule_hash")
        self.assertEqual(
            wide["strategy_rule_hash"],
            precompute_strategy_rule_hash(
                strategy_id=STRATEGY,
                eligibility_rule_id=wide["candidate"]["eligibility_rule_id"]),
            "純宣告式重建必須等於 run 出來的 hash,否則前移的閘門只是噪音")


class C11UnverifiableRuleAndFalsePositiveBurnTest(unittest.TestCase):
    """2026-08-21 對抗式驗證找到的兩個洞 —— 都是 C10 修好之後才被攻出來的。

    洞 1 · `signal_frame=` 是完整旁路
      SignalFrame 只帶 strategy_id / version,**不帶產生它的參數**。所以前置閘門
      看 `kwargs["params"] is None` 而去算 `default_parameters()` 的 hash;
      `run_golden_path` 因為收到 signal_frame 也不會呼叫 `make_signals`,
      `candidate.signal_params` 於是仍是 defaults —— **兩道 hash 閘門都放行**,
      一套沒凍結的規則吃掉 locked OS,而紀錄記的是凍結那套的 hash。
      實測用 `mom_window` 加倍算出的訊號成功繞過(已確認訊號確實不同),
      等於可以無限次「免費看」同一段 OS。規則身分在這條路上結構性地驗不了,
      所以不准走。

    洞 2 · 揭露紀錄前移造成**不可逆**的偽陽性燒毀
      C10 把紀錄移到 run 之前(理由對:看過就必須記)。代價是零資料載入的失敗也
      會留下一列 `phase=pre_run`,而 `reveal_status` 完全不看 phase → 該候選立刻
      `previously_seen=True` / `fresh_oos_claim_allowed=False`,append-only 撤不回。
      專案只有一段 locked OS,`fixture_name` 打錯一個字母就永久失去 fresh 宣稱。
      修法沿用 `preflight_ledger` 的同一個論證:狀態在 run 之前就已成立 → 提前驗。

    兩條釘的是同一件事:**擋下來時,OS 沒被載入 且 揭露紀錄沒有新增一列。**
    """

    def test_signal_frame_is_refused_before_any_os_panel(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-21")
            ledger = f"{td}/holdout_ledger.jsonl"
            frame = pd.DataFrame({"date": ["2026-01-02"], "stock_id": ["2330"]})
            with _DataLayerSpy() as spy:
                with self.assertRaises(HoldoutBoundaryError) as ctx:
                    reveal_locked_os(
                        strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                        authorization=REVEAL_AUTHORIZATION, output_dir=td,
                        stamp="os", fixture_kwargs=FIXTURE_KW,
                        signal_frame=frame, ledger_path=ledger)
                spy.assert_untouched(self)
            self.assertIn("signal_frame", str(ctx.exception))
            self.assertEqual(read_ledger(ledger), [],
                             "被擋下來就不該在揭露紀錄留下任何一列")

    def test_unknown_fixture_name_does_not_burn_the_holdout(self):
        """`fixture_name` 打錯一個字母 —— 零資料載入,不該燒掉 fresh 宣稱。"""
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-21")
            ledger = f"{td}/holdout_ledger.jsonl"
            with _DataLayerSpy() as spy:
                with self.assertRaises(HoldoutBoundaryError) as ctx:
                    reveal_locked_os(
                        strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                        authorization=REVEAL_AUTHORIZATION, output_dir=td,
                        stamp="os", fixture_name="sythetic",   # 少一個 n
                        fixture_kwargs=FIXTURE_KW, ledger_path=ledger)
                spy.assert_untouched(self)
            self.assertIn("fixture_name", str(ctx.exception))
            self.assertEqual(read_ledger(ledger), [],
                             "零資料載入的失敗不可以留下 pre_run 紀錄 —— 那撤不回來")

    def test_unwritable_output_dir_does_not_burn_the_holdout(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-21")
            ledger = f"{td}/holdout_ledger.jsonl"
            ro = pathlib.Path(td) / "readonly"
            ro.mkdir()
            ro.chmod(0o500)
            try:
                with _DataLayerSpy() as spy:
                    with self.assertRaises(HoldoutBoundaryError):
                        reveal_locked_os(
                            strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                            authorization=REVEAL_AUTHORIZATION,
                            output_dir=str(ro / "out"), stamp="os",
                            fixture_kwargs=FIXTURE_KW, ledger_path=ledger)
                    spy.assert_untouched(self)
                self.assertEqual(read_ledger(ledger), [])
            finally:
                ro.chmod(0o700)

    def test_a_legitimate_reveal_still_works(self):
        """負面測試不能把正常路徑一起擋掉 —— 這支是那兩道閘門的反向對照。"""
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            manifest = _is_manifest(proto, td)
            frozen = freeze_from_is_manifest(manifest=manifest, protocol=proto,
                                             frozen_at="2026-08-21")
            ledger = f"{td}/holdout_ledger.jsonl"
            res = reveal_locked_os(
                strategy_id=STRATEGY, protocol=proto, frozen=frozen,
                authorization=REVEAL_AUTHORIZATION, output_dir=td,
                stamp="os", fixture_kwargs=FIXTURE_KW, ledger_path=ledger,
                now=pd.Timestamp("2026-08-21 12:00:00").to_pydatetime())
            self.assertEqual(len(read_ledger(ledger)), 1)
            self.assertIn("os_reveal", res.audit)


if __name__ == "__main__":
    unittest.main()
