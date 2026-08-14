# -*- coding: utf-8 -*-
"""PIT 候選池強制點在引擎邊界(P0-3)的離線回歸測試。

原本的 bug:`backtest.py` 用 `universe_provider is None and dynamic_enabled and
not sample and symbols is None` 當「安全預設」自動補上月 PIT provider。但每個研究
入口都會顯式傳 `symbols=`(全部來自 `universe.get_research_candidates()` 讀的
**單一日期** top-N 靜態池),所以那個安全預設一次都不會觸發 —— 實際預設行為是把
「今天知道誰熱門」的排名回套整段歷史(AGENTS.md 陷阱 4 的選股 look-ahead),
而程式碼看起來像有保護。

修法:引擎不再從 `symbols is None` 推測呼叫端意圖,呼叫端必須表態(PIT provider /
顯式 static comparator / sample smoke),否則 fail-closed raise。
"""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np
import pandas as pd

import backtest
import config
from universes import MonthlyPITUniverseProvider, historical_pit_universe


# ── 測試用的離線 PIT provider ──────────────────────────────────────────────
# 1 月 A 成交值大、B 小 → 2 月候選池 = [A]
# 2 月 B 成交值大、A 小 → 3 月候選池 = [B]
def _pit_history() -> pd.DataFrame:
    rows = []
    for d in pd.bdate_range("2026-01-01", "2026-03-31"):
        big, small = ("B", "A") if d.month == 2 else ("A", "B")
        rows.append({"date": d, "stock_id": big, "turnover": 9e8})
        rows.append({"date": d, "stock_id": small, "turnover": 1e6})
    return pd.DataFrame(rows)


def _provider(top_n: int = 1) -> MonthlyPITUniverseProvider:
    return MonthlyPITUniverseProvider.from_history(
        _pit_history(), top_n=top_n, min_obs=5,
    )


def _factor_frame() -> pd.DataFrame:
    """假的因子表(每檔都一樣;stock_id 由 _prepare_panel 後補)。"""
    dates = pd.bdate_range("2026-01-01", "2026-03-31")
    px = 100.0 + np.arange(len(dates)) * 0.1
    return pd.DataFrame({
        "date": dates, "open": px, "high": px * 1.01, "low": px * 0.99,
        "close": px, "volume": 5_000_000.0, "turnover": 5e8,
        "avg_vol_lots": 5_000.0, "trend_ok": True,
    })


