# -*- coding: utf-8 -*-
"""
資料抓取層
==========
複用 FinMind token，抓取波段選股需要的四類資料：
  1. 日線 OHLCV            -> TaiwanStockPrice
  2. 三大法人買賣超        -> TaiwanStockInstitutionalInvestorsBuySell
  3. 融資融券              -> TaiwanStockMarginPurchaseShortSale
  4. 股票清單 / 產業別     -> TaiwanStockInfo

設計重點
--------
- 每檔股票每類資料 -> 一個 pickle 快取檔（_cache/<dataset>__<stock>.pkl）。
  快取當天有效，避免重複打 API（FinMind 免費版有流量限制）。
- 全部回傳 pandas.DataFrame，欄位統一小寫、date 轉成 datetime。
- 抓不到 / 失敗 -> 回傳空 DataFrame，不丟例外（讓上層自己決定跳過）。
"""

from __future__ import annotations

import time
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

import config

_SESSION = requests.Session()


# ── 快取工具 ────────────────────────────────────────────────────────────
def _snapshot_tag() -> str:
    """快取檔名的快照戳記。鎖快照時用日期,live 時用 'live'（維持 TTL 過期）。"""
    return getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"


def _cache_path(dataset: str, stock_id: str) -> Path:
    # 2026-07-24 修:把 snapshot 編進檔名。舊版 key 不含 snapshot,改 cutoff 卻靜默
    # 回傳舊快取（look-ahead）。現在 snapshot 一改 → 檔名 miss → 真重抓;舊快照檔留存
    # 供 bit-identical 重現。
    return config.CACHE_DIR / f"{dataset}__{stock_id}__{_snapshot_tag()}.pkl"


