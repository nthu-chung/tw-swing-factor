# -*- coding: utf-8 -*-
"""screener 市場參考交易日的離線回歸測試(P2)。

原本的 bug(這支測試逐條釘住)
------------------------------
`screener.screen()` 讓**每檔股票各自決定自己的「今天」**:

    sub = f[f["date"] <= as_of_ts]
    row = sub.iloc[-1]          # as_of=None 時是 f.iloc[-1]

實測:AAAA 的資料在 2026-04-01 就斷(停牌/下市),BBBB 到 2026-05-20。
`screen(as_of="2026-05-20")` 兩檔**都會**回傳,而 AAAA 那一列的 `date` 是
2026-04-01 —— 一檔停牌 7 週的股票用兩個月前的收盤價冒充當日候選,分數、
`close`、`trend_ok` 全部是舊的,照著下單根本買不到。同一份資料丟
`dynamic_universe.add_membership` 只會回 `['BBBB']`,因為那邊要求「當日必須有
一根 valid bar」——同一條規則在 repo 裡有多份實作,screener 這份漏了它。

現在的行為:先從大盤(TAIEX)序列解出市場參考交易日(`--date` 落在非交易日就退
到最近一個有效交易日),再要求每檔在**那一天**有 bar;只有更舊 bar 的排除並標
`stale_bar`,診斷資訊看得見它停在哪一天。

⚠ 本檔全部離線(HTTP 全 mock),合成資料只驗證程式行為,不代表任何策略績效。
"""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np
import pandas as pd

import config
import screener


# ── 合成市場行事曆與價量 ─────────────────────────────────────────────────
# 用工作日當交易日;2026-05-20 是週三(有效交易日),2026-05-23/24 是週末。
MARKET_DAYS = pd.bdate_range("2026-01-01", "2026-06-19")


def _market_frame(days: pd.DatetimeIndex = MARKET_DAYS) -> pd.DataFrame:
    n = len(days)
    close = 15000.0 * (1.0 + np.linspace(0.0, 0.10, n))
    return pd.DataFrame({
        "date": days,
        "open": close,
        "high": close * 1.005,
        "low": close * 0.995,
        "close": close,
        "volume": np.full(n, 5e9),
        "turnover": np.full(n, 3e11),
    })


def _price_frame(last_day: str, *, days: pd.DatetimeIndex = MARKET_DAYS,
                 start_px: float = 100.0) -> pd.DataFrame:
    """該股的價格序列,在 `last_day` 之後就沒有 bar(= 停牌/下市)。"""
    used = days[days <= pd.Timestamp(last_day)]
    n = len(used)
    close = start_px * (1.0 + np.linspace(0.0, 0.5, n))
    return pd.DataFrame({
        "date": used,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": np.full(n, 3e6),          # 3000 張,穩過流動性門檻
        "turnover": close * 3e6,
    })


def _bundle(last_day: str, start_px: float = 100.0) -> dict:
    price = _price_frame(last_day, start_px=start_px)
    inst = pd.DataFrame({
        "date": price["date"],
        "foreign_net": np.full(len(price), 1e5),
        "trust_net": np.full(len(price), 2e4),
        "dealer_net": np.zeros(len(price)),
        "inst_net": np.full(len(price), 1.2e5),
    })
    return {"price": price, "inst": inst, "margin": pd.DataFrame()}


class ResolveReferenceTradingDayTest(unittest.TestCase):
    """`--date` 先找市場當日/最近有效交易日。"""

    def test_defaults_to_market_last_trading_day(self):
        day = screener.resolve_reference_trading_day(_market_frame(), None)
        self.assertEqual(day, MARKET_DAYS[-1])

    def test_exact_trading_day_is_kept(self):
        day = screener.resolve_reference_trading_day(_market_frame(), "2026-05-20")
        self.assertEqual(day, pd.Timestamp("2026-05-20"))

    def test_non_trading_day_backs_off_to_previous_valid_session(self):
        """--date 給週日 → 退到前一個有效交易日(2026-05-22 週五)。"""
        day = screener.resolve_reference_trading_day(_market_frame(), "2026-05-24")
        self.assertEqual(day, pd.Timestamp("2026-05-22"))
        self.assertIn(day, set(MARKET_DAYS))

    def test_missing_market_calendar_fails_closed(self):
        """沒有行事曆時 raise;不可退回「每檔自己的最後一根 bar」。"""
        for empty in (None, pd.DataFrame(), pd.DataFrame({"date": []})):
            with self.assertRaises(RuntimeError):
                screener.resolve_reference_trading_day(empty, "2026-05-20")

    def test_date_before_calendar_start_fails_closed(self):
        with self.assertRaises(ValueError):
            screener.resolve_reference_trading_day(_market_frame(), "2020-01-01")


