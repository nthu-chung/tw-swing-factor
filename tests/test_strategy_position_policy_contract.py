# -*- coding: utf-8 -*-
"""StrategyPositionPolicy v1 的 contract-first 離線驗收。

這支測試刻意先於實作加入。交接當下預期只有 availability 測試失敗、其餘因公共
模組尚不存在而 skip；實作者建立 `strategies.position_policy` 後，所有 skip 會自動
解除。不得刪測試、永久 skip 或把 assert 改寬來取得綠燈。

這裡釘住的不是內部類別怎麼拆，而是曾經會製造假績效的外部行為：

* 正常排名換股只能在每週決策日，T 日資訊最早 T+1 執行。
* top-10 進、top-20 續抱；候選不足保留現金，不放大剩餘股票。
* risk-on/caution/risk-off 用 10/5/0 個固定 10% slot。
* hard stop 是收盤確認後退出意圖，不是假設盤中碰價精準成交。
* 跌停賣不掉時不能釋放現金買 replacement。
* 未來訊號不得改變過去決策；policy 規則必須進 provenance/hash。

完整需求見 `STRATEGY_POSITION_POLICY_SPEC.md`。
"""
from __future__ import annotations

import copy
import importlib
import unittest
from unittest import mock

import pandas as pd

import backtest
import config


try:
    _policy_module = importlib.import_module("strategies.position_policy")
except ModuleNotFoundError:
    _policy_module = None


POLICY_AVAILABLE = _policy_module is not None


def _spec(**overrides):
    """只覆寫測試要觀察的值；v1 預設值由 contract 逐項檢查。"""
    values = {
        "decision_frequency": "weekly",
        "entry_rank": 10,
        "exit_rank": 20,
        "max_slots": 10,
        "slot_weight": 0.10,
        "single_name_cap": 0.15,
        "hard_stop_pct": 0.08,
        "max_hold_days": 120,
        "risk_on_slots": 10,
        "caution_slots": 5,
        "risk_off_slots": 0,
    }
    values.update(overrides)
    return _policy_module.StrategyPositionPolicySpec(**values)


def _signals(ranks, *, score_start=1.0):
    """建立**完整**排名母體的訊號快照。

    `snapshot_complete=True` 是 2026-08-15 owner 同意的契約澄清後補上的:這個
    fixture 本來就是在描述「當日完整排名母體」(所有案例都假設沒列出的股票就是
    真的不在母體裡),旗標只是把那個前提寫出來。

    在此之前實作把缺旗標當成 True,等於用「我沒看到它」當成「它已經掉出母體」的
    證據 —— 持有 B、當天訊號只有 A 時 B 會被判 `exit / not_ranked` 賣掉。缺旗標
    的正確語意是 unknown(規格 §5、§9B.1),因此預設值改為 False,而**真的**完整
    的快照必須自己宣告。這是把前提寫明,不是放寬斷言:
    `test_decision_is_complete_and_auditable` 的 `assertTrue(d.snapshot_complete)`
    原封不動保留,缺旗照樣為 False 的行為另由
    `tests/test_strategy_position_policy_snapshot.py` 逐條釘住。
    """
    rows = []
    for i, (sid, rank) in enumerate(ranks.items()):
        rows.append({
            "stock_id": sid,
            "rank": int(rank),
            "raw_score": float(score_start - i * 0.01),
            "eligible": True,
            "snapshot_complete": True,
        })
    return pd.DataFrame(rows)


def _holdings(rows=()):
    return pd.DataFrame([
        {
            "stock_id": sid,
            "weight": float(weight),
            "entry_price": float(entry),
            "close": float(close),
            "holding_days": int(days),
        }
        for sid, weight, entry, close, days in rows
    ], columns=["stock_id", "weight", "entry_price", "close", "holding_days"])


def _policy(**spec_overrides):
    return _policy_module.StrategyPositionPolicy(_spec(**spec_overrides))


def _decide(policy, *, ranks, holdings=(), regime="risk_on",
            is_decision_day=True, as_of="2026-01-09", equity=1_000_000):
    return policy.decide(
        as_of=pd.Timestamp(as_of),
        signals=_signals(ranks),
        holdings=_holdings(holdings),
        equity=float(equity),
        regime=regime,
        is_decision_day=bool(is_decision_day),
    )


def _frame(decision, attr):
    value = getattr(decision, attr)
    if not isinstance(value, pd.DataFrame):
        value = pd.DataFrame(value)
    return value.copy()


