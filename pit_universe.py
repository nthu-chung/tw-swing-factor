# -*- coding: utf-8 -*-
"""
Point-in-time 候選池(survivorship-free)
=========================================
修掉 repo 的第一號已知偏誤:候選池原本是**單一日期**的成交值 top-N,卻套用到
整段回測歷史 —— 等於用「今天知道誰熱門」去決定兩年前能選哪些股。實測舊池 283 檔
裡有 83 檔在回測最初 60 天連成交值前 200 名都排不進去。

資料源(免費,交易所官方,含**當時在交易、後來下市**的股票)
---------------------------------------------------------------
  TWSE:https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX
        ?date=YYYYMMDD&type=ALLBUT0999&response=json
        → tables[8] 「每日收盤行情(全部)」;2026-07-31 有 1373 列 / 1093 檔 4 碼股
  TPEx:https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes
        ?date=YYYY/MM/DD&response=json
        → tables[0] 「上櫃股票行情」;10218 列中 888 檔為 4 碼股(其餘為權證等)

這兩個端點是**逐日快照**:那天在交易的就在,之後下市的當天照樣在。所以由它們
重建的池天然不含 survivorship bias(相對於「用今天的清單回套過去」)。

PIT 規則(關鍵)
---------------
池在 `effective_date` 生效時,只能用 `effective_date - lag_days` 之前(含)的資料:

    5 月的池  ← 用 4 月底為止的成交值排名
    7/31 的池 ← 用 7/30 收盤為止的成交值排名(lag_days=1)

`build_pit_pools` 的 `lag_days` 就是這個。預設 1(嚴格:不含生效當日),
月頻重建時等於「這個月用上個月的資料」。

界線
----
  - 只解決**候選池**這一層的 look-ahead 與倖存者偏誤。個股價格序列若仍從
    FinMind 逐檔抓,下市股仍可能抓不到 —— 但這裡的日快照本身就帶 OHLCV,
    可直接當價格來源(見 `daily_snapshot` 的回傳欄位)。
  - 排名鍵是**成交值**(turnover),不是市值。要做市值 universe 需另補股本資料。
"""
from __future__ import annotations

import time
from typing import Dict, Iterable, List, Optional

import pandas as pd
import requests
import urllib3

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
_SLEEP = 1.0            # 交易所端點,保守限流(與 twse_disposition 一致)

SNAPSHOT_COLUMNS = ["date", "stock_id", "name", "market",
                    "open", "high", "low", "close", "volume", "turnover"]

_HEADERS_TWSE = {"User-Agent": "Mozilla/5.0"}
_HEADERS_TPEX = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.tpex.org.tw/"}


def _num(v) -> float:
    """'1,322,798,650' / '--' / '' → float(NaN)。"""
    s = str(v).replace(",", "").strip()
    if not s or s in {"--", "---", "N/A"}:
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _is_stock(code: str) -> bool:
    """4 碼數字普通股;排除 00 開頭 ETF、權證(6碼)、CB(5碼)。"""
    c = str(code).strip()
    return len(c) == 4 and c.isdigit() and not c.startswith("00")


def _col(fields: List[str], *keys) -> Optional[int]:
    for i, f in enumerate(fields):
        if any(k in str(f) for k in keys):
            return i
    return None


def fetch_twse_day(day: pd.Timestamp, session: requests.Session,
                   retries: int = 3) -> pd.DataFrame:
    """抓 TWSE 某日全市場收盤行情。非交易日回空表。"""
    params = {"date": pd.Timestamp(day).strftime("%Y%m%d"),
              "type": "ALLBUT0999", "response": "json"}
    last = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(_SLEEP * attempt)
            j = session.get(TWSE_URL, params=params, timeout=40, verify=False).json()
            break
        except Exception as e:
            last = e
    else:
        raise RuntimeError(f"[pit] TWSE {params['date']} 重試 {retries} 次失敗:{type(last).__name__}")

    if str(j.get("stat", "")).upper() != "OK":
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    tabs = j.get("tables") or []
    # 找「每日收盤行情」那張(欄位含 證券代號 + 成交金額),不寫死 index
    tab = None
    for t in tabs:
        f = t.get("fields") or []
        if _col(f, "證券代號") is not None and _col(f, "成交金額") is not None:
            tab = t
            break
    if tab is None or not tab.get("data"):
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    f = tab["fields"]
    ix = {k: _col(f, *v) for k, v in {
        "code": ("證券代號",), "name": ("證券名稱",), "volume": ("成交股數",),
        "turnover": ("成交金額",), "open": ("開盤價",), "high": ("最高價",),
        "low": ("最低價",), "close": ("收盤價",)}.items()}
    rows = []
    for r in tab["data"]:
        code = str(r[ix["code"]]).strip()
        if not _is_stock(code):
            continue
        rows.append({
            "date": pd.Timestamp(day), "stock_id": code,
            "name": str(r[ix["name"]]).strip(), "market": "TWSE",
            "open": _num(r[ix["open"]]), "high": _num(r[ix["high"]]),
            "low": _num(r[ix["low"]]), "close": _num(r[ix["close"]]),
            "volume": _num(r[ix["volume"]]), "turnover": _num(r[ix["turnover"]]),
        })
    return pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)


