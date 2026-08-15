# -*- coding: utf-8 -*-
"""證券別過濾(興櫃/DR/創新板/ETF 洩漏)的離線回歸測試。

原 bug(2026-08-15 修)
----------------------
`universe._is_normal_stock(stock_id, market_type)` 收了 `market_type` 卻**完全
沒用它**,實際只檢查「4 碼數字且不以 00 開頭」。實測(repo 快取
`_cache/info__ALL__2026-08-06.pkl`,與 `data.fetch_stock_info` 去重規則相同):

  - TaiwanStockInfo 541 檔 `type=emerging`(興櫃)有 **381 檔通過**這個過濾;
  - 另有 11 檔存託憑證(DR,代號 91xx 也是 4 碼數字)與 29 檔創新板通過;
  - 凍結快照(2026-06-22)下,舊規則通過 2509 檔、本次修正擋掉 408 檔
    (興櫃 369 / 創新板 28 / DR 11);
  - PIT 逐日快照(<= 2026-06-22,1988 檔 4 碼代號)實際混進 28 檔創新板、
    4 檔 DR(9103/9105/9110/9136)與 1 檔興櫃(1780);
  - legacy 單日池 `outputs/universe_top100.json` 也含 1 檔創新板(7610 聯友金屬-創)。

為什麼是「會產生假 Sharpe」等級的缺陷:興櫃沒有 ±10% 漲跌停。2026-05 實測單日
|ret| > 10.5% 的比例為上市 0.034%、上櫃 0.042%、興櫃 **3.872%**(約 100 倍),
興櫃最大單日 +57.17%(6775 穎台科技 2026-05-12)、最小 -24.90%;而動能因子找的
正是那種標的。流動性也擋不住:2026-05 最大一檔興櫃(3595 山太士)日均成交值
14.75 億、全市場 ADV 排名 #188,直接落在 `DYNAMIC_UNIVERSE_CANDIDATE_POOL=300`
之內。

這裡釘住四件事:
  1. 只有上市/上櫃普通股能進池(興櫃/DR/創新板/ETF/ETN/受益證券/特別股全擋);
  2. 證券別資訊缺失時 **fail-closed**(raise 或明確排除並記數,絕不預設放行);
  3. 三個池建構點(`universe` / `pit_universe` / `current_watchlist`)共用**同一份**
     判定 —— patch 一處,三處都跟著改;
  4. 被排除的證券別統計進得了回測 summary(看得出「這份結果用的是哪一種池」)。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import backtest
import config
import current_watchlist
import pit_universe as pu
import security_type as st
import universe as uni


# ── 合成的 TaiwanStockInfo(欄名同 data.fetch_stock_info 的輸出)────────────
def _stock_info() -> pd.DataFrame:
    rows = [
        # (stock_id, market_type, industry, name)
        ("2330", "twse", "半導體業", "台積電"),          # 上市普通股 → 放行
        ("5481", "tpex", "電子零組件業", "新華"),        # 上櫃普通股 → 放行
        ("6775", "emerging", "光電業", "穎台科技"),      # 興櫃 → 擋(無漲跌停)
        ("3595", "emerging", "電子零組件業", "山太士"),  # 興櫃(ADV #188)→ 擋
        ("9105", "twse", "存託憑證", "泰金寶-DR"),       # DR,代號 4 碼數字 → 擋
        ("7631", "twse", "創新板股票", "聚賢研發-創"),   # 創新板(產業別標對)→ 擋
        ("2432", "twse", "創新版股票", "倚天酷碁-創"),   # 創新板(另一種寫法)→ 擋
        ("7835", "twse", "數位雲端", "永悅健康-創"),     # 創新板但產業別沒標 → 擋
        ("3231", "twse", "電腦及週邊設備業", "緯創"),    # 簡稱以「創」結尾的普通股 → 放行
        ("0050", "twse", "ETF", "元大台灣50"),           # ETF → 擋
        ("020019", "twse", "ETN", "統一價值ETN"),        # ETN → 擋
        ("01001T", "twse", "受益證券", "土銀富邦R1"),    # 受益證券 → 擋
        ("2881A", "twse", "金融保險", "富邦特"),         # 特別股(代號形狀)→ 擋
        ("2801", "twse", "金融保險", "彰銀"),            # 普通股但金融 → EXCLUDE_FINANCE 擋
    ]
    return pd.DataFrame(rows,
                        columns=["stock_id", "market_type", "industry", "name"])


def _registry() -> dict:
    return st.build_registry(_stock_info())


class _CleanLog:
    """每個測試都從空的排除紀錄簿開始(紀錄簿是 process 級的)。"""

    def setUp(self):
        st.reset_exclusion_log()
        st.reset_registry()
        self.addCleanup(st.reset_exclusion_log)
        self.addCleanup(st.reset_registry)


class WhitelistTest(_CleanLog, unittest.TestCase):
    def test_only_listed_common_stocks_pass(self):
        kept = st.filter_stock_info(_stock_info(), source="test")
        self.assertEqual(kept, ["2330", "2801", "3231", "5481"])

    def test_each_non_common_security_has_a_distinct_reason(self):
        """每一種非普通股都要有可辨識的排除理由(統計才看得出洩漏的是哪一類)。"""
        st.filter_stock_info(_stock_info(), source="test")
        reasons = {e["stock_id"]: e["reason"] for e in st.exclusion_log()}
        self.assertEqual(reasons["6775"], st.REASON_EMERGING)
        self.assertEqual(reasons["3595"], st.REASON_EMERGING)
        self.assertEqual(reasons["9105"], st.REASON_DR)
        self.assertEqual(reasons["7631"], st.REASON_INNOVATION_BOARD)
        self.assertEqual(reasons["2432"], st.REASON_INNOVATION_BOARD)
        self.assertEqual(reasons["7835"], st.REASON_INNOVATION_BOARD)
        self.assertEqual(reasons["0050"], st.REASON_ETF)
        self.assertEqual(reasons["020019"], st.REASON_ETN)
        self.assertEqual(reasons["01001T"], st.REASON_BENEFICIARY)
        self.assertEqual(reasons["2881A"], st.REASON_CODE_SHAPE)

    def test_code_shape_alone_cannot_tell_dr_from_common_stock(self):
        """DR 與興櫃的代號同樣是 4 碼數字 —— 這正是原本的過濾看不出來的原因。"""
        self.assertTrue(st.is_plausible_equity_code("9105"))   # DR
        self.assertTrue(st.is_plausible_equity_code("6775"))   # 興櫃
        self.assertTrue(st.is_plausible_equity_code("2330"))
        # 形狀相同,證券別判定卻不同 → 判準只能來自 TaiwanStockInfo
        self.assertEqual(st.classify("9105", "twse", "存託憑證", "泰金寶-DR"),
                         st.REASON_DR)
        self.assertEqual(st.classify("6775", "emerging", "光電業", "穎台科技"),
                         st.REASON_EMERGING)
        self.assertEqual(st.classify("2330", "twse", "半導體業", "台積電"), "")


class FailClosedTest(_CleanLog, unittest.TestCase):
    def test_missing_market_type_raises(self):
        """缺 market_type 不得當成可交易 —— 那正是原 bug 的另一種形態。"""
        info = _stock_info()
        info.loc[info["stock_id"] == "2330", "market_type"] = ""
        with self.assertRaises(st.SecurityTypeError) as ctx:
            st.filter_stock_info(info, source="test")
        self.assertIn("2330", str(ctx.exception))

    def test_missing_industry_raises(self):
        """只有 type 分不出 DR / 創新板 / ETF,產業別缺了就是不知道。"""
        info = _stock_info()
        info.loc[info["stock_id"] == "2330", "industry"] = None
        with self.assertRaises(st.SecurityTypeError):
            st.filter_stock_info(info, source="test")

    def test_unknown_industry_category_raises_instead_of_passing(self):
        """沒見過的產業別走白名單 → fail-closed,不靜默放行。"""
        info = _stock_info()
        info.loc[info["stock_id"] == "2330", "industry"] = "量子計算業"
        with self.assertRaisesRegex(st.SecurityTypeError, "無法判定證券別"):
            st.filter_stock_info(info, source="test")

    def test_id_not_in_stock_info_raises(self):
        with self.assertRaisesRegex(st.SecurityTypeError, "9999"):
            st.filter_ids(["2330", "9999"], registry=_registry(), source="test")

    def test_missing_fields_are_counted_when_caller_opts_into_exclude(self):
        kept = st.filter_ids(["2330", "9999"], registry=_registry(),
                             source="test", on_unknown="exclude")
        self.assertEqual(kept, ["2330"])
        summary = st.exclusion_summary()
        self.assertEqual(summary["by_reason"][st.REASON_NOT_IN_REGISTRY], 1)

    def test_there_is_no_allow_escape_hatch(self):
        """`on_unknown` 刻意沒有 'allow':缺證券別就放行正是要修掉的行為。"""
        with self.assertRaises(ValueError):
            st.filter_ids(["2330"], registry=_registry(), source="test",
                          on_unknown="allow")

    def test_empty_stock_info_raises(self):
        with self.assertRaises(st.SecurityTypeError):
            st.build_registry(pd.DataFrame())


class UniverseCallSiteTest(_CleanLog, unittest.TestCase):
    def test_get_universe_drops_emerging_dr_and_innovation_board(self):
        with mock.patch.object(uni.data, "fetch_stock_info",
                               return_value=_stock_info()):
            ids = uni.get_universe(sample=False)
        # 2801 由 EXCLUDE_FINANCE 擋掉;3231 緯創證明「創」結尾不等於創新板
        self.assertEqual(ids, ["2330", "3231", "5481"])
        for leaked in ("6775", "3595", "9105", "7631", "2432", "7835",
                       "0050", "2881A"):
            self.assertNotIn(leaked, ids)

    def test_is_normal_stock_actually_uses_market_type(self):
        """原 bug 的最小重現:同一個代號,只有 market_type 不同。"""
        self.assertTrue(uni._is_normal_stock("6775", "twse", "光電業", "穎台科技"))
        self.assertFalse(
            uni._is_normal_stock("6775", "emerging", "光電業", "穎台科技"))


class PitUniverseCallSiteTest(_CleanLog, unittest.TestCase):
    def _history(self) -> pd.DataFrame:
        rows = []
        for day in pd.bdate_range("2026-05-04", "2026-05-08"):
            for sid in ("2330", "9105", "7631", "0050"):
                rows.append({"date": day, "stock_id": sid, "name": sid,
                             "market": "TWSE", "open": 10.0, "high": 10.0,
                             "low": 10.0, "close": 10.0, "volume": 1000.0,
                             "turnover": 1e8})
        return pd.DataFrame(rows)

    def test_history_filter_drops_dr_and_innovation_board(self):
        st.set_registry(_registry())
        out = pu.apply_security_type_filter(self._history(), source="test")
        self.assertEqual(sorted(out["stock_id"].unique()), ["2330"])

    def test_stale_snapshot_cache_is_still_filtered(self):
        """逐日快照是**建檔時**就篩過的 pickle;過濾必須在載入端也生效。

        否則「快取比程式碼舊」會讓正式月頻池繼續吃到 DR / 創新板 —— 而
        `load_history_cached` 正是 `MonthlyPITUniverseProvider.from_cache` 的來源。
        """
        st.set_registry(_registry())
        history = self._history()
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            for day, chunk in history.groupby("date"):
                chunk.to_pickle(cache_dir / f"pitsnap__{day:%Y%m%d}.pkl")
            with mock.patch.object(pu.config, "CACHE_DIR", cache_dir):
                out = pu.load_history_cached(start="2026-05-04", end="2026-05-08")
        self.assertEqual(sorted(out["stock_id"].unique()), ["2330"])
        self.assertIn(st.REASON_DR, st.exclusion_summary()["by_reason"])

    def test_unknown_code_fails_closed_rather_than_dropping_delisted_silently(self):
        """PIT 池的存在理由是含下市股;查不到證券別要 raise,不是靜默排除。"""
        st.set_registry(_registry())
        history = self._history()
        history.loc[history["stock_id"] == "2330", "stock_id"] = "1234"
        with self.assertRaises(st.SecurityTypeError):
            pu.apply_security_type_filter(history, source="test")


class CurrentWatchlistCallSiteTest(_CleanLog, unittest.TestCase):
    def _payload(self, ids):
        fields = ["證券代號", "證券名稱", "成交股數", "成交金額",
                  "開盤價", "最高價", "最低價", "收盤價"]
        data = [[sid, sid, "1000", "10000", "10", "10", "10", "10"]
                for sid in ids]
        return {"stat": "OK", "tables": [
            {"title": "每日收盤行情(全部)", "fields": fields, "data": data}]}

    def test_live_screen_drops_dr_and_etf(self):
        st.set_registry(_registry())
        session = mock.Mock()
        response = mock.Mock()
        response.json.return_value = self._payload(["2330", "9105", "0050"])
        session.get.return_value = response
        from datetime import date
        out = current_watchlist.fetch_price_day(session, date(2026, 5, 4))
        self.assertEqual(list(out["stock_id"]), ["2330"])

    def test_unknown_id_is_excluded_and_counted_not_allowed(self):
        """live 工具用 `on_unknown='exclude'`:仍然不放行,但會記數。"""
        st.set_registry(_registry())
        mask = current_watchlist._regular_equity_mask(
            pd.Series(["2330", "8888"]))
        self.assertEqual(list(mask), [True, False])
        self.assertEqual(
            st.exclusion_summary()["by_reason"][st.REASON_NOT_IN_REGISTRY], 1)


class SingleImplementationTest(_CleanLog, unittest.TestCase):
    """三處必須共用同一份判定:patch 一處,三處都要跟著變。

    原本三個檔案各寫一份(`universe._is_normal_stock`、`pit_universe._is_stock`、
    `current_watchlist._regular_equity_mask`),所以「哪些證券可以進池」有三個
    答案,修好其中一個不代表另外兩個安全。
    """

    @staticmethod
    def _reject_2330(stock_id, market_type, industry, name):
        return "blocked_by_patch" if str(stock_id) == "2330" else ""

    def test_patching_the_single_rule_changes_all_three_call_sites(self):
        st.set_registry(_registry())
        with mock.patch.object(st, "classify", self._reject_2330):
            with mock.patch.object(uni.data, "fetch_stock_info",
                                   return_value=_stock_info()):
                universe_ids = uni.get_universe(sample=False)
            history = pd.DataFrame([{"date": pd.Timestamp("2026-05-04"),
                                     "stock_id": sid, "turnover": 1e8}
                                    for sid in ("2330", "5481")])
            pit_ids = pu.apply_security_type_filter(
                history, source="test")["stock_id"].tolist()
            mask = current_watchlist._regular_equity_mask(
                pd.Series(["2330", "5481"]))
        self.assertNotIn("2330", universe_ids)
        self.assertNotIn("2330", pit_ids)
        self.assertEqual(list(mask), [False, True])
        # 反面:沒有 patch 時三處都放行同一檔(證明上面不是因為別的原因擋掉)
        with mock.patch.object(uni.data, "fetch_stock_info",
                               return_value=_stock_info()):
            self.assertIn("2330", uni.get_universe(sample=False))

    def test_shape_rule_has_exactly_one_implementation(self):
        """「4 碼非 00」這條規則只准在 security_type 出現一次。

        它在 repo 裡長出四份副本(universe / pit_universe / build_universe /
        twse_disposition),正是「修好一處以為修好全部」的結構性成因。
        """
        import re
        root = Path(__file__).resolve().parent.parent
        skip_dirs = {"tests", "_cache", "outputs", ".venv", "__pycache__"}
        pattern = re.compile(r"isdigit\(\)")
        offenders = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if set(rel.parts) & skip_dirs or rel.name == "security_type.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(rel))
        self.assertEqual(offenders, [],
                         "代號形狀規則必須走 security_type.is_plausible_equity_code")


# ── summary 要看得出「這份結果用的是哪一種池」──────────────────────────────
def _factor_frame(start="2026-01-01", end="2026-03-31") -> pd.DataFrame:
    dates = pd.bdate_range(start, end)
    px = 100.0 + np.arange(len(dates)) * 0.1
    return pd.DataFrame({
        "date": dates, "open": px, "high": px * 1.01, "low": px * 0.99,
        "close": px, "volume": 5_000_000.0, "turnover": 5e8,
        "avg_vol_lots": 5_000.0, "trend_ok": True,
    })


class _PanelEnv:
    """`_prepare_panel` 需要的資料層 → 離線假資料(絕不打網路)。"""

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
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class SummaryDisclosureTest(_CleanLog, unittest.TestCase):
    def test_universe_section_reports_excluded_by_security_type(self):
        """修正會改變候選池組成 → 結果必須自己說得出用的是哪一種池。"""
        st.set_registry(_registry())
        st.filter_stock_info(_stock_info(), source="universe.get_universe")
        with _PanelEnv():
            panel = backtest._prepare_panel(
                ["2330", "5481"], 0.0, None, None,
                dynamic_enabled=False, static_universe_comparator=True)
        excluded = panel.attrs["universe"]["excluded_by_security_type"]
        self.assertEqual(excluded["by_reason"][st.REASON_EMERGING], 2)
        self.assertEqual(excluded["by_reason"][st.REASON_DR], 1)
        self.assertIn("6775", excluded["sample_ids"][st.REASON_EMERGING])
        self.assertEqual(excluded["rule"], "listed_common_stock_whitelist_v1")

    def test_backtest_summary_carries_the_same_field(self):
        st.set_registry(_registry())
        st.filter_stock_info(_stock_info(), source="universe.get_universe")
        with _PanelEnv():
            res = backtest.backtest_portfolio(
                symbols=["2330", "5481"], sample=False, dynamic_enabled=False,
                rebalance_every=5, top_n=2, static_universe_comparator=True)
        excluded = res["summary"]["universe"]["excluded_by_security_type"]
        self.assertGreaterEqual(excluded["total"], 9)
        self.assertIn("universe.get_universe", excluded["by_source"])

    def test_engine_boundary_also_rejects_non_common_industries(self):
        """引擎邊界的次要防線:原本只認字串 'ETF'/'ETN',DR 與受益證券擋不住。"""
        with _PanelEnv(), mock.patch.object(
                backtest.uni, "get_industry_map",
                return_value={"9105": "存託憑證", "2330": "半導體業"}):
            panel = backtest._prepare_panel(
                ["2330", "9105"], 0.0, None, None,
                dynamic_enabled=False, static_universe_comparator=True)
        self.assertEqual(sorted(panel["stock_id"].unique()), ["2330"])


if __name__ == "__main__":
    unittest.main()