class ReferenceBarTest(unittest.TestCase):
    """每檔的 bar 必須等於市場參考交易日。"""

    REF = pd.Timestamp("2026-05-20")

    def test_exact_bar_is_used(self):
        frame = _price_frame("2026-05-20")
        row, status, last_bar = screener.reference_bar(frame, self.REF)
        self.assertEqual(status, screener.REFERENCE_OK)
        self.assertEqual(pd.Timestamp(row["date"]), self.REF)
        self.assertEqual(last_bar, self.REF)

    def test_reference_day_bar_without_trading_is_not_a_candidate(self):
        """參考日有列**還不夠**:全日無成交的 bar,收盤價不是可成交價。

        原缺陷(2026-08-16 修):`reference_bar` 只檢查「參考日有沒有那一列」,
        與本模組宣稱的「與 dynamic_universe.add_membership 語意一致」相反。
        實測把某檔在參考日的 volume/turnover 設為 0,screener 仍把它放進候選、
        `stale_bar` 計數是 0;同一份資料在 `add_membership` 是
        `in_dynamic_universe=False`。兩份判定現在共用 `valid_bar_mask()`。
        """
        frame = _price_frame("2026-05-20")
        zeroed = frame.copy()
        last = pd.to_datetime(zeroed["date"]) == self.REF
        zeroed.loc[last, ["volume", "turnover"]] = 0
        row, status, last_bar = screener.reference_bar(zeroed, self.REF)
        self.assertIsNone(row, "全日無成交不得回列")
        self.assertEqual(status, screener.REFERENCE_NO_TRADE)
        self.assertEqual(last_bar, self.REF)

    def test_no_trade_and_membership_agree(self):
        """同一份資料,screener 與 dynamic_universe 必須給同一個答案。"""
        from universes import dynamic as dyn
        frame = _price_frame("2026-05-20")
        zeroed = frame.copy()
        last = pd.to_datetime(zeroed["date"]) == self.REF
        zeroed.loc[last, ["volume", "turnover"]] = 0
        exact = zeroed[pd.to_datetime(zeroed["date"]) == self.REF]
        self.assertFalse(bool(dyn.valid_bar_mask(exact).iloc[-1]))
        self.assertEqual(screener.reference_bar(zeroed, self.REF)[1],
                         screener.REFERENCE_NO_TRADE)

    def test_missing_columns_are_not_treated_as_tradable(self):
        frame = _price_frame("2026-05-20").drop(columns=["turnover"])
        row, status, _ = screener.reference_bar(frame, self.REF)
        self.assertIsNone(row)
        self.assertEqual(status, screener.REFERENCE_NO_TRADE)

    def test_older_bar_is_stale_not_a_candidate(self):
        """停牌股只有更舊 bar → stale_bar,不回列。

        同時證明「舊寫法會拿到那一根」:frame 裡確實存在 <= ref 的列,
        `f[f.date <= ref].iloc[-1]` 會回 2026-04-01 那一列。
        """
        frame = _price_frame("2026-04-01")
        legacy = frame[frame["date"] <= self.REF].iloc[-1]
        self.assertEqual(pd.Timestamp(legacy["date"]), pd.Timestamp("2026-04-01"))

        row, status, last_bar = screener.reference_bar(frame, self.REF)
        self.assertIsNone(row)
        self.assertEqual(status, screener.REFERENCE_STALE_BAR)
        self.assertEqual(last_bar, pd.Timestamp("2026-04-01"))

    def test_no_history_before_reference_is_no_asof(self):
        frame = _price_frame("2026-06-19")
        row, status, _ = screener.reference_bar(frame, pd.Timestamp("2025-01-02"))
        self.assertIsNone(row)
        self.assertEqual(status, screener.REFERENCE_NO_ASOF)

    def test_empty_frame_is_no_asof(self):
        row, status, _ = screener.reference_bar(pd.DataFrame(), self.REF)
        self.assertIsNone(row)
        self.assertEqual(status, screener.REFERENCE_NO_ASOF)


