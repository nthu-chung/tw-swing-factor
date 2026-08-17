# -*- coding: utf-8 -*-
"""外部訊號 → policy → 事件引擎 的唯一研究入口(policy_research_run)。

為什麼需要這組測試:`backtest_portfolio()` 的參數面同時服務三條路徑,少傳一個
參數的後果不是壞掉而是**安靜降級**成不可作正式證據的東西。這支入口把正確的
request 組合固定下來,所以要釘住的是「它真的把該傳的都傳了」與「稽核表不會
把未通過說成通過」。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

import research.golden_path as prr   # 實作已搬到 research 層;
                                     # policy_research_run 只剩 re-export
from strategy_kit.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)


class _FakeUniverse:
    def __init__(self):
        self.calls = 0

    def backtest_kwargs(self):
        self.calls += 1
        return {"symbols": ["A", "B"], "universe_provider": object(),
                "sample": False, "dynamic_enabled": True}


def _frame():
    """一份**合格**的 SignalFrame。

    2026-08-16 起外部訊號也要過 `validate_signal_frame`(§3.1:不為外部訊號開
    比較寬鬆的第二條路),所以這裡補上 `ranking_universe_count` —— 原本缺這一欄
    代表「當日排名母體有多大」無從查證,正是 validator 要擋的東西。
    斷言完全沒有改動,只有 fixture 補齊到契約要求的欄位。
    """
    # strategy 的 make_signals 是日頻；runner 自己負責選五個 weekly phase。
    days = list(pd.bdate_range("2026-01-05", periods=30))
    return pd.DataFrame(
        [{"date": d, "stock_id": "A", "rank": 1, "raw_score": 1.0,
          "eligible": True, "snapshot_complete": True,
          "ranking_universe_count": 1} for d in days])


def _summary(**over):
    base = {
        "period": ["2026-01-05", "2026-01-19"],
        "n_trades": 3,
        "universe": {
            "candidate_pool_pit": True,
            "formal_evidence_eligible": True,
            "excluded_by_security_type": {
                "total": 0, "rule": "listed_common_stock_whitelist_v1"},
        },
        "data": {"integrity_bypassed": False},
        "eval_audit": {"days_beyond_last_pick": 0},
        "strategy_position_policy": {
            "rules_hash": "abc123",
            "regime_evidence": "verified",
            "snapshot_complete_all_days": True,
            "capital_scenario": {"initial_capital": 1_000_000.0},
            "cash_audit": {"n_days": 10},
            "desired_realized_audit": {},
            "exit_reason_stats": {},
        },
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    days = pd.bdate_range("2026-01-05", periods=20)
    return {
        "summary": base,
        "equity_curve": pd.DataFrame({"date": days,
                                      "equity": [1_000_000.0] * len(days)}),
        "trades": pd.DataFrame(),
        "decision_log": [],
        "order_log": [],
    }


class RequestAssemblyTest(unittest.TestCase):
    def test_pit_universe_is_used_by_default(self):
        """呼叫端不自己湊 symbols —— 那正是舊研究腳本靜默退回單日靜態池的原因。"""
        uni = _FakeUniverse()
        with mock.patch.object(prr.event_backtest, "backtest_portfolio",
                               return_value=_summary()) as bp:
            result = prr.run_signal_frame_backtest(
                signal_frame=_frame(), universe=uni)
        self.assertEqual(uni.calls, 1)
        self.assertEqual(bp.call_count, 5)
        self.assertEqual(len(result["phase_results"]), 5)
        self.assertIn(result["representative_phase"], range(5))
        for call in bp.call_args_list:
            kw = call.kwargs
            self.assertEqual(kw["symbols"], ["A", "B"])
            self.assertIsNotNone(kw["universe_provider"])
            self.assertFalse(kw["sample"])

    def test_capital_scenarios_are_immutable_request_parameters(self):
        seen = []

        def _fake(**kwargs):
            seen.append((kwargs["initial_capital"], kwargs["order_size_mode"]))
            return _summary()

        with mock.patch.object(prr.event_backtest, "backtest_portfolio",
                               side_effect=_fake):
            prr.run_signal_frame_backtest(signal_frame=_frame(),
                                    universe=_FakeUniverse(), capital="research")
            prr.run_signal_frame_backtest(signal_frame=_frame(),
                                    universe=_FakeUniverse(), capital="personal")
        self.assertEqual(seen[:5], [(1_000_000.0, "research_fractional")] * 5)
        self.assertEqual(seen[5:], [(500_000.0, "odd_lot_proxy")] * 5)

    def test_unknown_capital_scenario_fails_closed(self):
        with self.assertRaises(ValueError):
            prr.run_signal_frame_backtest(signal_frame=_frame(),
                                    universe=_FakeUniverse(), capital="yolo")

    def test_policy_spec_is_cloned_for_each_phase(self):
        pol = StrategyPositionPolicy(StrategyPositionPolicySpec(
            max_slots=5, risk_on_slots=5, caution_slots=2,
            slot_weight=0.20, single_name_cap=0.30))
        with mock.patch.object(prr.event_backtest, "backtest_portfolio",
                               return_value=_summary()) as bp:
            prr.run_signal_frame_backtest(signal_frame=_frame(),
                                    universe=_FakeUniverse(), policy=pol)
        phase_policies = [c.kwargs["strategy_position_policy"]
                          for c in bp.call_args_list]
        self.assertEqual(len(phase_policies), 5)
        self.assertEqual(len({id(p) for p in phase_policies}), 5)
        self.assertTrue(all(p is not pol for p in phase_policies))
        self.assertTrue(all(p.spec == pol.spec for p in phase_policies))


class AuditSummaryTest(unittest.TestCase):
    def test_all_green_is_formal_evidence_ready(self):
        audit = prr.audit_summary(_summary())
        self.assertTrue(audit["formal_evidence_ready"])
        self.assertTrue(all(audit["checks"].values()))

    def test_eval_window_overflow_blocks_formal_evidence(self):
        res = _summary(eval_audit={"days_beyond_last_pick": 12})
        audit = prr.audit_summary(res)
        self.assertFalse(audit["checks"]["eval_window_not_overflowing"])
        self.assertFalse(audit["formal_evidence_ready"])

    def test_static_universe_blocks_formal_evidence(self):
        res = _summary(universe={"candidate_pool_pit": False})
        self.assertFalse(prr.audit_summary(res)["checks"]["pit_universe"])

    def test_integrity_bypass_blocks_formal_evidence(self):
        res = _summary(data={"integrity_bypassed": True})
        self.assertFalse(
            prr.audit_summary(res)["checks"]["price_integrity_not_bypassed"])

    def test_missing_security_type_rule_blocks_formal_evidence(self):
        """沒有 collector 就沒有 rule 欄位 = 白名單沒生效,不能算通過。"""
        res = _summary(universe={"excluded_by_security_type": {"total": 0}})
        self.assertFalse(prr.audit_summary(res)["checks"]["common_stock_only"])

    def test_unverified_regime_blocks_formal_evidence(self):
        res = _summary(strategy_position_policy={"regime_evidence": "unverified"})
        self.assertFalse(prr.audit_summary(res)["checks"]["regime_verified"])

    def test_incomplete_snapshot_blocks_formal_evidence(self):
        res = _summary(
            strategy_position_policy={"snapshot_complete_all_days": False})
        self.assertFalse(
            prr.audit_summary(res)["checks"]["snapshot_complete_all_days"])

    def test_missing_fields_count_as_failure_not_pass(self):
        """不知道 != 沒問題:欄位缺失一律視為未通過。"""
        audit = prr.audit_summary({"summary": {}})
        self.assertFalse(audit["formal_evidence_ready"])
        self.assertFalse(any(audit["checks"].values()))

    def test_format_audit_never_claims_the_strategy_works(self):
        text = prr.format_audit(prr.audit_summary(_summary()))
        self.assertIn("管線跑通 != 策略有效", text)
        self.assertIn("clean OOS", text)


if __name__ == "__main__":
    unittest.main()
