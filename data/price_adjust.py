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
CAPITAL_REDUCTION_DATASET = "TaiwanStockCapitalReductionReferencePrice"
_OHLC = ["open", "high", "low", "close"]

# 除權息因子的合理區間。<0.5 通常不是單純除權息(可能是分割/大配股/壞列),
# >1.02 也不合理(除權息只會降參考價)。
#
# 2026-08-15 修:超出區間的事件**過去是靜默 drop**,而且不留任何痕跡。
# 實測 1808 潤隆 2024-09-26:before=119.50 / after=52.95 → factor=0.443096,
# 低於 FACTOR_MIN 被丟掉且不報錯,結果「已還原」序列在該日仍留著 raw 的
# 119.5 → 53.5(-55.23%)。官方 TaiwanStockPriceAdj 在同一天的因子階梯正是
# ×2.256652(= 1/0.443134),證明那是真事件。
# 現在超出區間的事件會被記進 `uncovered`,由呼叫端決定怎麼處理,不再消失。
FACTOR_MIN = 0.50
FACTOR_MAX = 1.02


def _snapshot_tag() -> str:
    return getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"


def _snapshot_end() -> str:
    snap = _snapshot_tag()
    return snap if snap != "live" else pd.Timestamp.today().strftime("%Y-%m-%d")


def fetch_dividend_events(stock_id: str, refresh: bool = False) -> pd.DataFrame:
    """抓單檔的除權息結果(含快照戳快取)。回傳 date / factor / in_range。

    **不再靜默丟棄**超出合理區間的事件:它們照樣回傳,只是 `in_range=False`,
    由 `fetch_adjustment_events()` 決定是被減資資料解釋掉,還是列為未涵蓋。
    """
    snap = _snapshot_tag()
    cache = config.CACHE_DIR / f"divresult__{stock_id}__{snap}.pkl"
    if cache.exists() and not refresh:
        try:
            cached = pd.read_pickle(cache)
            if "in_range" in cached.columns:
                return cached
            # 舊格式(已被過濾過、沒有 in_range 欄):重抓,否則被丟掉的事件
            # 永遠回不來,而那正是這次要修的 bug。
        except Exception:
            pass
    # 重用資料層的 Authorization header + 有界重試。舊版把 token 放在 query string，
    # 可能進入 proxy/access log；且失敗回空表會讓未還原價冒充還原成功。
    import data as data_mod
    raw = data_mod.fetch_finmind_dataset(
        DIVIDEND_RESULT_DATASET, str(stock_id), "2000-01-01", _snapshot_end()
    )
    cols = ["date", "factor", "in_range"]
    if raw.empty:
        out = pd.DataFrame(columns=cols)
        out.to_pickle(cache)
        return out
    d = raw.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    before = pd.to_numeric(d.get("before_price"), errors="coerce")
    after = pd.to_numeric(d.get("after_price"), errors="coerce")
    d["factor"] = after / before
    d = d[d["date"].notna() & d["factor"].notna()]
    d["in_range"] = d["factor"].between(FACTOR_MIN, FACTOR_MAX)
    out = d[cols].sort_values("date").reset_index(drop=True)
    out.to_pickle(cache)
    return out


def fetch_capital_reduction_events(stock_id: str,
                                   refresh: bool = False) -> pd.DataFrame:
    """抓減資參考價事件(全市場一次抓 + 快照戳快取),回傳該檔的 date / factor。

    為什麼一定要接:減資**完全不在** `TaiwanStockDividendResult` 裡,而全市場
    2015~2026 共 532 筆減資中有 **139 筆(26.1%)的價格跳幅小於 0.11** ——
    也就是 `PRICE_INTEGRITY_RETURN_THRESHOLD` 的殘留斷點掃描**結構上看不到**。
    實例:1808 2025-11-24 的 raw 報酬只有 +2.61%(遠低於門檻),正確報酬是
    -4.87%,單日誤差 7.5 個百分點 —— 對 8% 硬停損是決定性的。
    「自建還原不涵蓋減資 + 掃描看不到小幅減資」是雙盲,必須從資料源頭補。

    因子與除權息同形式(after / before):減資後股數變少、參考價**上升**,
    所以 factor > 1,回溯還原會把減資前的價格放大到今天的尺度。
    """
    snap = _snapshot_tag()
    cache = config.CACHE_DIR / f"capred__ALL__{snap}.pkl"
    table = None
    if cache.exists() and not refresh:
        try:
            table = pd.read_pickle(cache)
        except Exception:
            table = None
    if table is None:
        import data as data_mod
        raw = data_mod.fetch_finmind_dataset(
            CAPITAL_REDUCTION_DATASET, None, "2000-01-01", _snapshot_end()
        )
        if raw is None or raw.empty:
            table = pd.DataFrame(columns=["date", "stock_id", "factor", "reason"])
        else:
            d = raw.copy()
            d["date"] = pd.to_datetime(d["date"], errors="coerce")
            before = pd.to_numeric(
                d.get("ClosingPriceonTheLastTradingDay"), errors="coerce")
            after = pd.to_numeric(
                d.get("PostReductionReferencePrice"), errors="coerce")
            d["factor"] = after / before
            d["reason"] = d.get("ReasonforCapitalReduction", "")
            d["stock_id"] = d["stock_id"].astype(str)
            table = d[d["date"].notna() & d["factor"].notna() & (d["factor"] > 0)][
                ["date", "stock_id", "factor", "reason"]
            ].sort_values(["stock_id", "date"]).reset_index(drop=True)
        table.to_pickle(cache)
    sub = table[table["stock_id"] == str(stock_id)]
    return sub[["date", "factor"]].sort_values("date").reset_index(drop=True)