@unittest.skipUnless(POLICY_AVAILABLE, "等待實作者新增 strategies.position_policy")
class StrategyPositionPolicySpecTest(unittest.TestCase):
    def test_v1_defaults_are_frozen_and_serializable(self):
        spec = _spec()
        self.assertEqual(spec.decision_frequency, "weekly")
        self.assertEqual(spec.entry_rank, 10)
        self.assertEqual(spec.exit_rank, 20)
        self.assertEqual(spec.max_slots, 10)
        self.assertAlmostEqual(spec.slot_weight, 0.10)
        self.assertAlmostEqual(spec.single_name_cap, 0.15)
        self.assertAlmostEqual(spec.hard_stop_pct, 0.08)
        self.assertEqual(spec.max_hold_days, 120)
        self.assertEqual(
            (spec.risk_on_slots, spec.caution_slots, spec.risk_off_slots),
            (10, 5, 0),
        )
        rules = spec.rules()
        for key in (
            "decision_frequency", "entry_rank", "exit_rank", "max_slots",
            "slot_weight", "single_name_cap", "hard_stop_pct",
            "max_hold_days", "risk_on_slots", "caution_slots",
            "risk_off_slots",
        ):
            self.assertIn(key, rules, f"{key} 會改績效，必須進 rules/hash")

    def test_invalid_rank_weight_and_slot_combinations_fail_closed(self):
        bad = (
            {"entry_rank": 10, "exit_rank": 10},
            {"entry_rank": 0},
            {"max_slots": 0},
            {"slot_weight": 0.0},
            {"single_name_cap": 0.05},
            {"risk_on_slots": 11},
            {"caution_slots": 10},
            {"hard_stop_pct": -0.01},
            {"max_hold_days": 0},
        )
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                _spec(**overrides)


@unittest.skipUnless(POLICY_AVAILABLE, "等待實作者新增 strategies.position_policy")
class WeeklyRankAndCashPolicyTest(unittest.TestCase):
    def test_top10_enters_top20_holds_and_rank21_exits(self):
        ranks = {f"S{i:02d}": i for i in range(1, 22)}
        holdings = (
            ("S20", 0.10, 100.0, 105.0, 20),
            ("S21", 0.10, 100.0, 105.0, 20),
        )
        d = _decide(_policy(), ranks=ranks, holdings=holdings)
        actions = _frame(d, "actions").set_index("stock_id")
        self.assertEqual(actions.loc["S20", "action"], "hold")
        self.assertEqual(actions.loc["S21", "action"], "exit")
        self.assertEqual(actions.loc["S21", "reason_code"], "rank_decay")
        entered = set(actions.index[actions["action"] == "enter"])
        self.assertTrue(entered.issubset({f"S{i:02d}" for i in range(1, 11)}))

    def test_normal_rank_churn_is_suppressed_off_decision_day(self):
        d = _decide(
            _policy(), ranks={"HELD": 99, "NEW": 1},
            holdings=(("HELD", 0.10, 100.0, 100.0, 10),),
            is_decision_day=False,
        )
        actions = _frame(d, "actions")
        self.assertFalse(
            actions["reason_code"].isin({"rank_decay", "new_top_k"}).any(),
            "非每週決策日不得因一般排名變動換股",
        )

    def test_candidate_shortage_keeps_cash_instead_of_grossing_up(self):
        d = _decide(_policy(), ranks={f"S{i}": i for i in range(1, 7)})
        targets = _frame(d, "targets")
        self.assertEqual(len(targets[targets["target_weight"] > 0]), 6)
        self.assertTrue((targets["target_weight"] <= 0.10 + 1e-12).all())
        self.assertAlmostEqual(float(d.target_cash_weight), 0.40, places=9)
        self.assertAlmostEqual(targets["target_weight"].sum() + d.target_cash_weight,
                               1.0, places=9)

    def test_scores_do_not_change_equal_slot_weights(self):
        signals = _signals({"A": 1, "B": 2})
        signals.loc[signals["stock_id"] == "A", "raw_score"] = 1_000.0
        signals.loc[signals["stock_id"] == "B", "raw_score"] = 0.001
        d = _policy().decide(
            as_of=pd.Timestamp("2026-01-09"), signals=signals,
            holdings=_holdings(), equity=1_000_000,
            regime="risk_on", is_decision_day=True,
        )
        weights = _frame(d, "targets").set_index("stock_id")["target_weight"]
        self.assertAlmostEqual(weights["A"], 0.10)
        self.assertAlmostEqual(weights["B"], 0.10)

    def test_capital_changes_notional_not_target_weights(self):
        p = _policy()
        ranks = {f"S{i}": i for i in range(1, 11)}
        d1 = _decide(p, ranks=ranks, equity=1_000_000)
        d2 = _decide(p, ranks=ranks, equity=500_000)
        t1 = _frame(d1, "targets").sort_values("stock_id").reset_index(drop=True)
        t2 = _frame(d2, "targets").sort_values("stock_id").reset_index(drop=True)
        self.assertEqual(list(t1["target_weight"]), list(t2["target_weight"]))
        self.assertEqual(list(t1["target_notional"]), [100_000.0] * 10)
        self.assertEqual(list(t2["target_notional"]), [50_000.0] * 10)

    def test_regime_tiers_use_10_5_0_slots_without_redistribution(self):
        ranks = {f"S{i:02d}": i for i in range(1, 11)}
        expected = {"risk_on": (10, 0.0), "caution": (5, 0.5),
                    "risk_off": (0, 1.0)}
        for regime, (n, cash) in expected.items():
            with self.subTest(regime=regime):
                d = _decide(_policy(), ranks=ranks, regime=regime)
                targets = _frame(d, "targets")
                self.assertEqual((targets["target_weight"] > 0).sum(), n)
                self.assertAlmostEqual(float(d.target_cash_weight), cash)
                if n:
                    self.assertTrue(
                        (targets.loc[targets["target_weight"] > 0,
                                     "target_weight"] == 0.10).all())

    def test_small_weight_drift_does_not_rebalance_or_average_down(self):
        d = _decide(
            _policy(), ranks={"WINNER": 1, "LAGGARD": 2},
            holdings=(
                ("WINNER", 0.12, 100.0, 120.0, 20),
                ("LAGGARD", 0.07, 100.0, 70.0, 20),
            ),
        )
        actions = _frame(d, "actions").set_index("stock_id")
        self.assertEqual(actions.loc["WINNER", "action"], "hold")
        self.assertEqual(actions.loc["LAGGARD", "action"], "hold")
        self.assertNotEqual(actions.loc["LAGGARD", "action"], "resize",
                            "不得只因跌到低於 10% 就機械式攤平")

    def test_weight_above_single_name_cap_is_trimmed_to_cap(self):
        d = _decide(
            _policy(), ranks={"A": 1},
            holdings=(("A", 0.16, 100.0, 160.0, 20),),
        )
        actions = _frame(d, "actions").set_index("stock_id")
        targets = _frame(d, "targets").set_index("stock_id")
        self.assertEqual(actions.loc["A", "action"], "resize")
        self.assertEqual(actions.loc["A", "reason_code"], "concentration_cap")
        self.assertAlmostEqual(targets.loc["A", "target_weight"], 0.15)


