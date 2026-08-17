# -*- coding: utf-8 -*-
"""`PanelOps` 的排名母體(`ranking_mask`)語意與「不准漏標」的守衛。

為什麼要有這一層:稠密 panel 是為了 `ts_` 而保留非成員列的,但橫斷面算子如果照單
全收,母體就變成「panel 剛好有哪些列」。那個錯誤不會 crash —— 它只會讓分數變成
另一套,而且單一 `cs_rank` 下完全看不出來(同日單調轉換),只在兩個 cs_ 加權組合
時才會不對稱地扭曲順序。所以母體必須是顯式的,而且新增算子時不能忘記處理。
"""
from __future__ import annotations

import inspect
import unittest

import numpy as np
import pandas as pd

import factor_engine.operators as op


def _panel() -> pd.DataFrame:
    """3 天 × 4 檔;A/B 在母體內,C/D 在母體外且數值刻意落在兩端。"""
    days = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    rows = []
    for sid, base, inside in (("A", 10.0, True), ("B", 20.0, True),
                              ("C", 1.0, False), ("D", 99.0, False)):
        for i, d in enumerate(days):
            rows.append({"date": d, "stock_id": sid, "x": base + i,
                         "in_scope": inside})
    return pd.DataFrame(rows).reset_index(drop=True)


def _ops(panel: pd.DataFrame, scoped: bool) -> op.PanelOps:
    return op.PanelOps(
        panel["date"], panel["stock_id"],
        ranking_mask=panel["in_scope"] if scoped else None,
        ranking_universe="scoped" if scoped else "panel")


class RankingMaskSemanticsTest(unittest.TestCase):
    def test_scope_excludes_outsiders_from_the_population(self):
        panel = _panel()
        ranked = _ops(panel, scoped=True).cs_rank(panel["x"])
        inside = panel["in_scope"].to_numpy()

        self.assertTrue(ranked[~inside].isna().all(),
                        "母體外不得拿到分數,否則會被下游當成可比較的名次")
        # 母體內只有 A/B 兩檔 → 名次必然是 0.5 與 1.0,與 C/D 的極端值無關。
        self.assertEqual(sorted(ranked[inside].round(6).unique().tolist()),
                         [0.5, 1.0])

    def test_outsiders_change_the_answer_when_not_scoped(self):
        """釘住缺陷本身:不設母體時,母體外的股票會改變母體內的分數。"""
        panel = _panel()
        scoped = _ops(panel, scoped=True).cs_rank(panel["x"])
        whole = _ops(panel, scoped=False).cs_rank(panel["x"])
        inside = panel["in_scope"].to_numpy()
        self.assertFalse(np.allclose(scoped[inside].to_numpy(),
                                     whole[inside].to_numpy()))

    def test_time_series_operators_ignore_the_mask(self):
        """`ts_` 必須看完整序列 —— 遮罩若也套到時序,就退化成稀疏 panel 的失真。"""
        panel = _panel()
        a = _ops(panel, scoped=True).ts_mean(panel["x"], 2)
        b = _ops(panel, scoped=False).ts_mean(panel["x"], 2)
        pd.testing.assert_series_equal(a, b)

    def test_group_and_regression_operators_are_scoped_too(self):
        panel = _panel()
        ops = _ops(panel, scoped=True)
        group = pd.Series("g1", index=panel.index)
        outside = ~panel["in_scope"].to_numpy()
        for name, args in (("group_rank", (panel["x"], group)),
                           ("group_zscore", (panel["x"], group)),
                           ("regression_neut", (panel["x"], panel["x"] * 2)),
                           ("cs_zscore", (panel["x"],)),
                           ("bucket", (panel["x"],))):
            with self.subTest(op=name):
                out = getattr(ops, name)(*args)
                self.assertTrue(pd.Series(out)[outside].isna().all(),
                                f"{name} 讓母體外的列拿到值")

    def test_multi_regression_scopes_its_list_argument(self):
        panel = _panel()
        ops = _ops(panel, scoped=True)
        out = ops.multi_regression(panel["x"], [panel["x"] * 2.0])
        self.assertTrue(out[~panel["in_scope"].to_numpy()].isna().all())

    def test_unscoped_is_bit_identical_to_the_old_behaviour(self):
        """不傳遮罩 = 舊行為。既有研究腳本不能因為這次改動而換掉數字。"""
        panel = _panel()
        plain = op.PanelOps(panel["date"], panel["stock_id"])
        pd.testing.assert_series_equal(plain.cs_rank(panel["x"]),
                                       _ops(panel, scoped=False).cs_rank(panel["x"]))


class RankingMaskFailClosedTest(unittest.TestCase):
    def test_misaligned_mask_fails_closed(self):
        panel = _panel()
        bad = panel["in_scope"].iloc[::-1].reset_index(drop=True).iloc[:5]
        with self.assertRaises(ValueError):
            op.PanelOps(panel["date"], panel["stock_id"], ranking_mask=bad)

    def test_empty_mask_fails_closed(self):
        panel = _panel()
        with self.assertRaises(ValueError):
            op.PanelOps(panel["date"], panel["stock_id"],
                        ranking_mask=pd.Series(False, index=panel.index))

    def test_non_series_mask_fails_closed(self):
        panel = _panel()
        with self.assertRaises(TypeError):
            op.PanelOps(panel["date"], panel["stock_id"],
                        ranking_mask=[True] * len(panel))

    def test_misaligned_input_series_fails_closed(self):
        """輸入 index 對不上就無法套母體;靜默放行等於母體失效。"""
        panel = _panel()
        ops = _ops(panel, scoped=True)
        with self.assertRaises(ValueError):
            ops.cs_rank(panel["x"].reset_index(drop=True).iloc[:6])


class NoUnscopedOperatorTest(unittest.TestCase):
    """守衛:新增橫斷面算子時忘記處理母體 → 這支測試紅,而不是報告錯。"""

    CROSS_SECTIONAL_PREFIXES = ("cs_", "group_", "regression_", "multi_regression",
                                "bucket")

    def _public_methods(self):
        for name, fn in inspect.getmembers(op.PanelOps, inspect.isfunction):
            if not name.startswith("_"):
                yield name, fn

    def test_every_cross_sectional_operator_is_marked(self):
        missing = [
            name for name, fn in self._public_methods()
            if name.startswith(self.CROSS_SECTIONAL_PREFIXES)
            and not getattr(fn, "_is_cross_sectional", False)
        ]
        self.assertEqual(missing, [], (
            f"這些橫斷面算子沒有掛 @_cross_sectional:{missing}。"
            "沒掛 = 它會忽略排名母體,在含非成員的稠密 panel 上安靜地算出另一套分數"))

    def test_time_series_operators_are_not_marked(self):
        wrong = [name for name, fn in self._public_methods()
                 if name.startswith("ts_")
                 and getattr(fn, "_is_cross_sectional", False)]
        self.assertEqual(wrong, [], (
            f"時序算子被當成橫斷面算子:{wrong}。ts_ 套上遮罩會讓 rolling 只看到"
            "成員列,那正是 AGENTS.md 陷阱 1"))


if __name__ == "__main__":
    unittest.main()