def fetch_adjustment_events(stock_id: str, refresh: bool = False):
    """合併除權息與減資,回傳 `(events, uncovered)`。

    `events`:要套用的因子(date / factor / source)。
    `uncovered`:偵測到、但**沒有任何資料源能解釋**的事件(date / factor)——
    多半是分割、面額變更或壞列。它們不會被套用(硬猜係數比留著缺口更危險),
    但一定要被看見:過去它們是被 `factor.between()` 靜默丟掉的。
    """
    div = fetch_dividend_events(stock_id, refresh=refresh)
    cap = fetch_capital_reduction_events(stock_id, refresh=refresh)

    accepted = []
    if not div.empty:
        ok = div[div["in_range"]].copy()
        ok["source"] = "dividend"
        accepted.append(ok[["date", "factor", "source"]])
    if not cap.empty:
        c = cap.copy()
        c["source"] = "capital_reduction"
        accepted.append(c[["date", "factor", "source"]])

    events = (pd.concat(accepted, ignore_index=True)
              if accepted else pd.DataFrame(columns=["date", "factor", "source"]))
    if not events.empty:
        # 同一天同時被兩個資料源記到時,以減資為準(它給的是官方參考價)。
        events = (events.sort_values(["date", "source"])
                        .drop_duplicates(subset=["date"], keep="last")
                        .sort_values("date").reset_index(drop=True))

    # 超出區間的除權息事件,若當天有減資紀錄就算已被解釋。
    if div.empty:
        uncovered = pd.DataFrame(columns=["date", "factor"])
    else:
        bad = div[~div["in_range"]]
        explained = set(pd.to_datetime(cap["date"])) if not cap.empty else set()
        uncovered = bad[~bad["date"].isin(explained)][["date", "factor"]]
        uncovered = uncovered.sort_values("date").reset_index(drop=True)
    return events, uncovered


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
        out["adj_factor_price"] = 1.0
        out["adj_factor_share"] = 1.0
        return _attach_raw_columns(out)

    dates = pd.to_datetime(out["date"])
    ev = events.sort_values("date")
    ev_dates = list(ev["date"])
    ev_facs = list(ev["factor"])
    anchor = current_anchor()

    if anchor == ANCHOR_LATEST_BAR:
        # back-adjusted:每個 bar 的因子 = **其後**所有事件因子連乘,
        # 最後一根 bar 的因子 = 1(= 今天的真實價)。
        # 代價:新事件會回頭改寫整段歷史 → 凍結的績效無法重現。
        cum = 1.0
        factors_desc = []
        ev_idx = len(ev) - 1
        for t in reversed(range(len(out))):
            d = dates.iloc[t]
            while ev_idx >= 0 and ev_dates[ev_idx] > d:
                cum *= float(ev_facs[ev_idx])
                ev_idx -= 1
            factors_desc.append(cum)
        adj = pd.Series(list(reversed(factors_desc)), index=out.index, dtype=float)
    else:
        # forward-adjusted(預設):每個 bar 的因子 = 1 / **截至當日**所有事件因子
        # 連乘,序列**起點**的因子 = 1。新事件只影響它自己與之後的 bar,
        # **既有歷史值永不改變** —— 這是凍結績效可重現的唯一來源。
        #
        # 兩種錨只差一個常數倍率(所有事件因子的總乘積),所以**報酬完全相同**;
        # 換錨不會改變任何策略的損益百分比。改變的是價格「水準」,而所有看絕對
        # 價位的規則(tick / 整張 / 最低手續費 / 漲跌停)在上一步已經改讀
        # as-traded 欄位,所以水準漂移不會傳到執行層。
        cum = 1.0
        factors_fwd = []
        ev_idx = 0
        for t in range(len(out)):
            d = dates.iloc[t]
            while ev_idx < len(ev_dates) and ev_dates[ev_idx] <= d:
                cum *= float(ev_facs[ev_idx])
                ev_idx += 1
            factors_fwd.append(1.0 / cum if cum else 1.0)
        adj = pd.Series(factors_fwd, index=out.index, dtype=float)

    out["adj_factor"] = adj
    out["adj_factor_price"] = adj
    # 股數/成交量因子:只吃分割與股票股利,**不吃現金股利**(CRSP 與 zipline 的
    # 慣例;LEAN 是另一派)。台股現金股利頻繁而分割罕見,若把現金股利也折進量,
    # 幾乎每一檔的歷史成交量都會被無謂改寫。
    # 目前的事件來源(DividendResult 的 after/before、減資參考價)分不出「這次
    # 有沒有配股」,所以先一律 1.0 並誠實標記 —— 寧可標成未涵蓋,也不要用價格
    # 因子冒充股數因子(兩者在 spin-off / rights / 純現金股利時本來就不相等)。
    out["adj_factor_share"] = 1.0
    out = _attach_raw_columns(out)
    for c in _OHLC:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce") * adj
    return out


