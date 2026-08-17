# -*- coding: utf-8 -*-
"""`trend_guard` 從硬編碼變成策略參數之後的契約。

為什麼需要這一份:`trend_ok` 原本寫死在 `_member_mask()` 裡,每支假說無條件
繼承一條它從沒宣告過的 MA 規則。改成參數之後,有三件事必須被釘住,否則這次
重構只是把硬編碼換了個位置:

  1. 它真的會改變可買集合(不是宣告了但沒接上)。
  2. 它真的會進 rules hash(否則兩套不同規則會有同一個 hash,forward 驗錯東西)。
  3. 預設值沒有被順手翻掉(翻預設 = 一次改掉所有既有策略的定義)。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import factor_engine.operators as op
from strategy_kit.signal_builder import (RANKING_PARAM, TREND_GUARD_PARAM,
                                         HypothesisStrategy)
from strategy_kit.registry import available, resolve


class _Probe(HypothesisStrategy):
    """最小策略:分數就是 close,好讓斷言只反映母體差異。"""

    name = "probe_trend_guard"
    ranking_universe = "eligible"

    def score(self, panel, ops, params):
        return panel["close"].astype(float)


def _panel(n_days: int = 6, n_stocks: int = 4) -> pd.DataFrame:
    days = pd.bdate_range("2026-01-05", periods=n_days)
    rows = []
    for s in range(n_stocks):
        sid = f"{1000 + s}"
        for i, d in enumerate(days):
            rows.append({
                "date": d, "stock_id": sid,
                "close": 100.0 + s * 10 + i,
                "volume": 1_000_000.0,
                "in_dynamic_universe": True,
                "in_candidate_pool": True,
                # 後兩檔永遠過不了趨勢閘門 → 開關會改變可買集合
                "trend_ok": s < 2,
            })
    return pd.DataFrame(rows)


class TrendGuardIsARealParameter(unittest.TestCase):

    def test_toggling_changes_the_buyable_set(self):
        """關掉閘門要真的多出股票 —— 否則參數只是裝飾。"""
        p = _panel()
        st = _Probe()
        on = st.make_signals(p, {**st.default_parameters(),
                                 TREND_GUARD_PARAM: True}, None)
        off = st.make_signals(p, {**st.default_parameters(),
                                  TREND_GUARD_PARAM: False}, None)
        self.assertEqual(set(on["stock_id"]), {"1000", "1001"})
        self.assertEqual(set(off["stock_id"]), {"1000", "1001", "1002", "1003"})

    def test_it_enters_default_parameters_so_it_reaches_the_rules_hash(self):
        """凍結是靠 `default_parameters()`;沒進去就不會進 hash。"""
        self.assertIn(TREND_GUARD_PARAM, _Probe().default_parameters())
        for sid in available():
            with self.subTest(strategy=sid):
                st = resolve(sid)
                if not isinstance(st, HypothesisStrategy):
                    continue      # s19 系列不走這套骨架
                self.assertIn(TREND_GUARD_PARAM, st.default_parameters())

    def test_two_settings_do_not_share_one_rule_id(self):
        """規則不同、eligibility_rule_id 相同的話,事後看不出跑的是哪一套。"""
        p, st = _panel(), _Probe()
        on = st.make_signals(p, {**st.default_parameters(),
                                 TREND_GUARD_PARAM: True}, None)
        off = st.make_signals(p, {**st.default_parameters(),
                                  TREND_GUARD_PARAM: False}, None)
        self.assertNotEqual(on["eligibility_rule_id"].iloc[0],
                            off["eligibility_rule_id"].iloc[0])

    def test_default_stays_true_for_every_registered_strategy(self):
        """既有策略的定義不可被這次重構靜默改掉。

        有意關掉的策略要在自己的類別裡明確宣告 `trend_guard = False`,
        並在 docstring 說明理由 —— 這個測試只擋「順手翻預設」。
        """
        self.assertIs(HypothesisStrategy.trend_guard, True)

    def test_declaring_the_guard_without_the_column_is_fail_closed(self):
        """宣告了閘門卻沒欄位 → raise,不可靜默略過。

        舊寫法是 `if "trend_ok" in columns`,缺欄位就當沒這回事;那會讓
        rules hash 記著 True 而實際沒套上,是「假規則」等級的問題。
        """
        p = _panel().drop(columns=["trend_ok"])
        st = _Probe()
        with self.assertRaises(ValueError) as ctx:
            st.make_signals(p, {**st.default_parameters(),
                                TREND_GUARD_PARAM: True}, None)
        self.assertIn("trend_ok", str(ctx.exception))
        # 明確關掉就該過
        out = st.make_signals(p, {**st.default_parameters(),
                                  TREND_GUARD_PARAM: False}, None)
        self.assertEqual(len(set(out["stock_id"])), 4)

    def test_non_bool_is_rejected(self):
        """1 / "true" 之類的等價值會讓同一套規則產生兩個 hash。"""
        st = _Probe()
        for bad in (1, 0, "true", "False", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    st.make_signals(_panel(), {**st.default_parameters(),
                                               TREND_GUARD_PARAM: bad}, None)

    def test_numpy_bool_is_accepted(self):
        """來自 pandas/numpy 的 bool 是 np.bool_,不該被上一條擋掉。"""
        st = _Probe()
        out = st.make_signals(_panel(), {**st.default_parameters(),
                                         TREND_GUARD_PARAM: np.bool_(False)}, None)
        self.assertEqual(len(set(out["stock_id"])), 4)

    def test_it_appears_in_the_parameter_space_as_categorical(self):
        """GA 只能在 True/False 之間跳,不能對它做數值變異。"""
        space = _Probe().parameter_space()[TREND_GUARD_PARAM]
        self.assertEqual(space["type"], "categorical")
        self.assertEqual(sorted(space["choices"], key=str), [False, True])

    def test_eligible_scope_follows_the_guard(self):
        """`eligible` 母體的定義要跟著參數走,不能還是舊的固定語意。"""
        p, st = _panel(), _Probe()
        off = st.make_signals(p, {**st.default_parameters(),
                                  RANKING_PARAM: "eligible",
                                  TREND_GUARD_PARAM: False}, None)
        # score_universe_count = cs_ 算子的母體大小,關掉閘門後應為全部 4 檔
        self.assertEqual(set(off["score_universe_count"]), {4})


if __name__ == "__main__":
    unittest.main()
