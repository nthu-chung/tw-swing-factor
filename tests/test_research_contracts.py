# -*- coding: utf-8 -*-
"""研究層 request 契約與策略 registry 的責任測試。

釘住兩件會讓「這個數字屬於哪一套規則」變成不可回答的事:

1. **兩層 identity 必須分離**(§8.2)。混成一個 hash 的後果:推進快照就換 hash,
   forward 永遠累積不起來;或不同 fold 的 metrics 共用同一個 id,無法歸因。
2. **runner 擁有的安全欄位不得被 kwargs 靜默覆寫**。那些欄位是 PIT provider、
   資金情境、評估窗與 provenance 的來源。
"""
from __future__ import annotations

import unittest

from research.contracts import (
    REQUEST_OWNED_KEYS,
    BacktestRequest,
    CandidateSpec,
    EvaluationProtocol,
)
from strategy_kit import registry


def _candidate(**over) -> CandidateSpec:
    base = dict(strategy_id="s19_reference_make_signals",
                strategy_version="1.0.0",
                signal_params={"mom_window": 20},
                portfolio_params={"max_slots": 10},
                exit_params={"hard_stop_pct": 0.20})
    base.update(over)
    return CandidateSpec(**base)


class IdentitySeparationTest(unittest.TestCase):
    def test_rule_hash_ignores_the_evaluation_protocol(self):
        """同一套規則換資料快照,rule hash 不得改變 —— 否則 forward 斷掉。"""
        cand = _candidate()
        a = BacktestRequest(cand, EvaluationProtocol(data_snapshot="2026-06-22"))
        b = BacktestRequest(cand, EvaluationProtocol(data_snapshot="2026-08-16"))
        self.assertEqual(a.strategy_rule_hash(), b.strategy_rule_hash())

    def test_run_hash_does_change_with_the_protocol(self):
        cand = _candidate()
        a = BacktestRequest(cand, EvaluationProtocol(data_snapshot="2026-06-22"))
        b = BacktestRequest(cand, EvaluationProtocol(data_snapshot="2026-08-16"))
        self.assertNotEqual(a.evaluation_run_hash(), b.evaluation_run_hash())

    def test_any_rule_change_changes_the_rule_hash(self):
        base = BacktestRequest(_candidate(), EvaluationProtocol())
        for over in ({"signal_params": {"mom_window": 60}},
                     {"portfolio_params": {"max_slots": 5}},
                     {"exit_params": {"hard_stop_pct": 0.08}},
                     {"strategy_version": "2.0.0"},
                     {"universe_rule": "static_top300"}):
            with self.subTest(over=over):
                other = BacktestRequest(_candidate(**over), EvaluationProtocol())
                self.assertNotEqual(base.strategy_rule_hash(),
                                    other.strategy_rule_hash())


class ImmutableRequestTest(unittest.TestCase):
    def test_engine_kwargs_cannot_override_runner_owned_fields(self):
        for key in REQUEST_OWNED_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    BacktestRequest(_candidate(), EvaluationProtocol(),
                                    engine_kwargs={key: "hijacked"})

    def test_unrelated_engine_kwargs_are_allowed(self):
        req = BacktestRequest(_candidate(), EvaluationProtocol(),
                              engine_kwargs={"let_positions_run": True})
        self.assertIn("let_positions_run", req.manifest()["engine_kwargs"])

    def test_manifest_carries_both_hashes_and_config_snapshot(self):
        m = BacktestRequest(_candidate(), EvaluationProtocol()).manifest()
        self.assertIn("strategy_rule_hash", m)
        self.assertIn("evaluation_run_hash", m)
        self.assertIn("SNAPSHOT_END_DATE", m["config_snapshot"])
        self.assertIn("PRICE_ADJUST_ANCHOR", m["config_snapshot"])


class RegistryTest(unittest.TestCase):
    def test_reference_strategy_is_registered(self):
        self.assertIn("s19_reference_make_signals", registry.available())

    def test_unknown_id_fails_closed(self):
        with self.assertRaises(KeyError):
            registry.resolve("../../evil/path")

    def test_resolved_strategy_name_must_match_the_registered_id(self):
        s = registry.resolve("s19_reference_make_signals")
        self.assertEqual(s.name, "s19_reference_make_signals")

    def test_fixture_evidence_status_is_not_a_performance_claim(self):
        self.assertEqual(registry.evidence_status("s19_reference_make_signals"),
                         "pipeline_fixture_no_performance_claim")


if __name__ == "__main__":
    unittest.main()
