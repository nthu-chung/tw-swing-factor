# -*- coding: utf-8 -*-
"""策略因子一律在稠密 panel 計算(P0-4)的離線回歸測試。

原本的 bug(管線不變式 3 / AGENTS.md 陷阱 1):
`backtest._prepare_panel` 的 `keep_non_members` 預設是 `False`,回傳的 panel 只留
動態 universe 的**成員日**。`rotation_research.build_research_panel` 用了這個預設值,
接著直接在該 panel 上做 `groupby("stock_id")` 的 `shift(1).rolling(20)` 算
`breakout_20` / `breakout_volume_ratio` / `positive_day_share_20`。

long panel 的 `rolling(20)` 算的是「20 **列**」而不是「20 個交易日」:一檔間歇進出
universe 的股票,那 20 列會橫跨 60+ 個日曆日 —— 突破價位拿的是幾個月前的高點、
量比拿的是幾個月前的均量。獨立模擬重現:突破訊號翻轉約 3%、命中率相對灌水約 +9.6%,
而這三個欄位直接決定 `rotation_breakout` 的 eligible 條件與 `signal_score`。

修法(本檔釘住的行為):
1. 公開入口 `backtest.build_research_panel()` **預設稠密**,`_prepare_panel` 降為
   引擎內部函式,並在 panel 上戳稠密度標籤。
2. `PanelOps` 的 ts_ 類算子在標成 `members_only` 的 panel 上 fail-closed raise。
3. `rotation_research` 先在完整個股序列上算 rolling,成員資格過濾留到選股階段 ——
   因子值與「當天是不是成員」無關。
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import backtest
import rotation_research
from factor_engine import operators as op
from factor_engine import panel_density
from universes import MonthlyPITUniverseProvider

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── rotation_research 用的合成稠密 panel ───────────────────────────────────
# B 每 3 天才是動態 universe 成員(間歇進出),C 一直是成員。
# 關鍵佈局:第 6 天(也是成員日)放一根 high=500 / volume=100000 的巨大長紅棒。
#   - 稠密(正確):第 63 天的 20 日視窗 = 第 43~62 天,看不到第 6 天那根。
#   - 稀疏(舊 bug):第 63 天往前數 20 **列** = 成員日 3,6,…,60,橫跨 58 個交易日,
#     把第 6 天的 500 元高點當成「近 20 日高點」→ 突破被吃掉、量比被稀釋。
N_DAYS = 70
SPIKE_I = 6
TARGET_I = 63
MEMBER_EVERY = 3


def _dense_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=N_DAYS)
    rows = []
    for sid in ("B", "C"):
        for i, d in enumerate(dates):
            high = 100.0 + i * 0.1
            volume = 1_000.0
            if sid == "B" and i == SPIKE_I:
                high, volume = 500.0, 100_000.0
            if sid == "B" and i == TARGET_I:
                high, volume = 151.0, 2_000.0
            member = True if sid == "C" else (i % MEMBER_EVERY == 0)
            rows.append({
                "date": d, "stock_id": sid, "name": f"N{sid}",
                "open": high - 2.0, "high": high, "low": high - 3.0,
                "close": high - 1.0, "volume": volume, "turnover": volume * high,
                "in_dynamic_universe": member, "trend_ok": True,
                "score_momentum": 0.8, "rs_excess": 0.01, "mom_ret": 0.02,
                "near_high": 0.99, "inst_6d": 1_000.0,
            })
    panel = pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)
    return panel_density.tag(panel, panel_density.DENSE)


def _build(panel: pd.DataFrame) -> pd.DataFrame:
    """跑 rotation_research 的 panel 建構,資料層全部換成合成資料(不打網路)。"""
    with (
        mock.patch.object(rotation_research.backtest, "build_research_panel",
                          return_value=panel),
        mock.patch.object(rotation_research.uni, "get_industry_map",
                          return_value={"B": "電子", "C": "電子"}),
    ):
        return rotation_research.build_rotation_panel(["B", "C"])


def _dense_reference(panel: pd.DataFrame, sid: str, i: int) -> dict:
    """在該股完整交易日序列上算的參考值(定義上的正確答案)。"""
    s = panel[panel["stock_id"] == sid].sort_values("date").reset_index(drop=True)
    window_high = s["high"].iloc[i - 20:i]
    window_vol = s["volume"].iloc[i - 20:i]
    up_share = s["close"].pct_change().gt(0).iloc[i - 19:i + 1].mean()
    return {
        "prior_high": float(window_high.max()),
        "breakout_20": bool(s["close"].iloc[i] > window_high.max()),
        "breakout_volume_ratio": float(s["volume"].iloc[i] / window_vol.mean()),
        "positive_day_share_20": float(up_share),
    }


class RotationRollingUsesConsecutiveTradingDaysTest(unittest.TestCase):
    def setUp(self):
        self.panel = _dense_panel()
        self.out = _build(self.panel)
        self.dates = sorted(self.panel["date"].unique())
        self.target_date = self.dates[TARGET_I]

    def _row(self, out: pd.DataFrame, sid: str, date) -> pd.Series:
        hit = out[(out["stock_id"] == sid) & (out["date"] == date)]
        self.assertEqual(len(hit), 1, "目標列必須存在且唯一")
        return hit.iloc[0]

    def test_rolling_window_is_20_consecutive_trading_days_of_that_stock(self):
        """20 日視窗必須是該股連續 20 個實際交易日,不是橫跨 58 天的 20 列。"""
        ref = _dense_reference(self.panel, "B", TARGET_I)
        row = self._row(self.out, "B", self.target_date)
        self.assertTrue(bool(row["breakout_20"]))
        self.assertEqual(bool(row["breakout_20"]), ref["breakout_20"])
        self.assertAlmostEqual(float(row["breakout_volume_ratio"]),
                               ref["breakout_volume_ratio"], places=9)
        self.assertAlmostEqual(float(row["positive_day_share_20"]),
                               ref["positive_day_share_20"], places=9)
        # 量比 = 2000 / 1000:視窗內完全沒有那根 100000 的爆量
        self.assertAlmostEqual(float(row["breakout_volume_ratio"]), 2.0, places=9)

    def test_old_sparse_rolling_would_have_flipped_the_signal(self):
        """釘住舊 bug 的方向:同一天在稀疏 panel 上算會得到相反的突破判定。

        這裡刻意用「舊寫法」(先過濾成員、再 rolling)重算一次,證明兩者不同 ——
        若哪天這個 assert 失敗,代表測試佈局失效(視窗差異被抹平),必須修測試,
        不是把主程式的過濾順序改回去。
        """
        sparse = self.panel[self.panel["in_dynamic_universe"]].copy()
        b = sparse[sparse["stock_id"] == "B"].sort_values("date").reset_index(drop=True)
        pos = int(b.index[b["date"] == self.target_date][0])
        prior_high_sparse = float(b["high"].iloc[pos - 20:pos].max())
        prior_vol_sparse = float(b["volume"].iloc[pos - 20:pos].mean())
        close = float(b["close"].iloc[pos])

        self.assertEqual(prior_high_sparse, 500.0)          # 第 6 天的高點被誤用
        self.assertFalse(close > prior_high_sparse)         # 舊行為:突破被吃掉
        self.assertLess(close / prior_high_sparse, 1.0)
        self.assertLess(float(b["volume"].iloc[pos]) / prior_vol_sparse,
                        rotation_research.BREAKOUT_VOLUME_RATIO)
        # 稀疏視窗真的橫跨遠超過 20 個交易日
        span = (self.dates.index(b["date"].iloc[pos - 1])
                - self.dates.index(b["date"].iloc[pos - 20]))
        self.assertGreater(span, 50)

    def test_membership_pattern_does_not_change_factor_values(self):
        """加入非成員日期不會改變因子值:因子只是該股價量序列的函數。"""
        always = self.panel.copy()
        always["in_dynamic_universe"] = True
        panel_density.tag(always, panel_density.DENSE)
        out_always = _build(always)

        cols = ["breakout_20", "breakout_volume_ratio", "positive_day_share_20"]
        merged = (
            self.out[["date", "stock_id"] + cols]
            .merge(out_always[["date", "stock_id"] + cols],
                   on=["date", "stock_id"], suffixes=("_a", "_b"))
        )
        self.assertGreater(len(merged), 20, "應有足夠的共同成員列可比對")
        for col in cols:
            a = merged[f"{col}_a"].astype(float).fillna(-999.0).to_numpy()
            b = merged[f"{col}_b"].astype(float).fillna(-999.0).to_numpy()
            np.testing.assert_allclose(a, b, rtol=1e-12,
                                       err_msg=f"{col} 受成員資格影響 = 稀疏 rolling 復活")

    def test_membership_filter_only_shrinks_picks_not_the_factor_window(self):
        """成員過濾只發生在選股階段:回傳列 = 成員列,但視窗值仍是稠密算的。"""
        b_rows = self.out[self.out["stock_id"] == "B"]
        expected_members = len([i for i in range(N_DAYS) if i % MEMBER_EVERY == 0])
        self.assertEqual(len(b_rows), expected_members)
        self.assertTrue(bool(b_rows["in_dynamic_universe"].all()))
        # 但「非成員日也算進視窗」:第 62 天(非成員)的高點必須影響第 63 天的判定
        ref = _dense_reference(self.panel, "B", TARGET_I)
        self.assertAlmostEqual(ref["prior_high"], 100.0 + 62 * 0.1, places=9)

    def test_returned_panel_is_labelled_members_only(self):
        """回傳的是成員列 → 必須標成 members_only,不可再被拿去算 ts_。"""
        self.assertEqual(panel_density.density_of(self.out),
                         panel_density.MEMBERS_ONLY)

    def test_sparse_panel_input_fails_closed(self):
        """萬一有人把 panel 來源改回稀疏,要在算因子之前就炸掉。"""
        sparse = self.panel[self.panel["in_dynamic_universe"]].reset_index(drop=True)
        panel_density.tag(sparse, panel_density.MEMBERS_ONLY)
        with self.assertRaisesRegex(ValueError, "稀疏 panel"):
            _build(sparse)


# ── 公開入口:預設稠密 ─────────────────────────────────────────────────────
def _pit_history() -> pd.DataFrame:
    """A 只在 2 月與 4 月是候選(3 月換 B)→ 稀疏 panel 會出現「離開再回來」的缺口。

    月頻 PIT 規則:M 月候選池只用完整的 M-1 曆月。這裡 1/3/4 月 A 成交值大、
    2 月 B 大 → 2 月候選 = [A]、3 月 = [B]、4 月 = [A]。
    """
    rows = []
    for d in pd.bdate_range("2026-01-01", "2026-04-30"):
        big, small = ("B", "A") if d.month == 2 else ("A", "B")
        rows.append({"date": d, "stock_id": big, "turnover": 9e8})
        rows.append({"date": d, "stock_id": small, "turnover": 1e6})
    return pd.DataFrame(rows)


def _factor_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", "2026-04-30")
    px = 100.0 + np.arange(len(dates)) * 0.1
    return pd.DataFrame({
        "date": dates, "open": px, "high": px * 1.01, "low": px * 0.99,
        "close": px, "volume": 5_000_000.0, "turnover": 5e8,
        "avg_vol_lots": 5_000.0, "trend_ok": True,
    })


class _PanelEnv:
    """把引擎建 panel 需要的資料層換成離線假資料(絕不打網路)。"""

    def __enter__(self):
        price = _factor_frame()
        self._patches = [
            mock.patch.object(backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(backtest.uni, "get_name_map", return_value={}),
            mock.patch.object(backtest.uni, "get_industry_map", return_value={}),
            mock.patch.object(backtest.data, "fetch_market_index",
                              return_value=pd.DataFrame()),
            mock.patch.object(backtest.data, "fetch_bundle",
                              side_effect=lambda *_a, **_k: {"price": price.copy()}),
            mock.patch.object(backtest.factors, "compute_factors",
                              side_effect=lambda *_a, **_k: price.copy()),
            # 用純函式而非 MagicMock:MagicMock 有 keys(),DataFrame.apply 會誤判成
            # dict-like 的多函式聚合而炸掉。
            mock.patch.object(backtest.factors, "composite_score",
                              new=lambda *_a, **_k: 80.0),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class BuildResearchPanelDefaultsToDenseTest(unittest.TestCase):
    def _panel(self, **kwargs) -> pd.DataFrame:
        provider = MonthlyPITUniverseProvider.from_history(
            _pit_history(), top_n=1, min_obs=5,
        )
        with _PanelEnv():
            return backtest.build_research_panel(
                ["A", "B"], dynamic_enabled=True, universe_top_n=10,
                universe_provider=provider, **kwargs,
            )

    def test_default_panel_is_dense_and_keeps_non_member_rows(self):
        """公開入口的預設值必須是稠密 panel(含非成員列 + 稠密度標籤)。"""
        panel = self._panel()
        self.assertFalse(panel.empty)
        self.assertEqual(panel_density.density_of(panel), panel_density.DENSE)
        self.assertTrue((~panel["in_dynamic_universe"]).any(),
                        "稠密 panel 必須保留非成員列,否則 ts_ 視窗會失真")
        # 每檔的日期序列連續(相鄰列剛好是相鄰交易日)
        all_dates = sorted(panel["date"].unique())
        for sid, grp in panel.groupby("stock_id"):
            idx = [all_dates.index(d) for d in sorted(grp["date"].unique())]
            self.assertEqual(np.diff(idx).max(), 1, f"{sid} 的序列有缺口")

    def test_members_only_is_opt_in_and_labelled(self):
        """members_only=True 要顯式指定,而且會被標記成稀疏(之後 ts_ 會被擋)。"""
        panel = self._panel(members_only=True)
        self.assertFalse(panel.empty)
        self.assertEqual(panel_density.density_of(panel), panel_density.MEMBERS_ONLY)
        self.assertTrue(bool(panel["in_dynamic_universe"].all()))
        # 這就是舊預設的破口:同一檔的相鄰兩列可以隔好幾週
        all_dates = sorted(_factor_frame()["date"].unique())
        gaps = []
        for _sid, grp in panel.groupby("stock_id"):
            idx = [all_dates.index(d) for d in sorted(grp["date"].unique())]
            if len(idx) > 1:
                gaps.append(int(np.diff(idx).max()))
        self.assertGreater(max(gaps), 1,
                           "測試資料應該要有間歇進出 universe 的股票")

    def test_public_entry_signature_defaults_to_dense(self):
        """簽章層面也要釘住:members_only 預設 False(不是誰都記得看文件)。"""
        sig = inspect.signature(backtest.build_research_panel)
        self.assertIs(sig.parameters["members_only"].default, False)
        self.assertEqual(sig.parameters["members_only"].kind,
                         inspect.Parameter.KEYWORD_ONLY)


# ── 算子層 fail-closed ─────────────────────────────────────────────────────
def _mini_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=30)
    rows = [{"date": d, "stock_id": sid, "close": 100.0 + i}
            for sid in ("A", "B") for i, d in enumerate(dates)]
    return pd.DataFrame(rows).sort_values(["date", "stock_id"]).reset_index(drop=True)


class OperatorsRefuseSparsePanelTest(unittest.TestCase):
    def test_ts_operators_fail_closed_on_members_only_panel(self):
        """稀疏 panel 上算 ts_ 一律 raise —— 失真的因子值不會 crash,只會變好看。"""
        panel = panel_density.tag(_mini_panel(), panel_density.MEMBERS_ONLY)
        ops = op.PanelOps(panel["date"], panel["stock_id"])
        for call in (
            lambda: ops.ts_mean(panel["close"], 5),
            lambda: ops.ts_delay(panel["close"], 1),
            lambda: ops.ts_ir(panel["close"], 5),
            lambda: ops.ts_backfill(panel["close"], 5),
            lambda: ops.hump(panel["close"], 1.0),
        ):
            with self.assertRaisesRegex(ValueError, "稀疏 panel"):
                call()

    def test_cross_sectional_operators_still_work_on_members_only_panel(self):
        """cs_/group_ 只看當日橫斷面 → 不受稠密度影響,不可被誤擋。"""
        panel = panel_density.tag(_mini_panel(), panel_density.MEMBERS_ONLY)
        ops = op.PanelOps(panel["date"], panel["stock_id"])
        self.assertEqual(len(ops.cs_rank(panel["close"])), len(panel))
        self.assertEqual(len(ops.cs_zscore(panel["close"])), len(panel))

    def test_untagged_and_dense_panels_are_allowed(self):
        """沒有標籤(手寫測試 panel)或標成 dense 都放行 —— 誤殺會逼人關閘門。"""
        plain = _mini_panel()
        self.assertIsNone(panel_density.density_of(plain))
        op.PanelOps(plain["date"], plain["stock_id"]).ts_mean(plain["close"], 5)
        dense = panel_density.tag(_mini_panel(), panel_density.DENSE)
        op.PanelOps(dense["date"], dense["stock_id"]).ts_mean(dense["close"], 5)

    def test_require_dense_message_points_at_the_safe_entry(self):
        panel = panel_density.tag(_mini_panel(), panel_density.MEMBERS_ONLY)
        with self.assertRaises(ValueError) as ctx:
            panel_density.require_dense(panel, who="t", what="x")
        msg = str(ctx.exception)
        self.assertIn("build_research_panel", msg)
        self.assertIn("in_dynamic_universe", msg)


class StrategiesUseThePublicEntryTest(unittest.TestCase):
    """結構性:strategies/ 不得碰引擎私有的 _prepare_panel。"""

    def _module_calls(self, path: pathlib.Path) -> set:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        return names

    def test_no_strategy_module_touches_prepare_panel(self):
        """新策略只能走 build_research_panel(預設稠密),不能自己選稠密度。

        用 AST 掃(不是字串比對):註解與 docstring 裡提到 `_prepare_panel` 是
        歷史說明,允許;真的把它當函式用就不允許。
        """
        offenders = []
        for path in sorted((REPO_ROOT / "strategies").glob("*.py")):
            if "_prepare_panel" in self._module_calls(path):
                offenders.append(path.name)
        self.assertEqual(offenders, [],
                         "strategies/ 必須用 backtest.build_research_panel()")

    def test_s19_uses_build_research_panel(self):
        """正向釘住:S19 的 legacy 對照路徑走的是公開入口。"""
        names = self._module_calls(REPO_ROOT / "strategies" / "s19_chip_momentum.py")
        self.assertIn("build_research_panel", names)


if __name__ == "__main__":
    unittest.main()
