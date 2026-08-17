# -*- coding: utf-8 -*-
"""S19 的 cs_ 排名母體必須是「當日 eligible universe」(spec §3.1)。

不變式原文:非成員股票可以貢獻自身時間序列歷史,但**不得改變正式可選股票當日的
cross-sectional rank**。

2026-08-16 稽核發現現況違反它:`build_signal` 對整個稠密 panel 做 cs_rank,
而稠密 panel 有 86.7% 的列是非成員。實測(PIT top100 池、463 個決策日、
348,493 列):top10 有 **76.7%** 的日子會不同,平均重疊率 86.9% ——
一檔可買股票的分數取決於一堆當天不能買的股票。

單因子不受影響(rank 是同日單調轉換,先排後濾與先濾後排順序相同);
S19 是兩個 cs_rank 的**加權組合**,非成員在兩個因子的分布位置不同,
會不對稱地扭曲組合順序。這支測試就是釘住這件事。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import strategies.s19_chip_momentum as s19
from strategy_kit.spec import StrategySpec


def _panel(n_days: int = 40) -> pd.DataFrame:
    """兩檔成員 + 兩檔非成員;非成員在兩個因子上的位置刻意相反。"""
    days = pd.bdate_range("2026-01-05", periods=n_days)
    rows = []
    rng = np.random.default_rng(7)
    profile = {
        # sid: (報酬漂移, 法人流量, 是否為當日成員)
        "M1": (0.004, 5_000.0, True),
        "M2": (0.003, 9_000.0, True),
        "X1": (0.020, -9_000.0, False),   # 動能極高、流量極差
        "X2": (-0.015, 20_000.0, False),  # 動能極差、流量極好
    }
    for sid, (drift, flow, member) in profile.items():
        px = 100.0
        for d in days:
            px *= 1.0 + drift + rng.normal(0, 0.002)
            rows.append({
                "date": d, "stock_id": sid, "close": px,
                "volume": 1_000_000.0, "turnover": px * 1_000_000.0,
                "foreign_net": flow, "trust_net": flow / 2.0,
                "in_dynamic_universe": member,
            })
    return pd.DataFrame(rows).sort_values(["stock_id", "date"]).reset_index(drop=True)


def _spec(scope: str) -> StrategySpec:
    sig = dict(s19.SPEC.rules()["signal"])
    sig["ranking_universe"] = scope
    sig["mom_window"] = 10
    sig["flow_window"] = 10
    sig["vol_window"] = 10
    return StrategySpec(
        name=s19.SPEC.name, signal=sig,
        portfolio=dict(s19.SPEC.rules()["portfolio"]),
        required_signal=s19.SPEC.required_signal,
        required_portfolio=s19.SPEC.required_portfolio,
    )


class RankingUniverseTest(unittest.TestCase):
    def test_default_is_the_spec_compliant_eligible_universe(self):
        self.assertEqual(s19.SPEC.sig("ranking_universe"), "eligible")

    def test_it_is_part_of_the_frozen_rules(self):
        """排名母體會改變選股 → 必須進 rules hash,否則 forward 驗的是另一套規則。"""
        self.assertIn("ranking_universe", s19.SPEC.rules()["signal"])
        self.assertIn("ranking_universe", s19.SPEC.required_signal)

    def test_non_members_get_no_score_under_the_eligible_scope(self):
        panel = _panel()
        score = s19.build_signal(panel, spec=_spec("eligible"))
        non_member = ~panel["in_dynamic_universe"]
        self.assertTrue(score[non_member].isna().all(),
                        "非成員不進當日母體,也不該拿到分數")

    def test_non_members_change_member_ranks_under_the_panel_scope(self):
        """釘住缺陷本身:舊行為下,不能買的股票會改變可買股票的分數。"""
        panel = _panel()
        elig = s19.build_signal(panel, spec=_spec("eligible"))
        allp = s19.build_signal(panel, spec=_spec("panel"))
        mask = panel["in_dynamic_universe"] & elig.notna() & allp.notna()
        self.assertTrue(mask.any())
        self.assertFalse(
            np.allclose(elig[mask].values, allp[mask].values),
            "兩種母體對成員股票應該給出不同分數(這正是 2026-08-16 稽核的發現)")

    def test_unknown_scope_fails_closed(self):
        with self.assertRaises(ValueError):
            s19.build_signal(_panel(), spec=_spec("whatever"))

    def test_panel_scope_still_works_when_membership_column_is_absent(self):
        """研究腳本可能餵沒有成員欄的 panel;此時只能退回 panel 母體,不得炸掉。"""
        panel = _panel().drop(columns=["in_dynamic_universe"])
        score = s19.build_signal(panel, spec=_spec("eligible"))
        self.assertTrue(score.notna().any())


if __name__ == "__main__":
    unittest.main()
