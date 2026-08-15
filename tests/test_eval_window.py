# -*- coding: utf-8 -*-
"""評估窗防護:注入 picks 時,回測不得在訊號用完後繼續計績效。

這是 2026-08-03 實際踩到的 bug —— `run_once` 只限制了 picks 的日期範圍,
但引擎的 all_dates 取自價格快取、沒有上界,結果 IS 的權益曲線跑超出切點
144 天,把 OS 段的 +87.2% 算進「IS Sharpe」(1.607 vs 真實的 0.306)。
用它選出來的參數也連帶失效。這裡把防護釘死。
"""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np
import pandas as pd

import backtest
import config
from _offline_registry import use_common_stocks


def _prices(sids=("1101", "1102", "1103"), n=200, seed=3):
    """建價格快取:前半段平盤、後半段大漲(放大洩漏的可見度)。"""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-01-01", periods=n)
    out = {}
    for k, sid in enumerate(sids):
        px, rows = 100.0, []
        for i, d in enumerate(days):
            drift = 0.0002 if i < n // 2 else 0.010     # 後半段強多頭
            px *= 1 + drift + rng.normal(0, 0.005)
            rows.append({"date": d, "open": px, "high": px * 1.01,
                         "low": px * 0.99, "close": px, "volume": 1e6})
        df = pd.DataFrame(rows)
        df["ma_exit"] = df["close"].rolling(config.BT_MA_EXIT).mean()
        out[sid] = df
    return out, list(days)


class EvalWindowTest(unittest.TestCase):
    def setUp(self):
        # 外部 picks 路徑有證券別閘門(fail-closed),測試代號要顯式宣告證券別。
        use_common_stocks(self, "1101", "1102", "1103")
        self.cache, self.days = _prices()
        self.cut = self.days[len(self.days) // 2]
        # 訊號只覆蓋前半段
        self.picks = {d: [("1101", 1.0, "1101"), ("1102", 0.9, "1102"), ("1103", 0.8, "1103")]
                      for d in self.days if d <= self.cut}

    def _run(self, **kw):
        with (
            mock.patch.object(backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(backtest, "_load_disposition_days", lambda *_a, **_k: {}),
            mock.patch.object(backtest.data, "fetch_price",
                              side_effect=lambda s, *a, **k: self.cache[s].copy()),
        ):
            return backtest.backtest_portfolio(
                symbols=list(self.cache), sample=False, rebalance_every=20,
                top_n=3, picks_by_date=self.picks, **kw)

    def test_default_stops_at_last_pick(self):
        """預設:評估窗不得超過最後一個訊號日。"""
        r = self._run()
        a = r["summary"]["eval_audit"]
        self.assertEqual(a["days_beyond_last_pick"], 0,
                         "訊號用完後仍在計績效 —— 這正是 IS 洩漏的形態")
        self.assertEqual(a["eval_window"][1], str(self.cut)[:10])
        self.assertEqual(a["picks_window"][1], str(self.cut)[:10])

    def test_explicit_end_date_is_respected(self):
        earlier = self.days[len(self.days) // 4]
        r = self._run(end_date=earlier)
        self.assertEqual(r["summary"]["eval_audit"]["eval_window"][1], str(earlier)[:10])

    def test_let_positions_run_is_opt_in_and_flagged(self):
        """顯式要求時才可跑過訊號末端,而且要在 audit 裡標示。"""
        r = self._run(let_positions_run=True)
        a = r["summary"]["eval_audit"]
        self.assertTrue(a["let_positions_run"])
        self.assertGreater(a["days_beyond_last_pick"], 0)

    def test_leak_would_inflate_sharpe(self):
        """證明這道防護有實質作用:放行洩漏會讓 Sharpe 明顯變高。

        測試資料的後半段是強多頭,若評估窗溢出,Sharpe 會被那段拉上去 ——
        這就是 S19 從 0.306 被灌到 1.607 的機制。
        """
        clean = self._run()["summary"]["sharpe"]
        leaked = self._run(let_positions_run=True)["summary"]["sharpe"]
        self.assertGreater(leaked, clean,
                           "若兩者相同,代表測試資料沒造出洩漏效果,測試本身失效")

    def test_stale_position_without_delisting_data_fails_closed(self):
        """下市/長停牌不能用最後收盤假裝可成交，否則會低估 long-only 左尾。"""
        self.cache["1101"] = self.cache["1101"].iloc[:40].copy()
        with mock.patch.object(config, "BT_DELIST_RECOVERY", None):
            with self.assertRaisesRegex(RuntimeError, "拒絕假設可用最後收盤"):
                self._run()

    def test_explicit_delisting_recovery_is_audited(self):
        self.cache["1101"] = self.cache["1101"].iloc[:40].copy()
        with mock.patch.object(config, "BT_DELIST_RECOVERY", 0.0):
            r = self._run()
        self.assertEqual(r["summary"]["delisting"]["recovery_assumption"], 0.0)
        self.assertEqual(r["summary"]["delisting"]["n_stale_exits"], 1)

    def test_rebalance_phase_is_explicit_and_changes_path(self):
        phase0 = self._run(rebalance_phase=0)
        phase1 = self._run(rebalance_phase=1)
        self.assertEqual(phase0["summary"]["params"]["rebalance_phase"], 0)
        self.assertEqual(phase1["summary"]["params"]["rebalance_phase"], 1)
        self.assertNotEqual(
            phase0["trades"].iloc[0]["entry_date"],
            phase1["trades"].iloc[0]["entry_date"],
        )

    def test_invalid_rebalance_phase_fails(self):
        with self.assertRaises(ValueError):
            self._run(rebalance_phase=20)


if __name__ == "__main__":
    unittest.main()
