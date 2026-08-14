# -*- coding: utf-8 -*-
"""
自建還原價(back-adjusted price)
================================
FinMind 的 `TaiwanStockPriceAdj` 需付費層(register 層打了回 400),但
**`TaiwanStockDividendResult` 免費可用**,而且直接給每次除權息的
`before_price`(除權息前參考價)與 `after_price`(除權息參考價)。
兩者比值就是該次事件的還原因子,不需要自己推股利換算公式。

    factor(e) = after_price(e) / before_price(e)

回溯還原(back-adjust):把除權息日 e **之前**的所有價格乘上其後所有事件因子的
累積乘積,使整段序列與「今天的股價尺度」一致:

    adj_price[t] = price[t] * Π{ factor(e) : e > t }

為什麼非做不可
--------------
未還原價的除息缺口會被回測當成真實下跌 → 假停損、假 MA 跌破、動能排名被機械性
壓低。台股現金股息殖利率常見 3~5%,而策略的硬停損是 -8% —— 一次除息就可能吃掉
一半的停損空間,這不是小數點誤差,是會系統性改變交易結果的偏誤。

界線(誠實聲明)
---------------
  - 只還原**除權息**。分割/減資/面額變更不在 DividendResult 裡
    (`TaiwanStockCapitalReductionReferencePrice` 在測試的股票上回 0 筆)。
    還原後仍殘留的大跳空由 `price_integrity` 稽核揪出,那些是**真斷點**,
    應排除該股或該區間,不可猜係數硬補。
  - 成交量未還原(配股會使股數膨脹)。本專案的量因子都是**同股票時序比值**
    (vol_ratio = 近5日均量/前5日均量),配股造成的水準跳動會同時出現在分子分母,
    影響有限;但跨股票的量水準比較不應直接用。
  - 還原價是**回溯**定義:今天新增一次除息,昨天以前的還原價會全部改變。
    這對回測是正確的(尺度一致),但表示還原價序列本身不是 PIT 不變量 ——
    快取有快照戳,同一快照內結果可重現。
"""
from __future__ import annotations

import pandas as pd

import config

DIVIDEND_RESULT_DATASET = "TaiwanStockDividendResult"
_OHLC = ["open", "high", "low", "close"]

# 還原因子的合理區間。<0.5 通常不是單純除權息(可能是分割/減資/壞列),
# >1.02 也不合理(除權息只會降參考價)。超出就不套用,留給 price_integrity 揪。
FACTOR_MIN = 0.50
FACTOR_MAX = 1.02


def fetch_dividend_events(stock_id: str, refresh: bool = False) -> pd.DataFrame:
    """抓單檔的除權息結果(含快照戳快取)。回傳 date / factor。"""
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"
    cache = config.CACHE_DIR / f"divresult__{stock_id}__{snap}.pkl"
    if cache.exists() and not refresh:
        try:
            return pd.read_pickle(cache)
        except Exception:
            pass
    end = snap if snap != "live" else pd.Timestamp.today().strftime("%Y-%m-%d")
    # 重用資料層的 Authorization header + 有界重試。舊版把 token 放在 query string，
    # 可能進入 proxy/access log；且失敗回空表會讓未還原價冒充還原成功。
    import data as data_mod
    raw = data_mod.fetch_finmind_dataset(
        DIVIDEND_RESULT_DATASET, str(stock_id), "2000-01-01", end
    )
    if raw.empty:
        out = pd.DataFrame(columns=["date", "factor"])
        out.to_pickle(cache)
        return out
    d = raw.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    before = pd.to_numeric(d.get("before_price"), errors="coerce")
    after = pd.to_numeric(d.get("after_price"), errors="coerce")
    d["factor"] = after / before
    d = d[
        d["date"].notna()
        & d["factor"].notna()
        & d["factor"].between(FACTOR_MIN, FACTOR_MAX)
    ]
    out = d[["date", "factor"]].sort_values("date").reset_index(drop=True)
    out.to_pickle(cache)
    return out


def adjust_prices(price: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """把除權息因子回溯套用到 OHLC。純函數,不改輸入。

    adj[t] = raw[t] * Π{ factor(e) : e > t }

    嚴格 `>`:除權息當日的價格已經是除權後的price,不可再乘自己的因子。
    """
    if price is None or price.empty:
        return price
    out = price.sort_values("date").reset_index(drop=True).copy()
    if events is None or events.empty:
        out["adj_factor"] = 1.0
        return out

    dates = pd.to_datetime(out["date"])
    ev = events.sort_values("date")
    # 每個 bar 的累積因子 = 其後所有事件因子連乘。
    # 由後往前掃:cum 初始 1,遇到事件日之前的 bar 就把該事件因子併入。
    cum = 1.0
    factors_desc = []
    ev_idx = len(ev) - 1
    ev_dates = list(ev["date"])
    ev_facs = list(ev["factor"])
    for t in reversed(range(len(out))):
        d = dates.iloc[t]
        while ev_idx >= 0 and ev_dates[ev_idx] > d:
            cum *= float(ev_facs[ev_idx])
            ev_idx -= 1
        factors_desc.append(cum)
    adj = pd.Series(list(reversed(factors_desc)), index=out.index, dtype=float)

    out["adj_factor"] = adj
    for c in _OHLC:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce") * adj
    return out


def adjust_price_frame(stock_id: str, price: pd.DataFrame,
                       refresh: bool = False) -> pd.DataFrame:
    """便利包裝:抓事件 + 套用還原。"""
    return adjust_prices(price, fetch_dividend_events(stock_id, refresh=refresh))
