# -*- coding: utf-8 -*-
"""StrategyPositionPolicy 事件引擎整合的回歸測試。

`tests/test_strategy_position_policy_contract.py` 是外部行為契約;這一支釘住的是
實作這條路徑時**必須守住、但契約沒有直接測到**的幾件事:

* legacy `picks_by_date` 路徑在 policy 關閉時完全不受影響(回傳結構不長新 key)。
* `initial_capital` / `order_size_mode` / `minimum_commission` 是 immutable
  request:只影響這一次呼叫,**不寫回全域 config**(規格 §5)。舊做法是就地改
  `config.BT_INITIAL_CAPITAL` 比較 100 萬 / 50 萬情境,兩次執行會互相污染。
* 決策日來自 signal_frame 的快照日期,不是「每 N 個交易日」或星期幾。
* 收盤確認的 hard stop 在**下一個交易日開盤**用實際價成交,跳空時不回填理論停損價。
* 處置期間禁新倉、一字漲停買不到,都必須在 order_log 留下未成交原因。
* risk_off 形成全數退出意圖。
* 單檔超過 single_name_cap 會被修剪到 cap,而不是整筆賣掉。

全部離線:價格用合成資料 mock,不碰網路也不讀 `_cache`。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

import backtest
import config
from strategies.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)


def _flat(dates, price=100.0, overrides=None):
    """全平盤 K 棒;`overrides` 可指定某幾天的 (open, high, low, close)。"""
    overrides = overrides or {}
    rows = []
    for d in dates:
        if d in overrides:
            o, hi, lo, c = overrides[d]
        else:
            o = hi = lo = c = price
            hi, lo = price * 1.01, price * 0.99
        rows.append({"date": d, "open": o, "high": hi, "low": lo, "close": c,
                     "volume": 1_000_000})
    return pd.DataFrame(rows)


def _signals(snapshots):
    rows = []
    for d, ranks in snapshots.items():
        for sid, rank in ranks.items():
            rows.append({"date": d, "stock_id": sid, "rank": int(rank),
                         "raw_score": float(100 - rank), "eligible": True,
                         "snapshot_complete": True})
    return pd.DataFrame(rows)


def _one_slot_policy(**overrides):
    values = {"entry_rank": 1, "exit_rank": 2, "max_slots": 1,
              "slot_weight": 1.0, "single_name_cap": 1.0,
              "risk_on_slots": 1, "caution_slots": 0, "risk_off_slots": 0}
    values.update(overrides)
    return StrategyPositionPolicy(StrategyPositionPolicySpec(**values))


def _run(prices, signals, policy, *, disposition=None, regime_by_date=None,
         initial_capital=1_000_000.0, order_size_mode="odd_lot_proxy",
         minimum_commission=0.0, end_date=None):
    symbols = sorted(prices)
    all_dates = sorted(set().union(*[set(p["date"]) for p in prices.values()]))
    with (
        mock.patch.object(backtest, "_assert_price_integrity", lambda *a, **k: None),
        mock.patch.object(backtest, "_load_disposition_days",
                          lambda *a, **k: dict(disposition or {})),
        mock.patch.object(backtest.data, "fetch_price",
                          side_effect=lambda sid, *a, **k: prices[sid].copy()),
        mock.patch.object(config, "BT_MODEL_LIMIT_LOCK", True),
    ):
        return backtest.backtest_portfolio(
            symbols=symbols, sample=False,
            start_date=str(all_dates[0])[:10],
            end_date=str(end_date or all_dates[-1])[:10],
            signal_frame=signals, strategy_position_policy=policy,
            regime_by_date=regime_by_date,
            initial_capital=initial_capital,
            order_size_mode=order_size_mode,
            minimum_commission=minimum_commission,
            static_universe_comparator=True,
        )


class LegacyParityTest(unittest.TestCase):
    def test_legacy_result_gains_no_new_keys_when_policy_is_off(self):
        """policy 關閉時 legacy 回傳結構必須一個新 key 都不長。

        decision_log / order_log / summary["strategy_position_policy"] 一旦無條件
        出現,既有報告與比對腳本會以為舊結果「少了東西」,而且 summary 的內容變了
        就無法逐位元證明行為沒變。
        """
        dates = list(pd.bdate_range("2026-01-05", periods=9))
        prices = {"A": _flat(dates)}
        picks = {d: [("A", 1.0, "A")] for d in dates[:-1]}
        with (
            mock.patch.object(backtest, "_assert_price_integrity", lambda *a, **k: None),
            mock.patch.object(backtest, "_load_disposition_days", lambda *a, **k: {}),
            mock.patch.object(backtest.data, "fetch_price",
                              return_value=prices["A"].copy()),
        ):
            result = backtest.backtest_portfolio(
                symbols=["A"], sample=False, rebalance_every=5, top_n=1,
                picks_by_date=picks, static_universe_comparator=True)
        self.assertEqual(sorted(result), ["equity_curve", "summary", "trades"])
        self.assertNotIn("strategy_position_policy", result["summary"])
        self.assertNotIn("signal_window", result["summary"]["eval_audit"])

    def test_policy_and_picks_by_date_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            backtest.backtest_portfolio(
                symbols=["A"], sample=False, picks_by_date={"x": []},
                signal_frame=pd.DataFrame({"date": [], "stock_id": [], "rank": []}),
                strategy_position_policy=_one_slot_policy(),
                static_universe_comparator=True)


class ImmutableCapitalRequestTest(unittest.TestCase):
    def test_request_capital_does_not_mutate_global_config(self):
        dates = list(pd.bdate_range("2026-01-05", periods=6))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1}})
        before = (config.BT_INITIAL_CAPITAL, config.BT_ORDER_SIZE_MODE,
                  config.BT_MIN_COMMISSION)
        result = _run(prices, signals, _one_slot_policy(),
                      initial_capital=500_000.0, order_size_mode="regular_lot",
                      minimum_commission=20.0)
        self.assertEqual(
            (config.BT_INITIAL_CAPITAL, config.BT_ORDER_SIZE_MODE,
             config.BT_MIN_COMMISSION), before)
        execution = result["summary"]["execution"]
        self.assertEqual(execution["initial_capital"], 500_000.0)
        self.assertEqual(execution["order_size_mode"], "regular_lot")
        self.assertEqual(execution["minimum_commission"], 20.0)

    def test_two_capital_scenarios_are_independent_in_one_process(self):
        dates = list(pd.bdate_range("2026-01-05", periods=6))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1}})
        big = _run(prices, signals, _one_slot_policy(), initial_capital=1_000_000.0)
        small = _run(prices, signals, _one_slot_policy(), initial_capital=500_000.0)
        self.assertEqual(big["summary"]["execution"]["initial_capital"], 1_000_000.0)
        self.assertEqual(small["summary"]["execution"]["initial_capital"], 500_000.0)
        self.assertGreater(float(big["equity_curve"]["equity"].iloc[-1]),
                           float(small["equity_curve"]["equity"].iloc[-1]))


class DecisionDayAndTimingTest(unittest.TestCase):
    def test_decision_days_come_from_snapshot_dates_not_weekday_math(self):
        """兩個決策日都是週一;用「當週最後交易日」推會算成週五而整段錯位。"""
        dates = list(pd.bdate_range("2026-01-05", periods=10))
        signals = _signals({dates[0]: {"A": 1}, dates[5]: {"A": 1}})
        prices = {"A": _flat(dates)}
        result = _run(prices, signals, _one_slot_policy())
        decisions = pd.DataFrame(result["decision_log"])
        decision_days = sorted(
            decisions.loc[decisions["is_decision_day"], "date"].unique())
        self.assertEqual([pd.Timestamp(x) for x in decision_days],
                         [dates[0], dates[5]])
        self.assertEqual(
            result["summary"]["strategy_position_policy"]["n_decision_days"], 2)

    def test_entry_never_fills_on_the_decision_day_itself(self):
        dates = list(pd.bdate_range("2026-01-05", periods=6))
        signals = _signals({dates[0]: {"A": 1}})
        prices = {"A": _flat(dates)}
        result = _run(prices, signals, _one_slot_policy())
        orders = pd.DataFrame(result["order_log"])
        filled = orders[orders["status"] == "filled"]
        self.assertTrue((filled["date"] > dates[0]).all(),
                        "T 日收盤形成的決策不得在 T 日成交")
        self.assertEqual(pd.Timestamp(filled.iloc[0]["date"]), dates[1])


class RiskStopTest(unittest.TestCase):
    def test_close_confirmed_stop_fills_at_next_open_even_on_a_gap(self):
        """跳空時用實際開盤價,不回填理論停損價(規格 §3.4)。"""
        dates = list(pd.bdate_range("2026-01-05", periods=8))
        # dates[3] 收盤 -10% 觸發停損確認;dates[4] 開盤再跳空到 85。
        prices = {"A": _flat(dates, overrides={
            dates[3]: (99.0, 99.5, 89.0, 90.0),
            dates[4]: (85.0, 86.0, 84.0, 85.5),
            dates[5]: (85.0, 86.0, 84.0, 85.5),
            dates[6]: (85.0, 86.0, 84.0, 85.5),
            dates[7]: (85.0, 86.0, 84.0, 85.5),
        })}
        signals = _signals({dates[0]: {"A": 1}})
        result = _run(prices, signals, _one_slot_policy())
        trades = result["trades"]
        stop = trades[trades["exit_reason"] == "risk_stop"]
        self.assertEqual(len(stop), 1)
        self.assertEqual(pd.Timestamp(stop.iloc[0]["exit_date"]), dates[4])
        self.assertAlmostEqual(float(stop.iloc[0]["exit_price"]), 85.0)

    def test_regime_risk_off_creates_exit_intent_for_every_holding(self):
        dates = list(pd.bdate_range("2026-01-05", periods=8))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1}})
        regimes = {d: ("risk_off" if d >= dates[3] else "risk_on") for d in dates}
        result = _run(prices, signals, _one_slot_policy(), regime_by_date=regimes)
        trades = result["trades"]
        self.assertEqual(list(trades["exit_reason"]), ["regime_reduce"])
        self.assertEqual(pd.Timestamp(trades.iloc[0]["exit_date"]), dates[4])
        policy_meta = result["summary"]["strategy_position_policy"]
        self.assertTrue(policy_meta["regime_pit_provenance"])


class TradabilityAuditTest(unittest.TestCase):
    def test_disposition_blocks_new_entry_and_is_recorded(self):
        dates = list(pd.bdate_range("2026-01-05", periods=6))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1}})
        result = _run(prices, signals, _one_slot_policy(),
                      disposition={"A": set(dates)})
        # 一筆都沒成交也必須留下「為什麼沒成交」——否則只剩一句錯誤字串。
        orders = pd.DataFrame(result["order_log"])
        self.assertTrue((orders["status"] != "filled").all())
        self.assertIn("disposition_no_new_position", set(orders["reason"]))
        audit = result["desired_realized_audit"]
        self.assertGreaterEqual(audit["disposition_entry_blocks"], 1)
        self.assertEqual(audit["n_realized_entries"], 0)

    def test_limit_up_lock_blocks_entry_and_is_recorded(self):
        dates = list(pd.bdate_range("2026-01-05", periods=6))
        # dates[1] 一字漲停(開高低收都是前收 × 1.1)→ 買不到。
        prices = {"A": _flat(dates, overrides={
            dates[1]: (110.0, 110.0, 110.0, 110.0)})}
        signals = _signals({dates[0]: {"A": 1}})
        result = _run(prices, signals, _one_slot_policy())
        orders = pd.DataFrame(result["order_log"])
        blocked = orders[(orders["date"] == dates[1]) &
                         (orders["reason"] == "limit_up_lock")]
        self.assertEqual(len(blocked), 1)
        self.assertNotEqual(blocked.iloc[0]["status"], "filled")
        audit = result["summary"]["strategy_position_policy"]["desired_realized_audit"]
        self.assertGreaterEqual(audit["limit_up_entry_skips"], 1)


class ConcentrationCapTest(unittest.TestCase):
    def test_position_above_cap_is_trimmed_not_liquidated(self):
        dates = list(pd.bdate_range("2026-01-05", periods=14))
        # A 一路上漲 → 權重衝過 single_name_cap;B 平盤當作另一個槽。
        a_rows = {}
        px = 100.0
        for i, d in enumerate(dates):
            if i >= 2:
                px *= 1.08
            a_rows[d] = (round(px, 2), round(px * 1.01, 2),
                         round(px * 0.99, 2), round(px, 2))
        prices = {"A": _flat(dates, overrides=a_rows), "B": _flat(dates)}
        policy = StrategyPositionPolicy(StrategyPositionPolicySpec(
            entry_rank=2, exit_rank=4, max_slots=2, slot_weight=0.40,
            single_name_cap=0.50, risk_on_slots=2, caution_slots=1,
            risk_off_slots=0))
        signals = _signals({dates[0]: {"A": 1, "B": 2},
                            dates[6]: {"A": 1, "B": 2}})
        result = _run(prices, signals, policy)
        orders = pd.DataFrame(result["order_log"])
        trims = orders[(orders["action"] == "resize") &
                       (orders["status"] == "filled")]
        self.assertGreaterEqual(len(trims), 1)
        # 修剪不等於清倉:A 必須還在帳上。
        self.assertGreater(result["summary"]["open_positions_end"], 0)
        self.assertIn("concentration_cap", set(result["trades"]["exit_reason"]))


class SignalFrameValidationTest(unittest.TestCase):
    def test_missing_columns_fail_closed(self):
        with self.assertRaises(ValueError):
            backtest._prepare_signal_snapshots(
                pd.DataFrame({"date": [pd.Timestamp("2026-01-05")],
                              "stock_id": ["A"]}))

    def test_duplicate_date_stock_rows_fail_closed(self):
        frame = pd.DataFrame({
            "date": [pd.Timestamp("2026-01-05")] * 2,
            "stock_id": ["A", "A"], "rank": [1, 5]})
        with self.assertRaises(ValueError):
            backtest._prepare_signal_snapshots(frame)


class EngineFailClosedTest(unittest.TestCase):
    def test_snapshot_date_that_is_not_a_trading_day_fails_closed(self):
        """快照日不是交易日 → 那個決策日會被靜默略過,回測仍會跑完。"""
        dates = list(pd.bdate_range("2026-01-05", periods=8))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1},
                            pd.Timestamp("2026-01-10"): {"A": 1}})  # 週六
        with self.assertRaises(ValueError):
            _run(prices, signals, _one_slot_policy())

    def test_partial_regime_map_fails_closed(self):
        """regime 缺值不得當成 risk_on —— 那是在資料缺口上偷偷恢復滿曝險。"""
        dates = list(pd.bdate_range("2026-01-05", periods=8))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1}})
        with self.assertRaises(ValueError):
            _run(prices, signals, _one_slot_policy(),
                 regime_by_date={dates[0]: "risk_on"})

    def test_market_filter_overlay_and_policy_are_mutually_exclusive(self):
        dates = list(pd.bdate_range("2026-01-05", periods=6))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1}})
        with mock.patch.object(config, "MARKET_FILTER_ENABLED", True):
            with self.assertRaises(ValueError):
                _run(prices, signals, _one_slot_policy())


class PolicyProvenanceTest(unittest.TestCase):
    def test_changing_any_rule_changes_the_rules_hash(self):
        base = StrategyPositionPolicy(StrategyPositionPolicySpec())
        seen = {base.rules_hash()}
        for override in ({"entry_rank": 8}, {"exit_rank": 25},
                         {"max_slots": 8, "risk_on_slots": 8},
                         {"slot_weight": 0.05}, {"single_name_cap": 0.20},
                         {"hard_stop_pct": 0.07}, {"max_hold_days": 60},
                         {"caution_slots": 4}, {"caution_slots": 3}):
            other = StrategyPositionPolicy(StrategyPositionPolicySpec(**override))
            with self.subTest(override=override):
                self.assertNotIn(other.rules_hash(), seen)
                seen.add(other.rules_hash())

    def test_summary_carries_rules_hash_and_capital_scenario(self):
        dates = list(pd.bdate_range("2026-01-05", periods=6))
        prices = {"A": _flat(dates)}
        signals = _signals({dates[0]: {"A": 1}})
        policy = _one_slot_policy()
        result = _run(prices, signals, policy, initial_capital=500_000.0)
        meta = result["summary"]["strategy_position_policy"]
        self.assertEqual(meta["rules_hash"], policy.rules_hash())
        self.assertEqual(meta["capital_scenario"]["initial_capital"], 500_000.0)
        self.assertEqual(meta["capital_scenario"]["source"],
                         "immutable_backtest_request")
        # 候選池是呼叫端給的 legacy 對照組 → 不得被標成可作正式證據。
        self.assertFalse(result["summary"]["universe"]["formal_evidence_eligible"])


if __name__ == "__main__":
    unittest.main()