RAW_SUFFIX = "_raw"
RAW_COLUMNS = tuple(f"{c}{RAW_SUFFIX}" for c in _OHLC)

# 還原的錨點。**不要用中文簡稱溝通**(中文圈把「錨最新」叫前復權 qfq、
# 「錨起點」叫後復權 hfq,與字面直覺相反,不同來源還會互相打架),
# 一律用錨點名稱。見 PRICE_SCALE_CONTRACT.md §1。
ANCHOR_LATEST_BAR = "latest_bar"      # back-adjusted:最新一根 = 真實價,歷史會被重寫
ANCHOR_SERIES_START = "series_start"  # forward-adjusted:起點 = 真實價,歷史凍結
_VALID_ANCHORS = (ANCHOR_LATEST_BAR, ANCHOR_SERIES_START)


def current_anchor() -> str:
    """目前設定的還原錨點(預設 series_start)。

    預設選 `series_start` 的理由:`latest_bar` 下每次除權息都會回頭改寫整段歷史,
    於是同一個 `SNAPSHOT_END_DATE` 隔一次事件再抓,歷史價格就不一樣 ——
    凍結的績效無法重現,`freeze_manifest` 的承諾在資料層被架空。
    兩種錨只差一個常數倍率,**報酬完全相同**,所以這個選擇不影響任何策略損益。
    """
    anchor = str(getattr(config, "PRICE_ADJUST_ANCHOR", ANCHOR_SERIES_START)).strip()
    if anchor not in _VALID_ANCHORS:
        raise ValueError(
            f"[fail-closed] 未知的還原錨點 {anchor!r};只接受 {_VALID_ANCHORS}")
    return anchor


def _attach_raw_columns(out: pd.DataFrame) -> pd.DataFrame:
    """在覆寫 OHLC **之前**,先把 as-traded 價格另存一份 `*_raw`。

    這是 `PRICE_SCALE_CONTRACT.md` 的鐵則一在程式上的第一步。為什麼非有不可:
    還原價是「今日等值」單位,而台股有一整組規則是看**絕對價位**的 ——
    升降單位(tick 價格帶)、整張 1000 股的資金門檻、20 元最低手續費、
    ±10% 漲跌停。實測 2327 在 2024-06-24 買一張的真實成本是 759,000 元,
    用還原價算只要 147,245 元(5.15 倍),而 12 檔樣本裡有 2 檔還原後落進不同的
    tick 價格帶。少了 `*_raw`,執行層就只能拿還原價去判這些規則。

    這一步**刻意只新增欄位、不改 `close` 的語意**:`close` 目前仍是還原價,
    所有既有消費端行為不變。把 `close` 換回原始價是後續步驟,要與消費端一起翻,
    否則會靜默改變每一份既有結果。
    """
    for c in _OHLC:
        if c in out.columns:
            out[f"{c}{RAW_SUFFIX}"] = pd.to_numeric(out[c], errors="coerce")
    return out


def adjust_price_frame(stock_id: str, price: pd.DataFrame,
                       refresh: bool = False) -> pd.DataFrame:
    """便利包裝:抓事件(除權息 + 減資)+ 套用還原。

    未涵蓋的事件不會被套用,但會寫進 `out.attrs`:
      - `adjustment_uncovered`:未涵蓋事件的 [(日期, 隱含因子)] 清單
      - `adjustment_complete`:沒有未涵蓋事件才是 True
      - `adjustment_sources`:實際套用的因子來源分佈

    這樣「還原過了」與「還原完整」就分得開 —— 過去兩者是同一件事,因為修不了的
    事件被靜默丟掉之後不留痕跡。
    """
    events, uncovered = fetch_adjustment_events(stock_id, refresh=refresh)
    out = adjust_prices(price, events)
    if out is None or not hasattr(out, "attrs"):
        return out
    out.attrs["adjustment_uncovered"] = [
        (str(d)[:10], round(float(f), 6))
        for d, f in zip(uncovered["date"], uncovered["factor"])
    ] if not uncovered.empty else []
    out.attrs["adjustment_complete"] = bool(uncovered.empty)
    out.attrs["adjustment_sources"] = (
        events["source"].value_counts().to_dict() if not events.empty else {})
    return out