class _PanelEnv:
    """把 `_prepare_panel` 需要的資料層換成離線假資料(絕不打網路)。"""

    def __enter__(self):
        price = _factor_frame()
        self._patches = [
            mock.patch.object(backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(backtest, "_load_disposition_days", lambda *_a, **_k: {}),
            mock.patch.object(backtest.uni, "get_name_map", return_value={}),
            mock.patch.object(backtest.uni, "get_industry_map", return_value={}),
            mock.patch.object(backtest.data, "fetch_market_index",
                              return_value=pd.DataFrame()),
            mock.patch.object(backtest.data, "fetch_bundle",
                              side_effect=lambda *_a, **_k: {"price": price.copy()}),
            mock.patch.object(backtest.data, "fetch_price",
                              side_effect=lambda *_a, **_k: price.copy()),
            mock.patch.object(backtest.factors, "compute_factors",
                              side_effect=lambda *_a, **_k: price.copy()),
            # 注意用 new=(純函式):MagicMock 有 keys(),DataFrame.apply 會把它
            # 誤判成 dict-like 的多函式聚合而炸掉。
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


class MissingProviderFailsClosedTest(unittest.TestCase):
    def test_dynamic_full_with_symbols_and_no_provider_raises(self):
        """dynamic + 正式(非 sample)+ 有 symbols + 無 provider → 必須 raise。

        這正是舊版靜默走靜態池的入口:呼叫端傳了 symbols,所以舊條件
        (`symbols is None`)不成立,provider 永遠不會被補上。
        """
        with self.assertRaisesRegex(RuntimeError, "PIT 候選池 provider"):
            backtest.backtest_portfolio(
                symbols=["A", "B"], sample=False, dynamic_enabled=True,
            )

    def test_prepare_panel_and_factor_ic_share_the_same_gate(self):
        """直接呼叫 _prepare_panel / factor_ic 的研究腳本也要被同一道閘門擋。"""
        with self.assertRaisesRegex(RuntimeError, "PIT 候選池 provider"):
            backtest._prepare_panel(
                ["A", "B"], 0.0, None, None, dynamic_enabled=True,
            )
        with self.assertRaisesRegex(RuntimeError, "PIT 候選池 provider"):
            backtest.factor_ic(
                symbols=["A", "B"], sample=False, dynamic_enabled=True,
            )

    def test_error_message_points_at_the_pit_entry_point(self):
        """錯誤訊息必須給出正式入口,而不是引導去開未來池逃生門。"""
        with self.assertRaises(RuntimeError) as ctx:
            backtest.backtest_portfolio(
                symbols=["A"], sample=False, dynamic_enabled=True,
            )
        msg = str(ctx.exception)
        self.assertIn("historical_pit_universe", msg)
        self.assertIn("static_universe_comparator=True", msg)
        self.assertNotIn("SWING_ALLOW_FUTURE_POOL", msg)

    def test_non_dynamic_full_run_also_needs_explicit_comparator(self):
        """關掉 dynamic universe 也是 legacy 單日池,同樣必須顯式宣告成對照組。"""
        with self.assertRaisesRegex(RuntimeError, "static_universe_comparator=True"):
            backtest.backtest_portfolio(
                symbols=["A", "B"], sample=False, dynamic_enabled=False,
            )

    def test_sample_smoke_is_still_allowed_but_labeled(self):
        """sample smoke test 不需要 provider,但不可冒充正式證據。"""
        symbols, provider, prov = backtest._resolve_universe_source(
            ["A"], sample=True, dynamic_enabled=True, universe_provider=None,
            static_universe_comparator=False, caller="t",
        )
        self.assertIsNone(provider)
        self.assertFalse(prov["formal_evidence_eligible"])
        self.assertFalse(prov["candidate_pool_pit"])


class ProviderConsistencyTest(unittest.TestCase):
    def test_symbols_outside_provider_union_raises(self):
        """symbols 與 provider 聯集不一致(含聯集外的股票)→ raise。

        多出來的股票代表候選池已經不是由 PIT 規則決定,若放行,provider 的
        metadata 會替一組不是它決定的 universe 背書。
        """
        with self.assertRaisesRegex(ValueError, "不在 PIT 候選池"):
            backtest._resolve_universe_source(
                ["A", "ZZZZ"], sample=False, dynamic_enabled=True,
                universe_provider=_provider(top_n=2),
                static_universe_comparator=False, caller="t",
            )

    def test_subset_is_allowed_and_counted(self):
        """只允許縮小(資料品質黑名單),而且要把扣掉幾檔記進 metadata。"""
        symbols, provider, prov = backtest._resolve_universe_source(
            ["A"], sample=False, dynamic_enabled=True,
            universe_provider=_provider(top_n=2),
            static_universe_comparator=False, caller="t",
        )
        self.assertEqual(symbols, ["A"])
        self.assertEqual(prov["candidate_symbols_excluded"], 1)
        self.assertTrue(prov["candidate_pool_pit"])

    def test_provider_and_static_comparator_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "互斥"):
            backtest._resolve_universe_source(
                None, sample=False, dynamic_enabled=True,
                universe_provider=_provider(),
                static_universe_comparator=True, caller="t",
            )


class DynamicWithProviderRunsTest(unittest.TestCase):
    def test_panel_applies_monthly_pit_membership(self):
        """dynamic full + 正確 provider → 通過,且候選資格逐月由 provider 決定。"""
        provider = _provider(top_n=1)
        with _PanelEnv():
            panel = backtest._prepare_panel(
                ["A", "B"], 0.0, None, None, dynamic_enabled=True,
                universe_top_n=10, keep_non_members=True,
                universe_provider=provider,
            )
        self.assertFalse(panel.empty)
        meta = panel.attrs["universe"]
        self.assertTrue(meta["candidate_pool_pit"])
        self.assertTrue(meta["formal_evidence_eligible"])
        self.assertEqual(meta["candidate_rule"],
                         "month_M_uses_only_calendar_month_M_minus_1")
        # 2 月只有 A 是候選(用完整 1 月建),3 月只有 B(用完整 2 月建)。
        feb = panel[panel["date"].between("2026-02-01", "2026-02-27")]
        mar = panel[panel["date"].between("2026-03-01", "2026-03-31")]
        self.assertEqual(set(feb.loc[feb["in_candidate_pool"], "stock_id"]), {"A"})
        self.assertEqual(set(mar.loc[mar["in_candidate_pool"], "stock_id"]), {"B"})
        self.assertEqual(set(mar.loc[mar["in_dynamic_universe"], "stock_id"]), {"B"})

    def test_summary_marks_pit_run_as_formal_evidence_eligible(self):
        provider = _provider(top_n=2)
        with _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=["A", "B"], sample=False, dynamic_enabled=True,
                universe_top_n=10, rebalance_every=5, top_n=2,
                universe_provider=provider,
            )
        u = res["summary"]["universe"]
        self.assertTrue(u["candidate_pool_pit"])
        self.assertTrue(u["formal_evidence_eligible"])
        self.assertEqual(u["candidate_pool_asof"], "2026-03-31")


