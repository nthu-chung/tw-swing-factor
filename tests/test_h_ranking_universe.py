# -*- coding: utf-8 -*-
"""H 系列的 cs_ 排名母體必須是顯式、可凍結的參數(規格 §3.1)。

2026-08-16 稽核發現:`make_signals()` 先對整個稠密 panel 呼叫 `score()`,所以
`ops.cs_rank(...)` 的母體是「panel 剛好有哪些列」。正式路徑的 panel 是**所有月份
候選池的聯集**(實測 753 檔),不是任何一天真實存在的橫斷面 —— 一檔股票的分數
因此取決於一堆當天不在候選池裡、甚至當時還沒進過池的股票。

修法是把母體綁在 `PanelOps` 上,由 `make_signals()` 依 `ranking_universe` 決定:
`pool`(當月 PIT 候選池,預設)/ `eligible`(當日可買)/ `panel`(舊行為,對照用)。
這支測試釘住三件事:預設值、三個母體真的不同、以及會靜默出錯的路徑全部 fail-closed。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from factor_engine.panel_density import DENSE, tag
from strategies.h1_volume_breakout import H1VolumeBreakout
from strategies.h3_short_reversal import H3ShortReversal
from strategies.h11_oversold_bounce import H11OversoldBounce
from strategy_kit.signal_builder import RANKING_PARAM

# sid: (報酬漂移, 量能振幅, 第三個因子輸入, 是否可買, 是否在當月候選池)
# 非成員在兩個因子上的位置刻意相反 —— 母體換掉時,兩個 cs_rank 會被不對稱地扭曲。
PROFILE = {
    "M1": (0.004, 0.10, 5_000.0, True, True),
    "M2": (0.003, 0.45, 9_000.0, True, True),
    "M3": (0.002, 0.25, 2_000.0, True, True),
    "P1": (0.020, 0.05, -9_000.0, False, True),    # 動能極高、量能/流量極差
    "P2": (-0.015, 0.80, 20_000.0, False, True),   # 動能極差、量能/流量極好
    "O1": (0.030, 0.02, -20_000.0, False, False),  # 池外,兩端再拉開一次
    "O2": (-0.025, 0.95, 30_000.0, False, False),
}
SMALL = {"high_window": 5, "vol_window": 5, "mom_window": 5, "flow_window": 5}


def _panel(n_days: int = 45) -> pd.DataFrame:
    days = pd.bdate_range("2026-01-05", periods=n_days)
    rng = np.random.default_rng(11)
    rows = []
    for sid, (drift, amp, flow, member, pool) in PROFILE.items():
        px = 100.0
        for i, d in enumerate(days):
            px *= 1.0 + drift + rng.normal(0.0, 0.002)
            volume = 1_000_000.0 * (1.0 + amp * ((i % 5) - 2) / 2.0)
            rows.append({
                "date": d, "stock_id": sid, "close": px,
                "volume": volume, "turnover": px * volume,
                "foreign_net": flow, "trust_net": flow / 2.0,
                "in_dynamic_universe": member, "in_candidate_pool": pool,
                "trend_ok": True,
            })
    panel = pd.DataFrame(rows).sort_values(["stock_id", "date"]).reset_index(drop=True)
    return tag(panel, DENSE)


def _params(strategy, scope: str) -> dict:
    values = strategy.default_parameters()
    values.update({k: v for k, v in SMALL.items() if k in values})
    values[RANKING_PARAM] = scope
    return values


def _scores(strategy, panel: pd.DataFrame, scope: str) -> pd.Series:
    """逐 (date, stock) 的分數。**必須 sort_index**:輸出是照名次排的,母體不同時
    列順序也不同,不對齊就會變成在比「順序」而不是在比「分數」。"""
    out = strategy.make_signals(panel, _params(strategy, scope))
    return out.set_index(["date", "stock_id"])["raw_score"].sort_index()


class DefaultAndFreezingTest(unittest.TestCase):
    def test_default_is_the_monthly_pit_candidate_pool(self):
        for cls in (H1VolumeBreakout, H3ShortReversal, H11OversoldBounce):
            with self.subTest(strategy=cls.name):
                self.assertEqual(cls().default_parameters()[RANKING_PARAM], "pool")

    def test_it_travels_with_the_frozen_parameters(self):
        """母體會改變選股 → 必須進 rules hash,否則 forward 驗的是另一套規則。"""
        self.assertIn(RANKING_PARAM, H1VolumeBreakout().default_parameters())
        self.assertIn(RANKING_PARAM, H1VolumeBreakout().parameter_space())

    def test_subclass_cannot_lose_it_by_overriding_defaults(self):
        """子類別會整個覆寫 `defaults`;母體不能靠寫在那裡才存在。"""
        self.assertNotIn(RANKING_PARAM, H1VolumeBreakout.defaults)
        self.assertIn(RANKING_PARAM, H1VolumeBreakout().default_parameters())


class ScopeChangesTheAnswerTest(unittest.TestCase):
    def test_two_factor_strategies_score_differently_per_scope(self):
        panel = _panel()
        for cls in (H1VolumeBreakout, H11OversoldBounce):
            with self.subTest(strategy=cls.name):
                s = cls()
                pool = _scores(s, panel, "pool")
                elig = _scores(s, panel, "eligible")
                whole = _scores(s, panel, "panel")
                self.assertFalse(np.allclose(pool.values, elig.values),
                                 "候選池母體與可買母體應給出不同分數")
                self.assertFalse(np.allclose(pool.values, whole.values),
                                 "候選池母體與全 panel 母體應給出不同分數")

    def test_single_factor_strategy_is_scope_invariant(self):
        """單一 cs_rank 是同日單調轉換,最終名次不受母體影響 —— H3 不必重跑。"""
        panel = _panel()
        s = H3ShortReversal()
        ranks = {}
        for scope in ("pool", "eligible", "panel"):
            out = s.make_signals(panel, _params(s, scope))
            ranks[scope] = out.set_index(["date", "stock_id"])["rank"].sort_index()
        pd.testing.assert_series_equal(ranks["pool"], ranks["eligible"])
        pd.testing.assert_series_equal(ranks["pool"], ranks["panel"])

    def test_only_buyable_stocks_are_emitted_whatever_the_scope(self):
        """母體變大不等於可買集合變大:池外/非成員不得出現在訊號裡。"""
        panel = _panel()
        buyable = {sid for sid, p in PROFILE.items() if p[3]}
        for scope in ("pool", "eligible", "panel"):
            with self.subTest(scope=scope):
                out = H1VolumeBreakout().make_signals(
                    panel, _params(H1VolumeBreakout(), scope))
                self.assertEqual(set(out["stock_id"]), buyable)


class ProvenanceTest(unittest.TestCase):
    def test_score_universe_is_recorded_with_its_size(self):
        panel = _panel()
        expected = {"eligible": 3, "pool": 5, "panel": 7}
        for scope, size in expected.items():
            with self.subTest(scope=scope):
                out = H1VolumeBreakout().make_signals(
                    panel, _params(H1VolumeBreakout(), scope))
                self.assertEqual(set(out["score_universe"]), {scope})
                self.assertEqual(set(out["score_universe_count"]), {size})

    def test_rank_universe_stays_the_buyable_set(self):
        """§6.1 的 `ranking_universe_count` 語意是 rank 的母體(當日輸出列數),
        與 cs_ 的 `score_universe_count` 是兩件事,不可混用。"""
        panel = _panel()
        out = H1VolumeBreakout().make_signals(
            panel, _params(H1VolumeBreakout(), "pool"))
        self.assertEqual(set(out["ranking_universe_count"]), {3})
        self.assertEqual(set(out["score_universe_count"]), {5})


class FailClosedTest(unittest.TestCase):
    def test_unknown_scope(self):
        with self.assertRaises(ValueError):
            H1VolumeBreakout().make_signals(
                _panel(), _params(H1VolumeBreakout(), "top300"))

    def test_pool_scope_without_the_column(self):
        """缺欄位時退回全 panel 等於靜默換掉母體 —— 必須 raise。"""
        panel = _panel().drop(columns=["in_candidate_pool"])
        with self.assertRaises(ValueError) as ctx:
            H1VolumeBreakout().make_signals(
                panel, _params(H1VolumeBreakout(), "pool"))
        self.assertIn("in_candidate_pool", str(ctx.exception))

    def test_buyable_stock_outside_the_ranking_scope(self):
        """可買卻不在母體 → 分數 NaN → 被靜默踢出選股。這必須是錯誤,不是少幾檔。"""
        panel = _panel()
        panel.loc[panel["stock_id"] == "M1", "in_candidate_pool"] = False
        with self.assertRaises(ValueError) as ctx:
            H1VolumeBreakout().make_signals(
                panel, _params(H1VolumeBreakout(), "pool"))
        self.assertIn("排名母體之外", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
