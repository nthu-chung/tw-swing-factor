# -*- coding: utf-8 -*-
"""歷史型資料的 cache key 必須含查詢範圍（P0-2 回歸測試）。

原本的 bug：`data._cache_path()` 的 key 只有 dataset／stock_id／snapshot，
`history_days` 根本沒進檔名，只流向 API 參數。實測在
`config.SNAPSHOT_END_DATE=2026-06-22` 下：

    data.fetch_price('2330')                      -> 482 列 2024-06-24~2026-06-18
    data.fetch_price('2330', history_days=2000)   -> 同樣 482 列，equals() 為 True

也就是說「取得 >3 年、含空頭段的歷史」這件 protocol 列為最優先的事，是一個**靜默
no-op**：沒有 miss、沒有重抓、沒有任何警告，回測窗看起來變長了但資料完全一樣。

這裡釘住：範圍進 key、短範圍快取不得回應長範圍請求、舊格式檔不得被當成任意範圍的
有效命中，以及所有歷史型 fetcher（不只 `fetch_price`）都吃到這條規則。
全部離線：`FINMIND_TOKEN` 被 patch 成空字串，任何誤走真實抓取都會 fail-closed。
"""

from __future__ import annotations

import glob
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import config
import data
import migrate_cache_range

SNAP = "2026-06-22"


def _raw_price() -> pd.DataFrame:
    """FinMind TaiwanStockPrice 的原始欄位名（max/min/Trading_Volume）。"""
    return pd.DataFrame({
        "date": ["2025-01-02", "2025-01-03"],
        "open": [10.0, 11.0],
        "max": [11.0, 12.0],
        "min": [9.5, 10.5],
        "close": [10.5, 11.5],
        "Trading_Volume": [1000, 1100],
        "Trading_money": [10500, 12650],
    })


def _clean_price() -> pd.DataFrame:
    """已正規化的價格表（直接當快取內容用）。"""
    return pd.DataFrame({
        "date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
        "open": [10.0, 11.0],
        "high": [11.0, 12.0],
        "low": [9.5, 10.5],
        "close": [10.5, 11.5],
        "volume": [1000.0, 1100.0],
        "turnover": [10500.0, 12650.0],
    })


class CacheRangeKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        data._legacy_cache_warned.clear()
        for p in (
            mock.patch.object(config, "CACHE_DIR", self.cache),
            mock.patch.object(config, "SNAPSHOT_END_DATE", SNAP),
            # 空 token = 任何真實抓取立刻 fail-closed，測試不可能打網路。
            mock.patch.object(config, "FINMIND_TOKEN", ""),
            # 自建還原價會去讀 divresult（另一條抓取路徑），這裡只測快取 key。
            mock.patch.object(config, "SELF_ADJUST_PRICES", False),
            mock.patch.object(config, "PRICE_DATASET", "TaiwanStockPrice"),
        ):
            p.start()
            self.addCleanup(p.stop)

    # ── key 本身 ────────────────────────────────────────────────────────
    def test_730_and_2000_days_produce_different_cache_keys(self):
        s730 = data.cache_scope("price", "2330", 730)
        s2000 = data.cache_scope("price", "2330", 2000)
        self.assertNotEqual(s730.path, s2000.path)
        self.assertEqual(s730.path.name, f"price__2330__{SNAP}__d730.pkl")
        self.assertEqual(s2000.path.name, f"price__2330__{SNAP}__d2000.pkl")
        # 2026-07-24 的快照戳不可因為加了範圍維度而掉。
        self.assertIn(SNAP, s730.path.name)
        # 範圍戳與實際查詢視窗來自同一次解析。
        self.assertNotEqual(s730.start, s2000.start)
        self.assertEqual(s730.end, s2000.end, "end 由 snapshot 決定，不隨範圍改變")

    def test_history_dataset_cannot_build_key_without_range(self):
        """結構性防呆：歷史型資料集少了範圍維度要 raise，不能悄悄退回舊檔名。"""
        with self.assertRaisesRegex(ValueError, "必須含範圍維度"):
            data.CacheScope(dataset="price", stock_id="2330", snapshot=SNAP, range_tag="")
        with self.assertRaisesRegex(ValueError, "有查詢範圍"):
            data.rangeless_cache_scope("price", "2330")

    def test_rangeless_dataset_keeps_three_part_name(self):
        """全市場清單沒有查詢範圍，維持三段式檔名（不必無謂作廢既有快取）。"""
        scope = data.rangeless_cache_scope("info", "ALL")
        self.assertEqual(scope.path.name, f"info__ALL__{SNAP}.pkl")
        with self.assertRaisesRegex(ValueError, "沒有查詢範圍"):
            data.CacheScope(dataset="info", stock_id="ALL", snapshot=SNAP, range_tag="d730")

    def test_non_positive_history_days_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "正整數"):
            data.cache_scope("price", "2330", -5)

    def test_cache_glob_matches_new_filenames(self):
        """稽核腳本改用 data.cache_glob()，不再自己拼檔名（否則會靜默掃到 0 檔）。"""
        scope = data.cache_scope("price", "2330")
        data._save_cache(scope, _clean_price())
        found = glob.glob(data.cache_glob("price"))
        self.assertEqual([Path(f).name for f in found], [scope.path.name])

    # ── 讀寫行為 ────────────────────────────────────────────────────────
    def test_longer_history_request_never_returns_short_cache(self):
        """原 bug 的直接複現點：730 天的快取不得回應 2000 天的請求。

        修正前 `fetch_price('2330', history_days=2000)` 會命中 730 天的檔案並
        靜默回傳短範圍資料；現在必須 cache miss，離線就 fail-closed raise。
        """
        short = _clean_price()
        data._save_cache(data.cache_scope("price", "2330", 730), short)

        got = data.fetch_price("2330", history_days=730)     # 自己的範圍仍要命中
        self.assertEqual(len(got), len(short))
        self.assertEqual(list(got["date"]), list(short["date"]))

        with self.assertRaises(data.FinMindAPIError):
            data.fetch_price("2330", history_days=2000)
        self.assertFalse(
            data.cache_scope("price", "2330", 2000).path.exists(),
            "抓取失敗不得留下 2000 天的快取檔",
        )

    def test_legacy_cache_without_range_is_not_a_hit(self):
        """舊格式（`dataset__id__snapshot.pkl`）不得被當成任意範圍的有效命中。"""
        legacy = self.cache / f"price__2330__{SNAP}.pkl"
        _clean_price().to_pickle(legacy)

        with self.assertRaises(data.FinMindAPIError):
            data.fetch_price("2330")
        with self.assertRaises(data.FinMindAPIError):
            data.fetch_price("2330", history_days=2000)
        self.assertTrue(legacy.exists(), "只是不採用，不刪使用者的舊快取")

    def test_all_history_fetchers_put_range_in_cache_key(self):
        """不是只修 fetch_price：每個有歷史範圍的 fetcher 都要把範圍寫進檔名。"""
        cases = {
            "price": ("price", lambda d: data.fetch_price("2330", d), _raw_price()),
            "price_limit": ("price_limit", lambda d: data.fetch_price_limits("2330", d),
                            pd.DataFrame({"date": ["2025-01-02"], "reference_price": [100.0],
                                          "limit_up": [110.0], "limit_down": [90.0]})),
            "inst": ("inst", lambda d: data.fetch_institutional("2330", d),
                     pd.DataFrame({"date": ["2025-01-02", "2025-01-02"],
                                   "buy": [500, 300], "sell": [100, 50],
                                   "name": ["Foreign_Investor", "Investment_Trust"]})),
            "margin": ("margin", lambda d: data.fetch_margin("2330", d),
                       pd.DataFrame({"date": ["2025-01-02"],
                                     "MarginPurchaseTodayBalance": [1000],
                                     "ShortSaleTodayBalance": [10],
                                     "MarginPurchaseLimit": [50000],
                                     "MarginPurchaseYesterdayBalance": [900],
                                     "ShortSaleYesterdayBalance": [8]})),
            "lending": ("lending", lambda d: data.fetch_lending("2330", d),
                        pd.DataFrame({"date": ["2025-01-02", "2025-01-02"],
                                      "volume": [1000, 2000]})),
            "fholding": ("fholding", lambda d: data.fetch_foreign_holding("2330", d),
                         pd.DataFrame({"date": ["2025-01-02"],
                                       "ForeignInvestmentSharesRatio": [70.0],
                                       "ForeignInvestmentRemainRatio": [30.0]})),
            "market_index": ("market", lambda d: data.fetch_market_index(d), _raw_price()),
        }
        for label, (dataset, call, raw) in cases.items():
            with self.subTest(label):
                with mock.patch.object(data, "_finmind_get", return_value=raw.copy()):
                    call(1234)
                written = sorted(p.name for p in self.cache.glob(f"{dataset}__*"))
                self.assertTrue(written, f"{label} 沒寫出快取")
                for name in written:
                    self.assertTrue(
                        name.endswith("__d1234.pkl"),
                        f"{label} 的快取檔名少了範圍維度：{name}",
                    )

    def test_market_index_default_range_is_market_history_days(self):
        """TAIEX 預設抓更長（MA200 暖身），範圍戳要反映實際使用的預設值。"""
        expect = f"market__TAIEX__{SNAP}__d{config.MARKET_HISTORY_DAYS}.pkl"
        with mock.patch.object(data, "_finmind_get", return_value=_raw_price()):
            data.fetch_market_index()
        self.assertTrue((self.cache / expect).exists())

    def test_vix_range_change_forces_refetch(self):
        """VIX 的 history_days 透過 period(2y/5y) 影響內容 → 也必須進 key。"""
        hist = pd.DataFrame(
            {"Close": [15.0, 16.0], "High": [16.0, 17.0], "Low": [14.0, 15.0]},
            index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
        )
        asked: list[str] = []

        class _Ticker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, period):
                asked.append(period)
                return hist

        fake_yf = types.SimpleNamespace(Ticker=_Ticker)
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf}):
            data.fetch_vix(history_days=2000)
            data.fetch_vix(history_days=2000)      # 同範圍第二次要命中快取
            data.fetch_vix(history_days=730)       # 不同範圍必須真的重抓
        self.assertEqual(asked, ["5y", "2y"])
        self.assertTrue((self.cache / f"market__VIX__{SNAP}__d2000.pkl").exists())
        self.assertTrue((self.cache / f"market__VIX__{SNAP}__d730.pkl").exists())