def fetch_tpex_day(day: pd.Timestamp, session: requests.Session,
                   retries: int = 3) -> pd.DataFrame:
    """抓 TPEx 某日全市場收盤行情。非交易日回空表。"""
    params = {"date": pd.Timestamp(day).strftime("%Y/%m/%d"), "response": "json"}
    last = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(_SLEEP * attempt)
            j = session.get(TPEX_URL, params=params, timeout=40, verify=False).json()
            break
        except Exception as e:
            last = e
    else:
        raise RuntimeError(f"[pit] TPEx {params['date']} 重試 {retries} 次失敗:{type(last).__name__}")

    # TPEx 對非交易日會回上一個交易日的資料 → 用回傳的 date 驗證,不符就當非交易日
    got = str(j.get("date", "")).strip()
    want = pd.Timestamp(day).strftime("%Y%m%d")
    if got and got != want:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    tabs = j.get("tables") or []
    tab = None
    for t in tabs:
        f = t.get("fields") or []
        if _col(f, "代號") is not None and _col(f, "成交金額") is not None:
            tab = t
            break
    if tab is None or not tab.get("data"):
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    f = tab["fields"]
    ix = {k: _col(f, *v) for k, v in {
        "code": ("代號",), "name": ("名稱",), "volume": ("成交股數",),
        "turnover": ("成交金額",), "open": ("開盤",), "high": ("最高",),
        "low": ("最低",), "close": ("收盤",)}.items()}
    rows = []
    for r in tab["data"]:
        code = str(r[ix["code"]]).strip()
        if not _is_stock(code):
            continue
        rows.append({
            "date": pd.Timestamp(day), "stock_id": code,
            "name": str(r[ix["name"]]).strip(), "market": "TPEX",
            "open": _num(r[ix["open"]]), "high": _num(r[ix["high"]]),
            "low": _num(r[ix["low"]]), "close": _num(r[ix["close"]]),
            "volume": _num(r[ix["volume"]]), "turnover": _num(r[ix["turnover"]]),
        })
    return pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)


def daily_snapshot(day, refresh: bool = False,
                   session: Optional[requests.Session] = None) -> pd.DataFrame:
    """某日全市場(上市+上櫃)收盤行情,含快取。非交易日回空表。"""
    day = pd.Timestamp(day)
    cache = config.CACHE_DIR / f"pitsnap__{day.strftime('%Y%m%d')}.pkl"
    if cache.exists() and not refresh:
        try:
            return pd.read_pickle(cache)
        except Exception:
            pass
    own = session is None
    if own:
        session = requests.Session()
    try:
        tw = fetch_twse_day(day, _with(session, _HEADERS_TWSE))
        tp = fetch_tpex_day(day, _with(session, _HEADERS_TPEX))
    finally:
        if own:
            session.close()
    out = pd.concat([tw, tp], ignore_index=True) if len(tw) or len(tp) else \
        pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    out.to_pickle(cache)
    return out


def _with(session: requests.Session, headers: dict) -> requests.Session:
    session.headers.update(headers)
    return session