class StaticComparatorTest(unittest.TestCase):
    def test_static_comparator_must_be_explicit(self):
        """legacy 單日池不可再靠預設值進來(見 MissingProviderFailsClosedTest),
        顯式打開才放行。"""
        with _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=["A", "B"], sample=False, dynamic_enabled=True,
                universe_top_n=10, rebalance_every=5, top_n=2,
                static_universe_comparator=True,
            )
        u = res["summary"]["universe"]
        self.assertTrue(u["static_universe_comparator"])
        self.assertFalse(u["candidate_pool_pit"])
        self.assertFalse(u["formal_evidence_eligible"],
                         "static 對照組不可被當成正式證據")
        self.assertIn("非 PIT", u["evidence_note"])
        self.assertIn("不可作正式證據", u["evidence_note"])

    def test_run_full_static_mode_is_flagged(self):
        """`main.py --static-universe` 這條對照路徑刻意保留,但要自動標記。"""
        dates = pd.bdate_range("2026-01-01", periods=120)

        def fake_portfolio(*_a, **kwargs):
            start = pd.Timestamp(kwargs.get("start_date") or dates[0])
            end = pd.Timestamp(kwargs.get("end_date") or dates[-1])
            eq = dates[(dates >= start) & (dates <= end)]
            calls.append(kwargs.copy())
            return {
                "summary": {
                    "n_trades": 1, "ann_ret": 0.1, "sharpe": 1.0,
                    "max_drawdown": -0.1, "cum_ret": 0.1,
                    "data": {"integrity_bypassed": False},
                    "universe": {"survivorship_free": False},
                    "eval_audit": {"eval_window": [str(eq[0].date()),
                                                   str(eq[-1].date())]},
                },
                "equity_curve": pd.DataFrame({"date": eq, "equity": 1.0}),
                "trades": pd.DataFrame(),
            }

        calls = []
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(backtest.uni, "get_universe", return_value=["A"]),
                mock.patch.object(backtest, "backtest_portfolio",
                                  side_effect=fake_portfolio),
                mock.patch.object(backtest, "factor_ic", return_value=pd.DataFrame()),
                mock.patch.object(config, "OUTPUT_DIR", Path(tmp)),
            ):
                backtest.run_full(sample=False, top_n=1, rebalance_every=1,
                                  dynamic_enabled=False, pool=100,
                                  static_comparator=True)
        self.assertTrue(calls)
        self.assertTrue(all(c["static_universe_comparator"] for c in calls))


