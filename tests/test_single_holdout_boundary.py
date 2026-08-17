# -*- coding: utf-8 -*-
"""`EVALUATION_DATA_BOUNDARY_SPEC.md` §8 的九項最小驗收(全離線)。

要證明的不是「CLI 跑得完」,而是**研究程序根本沒有取得 locked OS**,而且事件
引擎與 artifacts 都沒有越過當前 segment 邊界。
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import pandas as pd

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
    research_run,
    reveal_locked_os,
)
from strategies.s19_reference import S19ReferenceStrategy

STRATEGY = "s19_reference_make_signals"
FIXTURE_KW = {"n_symbols": 6, "n_days": 120}


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
        frozen = freeze_candidate(strategy_id=STRATEGY,
                                  strategy_rule_hash="not-the-real-hash",
                                  protocol=proto, frozen_at="2026-08-16")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(HoldoutBoundaryError):
                reveal_locked_os(strategy_id=STRATEGY, protocol=proto,
                                 frozen=frozen,
                                 authorization=REVEAL_AUTHORIZATION,
                                 output_dir=td, fixture_kwargs=FIXTURE_KW)


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

    def _real_rule_hash(self, proto, td):
        res = run_golden_path(
            strategy_id=STRATEGY, fixture_name="synthetic",
            capital=proto.capital_scenario, output_dir=td, stamp="probe",
            holdout_protocol=proto, segment=SEGMENT_IS,
            fixture_kwargs=FIXTURE_KW)
        return res.manifest["strategy_rule_hash"]

    def test_first_reveal_writes_ledger_second_is_previously_seen(self):
        proto = _protocol()
        with tempfile.TemporaryDirectory() as td:
            ledger = f"{td}/holdout_ledger.jsonl"
            rule_hash = self._real_rule_hash(proto, td)
            frozen = freeze_candidate(strategy_id=STRATEGY,
                                      strategy_rule_hash=rule_hash,
                                      protocol=proto, frozen_at="2026-08-16")

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

if __name__ == "__main__":
    unittest.main()
