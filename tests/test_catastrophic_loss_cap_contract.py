# -*- coding: utf-8 -*-
"""StrategyPositionPolicy v1.1 累積災難損失上限的 owner contract。

2026-08-16 owner 明確否決「單日 -8%／跌停，隔天就停損」的語意。這支測試刻意
先於實作加入，交接時預期紅燈；不得刪除、skip 或把 -20% 改回 -8% 取得綠燈。
"""
from __future__ import annotations

import unittest

import pandas as pd

from strategy_kit.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)


def _signals(ranks):
    return pd.DataFrame([
        {
            "stock_id": sid,
            "rank": rank,
            "raw_score": 1.0 - rank * 0.001,
            "eligible": True,
            "snapshot_complete": True,
        }
        for sid, rank in ranks.items()
    ])


def _holding(close, *, exit_pending=False):
    return pd.DataFrame([{
        "stock_id": "X",
        "weight": 0.10,
        "entry_price": 100.0,
        "close": float(close),
        "holding_days": 20,
        "exit_pending": bool(exit_pending),
    }])


def _decide(policy, *, close=None, ranks=None, holdings=None, date="2026-01-09"):
    if holdings is None:
        holdings = _holding(close) if close is not None else pd.DataFrame()
    return policy.decide(
        as_of=pd.Timestamp(date),
        signals=_signals(ranks or {"X": 1}),
        holdings=holdings,
        equity=1_000_000.0,
        regime="risk_on",
        is_decision_day=True,
        next_execution=pd.Timestamp(date) + pd.offsets.BDay(1),
    )


class CatastrophicLossCapContractTest(unittest.TestCase):
    def test_default_is_fixed_twenty_percent_cumulative_loss(self):
        self.assertAlmostEqual(StrategyPositionPolicySpec().hard_stop_pct, 0.20)

    def test_minus_8_10_and_19_percent_do_not_trigger_the_cap(self):
        for close in (92.0, 90.0, 81.0):
            with self.subTest(close=close):
                decision = _decide(StrategyPositionPolicy(), close=close)
                action = decision.actions.set_index("stock_id").loc["X"]
                self.assertEqual(action["action"], "hold")
                self.assertNotEqual(action["reason_code"], "risk_stop")

    def test_minus_20_and_worse_create_t_plus_one_exit_intent(self):
        for close in (80.0, 75.0):
            with self.subTest(close=close):
                decision = _decide(StrategyPositionPolicy(), close=close)
                action = decision.actions.set_index("stock_id").loc["X"]
                self.assertEqual(action["action"], "exit")
                self.assertEqual(action["reason_code"], "risk_stop")
                self.assertGreater(
                    pd.Timestamp(action["earliest_execution"]), decision.as_of)

    def test_deep_loss_with_known_pending_exit_is_not_duplicated(self):
        policy = StrategyPositionPolicy()
        decision = _decide(policy, holdings=_holding(65.0, exit_pending=True))
        action = decision.actions.set_index("stock_id").loc["X"]
        self.assertEqual(action["action"], "hold")
        self.assertEqual(
            action["reason_code"], "stop_breached_earlier_exit_pending")

    def test_stopped_name_must_leave_top20_before_top10_can_rearm_it(self):
        policy = StrategyPositionPolicy()

        stopped = _decide(policy, close=80.0, date="2026-01-09")
        self.assertEqual(
            stopped.actions.set_index("stock_id").loc["X", "reason_code"],
            "risk_stop",
        )

        # 假設退出已成交、realized holdings 已空；下週仍在 top 10 不得立刻買回。
        empty = pd.DataFrame()
        still_top = _decide(
            policy, holdings=empty, ranks={"X": 1}, date="2026-01-16")
        x = still_top.actions.set_index("stock_id").loc["X"]
        self.assertNotEqual(x["action"], "enter")
        self.assertEqual(x["reason_code"], "catastrophic_stop_not_rearmed")

        # 完整排名快照中先掉出 top 20，才解除鎖定。
        ranks = {f"S{i:02d}": i for i in range(1, 21)}
        ranks["X"] = 21
        _decide(policy, holdings=empty, ranks=ranks, date="2026-01-23")

        reentered = _decide(
            policy, holdings=empty, ranks={"X": 1}, date="2026-01-30")
        x = reentered.actions.set_index("stock_id").loc["X"]
        self.assertEqual(x["action"], "enter")
        self.assertEqual(x["reason_code"], "new_top_k")


if __name__ == "__main__":
    unittest.main()