class ScreenStaleBarTest(unittest.TestCase):
    """端到端(離線):停牌股不得冒充當日候選。"""

    BUNDLES = {
        "AAAA": _bundle("2026-04-01", start_px=100.0),   # 停在 2026-04-01
        "BBBB": _bundle("2026-05-20", start_px=120.0),   # 資料到 2026-05-20
    }

    def _screen(self, **kwargs):
        with (
            mock.patch.object(screener.data, "fetch_market_index",
                              return_value=_market_frame()),
            mock.patch.object(screener.data, "fetch_bundle",
                              side_effect=lambda sid, *a, **k: self.BUNDLES[sid]),
            mock.patch.object(screener.uni, "get_name_map",
                              return_value={"AAAA": "甲", "BBBB": "乙"}),
            mock.patch.object(screener.uni, "get_industry_map",
                              return_value={"AAAA": "電子", "BBBB": "電子"}),
        ):
            return screener.screen(symbols=["AAAA", "BBBB"], verbose=False, **kwargs)

    def test_suspended_stock_is_excluded_and_marked_stale(self):
        # 放寬分數/趨勢門檻,讓「AAAA 不在候選裡」是真的被 stale 擋掉,
        # 而不是因為結果剛好是空表而空包彈。
        with mock.patch.object(config, "TREND_GUARD_ENABLED", False), \
                mock.patch.object(config, "MIN_COMPOSITE", -1.0):
            result = self._screen(as_of="2026-05-20")
        diag = result.attrs["screen_diagnostics"]
        self.assertEqual(diag["reference_trading_day"], "2026-05-20")
        self.assertEqual(diag["skipped"]["stale_bar"], 1)
        self.assertEqual([s["stock_id"] for s in diag["stale_bar"]], ["AAAA"])
        self.assertEqual(diag["stale_bar"][0]["last_bar"], "2026-04-01")
        self.assertFalse(result.empty)
        self.assertNotIn("AAAA", set(result["stock_id"]))

    def test_every_scanned_row_sits_on_the_reference_day(self):
        """通過的每一列 date 都等於參考交易日(不是各自的最後一根)。"""
        with mock.patch.object(config, "TREND_GUARD_ENABLED", False), \
                mock.patch.object(config, "MIN_COMPOSITE", -1.0):
            result = self._screen(as_of="2026-05-20")
        self.assertFalse(result.empty)
        self.assertEqual(set(result["date"]), {"2026-05-20"})
        self.assertEqual(set(result["stock_id"]), {"BBBB"})

    def test_non_trading_date_resolves_before_matching_bars(self):
        """--date 給週末 → 參考日退到 2026-05-22;AAAA 仍是 stale。"""
        result = self._screen(as_of="2026-05-24")
        diag = result.attrs["screen_diagnostics"]
        self.assertEqual(diag["reference_trading_day"], "2026-05-22")
        self.assertEqual(diag["requested_date"], "2026-05-24")
        # BBBB 停在 05-20,05-22 沒有 bar → 這一天兩檔都是 stale。
        self.assertEqual(sorted(s["stock_id"] for s in diag["stale_bar"]),
                         ["AAAA", "BBBB"])
        self.assertTrue(result.empty)

    def test_liquidity_prefilter_only_sees_bars_up_to_the_reference_day(self):
        """`--date` 回看時流動性門檻不得用未來的量(舊版 `price.tail(20)`)。"""
        thin = _bundle("2026-06-19")
        # 參考日之後才爆量;參考日之前一直是低量 → 當時不該通過流動性門檻。
        after = pd.to_datetime(thin["price"]["date"]) > pd.Timestamp("2026-05-20")
        thin["price"].loc[~after, "volume"] = 1e4        # 10 張
        thin["price"].loc[after, "volume"] = 5e7         # 50000 張
        bundles = {"AAAA": thin, "BBBB": self.BUNDLES["BBBB"]}
        with (
            mock.patch.object(screener.data, "fetch_market_index",
                              return_value=_market_frame()),
            mock.patch.object(screener.data, "fetch_bundle",
                              side_effect=lambda sid, *a, **k: bundles[sid]),
            mock.patch.object(screener.uni, "get_name_map", return_value={}),
            mock.patch.object(screener.uni, "get_industry_map", return_value={}),
            mock.patch.object(config, "TREND_GUARD_ENABLED", False),
            mock.patch.object(config, "MIN_COMPOSITE", -1.0),
        ):
            result = screener.screen(symbols=["AAAA", "BBBB"], verbose=False,
                                     as_of="2026-05-20")
        self.assertEqual(result.attrs["screen_diagnostics"]["skipped"]["liquidity"], 1)
        self.assertEqual(set(result["stock_id"]), {"BBBB"})

    def test_diagnostics_exist_even_when_nothing_is_selected(self):
        result = self._screen(as_of="2026-06-19")
        self.assertTrue(result.empty)
        self.assertEqual(result.attrs["screen_diagnostics"]["skipped"]["stale_bar"], 2)


if __name__ == "__main__":
    unittest.main()
