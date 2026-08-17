# -*- coding: utf-8 -*-
"""operators.py 單元測試:重點鎖『因果性 / 分組正確性 / WQ 語意』。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import factor_engine.operators as op


def _panel():
    """兩檔股票、交錯日期的 long panel(index 打亂,測 index 對齊)。"""
    rows = []
    for sid in ["A", "B"]:
        for i, dt in enumerate(pd.date_range("2026-01-01", periods=6, freq="D")):
            rows.append({"date": dt, "stock_id": sid, "x": float(i + (0 if sid == "A" else 10))})
    df = pd.DataFrame(rows).sample(frac=1.0, random_state=0).reset_index(drop=True)
    return df


class TestElementwise(unittest.TestCase):
    def test_signed_power_preserves_sign(self):
        x = pd.Series([-4.0, 4.0, 0.0])
        r = op.signed_power(x, 0.5)
        self.assertAlmostEqual(r.iloc[0], -2.0)
        self.assertAlmostEqual(r.iloc[1], 2.0)
        self.assertEqual(r.iloc[2], 0.0)

    def test_s_log_1p_sign(self):
        x = pd.Series([-9.0, 9.0])
        r = op.s_log_1p(x)
        self.assertLess(r.iloc[0], 0)
        self.assertGreater(r.iloc[1], 0)
        self.assertAlmostEqual(r.iloc[1], np.log1p(9.0))

    def test_if_else(self):
        cond = pd.Series([True, False, True])
        r = op.if_else(cond, pd.Series([1, 1, 1]), pd.Series([0, 0, 0]))
        self.assertEqual(list(r), [1, 0, 1])


class TestTimeSeriesCausal(unittest.TestCase):
    def setUp(self):
        self.df = _panel()
        self.ops = op.PanelOps(self.df["date"], self.df["stock_id"])

    def test_ts_delta_and_mean_per_stock(self):
        d = self.df.assign(delta=self.ops.ts_delta(self.df["x"], 1),
                           mean3=self.ops.ts_mean(self.df["x"], 3))
        a = d[d["stock_id"] == "A"].sort_values("date")
        # A 的 x = 0,1,2,3,4,5 → delta1 = NaN,1,1,1,1,1
        self.assertTrue(np.isnan(a["delta"].iloc[0]))
        self.assertTrue((a["delta"].iloc[1:] == 1.0).all())
        # mean3 第三天 = (0+1+2)/3 = 1.0
        self.assertAlmostEqual(a["mean3"].iloc[2], 1.0)
        self.assertTrue(np.isnan(a["mean3"].iloc[1]))  # min_periods=d

    def test_ts_does_not_leak_future(self):
        """追加未來一列不得改變任何過去列的 ts_ 值(因果鐵證)。"""
        m1 = self.ops.ts_mean(self.df["x"], 3)
        d1 = self.df.assign(m=m1)
        # 追加 A 的第 7 天
        extra = pd.DataFrame([{"date": pd.Timestamp("2026-01-07"), "stock_id": "A", "x": 999.0}])
        df2 = pd.concat([self.df, extra], ignore_index=True)
        ops2 = op.PanelOps(df2["date"], df2["stock_id"])
        m2 = ops2.ts_mean(df2["x"], 3)
        d2 = df2.assign(m=m2)
        # 比對原本每一列(A/B、各日期)的 mean3 是否不變
        key = ["date", "stock_id"]
        merged = d1.merge(d2, on=key, suffixes=("_1", "_2"))
        both = merged.dropna(subset=["m_1", "m_2"])
        self.assertTrue(np.allclose(both["m_1"], both["m_2"]))

    def test_ts_rank_and_argmax_small(self):
        a = self.df[self.df["stock_id"] == "A"].sort_values("date")
        r = self.ops.ts_rank(self.df["x"], 3)
        d = self.df.assign(r=r)
        ar = d[d["stock_id"] == "A"].sort_values("date")
        # A 遞增 → 當前值永遠是過去3天最大 → ts_rank = 1.0(第3天起)
        self.assertAlmostEqual(ar["r"].iloc[2], 1.0)
        am = self.df.assign(am=self.ops.ts_arg_max(self.df["x"], 3))
        aa = am[am["stock_id"] == "A"].sort_values("date")
        self.assertEqual(aa["am"].iloc[2], 0)  # 當日即最大 → 0


class TestCrossSectional(unittest.TestCase):
    def setUp(self):
        self.df = _panel()
        self.ops = op.PanelOps(self.df["date"], self.df["stock_id"])

    def test_cs_zscore_mean0_std1_within_date(self):
        z = self.ops.cs_zscore(self.df["x"])
        d = self.df.assign(z=z)
        for dt, g in d.groupby("date"):
            if len(g) >= 2 and g["x"].std(ddof=0) > 0:
                self.assertAlmostEqual(g["z"].mean(), 0.0, places=6)
                self.assertAlmostEqual(g["z"].std(ddof=0), 1.0, places=6)

    def test_cs_rank_in_unit_interval(self):
        r = self.ops.cs_rank(self.df["x"])
        self.assertTrue(((r >= 0) & (r <= 1)).all())

    def test_cs_uses_only_same_date(self):
        """每天只有 A,B 兩檔 → z 分數應是 ±1(母體)。與其他日期無關。"""
        z = self.ops.cs_zscore(self.df["x"])
        d = self.df.assign(z=z)
        vals = sorted(d.groupby("date")["z"].apply(lambda s: round(s.abs().mean(), 6)).unique())
        self.assertEqual(vals, [1.0])


class TestGroup(unittest.TestCase):
    def test_group_neutralize_zero_mean_per_group(self):
        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-01-01"] * 4),
            "stock_id": ["A", "B", "C", "D"],
            "ind": ["半導體", "半導體", "航運", "航運"],
            "x": [1.0, 3.0, 10.0, 20.0],
        })
        ops = op.PanelOps(df["date"], df["stock_id"])
        neu = ops.group_neutralize(df["x"], group=df["ind"])
        d = df.assign(n=neu)
        for ind, g in d.groupby("ind"):
            self.assertAlmostEqual(g["n"].sum(), 0.0, places=6)
        # 半導體:1,3 → 去均值 = -1,+1
        self.assertAlmostEqual(d[d["stock_id"] == "A"]["n"].iloc[0], -1.0)
        self.assertAlmostEqual(d[d["stock_id"] == "B"]["n"].iloc[0], +1.0)


class TestRegression(unittest.TestCase):
    def test_ts_regression_recovers_known_line(self):
        """單股 y = 2 + 3x → 滾動 OLS 斜率≈3、截距≈2、殘差≈0、R²≈1。"""
        x = np.arange(1, 9, dtype=float)
        y = 2.0 + 3.0 * x
        df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=8, freq="D"),
                           "stock_id": "A", "x": x, "y": y})
        ops = op.PanelOps(df["date"], df["stock_id"])
        beta = ops.ts_regression(df["y"], df["x"], 4, rettype="beta")
        alpha = ops.ts_regression(df["y"], df["x"], 4, rettype="alpha")
        resid = ops.ts_regression(df["y"], df["x"], 4, rettype="resid")
        r2 = ops.ts_regression(df["y"], df["x"], 4, rettype="r2")
        self.assertAlmostEqual(beta.iloc[-1], 3.0, places=6)
        self.assertAlmostEqual(alpha.iloc[-1], 2.0, places=6)
        self.assertAlmostEqual(resid.iloc[-1], 0.0, places=6)
        self.assertAlmostEqual(r2.iloc[-1], 1.0, places=6)

    def test_ts_regression_causal(self):
        rng_x = np.array([1., 3., 2., 5., 4., 6., 8., 7.])
        y = 1.0 + 2.0 * rng_x + np.array([0.1, -0.1, 0.2, -0.2, 0.1, 0.0, -0.1, 0.1])
        df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=8, freq="D"),
                           "stock_id": "A", "x": rng_x, "y": y})
        ops = op.PanelOps(df["date"], df["stock_id"])
        b1 = ops.ts_regression(df["y"], df["x"], 4, rettype="beta").iloc[3]
        df2 = pd.concat([df, pd.DataFrame([{"date": pd.Timestamp("2026-01-09"),
                        "stock_id": "A", "x": 999., "y": -999.}])], ignore_index=True)
        ops2 = op.PanelOps(df2["date"], df2["stock_id"])
        b2 = ops2.ts_regression(df2["y"], df2["x"], 4, rettype="beta").iloc[3]
        self.assertAlmostEqual(b1, b2, places=9)   # 未來列不得改變過去斜率

    def test_regression_neut_zero_residual_on_exact_line(self):
        """當日 y = 5 + 2x(完全線性)→ regression_neut 殘差≈0、proj≈y。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-01-01"] * 5),
            "stock_id": list("ABCDE"),
            "x": [1., 2., 3., 4., 5.],
        })
        df["y"] = 5.0 + 2.0 * df["x"]
        ops = op.PanelOps(df["date"], df["stock_id"])
        resid = ops.regression_neut(df["y"], df["x"])
        proj = ops.regression_proj(df["y"], df["x"])
        self.assertTrue(np.allclose(resid.to_numpy(), 0.0, atol=1e-9))
        self.assertTrue(np.allclose(proj.to_numpy(), df["y"].to_numpy(), atol=1e-9))

    def test_multi_regression_zero_residual(self):
        """當日 y = 1 + 2·x1 − 1·x2(完全線性)→ 多因子殘差≈0。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-01-01"] * 6),
            "stock_id": list("ABCDEF"),
            "x1": [1., 2., 3., 1., 2., 4.],
            "x2": [2., 1., 0., 3., 5., 2.],
        })
        df["y"] = 1.0 + 2.0 * df["x1"] - 1.0 * df["x2"]
        ops = op.PanelOps(df["date"], df["stock_id"])
        resid = ops.multi_regression(df["y"], [df["x1"], df["x2"]], rettype="resid")
        self.assertTrue(np.allclose(resid.to_numpy(), 0.0, atol=1e-9))


if __name__ == "__main__":
    unittest.main()
