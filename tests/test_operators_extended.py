# -*- coding: utf-8 -*-
"""新增算子與 field 層的測試。最重要的是**因果性**:算子裡有前視會污染全部研究。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import operators as op


def _panel(n=80, sids=("A", "B"), seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for k, sid in enumerate(sids):
        px = 100.0 * (k + 1)
        for d in pd.bdate_range("2025-01-01", periods=n):
            r = rng.normal(0.001, 0.02)
            px *= 1 + r
            hi = px * (1 + abs(rng.normal(0, 0.01)))
            lo = px * (1 - abs(rng.normal(0, 0.01)))
            vol = float(rng.integers(1_000, 50_000)) * 1000
            rows.append({"date": d, "stock_id": sid, "open": px * (1 + rng.normal(0, 0.005)),
                         "high": max(hi, px), "low": min(lo, px), "close": px,
                         "volume": vol, "turnover": vol * px * (1 + rng.normal(0, 0.002))})
    return pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)


class CausalityTest(unittest.TestCase):
    """對每個 ts_ 算子:附加未來資料後,過去的值不得改變。"""

    def test_all_ts_operators_are_causal(self):
        base = _panel(n=60)
        extra = _panel(n=80).query("date > @base.date.max()")
        ext = pd.concat([base, extra], ignore_index=True).sort_values(
            ["date", "stock_id"]).reset_index(drop=True)

        ob = op.PanelOps(base["date"], base["stock_id"])
        oe = op.PanelOps(ext["date"], ext["stock_id"])
        bf = op.attach_fields(base, ob)
        ef = op.attach_fields(ext, oe)

        unary = ["ts_mean", "ts_std_dev", "ts_sum", "ts_min", "ts_max", "ts_median",
                 "ts_zscore", "ts_ir", "ts_returns", "ts_scale", "ts_rank",
                 "ts_arg_max", "ts_arg_min", "ts_decay_linear", "ts_delay", "ts_delta",
                 "ts_av_diff", "ts_max_diff", "ts_min_diff", "ts_min_max_diff",
                 "ts_min_max_cps", "ts_product", "ts_skewness", "ts_kurtosis",
                 "ts_quantile", "ts_entropy", "ts_count_nans", "ts_backfill",
                 "ts_decay_exp", "last_diff_value", "ts_rsi", "ts_atr",
                 "ts_bollinger_pos"]
        checked = 0
        for name in unary:
            b = getattr(ob, name)(bf["close"], 10)
            e = getattr(oe, name)(ef["close"], 10)
            mb = base[["date", "stock_id"]].assign(v=b.values)
            me = ext[["date", "stock_id"]].assign(v=e.values)
            m = mb.merge(me, on=["date", "stock_id"], suffixes=("_b", "_e"))
            both = m.dropna()
            self.assertGreater(len(both), 10, f"{name}: 可比對的值太少")
            np.testing.assert_allclose(
                both["v_b"].values, both["v_e"].values, rtol=1e-9,
                err_msg=f"{name} 不是因果的 —— 加入未來資料改變了過去的值")
            checked += 1
        self.assertEqual(checked, len(unary))

    def test_hump_is_causal(self):
        base = _panel(n=60)
        extra = _panel(n=80).query("date > @base.date.max()")
        ext = pd.concat([base, extra], ignore_index=True).sort_values(
            ["date", "stock_id"]).reset_index(drop=True)
        b = op.PanelOps(base["date"], base["stock_id"]).hump(base["close"], 1.0)
        e = op.PanelOps(ext["date"], ext["stock_id"]).hump(ext["close"], 1.0)
        m = (base[["date", "stock_id"]].assign(v=b.values)
             .merge(ext[["date", "stock_id"]].assign(v=e.values),
                    on=["date", "stock_id"], suffixes=("_b", "_e"))).dropna()
        np.testing.assert_allclose(m["v_b"].values, m["v_e"].values, rtol=1e-9)


class FieldTest(unittest.TestCase):
    def test_vwap_uses_real_turnover_not_approximation(self):
        p = _panel(n=10)
        o = op.PanelOps(p["date"], p["stock_id"])
        f = op.attach_fields(p, o)
        np.testing.assert_allclose(f["vwap"].values,
                                   (p["turnover"] / p["volume"]).values, rtol=1e-12)

    def test_true_range_includes_gaps(self):
        """true_range 必須含跳空,否則只是 high-low。"""
        p = pd.DataFrame({
            "date": pd.to_datetime(["2025-01-01", "2025-01-02"]),
            "stock_id": ["A", "A"],
            "open": [100.0, 130.0], "high": [101.0, 132.0],
            "low": [99.0, 129.0], "close": [100.0, 130.0],
            "volume": [1e6, 1e6], "turnover": [1e8, 1.3e8],
        })
        o = op.PanelOps(p["date"], p["stock_id"])
        f = op.attach_fields(p, o)
        self.assertAlmostEqual(f["true_range"].iloc[0], 2.0)        # 首日無前收 → high-low
        self.assertAlmostEqual(f["true_range"].iloc[1], 32.0)       # |132-100| 跳空主導
        self.assertGreater(f["true_range"].iloc[1], 132 - 129)

    def test_returns_and_gap(self):
        p = _panel(n=5, sids=("A",))
        o = op.PanelOps(p["date"], p["stock_id"])
        f = op.attach_fields(p, o)
        self.assertTrue(np.isnan(f["returns"].iloc[0]))             # 首日無前收
        exp = p["close"].iloc[1] / p["close"].iloc[0] - 1
        self.assertAlmostEqual(f["returns"].iloc[1], exp)


class IndicatorTest(unittest.TestCase):
    def test_rsi_matches_manual_composition(self):
        """ts_rsi 必須等於用 primitive 組出來的版本 —— 否則 GA 搜出的變體與它不一致。"""
        p = _panel(n=40, sids=("A",))
        o = op.PanelOps(p["date"], p["stock_id"])
        d = 14
        manual_delta = o.ts_delta(p["close"], 1)
        manual = (o.ts_sum(op.elem_max(manual_delta, 0.0), d)
                  / o.ts_sum(op.abs_(manual_delta), d).replace(0, np.nan))
        got = o.ts_rsi(p["close"], d)
        both = pd.DataFrame({"a": manual, "b": got}).dropna()
        self.assertGreater(len(both), 10)
        np.testing.assert_allclose(both["a"].values, both["b"].values, rtol=1e-12)

    def test_rsi_bounds_and_direction(self):
        up = pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=30),
                           "stock_id": "A", "close": np.arange(100.0, 130.0)})
        o = op.PanelOps(up["date"], up["stock_id"])
        r = o.ts_rsi(up["close"], 14).dropna()
        self.assertTrue((r >= 0).all() and (r <= 1).all())
        self.assertAlmostEqual(r.iloc[-1], 1.0, places=9, msg="一路上漲 RSI 應為 1")

    def test_atr_equals_mean_true_range(self):
        p = _panel(n=40, sids=("A",))
        o = op.PanelOps(p["date"], p["stock_id"])
        f = op.attach_fields(p, o)
        np.testing.assert_allclose(o.ts_atr(f["true_range"], 14).dropna().values,
                                   o.ts_mean(f["true_range"], 14).dropna().values,
                                   rtol=1e-12)


class TurnoverControlTest(unittest.TestCase):
    def test_hump_reduces_variation(self):
        """hump 的存在意義是壓周轉 —— 變動量必須真的變小。"""
        p = _panel(n=60, sids=("A",))
        o = op.PanelOps(p["date"], p["stock_id"])
        raw = o.cs_rank(p["close"]) * 0 + p["close"]     # 直接用價格看變動
        humped = o.hump(raw, threshold=0.5)
        self.assertLess(humped.diff().abs().mean(), raw.diff().abs().mean())
        self.assertLessEqual(humped.diff().abs().max(), 0.5 + 1e-9)

    def test_backfill_does_not_look_forward(self):
        """ts_backfill 只能用過去的值補,不可用未來。"""
        p = pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=5),
                          "stock_id": "A", "x": [1.0, np.nan, np.nan, 4.0, 5.0]})
        o = op.PanelOps(p["date"], p["stock_id"])
        f = o.ts_backfill(p["x"], 5)
        self.assertEqual(f.iloc[1], 1.0)       # 用過去的 1.0
        self.assertEqual(f.iloc[2], 1.0)
        self.assertNotEqual(f.iloc[1], 4.0)    # 絕不可用未來的 4.0


class GroupOpsTest(unittest.TestCase):
    def test_group_count_flags_singleton_groups(self):
        p = _panel(n=3, sids=("A", "B", "C"))
        o = op.PanelOps(p["date"], p["stock_id"])
        grp = p["stock_id"].map({"A": "g1", "B": "g1", "C": "g2"})
        n = o.group_count(p["close"], grp)
        self.assertTrue((n[p["stock_id"] == "C"] == 1).all())
        self.assertTrue((n[p["stock_id"] == "A"] == 2).all())
        # 單一成員組的中性化結果恆為 0 —— 這正是要用 group_count 擋掉的情況
        neut = o.group_neutralize(p["close"], grp)
        np.testing.assert_allclose(neut[p["stock_id"] == "C"].values, 0.0, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
