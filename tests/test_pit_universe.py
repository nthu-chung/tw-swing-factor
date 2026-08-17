# -*- coding: utf-8 -*-
"""pit_universe.py 單元測試:鎖 PIT 語意與解析(全離線)。"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

from universes import pit_snapshots as pu


TWSE_FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額",
               "開盤價", "最高價", "最低價", "收盤價"]
TPEX_FIELDS = ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價",
               "成交股數", "成交金額(元)"]


def _twse_json(rows):
    return {"stat": "OK", "tables": [
        {"title": "價格指數", "fields": ["指數", "收盤"], "data": [["發行量加權", "43120"]]},
        {"title": "每日收盤行情", "fields": TWSE_FIELDS, "data": rows},
    ]}


class ParseTest(unittest.TestCase):
    def _twse(self, rows, day="2026-07-31"):
        sess = mock.Mock()
        resp = mock.Mock(); resp.json.return_value = _twse_json(rows)
        sess.get.return_value = resp
        with mock.patch.object(pu.time, "sleep"):
            return pu.fetch_twse_day(pd.Timestamp(day), sess)

    def test_parses_and_filters_to_common_stock(self):
        out = self._twse([
            ["2330", "台積電", "68,139,691", "1", "166,661,984,712", "2400", "2430", "2390", "2425"],
            ["0050", "元大台灣50", "1,000", "1", "1,000,000", "10", "10", "10", "10"],   # ETF
            ["24552", "全新二", "1,000", "1", "1,000,000", "10", "10", "10", "10"],      # CB
            ["031001", "權證", "1,000", "1", "1,000,000", "10", "10", "10", "10"],       # 權證
        ])
        self.assertEqual(list(out["stock_id"]), ["2330"])
        r = out.iloc[0]
        self.assertEqual(r["turnover"], 166661984712.0)   # 逗號要被剝掉
        self.assertEqual(r["close"], 2425.0)
        self.assertEqual(r["market"], "TWSE")

    def test_finds_table_by_field_name_not_index(self):
        """TWSE 回 10 張表且順序可能變 —— 必須用欄位名找,不能寫死 index。"""
        sess = mock.Mock(); resp = mock.Mock()
        j = _twse_json([["2330", "台積電", "1", "1", "1000", "1", "1", "1", "1"]])
        j["tables"].reverse()                     # 把目標表換到別的位置
        resp.json.return_value = j
        sess.get.return_value = resp
        with mock.patch.object(pu.time, "sleep"):
            out = pu.fetch_twse_day(pd.Timestamp("2026-07-31"), sess)
        self.assertEqual(list(out["stock_id"]), ["2330"])

    def test_non_trading_day_returns_empty(self):
        sess = mock.Mock(); resp = mock.Mock()
        resp.json.return_value = {"stat": "很抱歉，沒有符合條件的資料!"}
        sess.get.return_value = resp
        with mock.patch.object(pu.time, "sleep"):
            out = pu.fetch_twse_day(pd.Timestamp("2026-08-01"), sess)
        self.assertTrue(out.empty)
        self.assertEqual(list(out.columns), pu.SNAPSHOT_COLUMNS)

    def test_tpex_rejects_stale_date(self):
        """TPEx 對非交易日會回**上一個交易日**的資料,不是空表 —— 必須用回傳 date 擋。"""
        sess = mock.Mock(); resp = mock.Mock()
        resp.json.return_value = {
            "date": "20260731",                       # 回的是 7/31
            "tables": [{"fields": TPEX_FIELDS,
                        "data": [["6182", "合晶", "100", "+1", "99", "101", "98", "100",
                                  "1,000", "100,000"]]}],
        }
        sess.get.return_value = resp
        with mock.patch.object(pu.time, "sleep"):
            out = pu.fetch_tpex_day(pd.Timestamp("2026-08-01"), sess)   # 查 8/1
        self.assertTrue(out.empty, "回傳日期不符查詢日時必須視為非交易日")

    def test_retry_raises_rather_than_faking_empty(self):
        sess = mock.Mock(); sess.get.side_effect = RuntimeError("ChunkedEncodingError")
        with mock.patch.object(pu.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "重試"):
                pu.fetch_twse_day(pd.Timestamp("2026-07-31"), sess, retries=3)
        self.assertEqual(sess.get.call_count, 3)


class PitSemanticsTest(unittest.TestCase):
    def _history(self):
        """3 檔股票 × 60 交易日;成交值排名隨時間反轉。

        月生效日落在第 0 / 22 / 42 個交易日,所以交叉點必須明顯早於 42
        (斜率 13 → 交叉在 i≈30),否則最後一個生效日還沒換人,測不出時間變化。
        """
        days = pd.bdate_range("2026-01-01", periods=60)
        rows = []
        for i, d in enumerate(days):
            rows += [
                {"date": d, "stock_id": "1111", "turnover": 1000 - 13 * i},
                {"date": d, "stock_id": "2222", "turnover": 500},
                {"date": d, "stock_id": "3333", "turnover": 200 + 13 * i},
            ]
        return pd.DataFrame(rows)

    def test_pool_changes_over_time(self):
        pools = pu.build_pit_pools(self._history(), top_n=1, lookback_days=5,
                                   lag_days=1, freq="M")
        picks = [pools[k][0] for k in sorted(pools)]
        self.assertEqual(picks[0], "1111", "前期應選成交值最高的 1111")
        self.assertEqual(picks[-1], "3333", "後期應換成 3333")

    def test_monthly_pool_uses_only_previous_calendar_month(self):
        """8 月池只能看 7 月；6 月舊強者與 8 月當月暴量都不得滲入。"""
        rows = []
        for d in pd.bdate_range("2026-06-01", "2026-06-30"):
            rows += [
                {"date": d, "stock_id": "A", "turnover": 1_000_000},
                {"date": d, "stock_id": "B", "turnover": 1},
            ]
        for d in pd.bdate_range("2026-07-01", "2026-07-31"):
            rows += [
                {"date": d, "stock_id": "A", "turnover": 10},
                {"date": d, "stock_id": "B", "turnover": 100},
            ]
        # 8 月第一個交易日 A 暴量；若偷看當月，排名會被反轉。
        rows += [
            {"date": pd.Timestamp("2026-08-03"), "stock_id": "A", "turnover": 1e12},
            {"date": pd.Timestamp("2026-08-03"), "stock_id": "B", "turnover": 1},
        ]
        pools = pu.build_pit_pools(pd.DataFrame(rows), top_n=1, freq="M")
        self.assertEqual(pools[pd.Timestamp("2026-08-03")], ["B"])

    def test_adding_current_month_rows_cannot_rewrite_month_pool(self):
        """反事實測試：附加 8 月未來資料，8 月既有 universe 必須完全不變。"""
        july = pd.DataFrame([
            {"date": d, "stock_id": sid, "turnover": value}
            for d in pd.bdate_range("2026-07-01", "2026-07-31")
            for sid, value in [("A", 100), ("B", 50)]
        ])
        first_august = pd.DataFrame([
            {"date": "2026-08-03", "stock_id": "A", "turnover": 1},
            {"date": "2026-08-03", "stock_id": "B", "turnover": 1e12},
        ])
        future_august = pd.DataFrame([
            {"date": d, "stock_id": sid, "turnover": value}
            for d in pd.bdate_range("2026-08-04", "2026-08-31")
            for sid, value in [("A", 1), ("B", 1e12)]
        ])
        before = pu.build_pit_pools(
            pd.concat([july, first_august], ignore_index=True), top_n=1, freq="M"
        )
        after = pu.build_pit_pools(
            pd.concat([july, first_august, future_august], ignore_index=True),
            top_n=1, freq="M",
        )
        self.assertEqual(before[pd.Timestamp("2026-08-03")], ["A"])
        self.assertEqual(after[pd.Timestamp("2026-08-03")], ["A"])

    def test_lag_excludes_effective_day(self):
        """lag_days=1 時,生效日當天的資料不得進入排名 —— 這是 PIT 的核心保證。"""
        h = self._history()
        days = sorted(h["date"].unique())
        spike_day = days[40]
        # 2222 是常數 500,在第 40 日本來排第二(3333 已反超)。給它生效日當天的天量:
        # lag_days=1 看不到 → 仍不是第一;lag_days=0 看得到 → 變第一。
        h.loc[(h["date"] == spike_day) & (h["stock_id"] == "2222"), "turnover"] = 1e9

        pools_lag1 = pu.build_pit_pools(h, top_n=1, lookback_days=5, lag_days=1, freq="D")
        pools_lag0 = pu.build_pit_pools(h, top_n=1, lookback_days=5, lag_days=0, freq="D")
        self.assertNotEqual(pools_lag1[pd.Timestamp(spike_day)][0], "2222",
                            "lag_days=1 不可看到生效日當天的天量")
        self.assertEqual(pools_lag0[pd.Timestamp(spike_day)][0], "2222",
                         "lag_days=0 才會看到當天")

    def test_pool_for_date_uses_most_recent_effective(self):
        pools = {pd.Timestamp("2026-01-01"): ["A"], pd.Timestamp("2026-03-01"): ["B"]}
        self.assertEqual(pu.pool_for_date(pools, "2026-02-15"), ["A"])
        self.assertEqual(pu.pool_for_date(pools, "2026-03-01"), ["B"])
        self.assertEqual(pu.pool_for_date(pools, "2025-12-31"), [])


class CachedHistoryCompletenessTest(unittest.TestCase):
    def test_formal_load_fails_when_business_day_cache_is_missing(self):
        """網路漏抓不能靜默變成「那天休市」，否則整月排名會被污染。"""
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            pu.config, "CACHE_DIR", Path(tmp)
        ):
            with self.assertRaisesRegex(RuntimeError, "快照不完整"):
                pu.load_history_cached(
                    start="2026-07-01", end="2026-07-03", require_complete=True,
                )


if __name__ == "__main__":
    unittest.main()