@unittest.skipUnless(POLICY_AVAILABLE, "等待實作者新增 strategies.position_policy")
class RiskTimingAndAuditTest(unittest.TestCase):
    def test_close_confirmed_stop_creates_exit_even_off_weekly_decision_day(self):
        d = _decide(
            _policy(), ranks={"A": 1},
            holdings=(("A", 0.10, 100.0, 91.9, 5),),
            is_decision_day=False,
        )
        actions = _frame(d, "actions").set_index("stock_id")
        self.assertEqual(actions.loc["A", "action"], "exit")
        self.assertEqual(actions.loc["A", "reason_code"], "risk_stop")
        self.assertGreater(pd.Timestamp(actions.loc["A", "earliest_execution"]),
                           pd.Timestamp("2026-01-09"))

    def test_intraday_low_without_close_break_must_not_trigger_manual_stop(self):
        signals = _signals({"A": 1})
        holdings = _holdings((("A", 0.10, 100.0, 93.0, 5),))
        # `intraday_low` 是刻意附加的欄位：v1 手動停損不得用它假裝已成交。
        holdings["intraday_low"] = 80.0
        d = _policy().decide(
            as_of=pd.Timestamp("2026-01-09"), signals=signals,
            holdings=holdings, equity=1_000_000,
            regime="risk_on", is_decision_day=False,
        )
        actions = _frame(d, "actions")
        risk_exits = actions[
            (actions["stock_id"] == "A") &
            (actions["reason_code"] == "risk_stop")
        ]
        self.assertTrue(risk_exits.empty)

    def test_max_hold_is_an_audited_exit_reason(self):
        d = _decide(
            _policy(), ranks={"A": 1},
            holdings=(("A", 0.10, 100.0, 110.0, 120),),
            is_decision_day=True,
        )
        actions = _frame(d, "actions").set_index("stock_id")
        self.assertEqual(actions.loc["A", "action"], "exit")
        self.assertEqual(actions.loc["A", "reason_code"], "max_hold")

    def test_decision_is_complete_and_auditable(self):
        d = _decide(_policy(), ranks={"A": 1, "B": 2})
        self.assertTrue(d.snapshot_complete)
        actions = _frame(d, "actions")
        targets = _frame(d, "targets")
        self.assertTrue({"stock_id", "action", "reason_code",
                         "decision_rank", "earliest_execution"}.issubset(actions))
        self.assertTrue({"stock_id", "target_weight",
                         "target_notional"}.issubset(targets))
        self.assertEqual(d.policy_rules, _spec().rules())

    def test_appending_future_signals_does_not_change_past_decision(self):
        p = _policy()
        base = _signals({"A": 1, "B": 2, "C": 3})
        d1 = p.decide(
            as_of=pd.Timestamp("2026-01-09"), signals=base,
            holdings=_holdings(), equity=1_000_000,
            regime="risk_on", is_decision_day=True,
        )
        future = base.copy()
        future["date"] = pd.Timestamp("2026-02-01")
        future["rank"] = [99, 98, 97]
        combined = pd.concat([
            base.assign(date=pd.Timestamp("2026-01-09")), future,
        ], ignore_index=True)
        d2 = p.decide(
            as_of=pd.Timestamp("2026-01-09"), signals=combined,
            holdings=_holdings(), equity=1_000_000,
            regime="risk_on", is_decision_day=True,
        )
        pd.testing.assert_frame_equal(
            _frame(d1, "targets").sort_values("stock_id").reset_index(drop=True),
            _frame(d2, "targets").sort_values("stock_id").reset_index(drop=True),
        )

    def test_policy_construction_defensively_copies_mutable_rules(self):
        spec = _spec()
        before = copy.deepcopy(spec.rules())
        policy = _policy_module.StrategyPositionPolicy(spec)
        # 若實作者內部另存 mutable state，不能反向污染 frozen spec/rules hash。
        if hasattr(policy, "_state") and isinstance(policy._state, dict):
            policy._state["probe"] = "mutated"
        self.assertEqual(spec.rules(), before)


