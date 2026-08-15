# -*- coding: utf-8 -*-
"""訊號快照「完整性」與「單一快照日」的回歸測試(2026-08-15 修正)。

這支測試釘住兩個獨立審查者用實際重現找出來的 P0 缺陷。兩者是同一個病灶的兩面:
policy 把「我今天沒看到這一列」當成「這檔已經掉出排名母體」的證據。

**原 bug 1 — `snapshot_complete` 缺省值是 True**
(`strategies/position_policy.py` 舊版第 255 行 `snapshot_complete = True`,只有
signal frame 帶了 `snapshot_complete` 欄才會變)。重現:持有 B、今天的訊號只有 A、
frame 沒有完整性旗標 → B 被判 `exit / not_ranked` 賣掉。規格 §5 要求這種情況視為
unknown、不得賣出。空表與「截至 as_of 沒有任何快照」是同一個 bug 的另一個出口
(舊版直接 `return (..., True)`)。這會直接改變換股次數、成本與績效。

**原 bug 2 — 多日 signals 取的是「每檔各自的最新列」**
(舊版 `sort_values("_asof").drop_duplicates(subset=["stock_id"], keep="last")`)。
重現:兩個快照日,某檔只出現在較舊那天 → 它會沿用舊快照的 rank 繼續被當成今天的
有效訊號,可能被當成 top-10 買進,也會因為「還在名單裡」而躲掉 `not_ranked`,而且
輸出裡完全看不出那個 rank 是舊的。正確語意是:只用截至 as_of 的**最新一個快照日**
的列,跨快照日合併 rank 一律禁止(規格 §9B)。

全部離線,價格用合成資料 mock。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

import backtest
import config
from _offline_registry import common_stocks
from strategies.position_policy import (
    StrategyPositionPolicy,
    StrategyPositionPolicySpec,
)


def _policy(**overrides):
    return StrategyPositionPolicy(StrategyPositionPolicySpec(**overrides))


def _signals(ranks, *, complete=None, date=None):
    """單一快照日的訊號表;`complete=None` 代表**不帶**完整性旗標。"""
    rows = []
    for sid, rank in ranks.items():
        row = {"stock_id": sid, "rank": int(rank),
               "raw_score": float(100 - rank), "eligible": True}
        if complete is not None:
            row["snapshot_complete"] = bool(complete)
        if date is not None:
            row["date"] = pd.Timestamp(date)
        rows.append(row)
    return pd.DataFrame(rows)


def _holdings(rows):
    return pd.DataFrame([
        {"stock_id": sid, "weight": float(w), "entry_price": float(e),
         "close": float(c), "holding_days": int(d)}
        for sid, w, e, c, d in rows
    ], columns=["stock_id", "weight", "entry_price", "close", "holding_days"])


def _decide(signals, holdings=(), *, as_of="2026-01-09", policy=None,
            is_decision_day=True):
    policy = policy or _policy()
    return policy.decide(
        as_of=pd.Timestamp(as_of), signals=signals,
        holdings=_holdings(holdings), equity=1_000_000.0,
        regime="risk_on", is_decision_day=bool(is_decision_day))


def _actions(decision):
    return decision.actions.set_index("stock_id")


class SnapshotCompletenessDefaultTest(unittest.TestCase):
    """缺少 `snapshot_complete` 欄 = 完整性未知 = False,不是 True。"""

    def test_missing_flag_reports_snapshot_incomplete(self):
        d = _decide(_signals({"1101": 1, "1102": 2}))
        self.assertIs(d.snapshot_complete, False)

    def test_missing_flag_must_not_sell_holding_absent_from_snapshot(self):
        """原 bug 的直接重現:B 不在今天的訊號裡就被 not_ranked 賣掉。"""
        d = _decide(_signals({"1101": 1}),
                    holdings=(("1102", 0.10, 100.0, 101.0, 20),))
        acts = _actions(d)
        self.assertEqual(acts.loc["1102", "action"], "hold")
        self.assertNotIn("1102", d.exits())
        self.assertNotIn("not_ranked", set(d.actions["reason_code"]))

    def test_declared_complete_snapshot_still_exits_by_not_ranked(self):
        """明確宣告完整時,退出規則必須照舊生效(修正沒有把功能關掉)。"""
        d = _decide(_signals({"1101": 1}, complete=True),
                    holdings=(("1102", 0.10, 100.0, 101.0, 20),))
        acts = _actions(d)
        self.assertEqual(acts.loc["1102", "action"], "exit")
        self.assertEqual(acts.loc["1102", "reason_code"], "not_ranked")
        self.assertIs(d.snapshot_complete, True)

    def test_flag_false_on_any_row_makes_whole_snapshot_incomplete(self):
        signals = _signals({"1101": 1, "1102": 2}, complete=True)
        signals.loc[signals["stock_id"] == "1102", "snapshot_complete"] = False
        d = _decide(signals, holdings=(("1103", 0.10, 100.0, 101.0, 20),))
        self.assertIs(d.snapshot_complete, False)
        self.assertEqual(_actions(d).loc["1103", "action"], "hold")

    def test_empty_signal_frame_is_incomplete_and_keeps_holdings(self):
        """舊版空表直接 `return (..., True)`,等於一次把整個組合清空。"""
        empty = pd.DataFrame(columns=["stock_id", "rank", "raw_score", "eligible"])
        d = _decide(empty, holdings=(("1101", 0.10, 100.0, 101.0, 20),))
        self.assertIs(d.snapshot_complete, False)
        self.assertEqual(_actions(d).loc["1101", "action"], "hold")
        self.assertEqual(d.exits(), {})

    def test_no_snapshot_on_or_before_as_of_is_incomplete(self):
        """訊號全在未來 → 截至 as_of 沒有任何有效快照,同樣是 unknown。"""
        future = _signals({"1101": 1}, complete=True, date="2026-02-01")
        d = _decide(future, holdings=(("1102", 0.10, 100.0, 101.0, 20),),
                    as_of="2026-01-09")
        self.assertIs(d.snapshot_complete, False)
        self.assertEqual(_actions(d).loc["1102", "action"], "hold")
        self.assertTrue(d.targets["target_weight"].le(0.10 + 1e-12).all())

    def test_none_signals_is_incomplete(self):
        d = _decide(None, holdings=(("1101", 0.10, 100.0, 101.0, 20),))
        self.assertIs(d.snapshot_complete, False)
        self.assertEqual(_actions(d).loc["1101", "action"], "hold")

    def test_incompleteness_does_not_suppress_risk_exits(self):
        """unknown 只擋「因為沒看到而賣」,不擋停損等每日強制退出。"""
        d = _decide(_signals({"1101": 1}),
                    holdings=(("1102", 0.10, 100.0, 91.0, 5),),
                    is_decision_day=False)
        acts = _actions(d)
        self.assertEqual(acts.loc["1102", "action"], "exit")
        self.assertEqual(acts.loc["1102", "reason_code"], "risk_stop")


class LatestSnapshotDayOnlyTest(unittest.TestCase):
    """多個快照日時只採用截至 as_of 的最新那一天,不跨快照日合併 rank。"""

    def _two_snapshots(self, old_ranks, new_ranks, *, complete=None):
        return pd.concat([
            _signals(old_ranks, complete=complete, date="2026-01-02"),
            _signals(new_ranks, complete=complete, date="2026-01-09"),
        ], ignore_index=True)

    def test_stock_only_in_older_snapshot_is_not_a_valid_candidate(self):
        """舊版會沿用 STALE 在上一個快照的 rank,把它當成今天的 top-10 買進。"""
        signals = self._two_snapshots({"1101": 1, "STALE": 2}, {"1101": 1},
                                      complete=True)
        d = _decide(signals, as_of="2026-01-09")
        entered = set(d.actions.loc[d.actions["action"] == "enter", "stock_id"])
        self.assertEqual(entered, {"1101"})
        self.assertNotIn("STALE", d.target_map())

    def test_stale_rank_does_not_shield_holding_from_not_ranked(self):
        """已持有的 STALE 掉出最新快照 → 完整語意下必須 not_ranked 退出。"""
        signals = self._two_snapshots({"1101": 1, "STALE": 2}, {"1101": 1},
                                      complete=True)
        d = _decide(signals, holdings=(("STALE", 0.10, 100.0, 101.0, 20),),
                    as_of="2026-01-09")
        acts = _actions(d)
        self.assertEqual(acts.loc["STALE", "action"], "exit")
        self.assertEqual(acts.loc["STALE", "reason_code"], "not_ranked")

    def test_stale_holding_is_not_sold_when_completeness_unknown(self):
        """A1 與 A2 疊在一起:掉出最新快照 + 沒有完整性旗標 → 不得賣。"""
        signals = self._two_snapshots({"1101": 1, "STALE": 2}, {"1101": 1})
        d = _decide(signals, holdings=(("STALE", 0.10, 100.0, 101.0, 20),),
                    as_of="2026-01-09")
        self.assertIs(d.snapshot_complete, False)
        acts = _actions(d)
        self.assertEqual(acts.loc["STALE", "action"], "hold")
        self.assertNotIn("STALE", d.exits())

    def test_completeness_comes_from_latest_snapshot_only(self):
        """較舊快照宣告完整,不能替最新那個沒宣告的快照背書。"""
        signals = pd.concat([
            _signals({"1101": 1, "1102": 2}, complete=True, date="2026-01-02"),
            _signals({"1101": 1}, date="2026-01-09"),
        ], ignore_index=True)
        d = _decide(signals, holdings=(("1102", 0.10, 100.0, 101.0, 20),),
                    as_of="2026-01-09")
        self.assertIs(d.snapshot_complete, False)
        self.assertEqual(_actions(d).loc["1102", "action"], "hold")

    def test_latest_snapshot_rank_wins_over_older_snapshot_rank(self):
        signals = self._two_snapshots({"1101": 1, "1102": 2}, {"1101": 25, "1102": 1},
                                      complete=True)
        d = _decide(signals, holdings=(("1101", 0.10, 100.0, 101.0, 20),),
                    as_of="2026-01-09")
        acts = _actions(d)
        self.assertEqual(acts.loc["1101", "action"], "exit")
        self.assertEqual(acts.loc["1101", "reason_code"], "rank_decay")
        self.assertEqual(float(acts.loc["1101", "decision_rank"]), 25.0)

    def test_future_snapshot_does_not_change_past_decision(self):
        signals = pd.concat([
            _signals({"1101": 1, "1102": 2}, complete=True, date="2026-01-09"),
            _signals({"1101": 30, "1102": 31}, complete=True, date="2026-02-01"),
        ], ignore_index=True)
        d = _decide(signals, as_of="2026-01-09")
        self.assertEqual(set(d.target_map()), {"1101", "1102"})
        self.assertEqual(
            float(_actions(d).loc["1101", "decision_rank"]), 1.0)

    def test_duplicate_stock_in_one_snapshot_day_fails_closed(self):
        """同一快照日兩個 rank:舊版靜默取最後一列,決策取決於列順序。"""
        signals = pd.concat([
            _signals({"1101": 1}, complete=True, date="2026-01-09"),
            _signals({"1101": 9}, complete=True, date="2026-01-09"),
        ], ignore_index=True)
        with self.assertRaises(ValueError):
            _decide(signals, as_of="2026-01-09")


def _flat_prices(dates, price=100.0):
    return pd.DataFrame([
        {"date": d, "open": price, "high": price * 1.01, "low": price * 0.99,
         "close": price, "volume": 1_000_000}
        for d in dates
    ])


class EngineSnapshotCompletenessTest(unittest.TestCase):
    """引擎路徑:signal_frame 沒宣告完整性時,消失的持股不得被自動賣掉。"""

    def _run(self, signals, prices):
        with (
            # signal_frame 也過證券別閘門(fail-closed),測試代號要宣告證券別。
            common_stocks(*sorted(prices)),
            mock.patch.object(backtest, "_assert_price_integrity",
                              lambda *a, **k: None),
            mock.patch.object(backtest, "_load_disposition_days",
                              lambda *a, **k: {}),
            mock.patch.object(backtest.data, "fetch_price",
                              side_effect=lambda sid, *a, **k: prices[sid].copy()),
            mock.patch.object(config, "BT_MODEL_LIMIT_LOCK", True),
        ):
            return backtest.backtest_portfolio(
                symbols=sorted(prices), sample=False,
                start_date=str(min(df["date"].min() for df in prices.values()))[:10],
                end_date=str(max(df["date"].max() for df in prices.values()))[:10],
                signal_frame=signals,
                strategy_position_policy=_policy(),
                initial_capital=1_000_000.0,
                order_size_mode="odd_lot_proxy",
                minimum_commission=0.0,
                static_universe_comparator=True,
            )

    def _signal_frame(self, dates, *, complete):
        rows = []
        for d, ranks in ((dates[0], {"1101": 1, "1102": 2}), (dates[5], {"1101": 1})):
            frame = _signals(ranks, complete=complete, date=d)
            rows.append(frame)
        return pd.concat(rows, ignore_index=True)

    def test_undeclared_completeness_keeps_the_vanished_holding(self):
        dates = list(pd.bdate_range("2026-01-05", periods=9))
        prices = {sid: _flat_prices(dates) for sid in ("1101", "1102")}
        result = self._run(self._signal_frame(dates, complete=None), prices)

        orders = pd.DataFrame(result["order_log"])
        filled_buys = orders[(orders["side"] == "buy") &
                             (orders["status"] == "filled")]
        # 先確認 B 真的買到了,否則「沒有賣出」是空跑出來的。
        self.assertIn("1102", set(filled_buys["stock_id"]))
        self.assertNotIn(
            "1102", set(orders.loc[orders["side"] == "sell", "stock_id"]),
            "沒有宣告完整性的快照不得因為 B 消失就送出賣單")
        trades = result["trades"]
        reasons = set(trades["exit_reason"]) if not trades.empty else set()
        self.assertNotIn("not_ranked", reasons)
        policy_summary = result["summary"]["strategy_position_policy"]
        self.assertFalse(policy_summary["snapshot_complete_all_days"])

    def test_declared_completeness_exits_the_vanished_holding(self):
        dates = list(pd.bdate_range("2026-01-05", periods=9))
        prices = {sid: _flat_prices(dates) for sid in ("1101", "1102")}
        result = self._run(self._signal_frame(dates, complete=True), prices)

        trades = result["trades"]
        b_exit = trades[(trades["stock_id"] == "1102") &
                        (trades["exit_reason"] == "not_ranked")]
        self.assertEqual(len(b_exit), 1)
        self.assertEqual(pd.Timestamp(b_exit.iloc[0]["exit_date"]), dates[6])
        policy_summary = result["summary"]["strategy_position_policy"]
        self.assertTrue(policy_summary["snapshot_complete_all_days"])


if __name__ == "__main__":
    unittest.main()