def _load_cache(dataset: str, stock_id: str, max_age_hours: int = 12) -> Optional[pd.DataFrame]:
    """
    讀快取。當 config.SNAPSHOT_END_DATE 有值時：快取永久有效（鎖住資料快照，
    避免邊界漂移），且快照戳已進檔名 → 不同快照不會互相命中。
    SNAPSHOT_END_DATE 為空字串時退回原本的 max_age_hours 過期邏輯。
    """
    p = _cache_path(dataset, stock_id)
    if not p.exists():
        return None
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip()
    if not snap:
        age_h = (time.time() - p.stat().st_mtime) / 3600
        if age_h > max_age_hours:
            return None
    try:
        with open(p, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None
    # 安全網:凍結快照下,絕不回傳超過快照日的資料列（擋任何殘留的未來洩漏）。
    if snap and isinstance(df, pd.DataFrame) and "date" in df.columns:
        try:
            end = pd.to_datetime(snap)
            dts = pd.to_datetime(df["date"])
            if (dts > end).any():
                df = df[dts <= end].copy()
        except Exception:
            pass
    return df


def _save_cache(dataset: str, stock_id: str, df: pd.DataFrame) -> None:
    try:
        with open(_cache_path(dataset, stock_id), "wb") as f:
            pickle.dump(df, f)
    except Exception:
        pass


# ── FinMind 低階呼叫 ────────────────────────────────────────────────────
def _finmind_get(dataset: str, data_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    """打 FinMind API，回傳 DataFrame（失敗回空）。"""
    if not config.FINMIND_TOKEN:
        print("[data] 警告：FINMIND_TOKEN 未設定，無法抓取資料")
        return pd.DataFrame()

    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    headers = {"Authorization": f"Bearer {config.FINMIND_TOKEN}"}
    try:
        time.sleep(config.FINMIND_SLEEP)
        resp = _SESSION.get(
            config.FINMIND_BASE, params=params, headers=headers, timeout=30
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or []
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    except requests.HTTPError as e:
        # 402 = 流量用盡；429 = 太頻繁
        code = getattr(e.response, "status_code", "?")
        print(f"[data] {dataset} {data_id} HTTP {code}：{e}")
        return pd.DataFrame()
    except Exception as e:
        # 不印 request headers / 完整 URL，避免認證資訊進入 log。
        print(f"[data] {dataset} {data_id} 連線失敗：{type(e).__name__}")
        return pd.DataFrame()


def _date_range(history_days: int = None):
    """
    抓取視窗 [start, end]。
    - 若 config.SNAPSHOT_END_DATE 有值（推薦），end 鎖在那天，回測視窗不會
      隨日曆漂移；換快照才更新（可避免 IS Sharpe 因邊界漂移而改變）。
    - 若 SNAPSHOT_END_DATE 為空，退回 datetime.now()（探索 / debug 用）。
    """
    history_days = history_days or config.HISTORY_DAYS
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip()
    if snap:
        try:
            end = datetime.strptime(snap, "%Y-%m-%d")
        except ValueError:
            print(f"[data] 警告：SNAPSHOT_END_DATE='{snap}' 格式不對，退回 now()")
            end = datetime.now()
    else:
        end = datetime.now()
    start = end - timedelta(days=history_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ── 1. 股票清單 / 產業別 ────────────────────────────────────────────────
def fetch_stock_info() -> pd.DataFrame:
    """全市場股票基本資料（代號、名稱、產業別、類型）。"""
    cached = _load_cache("info", "ALL", max_age_hours=24 * 7)
    if cached is not None:
        return cached
    df = _finmind_get("TaiwanStockInfo", "", "", "")
    if df.empty:
        return df
    # 欄位：industry_category, stock_id, stock_name, type, date
    df = df.rename(columns={
        "stock_id": "stock_id",
        "stock_name": "name",
        "industry_category": "industry",
        "type": "market_type",
    })
    # 去重（同一股票可能多筆）
    df = df.drop_duplicates(subset=["stock_id"], keep="last").reset_index(drop=True)
    _save_cache("info", "ALL", df)
    return df


# ── 2. 日線 OHLCV ──────────────────────────────────────────────────────
def _clean_price_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize price data and remove non-tradable placeholder bars.

    FinMind raw histories can contain suspended/no-trade rows with zero OHLCV.
    They are not executable bars and must not enter rolling factors, liquidity
    ranks, stop-loss checks, or mark-to-market returns.
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    out = df.copy()
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
    numeric = ["open", "high", "low", "close", "volume", "turnover"]
    for c in numeric:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    required = [c for c in ["open", "high", "low", "close", "volume"] if c in out.columns]
    if required:
        mask = out[required].notna().all(axis=1) & (out[required] > 0).all(axis=1)
        out = out[mask]
    return out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def fetch_price(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    日線資料，欄位：date, open, high, low, close, volume(股), turnover
    volume 用 Trading_Volume（成交股數）。
    """
    dataset = getattr(config, "PRICE_DATASET", "TaiwanStockPrice")
    cache_key = "price_adj" if dataset == "TaiwanStockPriceAdj" else "price"
    cached = _load_cache(cache_key, stock_id)
    if cached is not None:
        out = _clean_price_frame(cached)
        out.attrs["price_dataset"] = dataset
        return out
    start, end = _date_range(history_days)
    df = _finmind_get(dataset, stock_id, start, end)
    if df.empty:
        return df
    rename = {
        "date": "date",
        "open": "open",
        "max": "high",
        "min": "low",
        "close": "close",
        "Trading_Volume": "volume",      # 成交股數
        "Trading_money": "turnover",     # 成交金額
    }
    df = df.rename(columns=rename)
    keep = [c for c in ["date", "open", "high", "low", "close", "volume", "turnover"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close", "volume", "turnover"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = _clean_price_frame(df)
    df.attrs["price_dataset"] = dataset
    _save_cache(cache_key, stock_id, df)
    return df


# ── 3. 三大法人買賣超 ───────────────────────────────────────────────────
def fetch_institutional(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    三大法人買賣超，整理成寬表：
      date, foreign_net, trust_net, dealer_net, inst_net (=foreign+trust，主力)
    單位：股（FinMind 原始為股數 buy/sell，net = buy - sell）。
    """
    cached = _load_cache("inst", stock_id)
    if cached is not None:
        return cached
    start, end = _date_range(history_days)
    raw = _finmind_get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start, end)
    if raw.empty:
        return raw

    # 原始欄位：date, stock_id, buy, sell, name（name 是法人別）
    raw["date"] = pd.to_datetime(raw["date"])
    raw["buy"] = pd.to_numeric(raw["buy"], errors="coerce").fillna(0)
    raw["sell"] = pd.to_numeric(raw["sell"], errors="coerce").fillna(0)
    raw["net"] = raw["buy"] - raw["sell"]

    # name 可能值：Foreign_Investor / Investment_Trust / Dealer_self /
    #             Dealer_Hedging / Foreign_Dealer_Self ...
    def _classify(n: str) -> str:
        n = str(n)
        if n.startswith("Foreign"):
            return "foreign"
        if "Trust" in n:
            return "trust"
        if "Dealer" in n:
            return "dealer"
        return "other"

    raw["grp"] = raw["name"].map(_classify)
    pivot = raw.pivot_table(index="date", columns="grp", values="net", aggfunc="sum").fillna(0)
    for col in ["foreign", "trust", "dealer"]:
        if col not in pivot.columns:
            pivot[col] = 0.0
    out = pd.DataFrame({
        "date": pivot.index,
        "foreign_net": pivot["foreign"].values,
        "trust_net": pivot["trust"].values,
        "dealer_net": pivot["dealer"].values,
    })
    # 主力 = 外資 + 投信（排除自營商避險雜訊）
    out["inst_net"] = out["foreign_net"] + out["trust_net"]
    out = out.sort_values("date").reset_index(drop=True)
    _save_cache("inst", stock_id, out)
    return out


# ── 4. 融資融券 ────────────────────────────────────────────────────────
def fetch_margin(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    融資融券，欄位整理：
      date, margin_balance(融資餘額,張), short_balance(融券餘額,張),
      margin_limit(融資限額), margin_change, short_change
    """
    cached = _load_cache("margin", stock_id)
    if cached is not None:
        return cached
    start, end = _date_range(history_days)
    df = _finmind_get("TaiwanStockMarginPurchaseShortSale", stock_id, start, end)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    rename = {
        "MarginPurchaseTodayBalance": "margin_balance",
        "ShortSaleTodayBalance": "short_balance",
        "MarginPurchaseLimit": "margin_limit",
        "MarginPurchaseYesterdayBalance": "margin_yday",
        "ShortSaleYesterdayBalance": "short_yday",
    }
    df = df.rename(columns=rename)
    for c in ["margin_balance", "short_balance", "margin_limit", "margin_yday", "short_yday"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "margin_yday" in df.columns:
        df["margin_change"] = df["margin_balance"] - df["margin_yday"]
    if "short_yday" in df.columns:
        df["short_change"] = df["short_balance"] - df["short_yday"]
    keep = [c for c in ["date", "margin_balance", "short_balance", "margin_limit",
                        "margin_change", "short_change"] if c in df.columns]
    df = df[keep].sort_values("date").reset_index(drop=True)
    _save_cache("margin", stock_id, df)
    return df


# ── 5. 借券賣出（大戶/外資空方代理）─────────────────────────────────────
def fetch_lending(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    借券資料 -> TaiwanStockSecuritiesLending（免費版可用）。
    原始為「逐筆借券交易」：date, transaction_type(議借/競價/標借), volume(股), fee_rate, close。
    借券通常是「借股票去放空」，新增借券量 = 潛在空方壓力。

    整理成每日聚合（point-in-time 友善）：
      date, lending_vol(當日新增借券量,股), lending_vol_5d(近5日借券量,股)
    註：FinMind 免費版此 dataset 給的是「當日借券交易量」而非「借券賣出餘額」。
        我們用「借券量的變化/水準」當空方壓力代理，仍能捕捉放空意圖的增減。
    """
    cached = _load_cache("lending", stock_id)
    if cached is not None:
        return cached
    start, end = _date_range(history_days)
    raw = _finmind_get("TaiwanStockSecuritiesLending", stock_id, start, end)
    if raw.empty:
        return raw
    raw["date"] = pd.to_datetime(raw["date"])
    raw["volume"] = pd.to_numeric(raw.get("volume"), errors="coerce").fillna(0)
    # 逐筆 -> 每日聚合
    daily = raw.groupby("date", as_index=False)["volume"].sum()
    daily = daily.rename(columns={"volume": "lending_vol"}).sort_values("date").reset_index(drop=True)
    daily["lending_vol_5d"] = daily["lending_vol"].rolling(5, min_periods=1).sum()
    _save_cache("lending", stock_id, daily)
    return daily


# ── 6. 外資持股比例 / 距上限 ────────────────────────────────────────────
def fetch_foreign_holding(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    外資持股 -> TaiwanStockShareholding（免費版可用）。
    原始欄位含 ForeignInvestmentSharesRatio(外資持股比例%)、
    ForeignInvestmentRemainRatio(距上限剩餘比例%)。

    整理成：date, foreign_ratio(外資持股比例%), foreign_remain_ratio(距上限剩餘%)
    註：此資料約每日申報但偶有缺漏，上層用 merge_asof(backward) 對齊即可。
    """
    cached = _load_cache("fholding", stock_id)
    if cached is not None:
        return cached
    start, end = _date_range(history_days)
    df = _finmind_get("TaiwanStockShareholding", stock_id, start, end)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    rename = {
        "ForeignInvestmentSharesRatio": "foreign_ratio",
        "ForeignInvestmentRemainRatio": "foreign_remain_ratio",
    }
    df = df.rename(columns=rename)
    for c in ["foreign_ratio", "foreign_remain_ratio"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = [c for c in ["date", "foreign_ratio", "foreign_remain_ratio"] if c in df.columns]
    df = df[keep].dropna(subset=["foreign_ratio"]).sort_values("date").reset_index(drop=True)
    _save_cache("fholding", stock_id, df)
    return df


# ── 市場層級：VIX 恐慌指數 ──────────────────────────────────────────────
# VIX 不是個股資料，獨立歸類為 "market"（快取檔 market__VIX.pkl），
# 與個股的 price/inst/margin 分開，避免混在同一命名空間。
# 台灣無穩定免費 VIX 來源 → 用美國 ^VIX 當市場恐慌的代理（台股高度跟隨美股情緒）。
def fetch_vix(history_days: int = None) -> pd.DataFrame:
    """
    回傳 VIX 日資料：date, vix_close, vix_high, vix_low。
    來源 yfinance ^VIX。失敗回空 DataFrame。
    """
    cached = _load_cache("market", "VIX")
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        days = history_days or config.HISTORY_DAYS
        period = "2y" if days <= 730 else "5y"
        hist = yf.Ticker("^VIX").history(period=period)
        if hist.empty:
            return pd.DataFrame()
        out = pd.DataFrame({
            "date": pd.to_datetime(hist.index.date),
            "vix_close": pd.to_numeric(hist["Close"], errors="coerce").values,
            "vix_high": pd.to_numeric(hist["High"], errors="coerce").values,
            "vix_low": pd.to_numeric(hist["Low"], errors="coerce").values,
        })
        out = out.dropna(subset=["vix_close"]).sort_values("date").reset_index(drop=True)
        _save_cache("market", "VIX", out)
        return out
    except Exception as e:
        print(f"[data] VIX 抓取失敗：{e}")
        return pd.DataFrame()


# ── 市場層級：大盤加權指數（RS / 抗跌因子的基準）─────────────────────────
# 相對強勢 (relative strength)、下行 beta、抗跌度等因子都需要一條「大盤」序列
# 當基準。用 FinMind 的 TAIEX（發行量加權股價指數），full OHLCV、純 FinMind
# 來源（不引入 yfinance 個股）。快取檔 market__TAIEX.pkl，與個股命名空間分開。
def fetch_market_index(history_days: int = None) -> pd.DataFrame:
    """
    回傳大盤加權指數（TAIEX）日資料：date, open, high, low, close, volume。
    來源 FinMind TaiwanStockPrice / data_id=TAIEX。失敗回空 DataFrame。
    """
    cached = _load_cache("market", "TAIEX")
    if cached is not None:
        return cached
    # 大盤抓更長歷史（市場濾網 MA200 暖身用），預設 MARKET_HISTORY_DAYS。
    history_days = history_days or getattr(config, "MARKET_HISTORY_DAYS", config.HISTORY_DAYS)
    start, end = _date_range(history_days)
    df = _finmind_get("TaiwanStockPrice", "TAIEX", start, end)
    if df.empty:
        return df
    rename = {
        "date": "date",
        "open": "open",
        "max": "high",
        "min": "low",
        "close": "close",
        "Trading_Volume": "volume",
        "Trading_money": "turnover",
    }
    df = df.rename(columns=rename)
    keep = [c for c in ["date", "open", "high", "low", "close", "volume", "turnover"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close", "volume", "turnover"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    _save_cache("market", "TAIEX", df)
    return df


# ── 整合：一次取得單檔所有資料 ──────────────────────────────────────────
def fetch_bundle(stock_id: str, history_days: int = None) -> dict:
    """
    回傳 {'price':df, 'inst':df, 'margin':df, 'lending':df, 'fholding':df}。
    任何一項抓不到就是空 DataFrame。
    """
    return {
        "price": fetch_price(stock_id, history_days),
        "inst": fetch_institutional(stock_id, history_days),
        "margin": fetch_margin(stock_id, history_days),
        "lending": fetch_lending(stock_id, history_days),
        "fholding": fetch_foreign_holding(stock_id, history_days),
    }


if __name__ == "__main__":
    # 簡單自我測試
    print(f"FINMIND_TOKEN 設定：{'是' if config.FINMIND_TOKEN else '否'} (len={len(config.FINMIND_TOKEN)})")
    sid = "2330"
    b = fetch_bundle(sid)
    for k, v in b.items():
        print(f"  {k}: {len(v)} rows", list(v.columns) if not v.empty else "(空)")