class ExternalPicksProvenanceTest(unittest.TestCase):
    def test_external_picks_keep_real_provider_metadata(self):
        """external picks(S19 走的路徑)傳了 provider 時,summary 必須保留
        provider 的真實 metadata,不能只寫 external_picks_by_date。"""
        provider = _provider(top_n=2)
        dates = pd.bdate_range("2026-03-02", "2026-03-20")
        picks = {d: [("A", 80.0, "A")] for d in dates}
        with _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=["A"], sample=False, dynamic_enabled=True,
                rebalance_every=5, top_n=1, picks_by_date=picks,
                universe_provider=provider,
            )
        u = res["summary"]["universe"]
        self.assertEqual(u["picks_source"], "external_picks_by_date")
        self.assertEqual(u["candidate_rule"],
                         "month_M_uses_only_calendar_month_M_minus_1")
        self.assertEqual(u["candidate_pool_asof"], "2026-03-31")
        self.assertTrue(u["candidate_pool_pit"])
        self.assertEqual(u["candidate_symbols_excluded"], 1)

    def test_external_picks_without_provider_cannot_claim_pit(self):
        """沒傳 provider 時引擎無法驗證候選池 → 誠實標記,不猜。"""
        dates = pd.bdate_range("2026-03-02", "2026-03-20")
        picks = {d: [("A", 80.0, "A")] for d in dates}
        with _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=["A"], sample=False, dynamic_enabled=True,
                rebalance_every=5, top_n=1, picks_by_date=picks,
            )
        u = res["summary"]["universe"]
        self.assertFalse(u["candidate_pool_pit"])
        self.assertFalse(u["formal_evidence_eligible"])


class HistoricalPITEntryPointTest(unittest.TestCase):
    def test_entry_point_gives_engine_ready_kwargs(self):
        """新策略取得月頻 PIT 候選池要是預設、最短路徑。"""
        provider = _provider(top_n=2)
        with mock.patch.object(MonthlyPITUniverseProvider, "from_cache",
                               return_value=provider) as from_cache:
            pit = historical_pit_universe(candidate_pool_n=7)
        from_cache.assert_called_once_with(
            top_n=7, min_obs=config.DYNAMIC_UNIVERSE_MONTHLY_MIN_OBS)
        kwargs = pit.backtest_kwargs()
        self.assertEqual(kwargs["symbols"], ["A", "B"])
        self.assertIs(kwargs["universe_provider"], provider)
        self.assertFalse(kwargs["sample"])
        self.assertTrue(kwargs["dynamic_enabled"])
        # 直接餵給引擎不會被閘門擋(意圖已經完整表達)。
        symbols, resolved, prov = backtest._resolve_universe_source(
            kwargs["symbols"], sample=kwargs["sample"],
            dynamic_enabled=kwargs["dynamic_enabled"],
            universe_provider=kwargs["universe_provider"],
            static_universe_comparator=False, caller="t",
        )
        self.assertTrue(prov["formal_evidence_eligible"])

    def test_blacklist_is_subtracted_and_reported(self):
        provider = _provider(top_n=2)
        with mock.patch.object(MonthlyPITUniverseProvider, "from_cache",
                               return_value=provider):
            pit = historical_pit_universe(exclude=["B"])
        self.assertEqual(pit.symbols, ["A"])
        self.assertEqual(pit.excluded, ("B",))
        self.assertEqual(pit.metadata()["candidate_symbols_excluded"], 1)

    def test_empty_after_blacklist_fails_closed(self):
        provider = _provider(top_n=2)
        with mock.patch.object(MonthlyPITUniverseProvider, "from_cache",
                               return_value=provider):
            with self.assertRaisesRegex(ValueError, "拒絕降級"):
                historical_pit_universe(exclude=["A", "B"])


class FormalEntriesDoNotUseStaticPoolTest(unittest.TestCase):
    """正式入口不得再拿單日靜態池當 PIT 替代品(結構性釘住)。"""

    def test_formal_entry_sources_have_no_get_research_candidates(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        formal = ["main.py", "forward_test.py", "strategies/s19_chip_momentum.py"]
        for rel in formal:
            src = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn(
                "get_research_candidates(", src,
                f"{rel} 是正式入口,不可用 legacy 單日靜態池當 PIT 替代品",
            )

    def test_backtest_no_longer_guesses_intent_from_symbols_is_none(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "backtest.py").read_text(encoding="utf-8")
        self.assertNotIn("and not sample and symbols is None", src,
                         "PIT 強制不可再靠 symbols is None 推測呼叫端意圖")


if __name__ == "__main__":
    unittest.main()
