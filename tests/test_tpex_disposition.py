# -*- coding: utf-8 -*-
"""tpex_disposition.py 單元測試:全部離線(mock 掉 HTTP),鎖解析與 PIT 語意。"""
from __future__ import annotations

import unittest
from unittest import mock

import pandas as pd

import tpex_disposition as tp


DISP_FIELDS = ["編號", "公布日期", "證券代號", "證券名稱", "累計",
               "處置起訖時間", "處置原因", "處置內容", "收盤價", "本益比", " "]
ATT_FIELDS = ["編號", "證券代號", "證券名稱", "累計",
              "注意交易資訊", "公告日期", "收盤價", "本益比", "link"]


def _disp_row(code, name, ann, period, reason="因連續3個營業日", measure="每5分鐘撮合"):
    return [1, ann, code, name, 3, period, reason, measure, "55.00", "N/A"," "]


class TpexDisposalParseTest(unittest.TestCase):
    def _run(self, rows, start="2026-01-01", end="2026-06-22"):
        with mock.patch.object(tp, "_fetch", return_value=(DISP_FIELDS, rows)):
            return tp.fetch_disposal_history(start, end)

    def test_parses_real_period_and_is_pit_safe(self):
        out = self._run([
            _disp_row("1591", "駿吉-KY", "115/04/14", "115/04/15~115/04/28"),
        ])
        self.assertEqual(len(out), 1)
        r = out.iloc[0]
        self.assertEqual(r["stock_id"], "1591")
        self.assertEqual(r["disp_start"], pd.Timestamp("2026-04-15"))
        self.assertEqual(r["disp_end"], pd.Timestamp("2026-04-28"))
        self.assertEqual(r["source"], "tpex_disposal_actual")
        # 公布日必須早於處置起始日,否則就是前視。
        self.assertLess(r["announce_date"], r["disp_start"])

    def test_convertible_bonds_and_etf_are_excluded(self):
        """5 碼 CB(24552/61828)與 00 開頭 ETF 不是普通股,必須排除。"""
        out = self._run([
            _disp_row("24552", "全新二", "115/06/18", "115/06/22~115/07/03"),
            _disp_row("61828", "合晶八", "115/06/18", "115/06/22~115/07/03"),
            _disp_row("0050", "元大台灣50", "115/06/18", "115/06/22~115/07/03"),
            _disp_row("6182", "合晶", "115/06/18", "115/06/22~115/07/03"),
        ])
        self.assertEqual(list(out["stock_id"]), ["6182"])

    def test_embedded_relative_links_are_stripped(self):
        """名稱/原因欄夾帶 (../..) 與 (./attention.html),不可流進資料。"""
        out = self._run([
            _disp_row(
                "6182",
                "合晶(../../mainboard/listed/company-detail.html?code=6182)",
                "115/06/18", "115/06/22~115/07/03",
                reason="因連續3個營業日達標(./attention.html)",
            ),
        ])
        self.assertEqual(out.iloc[0]["name"], "合晶")
        self.assertNotIn("(", out.iloc[0]["reason"])
        self.assertNotIn("attention.html", out.iloc[0]["reason"])

    def test_boundary_duplicates_are_deduped(self):
        """期間跨查詢邊界時兩段都會回傳同一筆 → 必須去重(實測 2024 有 14 筆)。"""
        dup = _disp_row("6182", "合晶", "115/06/18", "115/06/22~115/07/03")
        out = self._run([dup, dup, _disp_row("1591", "駿吉", "115/04/14", "115/04/15~115/04/28")])
        self.assertEqual(len(out), 2)
        self.assertEqual(sorted(out["stock_id"]), ["1591", "6182"])

    def test_malformed_period_is_dropped_not_guessed(self):
        out = self._run([
            _disp_row("6182", "合晶", "115/06/18", "(無)"),
            _disp_row("1591", "駿吉", "115/04/14", "115/04/15~115/04/28"),
        ])
        self.assertEqual(list(out["stock_id"]), ["1591"])

    def test_empty_result_keeps_full_schema(self):
        """查無資料時欄位仍須完整,否則下游取 announce_date 會 KeyError。"""
        out = self._run([])
        self.assertTrue(out.empty)
        self.assertEqual(list(out.columns), tp.DISP_COLUMNS)


class TpexAttentionParseTest(unittest.TestCase):
    def test_attention_rows_parsed_and_filtered(self):
        rows = [
            [1, "2061", "風青", 43, "漲幅達59%", "115/06/22", "55.50", "N/A", "../x"],
            [2, "24552", "全新二", 7, "轉換公司債", "115/06/22", "10.0", "N/A", "../x"],
        ]
        with mock.patch.object(tp, "_fetch", return_value=(ATT_FIELDS, rows)):
            out = tp.fetch_attention_history("2026-01-01", "2026-06-22")
        self.assertEqual(list(out["stock_id"]), ["2061"])
        self.assertEqual(out.iloc[0]["notice_date"], pd.Timestamp("2026-06-22"))


class TpexFetchRetryTest(unittest.TestCase):
    def test_transient_failure_raises_instead_of_faking_empty(self):
        """瞬斷若回空表,會被當成『該期間無處置』靜默漏資料 —— 必須 raise。"""
        sess = mock.Mock()
        sess.get.side_effect = RuntimeError("ChunkedEncodingError")
        with mock.patch.object(tp.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "重試"):
                tp._fetch("disposal", "2026-01-01", "2026-06-22", sess, retries=3)
        self.assertEqual(sess.get.call_count, 3)

    def test_recovers_when_a_later_attempt_succeeds(self):
        ok = mock.Mock()
        ok.json.return_value = {"tables": [{"fields": DISP_FIELDS, "data": []}]}
        sess = mock.Mock()
        sess.get.side_effect = [RuntimeError("boom"), ok]
        with mock.patch.object(tp.time, "sleep"):
            fields, data_rows = tp._fetch("disposal", "2026-01-01", "2026-06-22", sess, retries=3)
        self.assertEqual(fields, DISP_FIELDS)
        self.assertEqual(data_rows, [])


class BacktestMergeTest(unittest.TestCase):
    """回測端必須合併兩市場,且單邊缺檔時要出聲而不是靜默半套。"""

    def _disp_frame(self, sid, start, end):
        return pd.DataFrame([{
            "stock_id": sid, "disp_start": pd.Timestamp(start),
            "disp_end": pd.Timestamp(end), "measure": "", "reason": "",
            "source": "test",
        }])

    def test_merges_twse_and_tpex(self):
        import backtest
        days = pd.date_range("2026-04-13", "2026-05-01", freq="B")
        tw = self._disp_frame("2330", "2026-04-15", "2026-04-20")
        tpx = self._disp_frame("6182", "2026-04-22", "2026-04-28")

        def fake_exists(self):
            return True

        with (
            mock.patch.object(backtest.config, "BT_MODEL_DISPOSITION", True),
            mock.patch.object(type(backtest.config.CACHE_DIR / "x"), "exists", fake_exists),
            mock.patch.object(backtest.pd, "read_pickle", side_effect=[tw, tpx]),
        ):
            out = backtest._load_disposition_days(days)
        self.assertIn("2330", out)
        self.assertIn("6182", out)
        self.assertIn(pd.Timestamp("2026-04-24"), out["6182"])


if __name__ == "__main__":
    unittest.main()
