# -*- coding: utf-8 -*-
"""兩個引擎的對拍:**量出差距,不斷言相等**。

`vec_backtest` 刻意丟掉 T+1 成交、漲跌停、處置、整股與現金帳,所以它**不可能**
等於事件引擎。要求相等就只能靠放寬容差來通過,那等於沒測。

這裡釘住的是三件真正會讓它變危險的事:

1. **它不能冒充正式證據**(`engine` 標記與 `formal_evidence_eligible=False`)。
2. **它不能偷偷少跑相位** —— 只報一個相位等於挑路徑,那是兩個引擎共同的鐵則。
3. **它的相位定義必須與事件引擎逐值相同**;否則對拍比的是兩件不同的事,
   而差距會被歸因到「近似」,實際上是「切在不同天」。

真正的差距分布由 `runs`/研究層在真實資料上量(離線測試沒有真實 panel),
量出來的結果要寫進報告 —— 這支只保證「量的時候比的是同一個東西」。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from backtest import event_backtest, vec_backtest as vb


def _signals(n_days: int = 60, n_stocks: int = 30) -> pd.DataFrame:
    days = pd.bdate_range("2026-01-05", periods=n_days)
    rng = np.random.default_rng(7)
    rows = []
    for d in days:
        score = rng.normal(0, 1, n_stocks)
        order = np.argsort(-score)
        for rank, j in enumerate(order, 1):
            rows.append({"date": d, "stock_id": f"S{j:02d}", "rank": rank,
                         "raw_score": float(score[j])})
    return pd.DataFrame(rows)


def _panel(sig: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    out = []
    for sid, g in sig.groupby("stock_id"):
        d = sorted(g["date"].unique())
        px = 100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.02, len(d)))
        out.append(pd.DataFrame({"date": d, "stock_id": sid, "close": px}))
    return pd.concat(out, ignore_index=True)


class VecEngineContractTest(unittest.TestCase):
    def setUp(self):
        self.sig = _signals()
        self.pan = _panel(self.sig)
        self.res = vb.vec_backtest(signal_frame=self.sig, panel=self.pan)

    def test_result_cannot_masquerade_as_formal_evidence(self):
        """近似引擎的輸出必須自帶標記,而且不得可被標成正式證據。"""
        self.assertEqual(self.res["engine"], "vectorized_approximate")
        self.assertFalse(self.res["formal_evidence_eligible"])
        self.assertIn("no_t1_fill_simulation", self.res["approximations"])
        self.assertIn("向量化近似", self.res["claim_boundary"])

    def test_runs_every_equivalent_phase(self):
        """只報一個相位等於挑路徑 —— 這條對兩個引擎一視同仁。"""
        self.assertEqual(len(self.res["phase_results"]), 5)
        self.assertEqual(self.res["phase_stats"]["n_phases"], 5)
        for k in ("sharpe_median", "sharpe_min", "worst_max_drawdown"):
            self.assertIn(k, self.res["phase_stats"])
        # 報最小值而不是最大值:實際下單只會走其中一條路徑
        self.assertLessEqual(self.res["phase_stats"]["sharpe_min"],
                             self.res["phase_stats"]["sharpe_median"])

    def test_phase_definition_matches_the_event_engine(self):
        """相位切在哪一天必須與事件引擎逐值相同,否則對拍沒有意義。"""
        days = sorted(self.sig["date"].unique())
        for phase in range(5):
            with self.subTest(phase=phase):
                mine = vb._weekly_phase_days(days, phase)
                theirs = event_backtest.select_decision_snapshots(
                    days, decision_frequency="weekly", phase=phase)
                self.assertEqual([pd.Timestamp(x) for x in mine],
                                 [pd.Timestamp(x) for x in theirs])

    def test_hysteresis_band_is_honoured(self):
        """進 top entry_rank 才買、掉出 exit_rank 才賣 —— H4 的主要出場來源。"""
        tight = vb.vec_backtest(signal_frame=self.sig, panel=self.pan,
                                entry_rank=5, exit_rank=5, max_slots=5)
        loose = vb.vec_backtest(signal_frame=self.sig, panel=self.pan,
                                entry_rank=5, exit_rank=25, max_slots=5)
        # 沒有 hysteresis(entry==exit)一定換得更兇
        self.assertGreater(tight["turnover"], loose["turnover"])

    def test_costs_reduce_return_monotonically(self):
        free = vb.vec_backtest(signal_frame=self.sig, panel=self.pan,
                               cost_one_way=0.0, sell_tax=0.0)
        paid = vb.vec_backtest(signal_frame=self.sig, panel=self.pan,
                               cost_one_way=0.000399, sell_tax=0.003)
        self.assertLess(paid["cum_return"], free["cum_return"])

    def test_is_fast_enough_to_be_a_screen(self):
        """篩子的意義在於快;慢到和事件引擎同級就沒有存在價值。"""
        import time
        t0 = time.time()
        vb.vec_backtest(signal_frame=self.sig, panel=self.pan)
        self.assertLess(time.time() - t0, 5.0)


if __name__ == "__main__":
    unittest.main()
