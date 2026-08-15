# -*- coding: utf-8 -*-
"""S19 策略單元測試:鎖因果性與研究過程中實際踩過的坑(全離線)。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import chip_momentum_strategy as s19
from _offline_registry import use_common_stocks


def _panel(n_days=90, sids=("1101", "1102", "1103"), seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.bdate_range("2025-01-01", periods=n_days)
    for k, sid in enumerate(sids):
        px = 100.0
        for i, d in enumerate(dates):
            px *= 1.0 + rng.normal(0.001 * (k + 1), 0.02)
            rows.append({
                "date": d, "stock_id": sid, "name": f"N{sid}",
                "close": px, "volume": 1e6 * (k + 1),
                "foreign_net": rng.normal(1e4 * (k + 1), 5e3),
                "trust_net": rng.normal(5e3, 2e3),
                "in_dynamic_universe": True, "trend_ok": True,
            })
    return pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)


class SignalCausalityTest(unittest.TestCase):
    def test_future_rows_do_not_change_past_signal(self):
        """加上未來的列,不得改變過去任何一天的訊號值(前視檢查)。"""
        base = _panel(n_days=80)
        extended = pd.concat([base, _panel(n_days=100).query("date > @base.date.max()")],
                             ignore_index=True)
        extended = extended.sort_values(["date", "stock_id"]).reset_index(drop=True)

        s_base = s19.build_signal(base)
        s_ext = s19.build_signal(extended)

        key_base = base[["date", "stock_id"]].copy(); key_base["s"] = s_base.values
        key_ext = extended[["date", "stock_id"]].copy(); key_ext["s"] = s_ext.values
        merged = key_base.merge(key_ext, on=["date", "stock_id"], suffixes=("_b", "_e"))
        both = merged.dropna(subset=["s_b", "s_e"])
        self.assertGreater(len(both), 0, "應有可比對的重疊列")
        np.testing.assert_allclose(both["s_b"].values, both["s_e"].values, rtol=1e-9)

    def test_signal_needs_dense_panel(self):
        """稀疏 panel(只留成員日)會讓 ts_ 橫跨遠超過視窗的日曆期間 → 值不同。

        這是研究時真的踩到的坑:第一版 panel 用預設 keep_non_members=False,
        `ts_ir(...,20)` 實際算的是「20 列」而非「20 交易日」。
        """
        dense = _panel(n_days=90)
        sparse = dense.iloc[::3].reset_index(drop=True)   # 模擬間歇成員
        sd = s19.build_signal(dense)
        ss = s19.build_signal(sparse)
        d_last = pd.DataFrame({"date": dense["date"], "sid": dense["stock_id"], "v": sd.values})
        s_last = pd.DataFrame({"date": sparse["date"], "sid": sparse["stock_id"], "v": ss.values})
        m = d_last.merge(s_last, on=["date", "sid"], suffixes=("_d", "_s")).dropna()
        self.assertGreater(len(m), 5)
        # 兩者不應相同 —— 若相同代表稀疏性沒被正確反映,測試本身失效
        self.assertFalse(np.allclose(m["v_d"].values, m["v_s"].values, rtol=1e-6))


class PicksGateTest(unittest.TestCase):
    def test_gates_exclude_non_members_and_non_trend(self):
        p = _panel(n_days=40)
        p.loc[p["stock_id"] == "1101", "in_dynamic_universe"] = False
        p.loc[p["stock_id"] == "1102", "trend_ok"] = False
        picks = s19.build_picks(p, s19.build_signal(p))
        chosen = {sid for lst in picks.values() for sid, _, _ in lst}
        self.assertNotIn("1101", chosen)
        self.assertNotIn("1102", chosen)
        self.assertIn("1103", chosen)

    def test_picks_sorted_descending(self):
        p = _panel(n_days=40)
        picks = s19.build_picks(p, s19.build_signal(p))
        for lst in picks.values():
            scores = [x[1] for x in lst]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_phase_shifts_rebalance_days(self):
        p = _panel(n_days=40)
        sig = s19.build_signal(p)
        p0 = s19.build_picks(p, sig, phase=0)
        p2 = s19.build_picks(p, sig, phase=2)
        self.assertEqual(len(p0) - 2, len(p2))
        self.assertEqual(sorted(p0)[2], sorted(p2)[0])


class BaselineTest(unittest.TestCase):
    def test_baseline_is_gap_safe(self):
        """成員進出造成的日期斷點不得被當成單日巨幅報酬。

        踩過的坑:先篩成員再算 pct_change,會把「離開 universe 期間的累積漲幅」
        壓成一天,把基準年化灌到 +1150%。正確作法是先算報酬再篩成員。
        """
        p = _panel(n_days=60)
        # 讓 A 中間有一段不是成員(價格照樣連續上漲)
        mid = p["date"].between(p["date"].quantile(0.3), p["date"].quantile(0.7))
        p.loc[(p["stock_id"] == "1101") & mid, "in_dynamic_universe"] = False
        b = s19.equal_weight_baseline(p)
        self.assertIn("sharpe", b)
        # 隨機漫步 + 小漂移不可能出現這種年化
        self.assertLess(abs(b["ann_ret"]), 5.0, f"基準年化異常:{b}")

    def test_baseline_matches_engine_convention(self):
        """必須用算術年化(mean*252 / std*sqrt(252)),與 backtest 引擎一致。"""
        p = _panel(n_days=80)
        b = s19.equal_weight_baseline(p)
        full = p.sort_values(["stock_id", "date"]).copy()
        full["r"] = full.groupby("stock_id")["close"].pct_change()
        eq = full[full["in_dynamic_universe"] == True].groupby("date")["r"].mean().dropna()  # noqa: E712
        self.assertAlmostEqual(b["ann_ret"], eq.mean() * 252, places=9)
        self.assertAlmostEqual(b["ann_vol"], eq.std(ddof=1) * np.sqrt(252), places=9)


class FrozenParamsTest(unittest.TestCase):
    def test_portfolio_config_is_restored_after_run(self):
        """run_once 會暫改 config,必須還原,否則污染同一 process 的其他回測。"""
        import config
        # run_once 走外部 picks 路徑,證券別閘門 fail-closed → 代號要顯式宣告。
        use_common_stocks(self, "1101", "1102", "1103")
        p = _panel(n_days=30)
        before = (config.BT_MAX_POSITIONS, config.BT_MA_EXIT, config.BT_TREND_STOP_LOSS)
        s19.run_once(p, s19.build_signal(p), symbols=[], start=None, end=None)
        after = (config.BT_MAX_POSITIONS, config.BT_MA_EXIT, config.BT_TREND_STOP_LOSS)
        self.assertEqual(before, after)

    def test_low_turnover_params_are_frozen(self):
        """凍結參數若被改動,IS 的 Sharpe 結論即失效 —— 用測試釘住。"""
        self.assertEqual(s19.PORT_MAX_POSITIONS, 10)
        self.assertEqual(s19.PORT_REBALANCE_DAYS, 20)
        self.assertEqual(s19.PORT_MA_EXIT, 60)
        self.assertEqual(s19.PORT_STOP_LOSS, 0.15)


if __name__ == "__main__":
    unittest.main()