def load_history(start, end, refresh: bool = False,
                 verbose: bool = True) -> pd.DataFrame:
    """抓 [start, end] 每個日曆日的全市場快照,合併成 long panel。

    非交易日回空表、自動略過。逐日快取,重跑只補缺的日子。
    """
    days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="B")
    frames, n_trading = [], 0
    sess = requests.Session()
    try:
        for i, d in enumerate(days, 1):
            snap = daily_snapshot(d, refresh=refresh, session=sess)
            if not snap.empty:
                frames.append(snap)
                n_trading += 1
            if verbose and i % 20 == 0:
                print(f"  [pit] {i}/{len(days)} 日曆日,已收 {n_trading} 個交易日", flush=True)
    finally:
        sess.close()
    if not frames:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["date", "stock_id"]).reset_index(drop=True)


def load_history_cached(start: str = "2024-06-01", end: Optional[str] = None,
                        verbose: bool = False) -> pd.DataFrame:
    """讀已快取的逐日快照合併成 history(不發新請求,缺的日子直接略過)。

    `load_history` 會對缺的日子打 API;這支只吃 `_cache/pitsnap__*.pkl`,
    給回測/報告重現用 —— 不希望產報告時因為網路而變慢或變動。
    """
    end = end or (getattr(config, "SNAPSHOT_END_DATE", "").strip()
                  or pd.Timestamp.today().strftime("%Y-%m-%d"))
    frames = []
    for p in sorted(config.CACHE_DIR.glob("pitsnap__*.pkl")):
        day = p.stem.split("__")[1]
        if not (pd.Timestamp(start) <= pd.Timestamp(day) <= pd.Timestamp(end)):
            continue
        try:
            d = pd.read_pickle(p)
        except Exception:
            continue
        if d is not None and not d.empty:
            frames.append(d)
    if not frames:
        raise RuntimeError(
            "[pit] 找不到任何快照快取。先跑 pit_universe.load_history(start, end) 建立。")
    out = pd.concat(frames, ignore_index=True)
    if verbose:
        print(f"[pit] 快取載入 {out['date'].nunique()} 交易日 / {out['stock_id'].nunique()} 檔")
    return out.sort_values(["date", "stock_id"]).reset_index(drop=True)


def build_pit_pools(history: pd.DataFrame, top_n: int = 300,
                    lookback_days: int = 20, lag_days: int = 1,
                    freq: str = "M") -> Dict[pd.Timestamp, List[str]]:
    """逐時點建候選池。回傳 {生效日: [stock_id, ...]}。

    PIT 保證:生效日 D 的池只用 `D - lag_days` 之前(含)的成交值資料。
      freq="M" → 每月第一個交易日生效,用上個月底為止的資料(你要的「5月用4月」)
      freq="D" → 每個交易日生效,用前一日為止的資料

    lag_days=1 是嚴格版(不含生效當日)。若執行慣例是「T 收盤選股、T+1 開盤進場」,
    lag_days=0 也不算前視(T 的成交值在 T 收盤已知),但預設取嚴格的 1。
    """
    if history.empty:
        return {}
    h = history[["date", "stock_id", "turnover"]].copy()
    h["date"] = pd.to_datetime(h["date"])
    trading_days = pd.DatetimeIndex(sorted(h["date"].unique()))

    if freq == "D":
        effective = list(trading_days)
    else:
        s = pd.Series(trading_days, index=trading_days)
        effective = list(s.groupby(s.dt.to_period("M")).first())

    pools: Dict[pd.Timestamp, List[str]] = {}
    for eff in effective:
        cutoff_idx = trading_days.searchsorted(eff) - lag_days
        if cutoff_idx < 0:
            continue
        cutoff = trading_days[cutoff_idx]
        lo_idx = max(0, cutoff_idx - lookback_days + 1)
        lo = trading_days[lo_idx]
        win = h[(h["date"] >= lo) & (h["date"] <= cutoff)]
        if win.empty:
            continue
        avg = (win.groupby("stock_id")["turnover"].mean()
               .sort_values(ascending=False))
        pools[pd.Timestamp(eff)] = list(avg.head(top_n).index)
    return pools


def pool_for_date(pools: Dict[pd.Timestamp, List[str]], day) -> List[str]:
    """取在 `day` 當下生效的池(最近一個 <= day 的生效日)。"""
    day = pd.Timestamp(day)
    keys = [k for k in sorted(pools) if k <= day]
    return pools[keys[-1]] if keys else []