class MigrateCacheRangeTest(unittest.TestCase):
    """遷移腳本：預設不動檔案，且只碰 data.py 管理的資料集。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        for p in (
            mock.patch.object(config, "CACHE_DIR", self.cache),
            mock.patch.object(config, "SNAPSHOT_END_DATE", SNAP),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _touch(self, name: str) -> Path:
        path = self.cache / name
        path.write_bytes(b"placeholder")
        return path

    def _touch_frame(self, name: str, days: int) -> Path:
        """寫一份**內容與檔名將宣告的範圍相符**的真快取。

        2026-08-15 第三輪審查後,遷移腳本會開檔驗證內容跨度(原本只看檔名,
        連 `b"placeholder"` 都照樣改名 —— 那正是「檔名與內容脫鉤」的來源)。
        所以要被遷移的檔案必須是合法 DataFrame;仍用 placeholder 的是
        「本來就不該被碰」的檔案,遷移腳本不會讀它們的內容。
        """
        end = pd.to_datetime(SNAP)
        idx = pd.bdate_range(end - pd.Timedelta(days=int(days) - 3), end)
        path = self.cache / name
        pd.DataFrame({"date": idx, "close": 1.0}).to_pickle(path)
        return path

    def test_dry_run_never_touches_files(self):
        old = self._touch_frame(f"price__2330__{SNAP}.pkl", config.HISTORY_DAYS)
        moved = migrate_cache_range.main(apply=False)
        self.assertEqual(moved, 1)
        self.assertTrue(old.exists())
        self.assertEqual([p.name for p in self.cache.glob("*.pkl")], [old.name])

    def test_apply_uses_each_datasets_own_default_range(self):
        d = config.HISTORY_DAYS
        m = config.MARKET_HISTORY_DAYS
        self._touch_frame(f"price__2330__{SNAP}.pkl", d)
        self._touch_frame(f"inst__2330__{SNAP}.pkl", d)
        self._touch_frame(f"market__TAIEX__{SNAP}.pkl", m)
        self._touch_frame(f"market__VIX__{SNAP}.pkl", d)
        # 這些不是 data.py 的命名空間（或本來就沒有範圍維度），不可被改名。
        untouched = [f"info__ALL__{SNAP}.pkl", f"disposition__ALL__{SNAP}.pkl",
                     f"divresult__2330__{SNAP}.pkl", "pitsnap__20260622.pkl"]
        for name in untouched:
            self._touch(name)

        migrate_cache_range.main(apply=True)
        names = {p.name for p in self.cache.glob("*.pkl")}
        self.assertIn(f"price__2330__{SNAP}__d{d}.pkl", names)
        self.assertIn(f"inst__2330__{SNAP}__d{d}.pkl", names)
        self.assertIn(f"market__TAIEX__{SNAP}__d{m}.pkl", names)
        self.assertIn(f"market__VIX__{SNAP}__d{d}.pkl", names)
        for name in untouched:
            self.assertIn(name, names)
        self.assertNotIn(f"price__2330__{SNAP}.pkl", names)

    def test_apply_never_overwrites_an_existing_new_file(self):
        old = self._touch(f"price__2330__{SNAP}.pkl")
        new = self._touch(f"price__2330__{SNAP}__d{config.HISTORY_DAYS}.pkl")
        migrate_cache_range.main(apply=True)
        self.assertTrue(old.exists())
        self.assertEqual(new.read_bytes(), b"placeholder")

    def _write_window(self, name: str, first: str, last: str) -> Path:
        path = self.cache / name
        idx = pd.bdate_range(first, last)
        pd.DataFrame({"date": idx, "close": 1.0}).to_pickle(path)
        return path

    def test_content_beyond_the_snapshot_is_not_renamed(self):
        """內容比快照還新 → 檔名會宣告一個它不具備的範圍,拒絕改名。

        原 bug(2026-08-15 第三輪審查):`plan()` 只看檔名、從不開檔,
        連 `b"placeholder"` 都會被蓋上 `d730` 戳。真實 `_cache/` 有多個 snapshot,
        只要當初以不同 HISTORY_DAYS 抓過、或抓取被 FinMind 402 截斷,
        改名後就等於用檔名替錯的內容永久背書。
        """
        old = self._write_window(f"price__2330__{SNAP}.pkl",
                                 "2025-01-02", "2026-12-31")
        moved = migrate_cache_range.main(apply=True, verbose=False)
        self.assertEqual(moved, 0)
        self.assertTrue(old.exists(), "拒絕改名的檔案必須原封不動")

    def test_content_longer_than_declared_range_is_not_renamed(self):
        """內容涵蓋得比 d{days} 宣告的還長 → 同樣是脫鉤,拒絕改名。"""
        end = pd.to_datetime(SNAP)
        too_early = (end - pd.Timedelta(days=config.HISTORY_DAYS + 400)
                     ).strftime("%Y-%m-%d")
        old = self._write_window(f"price__2330__{SNAP}.pkl", too_early, SNAP)
        moved = migrate_cache_range.main(apply=True, verbose=False)
        self.assertEqual(moved, 0)
        self.assertTrue(old.exists())

    def test_unreadable_cache_is_not_renamed(self):
        old = self._touch(f"price__2330__{SNAP}.pkl")
        moved = migrate_cache_range.main(apply=True, verbose=False)
        self.assertEqual(moved, 0)
        self.assertTrue(old.exists())

    def test_short_coverage_is_allowed_but_reported(self):
        """新上市/下市/當初截斷的檔案分不出來 → 放行但要留註記,不是靜默通過。"""
        end = pd.to_datetime(SNAP)
        late = (end - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
        self._write_window(f"price__2330__{SNAP}.pkl", late, SNAP)
        _, _, rejected = migrate_cache_range.plan(self.cache)
        self.assertEqual(rejected, [])
        ok, note = migrate_cache_range._verify_content(
            self.cache / f"price__2330__{SNAP}.pkl", SNAP, config.HISTORY_DAYS)
        self.assertTrue(ok)
        self.assertIn("未證實", note)


# ── 處置/注意快取:同一個 bug 的第二個現場 ────────────────────────────────
class DispositionCacheRangeTest(unittest.TestCase):
    """處置快取的 key 也必須含查詢範圍(P0-2 第二輪補)。

    原 bug:P0-2 只修了 `data.py` 那一層,但 `twse_disposition` /
    `tpex_disposition` 自己拼 `disposition__ALL__{snapshot}.pkl`,查詢範圍不進
    檔名。實測:先放一份只涵蓋 2026-05-01~05-10 的快取,再以
    `load_disposition('2021-01-01','2026-06-22', [])` 請求 5 年半 →
    `fetch_notice_history` 一次都沒被呼叫(calls=[]),直接回傳那 1 列,零警告。
    這層資料直接決定回測的「處置期間禁新倉」,拿到只涵蓋近期的表 = 更早的
    期間全部被當成「沒被處置」而放行進場。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        for p in (
            mock.patch.object(config, "CACHE_DIR", self.cache),
            mock.patch.object(config, "SNAPSHOT_END_DATE", SNAP),
        ):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _disp(sid: str = "1234") -> pd.DataFrame:
        return pd.DataFrame([{
            "stock_id": sid,
            "disp_start": pd.Timestamp("2026-05-04"),
            "disp_end": pd.Timestamp("2026-05-08"),
            "measure": "人工", "reason": "test", "source": "unit-test",
        }])

    def test_wider_request_does_not_hit_a_narrow_cache(self):
        import twse_disposition

        self._disp().to_pickle(
            twse_disposition.cache_path("2026-05-01", "2026-05-10"))
        calls = []

        def _fetch(start, end):
            calls.append((start, end))
            return pd.DataFrame()          # 離線:當作抓不到,不打網路

        with mock.patch.object(twse_disposition, "fetch_notice_history",
                               side_effect=_fetch):
            out = twse_disposition.load_disposition("2021-01-01", SNAP, [])
        self.assertEqual(calls, [("2021-01-01", SNAP)],
                         "更寬的請求必須 miss 並重抓,不得靜默回傳短範圍快取")
        self.assertTrue(out.empty)

    def test_same_range_hits_without_refetching(self):
        import twse_disposition

        self._disp().to_pickle(twse_disposition.cache_path("2021-01-01", SNAP))
        with mock.patch.object(twse_disposition, "fetch_notice_history",
                               side_effect=AssertionError("不該重抓")):
            out = twse_disposition.load_disposition("2021-01-01", SNAP, [])
        self.assertEqual(len(out), 1)

    def test_tpex_cache_key_carries_the_range_too(self):
        import tpex_disposition

        self._disp().to_pickle(
            tpex_disposition.cache_path("2026-05-01", "2026-05-10"))
        calls = []
        with mock.patch.object(tpex_disposition, "fetch_disposal_history",
                               side_effect=lambda s, e: (calls.append((s, e))
                                                         or pd.DataFrame())):
            tpex_disposition.load_disposition("2021-01-01", SNAP)
        self.assertEqual(calls, [("2021-01-01", SNAP)])

    def test_backtest_consumer_refuses_a_cache_that_does_not_cover_the_window(self):
        """回測讀處置快取時也要驗涵蓋範圍,不能「有檔案就用」。"""
        import tpex_disposition
        import twse_disposition
        from execution import tradability

        self._disp().to_pickle(
            twse_disposition.cache_path("2026-05-01", "2026-05-10"))
        self._disp().to_pickle(
            tpex_disposition.cache_path("2026-05-01", "2026-05-10"))
        days = pd.bdate_range("2021-01-04", "2026-05-10")
        with mock.patch.object(config, "BT_MODEL_DISPOSITION", True):
            with self.assertRaisesRegex(RuntimeError, "未涵蓋"):
                tradability.load_disposition_days(days)

    def test_backtest_consumer_uses_a_covering_cache(self):
        import tpex_disposition
        import twse_disposition
        from execution import tradability

        self._disp("1111").to_pickle(
            twse_disposition.cache_path("2021-01-01", SNAP))
        self._disp("2222").to_pickle(
            tpex_disposition.cache_path("2021-01-01", SNAP))
        days = pd.bdate_range("2026-05-01", "2026-05-12")
        with mock.patch.object(config, "BT_MODEL_DISPOSITION", True):
            out = tradability.load_disposition_days(days)
        self.assertEqual(set(out), {"1111", "2222"})

    def test_legacy_rangeless_disposition_cache_is_not_a_hit(self):
        """舊格式(檔名不含範圍)不得被當成任意範圍的有效命中。"""
        from execution import tradability

        self._disp().to_pickle(self.cache / f"disposition__ALL__{SNAP}.pkl")
        self._disp().to_pickle(self.cache / f"disposition_tpex__ALL__{SNAP}.pkl")
        days = pd.bdate_range("2026-05-01", "2026-05-12")
        with mock.patch.object(config, "BT_MODEL_DISPOSITION", True):
            with self.assertRaisesRegex(RuntimeError, "舊格式"):
                tradability.load_disposition_days(days)


if __name__ == "__main__":
    unittest.main()
