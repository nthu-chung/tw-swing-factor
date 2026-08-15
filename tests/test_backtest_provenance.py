# -*- coding: utf-8 -*-
"""回測 summary 的完整 provenance(P1-2)離線回歸測試。

這裡釘住的是「結果自己說得出它是怎麼算出來的」。過去 summary 缺的每一項都對應
一個實際發生過、事後無法還原的狀況:

1. **pool as-of 戳錯檔案**:`_prepare_panel` 寫的是
   `build_universe.load_asof(universe_top_n)` —— `universe_top_n` 是**每日**
   dynamic universe 的 top-N(100),不是候選池。真正被套進歷史的是
   `outputs/universe_top{DYNAMIC_UNIVERSE_CANDIDATE_POOL}.json`(300)。
   實測 top100 的 `as_of=2026-06-20`(<= 快照 2026-06-22,看起來完全合規)、
   top300 的 `as_of=2026-08-03`(> 快照 = 未來池),而同一份 metadata 的
   `candidate_source` 誠實寫著 top300 —— summary 自我矛盾,且錯在
   「看起來合規」這個方向。
2. **未來池逃生門只 print**:`SWING_ALLOW_FUTURE_POOL=1` 放行後不進任何 summary
   欄位(價格逃生門至少有 `data.integrity_bypassed`),存進 outputs/ 之後含選股
   前視的績效跟乾淨結果長得一模一樣。
3. **`FACTOR_WEIGHTS` 不在 summary**:最決定性的研究參數沒有 provenance,
   換一組權重重跑,兩份結果沒有任何欄位分得出來。
4. **IS/embargo/OS 邊界、git commit 不在 summary**:數字對不回切割,也對不回
   任何一份程式碼。
5. **全域 config 被就地改寫且還原不完整**:`regime_strategy_lab` 只還原
   `MARKET_FILTER_ENABLED`,把 `MARKET_FILTER_RULE` 永久留在 "vol";
   `market_filter_eval` 把 rule/weight 留在最後一個變體(`('ma60', 0.5)`)。
   summary 現在記**實際生效值**,污染事後看得見;還原漏洞本身也一併修掉。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import backtest
import config
import market_filter_eval
import provenance
import regime_strategy_lab
import universe as uni
from evaluation.splits import build_evaluation_split
from universes import MonthlyPITUniverseProvider


# ── 離線資料層(絕不打網路)────────────────────────────────────────────────
def _factor_frame(start="2026-01-01", end="2026-03-31") -> pd.DataFrame:
    dates = pd.bdate_range(start, end)
    px = 100.0 + np.arange(len(dates)) * 0.1
    return pd.DataFrame({
        "date": dates, "open": px, "high": px * 1.01, "low": px * 0.99,
        "close": px, "volume": 5_000_000.0, "turnover": 5e8,
        "avg_vol_lots": 5_000.0, "trend_ok": True,
    })


class _PanelEnv:
    """`_prepare_panel` / `backtest_portfolio` 需要的資料層 → 離線假資料。"""

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


def _pit_history() -> pd.DataFrame:
    rows = []
    for d in pd.bdate_range("2026-01-01", "2026-03-31"):
        big, small = ("B", "A") if d.month == 2 else ("A", "B")
        rows.append({"date": d, "stock_id": big, "turnover": 9e8})
        rows.append({"date": d, "stock_id": small, "turnover": 1e6})
    return pd.DataFrame(rows)


def _provider(top_n: int = 2) -> MonthlyPITUniverseProvider:
    return MonthlyPITUniverseProvider.from_history(
        _pit_history(), top_n=top_n, min_obs=5)


def _write_pool(out_dir: Path, top_n: int, ids, as_of: str) -> None:
    rows = [{"stock_id": s, "rank": i + 1, "as_of": as_of}
            for i, s in enumerate(ids)]
    (out_dir / f"universe_top{top_n}.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")


class _PoolFiles:
    """建立臨時 `outputs/`,裡面放兩份池:每日 top-N 與真正的候選池。"""

    def __init__(self, *, daily_n=3, daily_asof="2026-01-05",
                 pool_n=5, pool_asof="2026-02-10",
                 snapshot="2026-03-31"):
        self.daily_n, self.daily_asof = daily_n, daily_asof
        self.pool_n, self.pool_asof = pool_n, pool_asof
        self.snapshot = snapshot
        self.ids = [f"S{i:02d}" for i in range(pool_n)]

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        out = Path(self._tmp.name)
        _write_pool(out, self.daily_n, self.ids[:self.daily_n], self.daily_asof)
        _write_pool(out, self.pool_n, self.ids, self.pool_asof)
        self._patches = [
            mock.patch.object(config, "OUTPUT_DIR", out),
            mock.patch.object(config, "DYNAMIC_UNIVERSE_CANDIDATE_POOL", self.pool_n),
            mock.patch.object(config, "SNAPSHOT_END_DATE", self.snapshot),
        ]
        for p in self._patches:
            p.start()
        uni.reset_future_pool_bypass_log()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        uni.reset_future_pool_bypass_log()
        return False


class CandidatePoolAsOfTest(unittest.TestCase):
    """pool as-of 必須等於**實際使用的候選池那份檔案**的 as_of。"""

    def test_pool_asof_is_not_the_daily_top_n_file(self):
        with _PoolFiles() as pools:
            prov = backtest._legacy_pool_provenance(
                pools.ids, dynamic_enabled=True, universe_top_n=pools.daily_n)
        self.assertEqual(prov["candidate_pool_asof"], pools.pool_asof)
        self.assertEqual(prov["candidate_pool_top_n"], pools.pool_n)
        self.assertEqual(prov["candidate_pool_file"],
                         f"outputs/universe_top{pools.pool_n}.json")
        self.assertNotEqual(
            prov["candidate_pool_asof"], pools.daily_asof,
            "戳的是每日 top-N 那份的 as_of —— 正是原 bug:池是 top300、"
            "戳卻來自 top100,而且戳出來的日期看起來合規",
        )
        # 每日 top-N 與候選池是兩件事,兩者都要記且不可互相冒充。
        self.assertEqual(prov["dynamic_universe_top_n"], pools.daily_n)

    def test_blacklisted_subset_still_resolves_to_the_same_pool(self):
        """扣掉資料品質黑名單(子集)仍算同一份池,as_of 不變。"""
        with _PoolFiles() as pools:
            prov = backtest._legacy_pool_provenance(
                pools.ids[:-1], dynamic_enabled=True,
                universe_top_n=pools.daily_n)
        self.assertEqual(prov["candidate_pool_asof"], pools.pool_asof)
        self.assertEqual(prov["candidate_pool_top_n"], pools.pool_n)

    def test_subset_of_two_pools_never_resolves_to_the_smaller_older_one(self):
        """symbols 同時是小池與大池的子集時,不可回傳小池的 as_of。

        原 bug(2026-08-15 修):比對迴圈由小到大找第一個 superset,理由寫成
        「最小的 superset 就是實際用的那一份」—— 那只在 symbols 等於整份池
        (頂多扣黑名單)時成立。實測 daily top3(as_of 2026-01-05)+ 真候選池
        top5(as_of 2026-08-03)+ 快照 2026-03-31,傳兩者的共同子集會解析到
        top3,summary 因此戳上一個看起來合規的日期,`future_pool_bypassed`
        也跟著變成 False —— 未來池冒充乾淨池,只是換了觸發條件。
        """
        with _PoolFiles(pool_asof="2026-08-03", snapshot="2026-03-31") as pools:
            prov = backtest._legacy_pool_provenance(
                pools.ids[:2], dynamic_enabled=True,
                universe_top_n=pools.daily_n)
        self.assertEqual(prov["candidate_pool_resolved_by"],
                         "expected_candidate_pool")
        self.assertEqual(prov["candidate_pool_top_n"], pools.pool_n)
        self.assertEqual(prov["candidate_pool_asof"], "2026-08-03")
        self.assertNotEqual(prov["candidate_pool_asof"], pools.daily_asof)

    def test_subset_still_triggers_future_pool_detection(self):
        """接續上一條:戳對池之後,未來池偵測必須跟著成立。"""
        with _PoolFiles(pool_asof="2026-08-03", snapshot="2026-03-31") as pools, \
                _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=pools.ids[:2], sample=False, dynamic_enabled=True,
                universe_top_n=pools.daily_n, rebalance_every=5, top_n=2,
                static_universe_comparator=True)
        u = res["summary"]["universe"]
        self.assertTrue(u["future_pool_bypassed"])
        self.assertFalse(u["formal_evidence_eligible"])

    def test_expected_pool_asof_is_checked_even_when_unresolved(self):
        """解析不到實際用的池,也不能讓未來池偵測整條失效。

        `_future_pool_provenance` 原本只比對「解析出來的那份池」的 as_of ——
        解析一歪(或解析不到)就等於沒有偵測。config 宣告的 expected 池永遠要
        比對一次。
        """
        with _PoolFiles(pool_asof="2026-08-03", snapshot="2026-03-31") as pools, \
                _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=["NOT_IN_ANY_POOL"], sample=False, dynamic_enabled=True,
                universe_top_n=pools.daily_n, rebalance_every=5, top_n=1,
                static_universe_comparator=True)
        u = res["summary"]["universe"]
        self.assertIsNone(u["candidate_pool_asof"])
        self.assertTrue(u["future_pool_bypassed"])
        self.assertEqual(u["future_pool_bypass_events"][0]["pool_asof"],
                         "2026-08-03")

    def test_ambiguous_resolution_reports_no_asof_and_downgrades(self):
        """expected 池不涵蓋 symbols 時,別份池的 as_of 只是猜的 → 不給戳。"""
        with _PoolFiles() as pools:
            # config 宣告的候選池檔根本不存在 → 只能往其他池猜
            with mock.patch.object(config, "DYNAMIC_UNIVERSE_CANDIDATE_POOL",
                                   pools.pool_n + 4):
                prov = backtest._legacy_pool_provenance(
                    pools.ids[:2], dynamic_enabled=True,
                    universe_top_n=pools.daily_n)
        self.assertTrue(prov["candidate_pool_asof_ambiguous"])
        self.assertIsNone(prov["candidate_pool_asof"])
        self.assertEqual(prov["candidate_pool_asof_source"],
                         "ambiguous_not_expected_pool")
        meta = dict(prov, formal_evidence_eligible=True)
        backtest._apply_pool_asof_downgrade(meta)
        self.assertFalse(meta["formal_evidence_eligible"])
        self.assertIn("無法確定", meta["evidence_note"])

    def test_unresolvable_pool_reports_none_not_a_plausible_date(self):
        """比對不到池檔時必須回 None,不可拿快照日或別份池頂替。

        頂替出來的戳會讓人以為候選池的 PIT 已經被驗證過(舊版就是拿
        `SNAPSHOT_END_DATE` 當 fallback)。
        """
        with _PoolFiles() as pools:
            prov = backtest._legacy_pool_provenance(
                ["NOT_IN_ANY_POOL"], dynamic_enabled=True,
                universe_top_n=pools.daily_n)
        self.assertIsNone(prov["candidate_pool_asof"])
        self.assertEqual(prov["candidate_pool_asof_source"], "unresolved")
        self.assertNotEqual(prov["candidate_pool_asof"], pools.snapshot)

    def test_summary_reports_the_real_pool_asof(self):
        """走完整引擎:static comparator 的 summary 要拿到真正那份池的 as_of。"""
        with _PoolFiles() as pools, _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=pools.ids, sample=False, dynamic_enabled=True,
                universe_top_n=pools.daily_n, rebalance_every=5, top_n=2,
                static_universe_comparator=True)
        u = res["summary"]["universe"]
        self.assertEqual(u["candidate_pool_asof"], pools.pool_asof)
        self.assertEqual(u["candidate_pool_top_n"], pools.pool_n)
        self.assertEqual(u["candidate_pool_asof_source"],
                         f"universe_top{pools.pool_n}.json")

    def test_provider_metadata_wins_on_pit_path(self):
        """PIT provider 路徑:候選池規則 / pool size / pool as-of 由 provider 決定,
        不可被 legacy 池檔的日期蓋掉。"""
        with _PoolFiles(), _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=["A", "B"], sample=False, dynamic_enabled=True,
                universe_top_n=10, rebalance_every=5, top_n=2,
                universe_provider=_provider(top_n=2))
        u = res["summary"]["universe"]
        self.assertEqual(u["candidate_rule"],
                         "month_M_uses_only_calendar_month_M_minus_1")
        self.assertEqual(u["candidate_pool_top_n"], 2)
        self.assertEqual(u["candidate_pool_asof"], "2026-03-31")
        self.assertTrue(u["candidate_pool_pit"])


class FuturePoolBypassTest(unittest.TestCase):
    """未來池逃生門必須在 summary 留下痕跡(對齊價格逃生門的 integrity_bypassed)。"""

    def test_bypass_event_is_recorded_by_universe_loader(self):
        with mock.patch.object(config, "SNAPSHOT_END_DATE", "2026-06-22"), \
                mock.patch.object(config, "ALLOW_FUTURE_POOL", True):
            uni.reset_future_pool_bypass_log()
            uni._assert_universe_pit("2026-08-03", 300)
            log = uni.future_pool_bypass_log()
        uni.reset_future_pool_bypass_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["pool_asof"], "2026-08-03")
        self.assertEqual(log[0]["pool_top_n"], 300)

    def test_future_pool_shows_up_in_summary_and_blocks_formal_evidence(self):
        """池建構日晚於快照 → summary 標 future_pool_bypassed,且不可作正式證據。"""
        with _PoolFiles(pool_asof="2026-09-09", snapshot="2026-03-31") as pools, \
                mock.patch.object(config, "ALLOW_FUTURE_POOL", True), _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=pools.ids, sample=False, dynamic_enabled=True,
                universe_top_n=pools.daily_n, rebalance_every=5, top_n=2,
                static_universe_comparator=True)
        u = res["summary"]["universe"]
        self.assertTrue(u["future_pool_bypassed"])
        self.assertTrue(u["future_pool_bypass_allowed"])
        self.assertEqual(u["future_pool_bypass_events"][0]["pool_asof"], "2026-09-09")
        self.assertFalse(u["formal_evidence_eligible"])
        self.assertIn("未來池", u["evidence_note"])

    def test_clean_pool_is_not_flagged(self):
        with _PoolFiles() as pools, _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=pools.ids, sample=False, dynamic_enabled=True,
                universe_top_n=pools.daily_n, rebalance_every=5, top_n=2,
                static_universe_comparator=True)
        u = res["summary"]["universe"]
        self.assertFalse(u["future_pool_bypassed"])
        self.assertEqual(u["future_pool_bypass_events"], [])


class _ExternalPicksRun:
    """external picks 路徑(S19 走的那條)的最小離線執行。"""

    def __init__(self):
        self.price = _factor_frame("2026-01-01", "2026-02-27")
        self.dates = list(self.price["date"])
        self.picks = {d: [("A", 80.0, "A")] for d in self.dates}

    def run(self, **kwargs):
        price = self.price.copy()
        with (
            mock.patch.object(backtest, "_assert_price_integrity", lambda *_a, **_k: None),
            mock.patch.object(backtest, "_load_disposition_days", lambda *_a, **_k: {}),
            mock.patch.object(backtest.data, "fetch_price",
                              side_effect=lambda *_a, **_k: price.copy()),
        ):
            return backtest.backtest_portfolio(
                symbols=["A"], sample=False, dynamic_enabled=True,
                rebalance_every=5, top_n=1, picks_by_date=self.picks, **kwargs)


class SummaryProvenanceFieldsTest(unittest.TestCase):
    """規格清單的每一類欄位都要在 summary 裡,而且值正確。"""

    def setUp(self):
        uni.reset_future_pool_bypass_log()
        self.runner = _ExternalPicksRun()

    def test_factor_weights_are_recorded(self):
        weights = {"momentum": 1.0, "flow": 0.25}
        with mock.patch.object(config, "FACTOR_WEIGHTS", weights):
            res = self.runner.run()
        params = res["summary"]["params"]
        self.assertEqual(params["factor_weights"], weights)
        # external picks:引擎沒算 composite,權重當下沒生效 → 必須標清楚。
        self.assertFalse(params["factor_weights_applied"])
        self.assertEqual(params["picks_source"], "external_picks_by_date")

    def test_factor_weights_snapshot_is_a_copy(self):
        """summary 存的是當下的複本,呼叫端事後改 config 不會回頭改寫結果。"""
        weights = {"momentum": 1.0}
        with mock.patch.object(config, "FACTOR_WEIGHTS", weights):
            res = self.runner.run()
            weights["momentum"] = 999.0
        self.assertEqual(res["summary"]["params"]["factor_weights"],
                         {"momentum": 1.0})

    def test_engine_path_marks_factor_weights_as_applied(self):
        with _PoolFiles() as pools, _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=pools.ids, sample=False, dynamic_enabled=True,
                universe_top_n=pools.daily_n, rebalance_every=5, top_n=2,
                static_universe_comparator=True)
        params = res["summary"]["params"]
        self.assertTrue(params["factor_weights_applied"])
        self.assertEqual(params["picks_source"], "engine_composite")

    def test_portfolio_and_strategy_params_are_recorded(self):
        res = self.runner.run()
        p = res["summary"]["params"]
        for key in ("exit_mode", "ma_exit", "trend_stop", "max_hold",
                    "min_composite", "rebalance_every", "rebalance_phase",
                    "max_positions", "top_n", "trend_guard", "stale_exit_days",
                    "let_positions_run"):
            self.assertIn(key, p)
        self.assertEqual(p["top_n"], 1)
        self.assertEqual(p["rebalance_every"], 5)

    def test_strategy_spec_is_recorded_when_supplied(self):
        """走 external picks 的策略,其訊號視窗/權重只在 spec 裡 —— 要進 summary。"""
        from strategies.s19_chip_momentum import SPEC
        res = self.runner.run(strategy_spec=SPEC)
        strat = res["summary"]["params"]["strategy"]
        self.assertEqual(strat["name"], SPEC.name)
        self.assertEqual(strat["signal"], dict(sorted(SPEC.signal.items())))
        self.assertEqual(strat["portfolio"], dict(sorted(SPEC.portfolio.items())))

    def test_no_strategy_spec_means_none_not_a_guess(self):
        self.assertIsNone(self.runner.run()["summary"]["params"]["strategy"])

    def test_price_dataset_and_self_adjust_are_recorded(self):
        res = self.runner.run()
        d = res["summary"]["data"]
        self.assertEqual(d["price_dataset"],
                         getattr(config, "PRICE_DATASET", "TaiwanStockPrice"))
        self.assertEqual(d["self_adjust_prices"], bool(config.SELF_ADJUST_PRICES))
        self.assertIn("integrity_bypassed", d)
        self.assertIn("allow_unadjusted_backtest", d)
        self.assertEqual(d["snapshot_end"], config.SNAPSHOT_END_DATE)

    def test_execution_limit_disposition_and_cost_settings_are_recorded(self):
        res = self.runner.run()
        s = res["summary"]
        self.assertIn("modeled", s["limit_lock"])
        self.assertIn("n_entries_skipped_limit_up", s["limit_lock"])
        self.assertIn("modeled", s["disposition"])
        e = s["execution"]
        self.assertEqual(e["order_size_mode"], config.BT_ORDER_SIZE_MODE)
        self.assertEqual(e["price_limit_source"], config.BT_PRICE_LIMIT_SOURCE)
        self.assertEqual(e["regular_lot_shares"], config.BT_REGULAR_LOT_SHARES)
        self.assertEqual(e["commission_rate"], float(config.BT_FEE))
        self.assertEqual(e["sell_tax_rate"], float(config.BT_TAX))

    def test_dynamic_universe_settings_are_recorded_on_external_picks_path(self):
        """external picks 路徑過去完全沒有 dynamic universe 設定。"""
        u = self.runner.run()["summary"]["universe"]
        self.assertTrue(u["dynamic_enabled"])
        self.assertEqual(u["lookback"], config.DYNAMIC_UNIVERSE_LOOKBACK)
        self.assertEqual(u["min_avg_turnover"], config.DYNAMIC_UNIVERSE_MIN_AVG_TURNOVER)
        self.assertEqual(u["min_avg_volume_lots"],
                         config.DYNAMIC_UNIVERSE_MIN_AVG_VOLUME_LOTS)
        self.assertEqual(u["candidate_pool_n_config"],
                         config.DYNAMIC_UNIVERSE_CANDIDATE_POOL)

    def test_external_picks_without_provider_has_no_invented_pool_asof(self):
        """沒有 provider 就不知道候選池是什麼 —— 舊版在這裡填快照日。"""
        u = self.runner.run()["summary"]["universe"]
        self.assertIsNone(u["candidate_pool_asof"])
        self.assertEqual(u["candidate_pool_asof_source"], "unresolved")

    def test_phase_is_recorded(self):
        res = self.runner.run(rebalance_phase=3)
        self.assertEqual(res["summary"]["params"]["rebalance_phase"], 3)

    def test_git_commit_is_recorded(self):
        res = self.runner.run()
        g = res["summary"]["provenance"]
        for key in ("git_commit", "git_branch", "git_dirty", "git_dirty_file_count"):
            self.assertIn(key, g)
        self.assertEqual(g["git_commit"], provenance.git_state()["git_commit"])

    def test_git_state_is_honest_when_git_is_unavailable(self):
        provenance.reset_cache()
        try:
            with mock.patch.object(provenance, "_git", return_value=None):
                state = provenance.git_state(use_cache=False)
        finally:
            provenance.reset_cache()
        self.assertEqual(state["git_commit"], provenance.UNKNOWN)
        self.assertFalse(state["git_dirty"])

    def test_eval_audit_is_recorded(self):
        a = self.runner.run()["summary"]["eval_audit"]
        self.assertEqual(a["days_beyond_last_pick"], 0)
        self.assertIn("picks_window", a)
        self.assertIn("eval_window", a)


class EvaluationSplitProvenanceTest(unittest.TestCase):
    """IS / embargo / OS 的固定日期要跟著結果走。"""

    def setUp(self):
        self.runner = _ExternalPicksRun()
        self.split = build_evaluation_split(self.runner.dates, embargo_days=5)

    def test_declared_split_is_recorded(self):
        res = self.runner.run(evaluation_split_info=self.split, segment="IS")
        ev = res["summary"]["evaluation"]
        self.assertTrue(ev["split_declared"])
        self.assertEqual(ev["segment"], "IS")
        self.assertEqual(ev["is_window"], list(self.split.is_window))
        self.assertEqual(ev["os_window"], list(self.split.os_window))
        self.assertEqual(ev["embargo_trading_days"], self.split.n_embargo)

    def test_split_dict_is_accepted(self):
        res = self.runner.run(evaluation_split_info=self.split.to_dict(),
                              segment="OS")
        self.assertEqual(res["summary"]["evaluation"]["os_window"],
                         list(self.split.os_window))

    def test_undeclared_split_is_not_guessed(self):
        """沒宣告切割時不可假裝有 —— 這個數字不能事後被稱為 IS 或 OS。"""
        ev = self.runner.run()["summary"]["evaluation"]
        self.assertFalse(ev["split_declared"])
        self.assertIsNone(ev["segment"])
        self.assertIsNone(ev["is_window"])
        self.assertIsNone(ev["os_window"])
        # 全域切割設定仍要記(它決定了下一次切在哪)。
        self.assertEqual(ev["is_os_split_config"], config.IS_OS_SPLIT)
        self.assertEqual(ev["embargo_days_config"], config.EMBARGO_DAYS)

    def test_run_full_passes_split_and_segment_down_to_each_phase(self):
        """正式 IS/OS 掃描的每一段結果都要帶著它自己的段名與切割邊界。"""
        dates = pd.bdate_range("2026-01-01", periods=120)
        calls = []

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
        segments = [c.get("segment") for c in calls if c.get("segment")]
        self.assertEqual(set(segments), {"IS", "OS"})
        for c in calls:
            if c.get("segment"):
                self.assertIsNotNone(c.get("evaluation_split_info"),
                                     "段結果沒有帶切割邊界 = 事後無法判斷它是哪一段")


class ConfigRestoreLeakTest(unittest.TestCase):
    """全域 config 被就地改寫的模組必須完整還原,而且污染要在 summary 看得見。"""

    def setUp(self):
        self.orig = (config.MARKET_FILTER_ENABLED, config.MARKET_FILTER_RULE,
                     config.MARKET_FILTER_RISKOFF_WEIGHT)

    def tearDown(self):
        (config.MARKET_FILTER_ENABLED, config.MARKET_FILTER_RULE,
         config.MARKET_FILTER_RISKOFF_WEIGHT) = self.orig

    def test_summary_records_effective_filter_config_even_when_disabled(self):
        """濾網關著也要記實際的 config 值 —— 這是污染唯一看得見的地方。"""
        config.MARKET_FILTER_ENABLED = False
        config.MARKET_FILTER_RULE = "vol"
        config.MARKET_FILTER_RISKOFF_WEIGHT = 0.5
        mf = _ExternalPicksRun().run()["summary"]["market_filter"]
        self.assertFalse(mf["enabled"])
        self.assertIsNone(mf["rule"], "沒生效的規則不可寫進 rule 欄位")
        self.assertEqual(mf["config_rule"], "vol")
        self.assertEqual(mf["config_riskoff_weight"], 0.5)

    def test_regime_strategy_lab_restores_rule_not_just_enabled(self):
        """原 bug:只還原 MARKET_FILTER_ENABLED,RULE 被永久留在 "vol"。"""
        config.MARKET_FILTER_ENABLED = False
        config.MARKET_FILTER_RULE = "ma200"
        with mock.patch.object(regime_strategy_lab.backtest, "backtest_portfolio",
                               return_value={"summary": {}}) as bt:
            regime_strategy_lab._run(["A"], {"d": []}, filt=True)
        bt.assert_called_once()
        self.assertEqual(config.MARKET_FILTER_RULE, "ma200")
        self.assertFalse(config.MARKET_FILTER_ENABLED)

    def test_regime_strategy_lab_restores_on_exception(self):
        config.MARKET_FILTER_ENABLED = False
        config.MARKET_FILTER_RULE = "ma200"
        with mock.patch.object(regime_strategy_lab.backtest, "backtest_portfolio",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                regime_strategy_lab._run(["A"], {"d": []}, filt=True)
        self.assertEqual(config.MARKET_FILTER_RULE, "ma200")

    def test_market_filter_eval_restores_rule_and_weight(self):
        """原 bug:收尾用 `_set_filter(*orig)`,而它在 enabled=False 時不碰
        rule/weight → 跑完之後全域參數停在最後一個變體 ('ma60', 0.5)。"""
        config.MARKET_FILTER_ENABLED = False
        config.MARKET_FILTER_RULE = "ma200"
        config.MARKET_FILTER_RISKOFF_WEIGHT = 0.0
        # 用一組跟腳本內部基線({"momentum": 1.0})不同的權重,否則「還原了」與
        # 「沒還原」長得一樣,測試會變成空包彈。
        with mock.patch.object(config, "FACTOR_WEIGHTS", {"flow": 0.7}):
            self._run_market_filter_eval()
            self.assertEqual(config.FACTOR_WEIGHTS, {"flow": 0.7})
        self.assertEqual(config.MARKET_FILTER_RULE, "ma200")
        self.assertEqual(config.MARKET_FILTER_RISKOFF_WEIGHT, 0.0)
        self.assertFalse(config.MARKET_FILTER_ENABLED)

    def test_market_filter_eval_restores_on_exception(self):
        config.MARKET_FILTER_ENABLED = False
        config.MARKET_FILTER_RULE = "ma200"
        config.MARKET_FILTER_RISKOFF_WEIGHT = 0.0
        with mock.patch.object(config, "FACTOR_WEIGHTS", {"flow": 0.7}):
            with self.assertRaises(RuntimeError):
                self._run_market_filter_eval(boom=True)
            self.assertEqual(config.FACTOR_WEIGHTS, {"flow": 0.7},
                             "中途 raise 時舊版直接跳過還原")
        self.assertEqual(config.MARKET_FILTER_RULE, "ma200")
        self.assertEqual(config.MARKET_FILTER_RISKOFF_WEIGHT, 0.0)

    def _run_market_filter_eval(self, boom: bool = False):
        dates = pd.bdate_range("2026-01-01", periods=60)
        eq = pd.DataFrame({"date": dates,
                           "equity": np.linspace(1.0, 1.2, len(dates))})
        fake_split = {"is": (str(dates[0].date()), str(dates[30].date())),
                      "os": (str(dates[40].date()), str(dates[-1].date())),
                      "n": len(dates), "split": {"n_embargo": 5}, "eq_full": eq}
        res = {"summary": {"n_trades": 1, "exit_breakdown": {},
                           "market_filter": {"n_filter_exits": 0,
                                             "n_regime_switches": 0}},
               "equity_curve": eq}

        def _run(*_a, **_k):
            if boom:
                raise RuntimeError("boom")
            return res

        with (
            mock.patch.object(market_filter_eval.uni, "get_research_candidates",
                              return_value=["A"]),
            mock.patch.object(market_filter_eval, "_split", return_value=fake_split),
            mock.patch.object(market_filter_eval, "_run", side_effect=_run),
            mock.patch.object(market_filter_eval, "_report", lambda *_a, **_k: None),
        ):
            market_filter_eval.run(pool=5, rebalance=5, pick=2)


if __name__ == "__main__":
    unittest.main()