def _flat_prices(dates, *, locked_down_on=None, unlock_on=None):
    rows = []
    for d in dates:
        if locked_down_on is not None and d == locked_down_on:
            o = hi = lo = c = 90.0
        elif unlock_on is not None and d == unlock_on:
            o, hi, lo, c = 89.0, 90.0, 88.0, 89.0
        else:
            o, hi, lo, c = 100.0, 101.0, 99.0, 100.0
        rows.append({
            "date": d, "open": o, "high": hi, "low": lo, "close": c,
            "volume": 1_000_000,
        })
    return pd.DataFrame(rows)


def _signal_frame(decision_dates, snapshots):
    rows = []
    for d, ranks in zip(decision_dates, snapshots):
        for sid, rank in ranks.items():
            rows.append({
                "date": d, "stock_id": sid, "rank": int(rank),
                "raw_score": float(100 - rank), "eligible": True,
                "snapshot_complete": True,
            })
    return pd.DataFrame(rows)


@unittest.skipUnless(POLICY_AVAILABLE, "等待實作者新增 strategies.position_policy")
class EventBacktestPolicyIntegrationTest(unittest.TestCase):
    """policy 必須走現有事件引擎，不能只產生一張看起來正確的 target 表。"""

    def _run(self, *, policy, signals, prices, initial_capital=1_000_000):
        symbols = sorted(prices)
        with (
            mock.patch.object(backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(backtest, "_load_disposition_days", lambda *_a, **_k: {}),
            mock.patch.object(
                backtest.data, "fetch_price",
                side_effect=lambda sid, *a, **k: prices[sid].copy(),
            ),
            mock.patch.object(config, "BT_MODEL_LIMIT_LOCK", True),
        ):
            return backtest.backtest_portfolio(
                symbols=symbols,
                sample=False,
                start_date=str(min(df["date"].min() for df in prices.values()))[:10],
                end_date=str(max(df["date"].max() for df in prices.values()))[:10],
                signal_frame=signals,
                strategy_position_policy=policy,
                initial_capital=float(initial_capital),
                order_size_mode="odd_lot_proxy",
                minimum_commission=0.0,
                static_universe_comparator=True,
            )

    def test_rank_exit_is_decided_at_close_and_filled_next_open(self):
        dates = list(pd.bdate_range("2026-01-05", periods=9))
        decision_dates = [dates[0], dates[5]]
        signals = _signal_frame(
            decision_dates,
            [
                {"A": 1, "B": 2},
                {"A": 21, "B": 2, "C": 1},
            ],
        )
        prices = {sid: _flat_prices(dates) for sid in ("A", "B", "C")}
        result = self._run(policy=_policy(), signals=signals, prices=prices)

        trades = result["trades"]
        a = trades[(trades["stock_id"] == "A") &
                   (trades["exit_reason"] == "rank_decay")]
        self.assertEqual(len(a), 1)
        self.assertEqual(pd.Timestamp(a.iloc[0]["exit_date"]), dates[6])
        self.assertGreater(pd.Timestamp(a.iloc[0]["exit_date"]), decision_dates[1])

        decisions = pd.DataFrame(result["decision_log"])
        row = decisions[(decisions["date"] == decision_dates[1]) &
                        (decisions["stock_id"] == "A")]
        self.assertEqual(row.iloc[0]["action"], "exit")
        self.assertEqual(row.iloc[0]["reason_code"], "rank_decay")

        summary = result["summary"]
        self.assertEqual(
            summary["strategy_position_policy"]["rules"], _spec().rules())
        self.assertEqual(summary["execution"]["initial_capital"], 1_000_000.0)
        self.assertEqual(summary["execution"]["order_size_mode"], "odd_lot_proxy")

    def test_locked_exit_keeps_realized_holding_and_cannot_fund_replacement(self):
        """一字跌停賣不掉卻先買 replacement，會憑空增加曝險與績效。"""
        dates = list(pd.bdate_range("2026-01-05", periods=9))
        decision_dates = [dates[0], dates[5]]
        signals = _signal_frame(
            decision_dates,
            [{"A": 1}, {"A": 3, "C": 1}],
        )
        one_slot = _policy(
            entry_rank=1, exit_rank=2,
            max_slots=1, slot_weight=1.0, single_name_cap=1.0,
            risk_on_slots=1, caution_slots=0, risk_off_slots=0,
        )
        prices = {
            "A": _flat_prices(dates, locked_down_on=dates[6], unlock_on=dates[7]),
            "C": _flat_prices(dates),
        }
        result = self._run(policy=one_slot, signals=signals, prices=prices)
        orders = pd.DataFrame(result["order_log"])

        locked_sell = orders[
            (orders["date"] == dates[6]) & (orders["stock_id"] == "A") &
            (orders["side"] == "sell")
        ]
        self.assertEqual(len(locked_sell), 1)
        self.assertNotEqual(locked_sell.iloc[0]["status"], "filled")
        self.assertIn("limit", str(locked_sell.iloc[0]["reason"]).lower())

        imaginary_buy = orders[
            (orders["date"] == dates[6]) & (orders["stock_id"] == "C") &
            (orders["side"] == "buy") & (orders["status"] == "filled")
        ]
        self.assertTrue(
            imaginary_buy.empty,
            "A 跌停未賣出時，不得使用不存在的 proceeds 買 C",
        )

        filled_sell = orders[
            (orders["date"] == dates[7]) & (orders["stock_id"] == "A") &
            (orders["side"] == "sell") & (orders["status"] == "filled")
        ]
        filled_buy = orders[
            (orders["date"] == dates[7]) & (orders["stock_id"] == "C") &
            (orders["side"] == "buy") & (orders["status"] == "filled")
        ]
        self.assertEqual(len(filled_sell), 1)
        self.assertEqual(len(filled_buy), 1)

        desired_realized = result["summary"]["strategy_position_policy"][
            "desired_realized_audit"]
        self.assertGreaterEqual(desired_realized["limit_down_exit_delays"], 1)

    def test_legacy_picks_path_remains_available_when_policy_is_absent(self):
        dates = list(pd.bdate_range("2026-01-05", periods=9))
        prices = {"A": _flat_prices(dates)}
        picks = {d: [("A", 1.0, "A")] for d in dates[:-1]}
        with (
            mock.patch.object(backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(backtest, "_load_disposition_days", lambda *_a, **_k: {}),
            mock.patch.object(backtest.data, "fetch_price", return_value=prices["A"].copy()),
        ):
            result = backtest.backtest_portfolio(
                symbols=["A"], sample=False, rebalance_every=5, top_n=1,
                picks_by_date=picks, static_universe_comparator=True,
            )
        self.assertIn("summary", result)
        self.assertIn("equity_curve", result)


class StrategyPositionPolicyAvailabilityTest(unittest.TestCase):
    def test_public_policy_module_exists(self):
        self.assertIsNotNone(
            _policy_module,
            "尚未實作 strategies.position_policy；請依 "
            "STRATEGY_POSITION_POLICY_SPEC.md 完成後再跑本測試",
        )
        self.assertTrue(hasattr(_policy_module, "StrategyPositionPolicy"))
        self.assertTrue(hasattr(_policy_module, "StrategyPositionPolicySpec"))


if __name__ == "__main__":
    unittest.main()
