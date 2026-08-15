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
- 每檔股票每類資料 -> 一個 pickle 快取檔
  （`_cache/<dataset>__<stock>__<snapshot>__d<history_days>.pkl`）。
  檔名帶「所有會影響內容的輸入」：快照結束日 + 查詢範圍，換任一個就是 cache miss。
  快取當天有效，避免重複打 API（FinMind 免費版有流量限制）。
- 全部回傳 pandas.DataFrame，欄位統一小寫、date 轉成 datetime。
- 服務明確回「沒有資料」才回空 DataFrame；連線、額度或認證失敗會有界重試後
  raise，禁止把 API 故障靜默解讀成「該期間沒有資料」。
"""

from __future__ import annotations

import time
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

import config

_SESSION = requests.Session()


class FinMindAPIError(RuntimeError):
    """FinMind 資料無法可靠取得；不得用空表冒充真實的無資料。"""


# ── 抓取視窗 ────────────────────────────────────────────────────────────
def _resolve_history_days(history_days: Optional[int] = None,
                          default_attr: str = "HISTORY_DAYS") -> int:
    """把 None／0 解析成設定檔預設值並正規化成 int。

    範圍解析**只有這一個入口**:cache key 與 API 查詢視窗都從它推導,才不會再出現
    「檔名用預設、查詢用參數」的分裂（那正是 2026-08-15 這個 bug 的根因）。
    """
    default = getattr(config, default_attr, None) or config.HISTORY_DAYS
    days = int(history_days or default)
    if days <= 0:
        raise ValueError(f"history_days 必須為正整數,得到 {history_days!r}")
    return days


def _date_range(history_days: int = None):
    """
    抓取視窗 [start, end]。
    - 若 config.SNAPSHOT_END_DATE 有值（推薦），end 鎖在那天，回測視窗不會
      隨日曆漂移；換快照才更新（可避免 IS Sharpe 因邊界漂移而改變）。
    - 若 SNAPSHOT_END_DATE 為空，退回 datetime.now()（探索 / debug 用）。
    """
    history_days = _resolve_history_days(history_days)
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip()
    if snap:
        try:
            end = datetime.strptime(snap, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"SNAPSHOT_END_DATE={snap!r} 格式錯誤；拒絕退回 now() 以免讀到未來資料"
            ) from exc
    else:
        end = datetime.now()
    start = end - timedelta(days=history_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ── 快取工具 ────────────────────────────────────────────────────────────
def _snapshot_tag() -> str:
    """快取檔名的快照戳記。鎖快照時用日期,live 時用 'live'（維持 TTL 過期）。"""
    return getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"


# 沒有查詢範圍的資料集(一次抓完整表,不吃 history_days)。這份白名單刻意寫成
# opt-out:除此之外一律視為歷史型 → 新增資料集時「忘記做事」的結果是 fail-closed
# 報錯,而不是靜默退回無範圍的 key。
_RANGELESS_DATASETS = frozenset({"info"})

_legacy_cache_warned: set[str] = set()


@dataclass(frozen=True)
class CacheScope:
    """一次抓取請求的「快取身分 + 查詢視窗」。

    2026-08-15 修的 bug:舊 key 只有 dataset／stock_id／snapshot,history_days 根本
    沒進檔名。實測 `fetch_price('2330')` 與 `fetch_price('2330', history_days=2000)`
    命中同一個檔案,回傳完全相同的 482 列（2024-06-24~2026-06-18）、`equals()` 為
    True、零警告 —— protocol 列為最優先的「取得 >3 年含空頭資料」因此變成靜默 no-op。

    根因不是某個函式寫錯,而是每個 `fetch_*` 自己手寫 dataset 字串、範圍只流向 API
    參數,沒有任何結構強迫兩者一致。因此改成:查詢視窗與 key 由同一個物件推導,
    「會影響內容的輸入」必定同時出現在檔名裡。

    同一個道理的前一次事故（2026-07-24）:key 原本連 snapshot 都沒有,改 cutoff 卻
    靜默回傳舊快取（look-ahead）。所以 snapshot 也在 key 裡,一改就 miss → 真重抓,
    舊快照檔留存供 bit-identical 重現。
    """

    dataset: str
    stock_id: str
    snapshot: str
    range_tag: str
    start: str = ""
    end: str = ""
    days: int = 0

    def __post_init__(self) -> None:
        if self.dataset in _RANGELESS_DATASETS:
            if self.range_tag:
                raise ValueError(
                    f"{self.dataset} 沒有查詢範圍,不該帶 range_tag={self.range_tag!r}"
                )
        elif not self.range_tag:
            raise ValueError(
                f"{self.dataset} 是歷史型資料集:cache key 必須含範圍維度,"
                "否則不同 history_days 會互相命中(2026-08-15 的 bug)"
            )

    @property
    def path(self) -> Path:
        parts = [self.dataset, self.stock_id, self.snapshot]
        if self.range_tag:
            parts.append(self.range_tag)
        return config.CACHE_DIR / ("__".join(parts) + ".pkl")

    @property
    def legacy_path(self) -> Path:
        """修正前的檔名（不含範圍維度）。只用來提示需要遷移,不會被當成命中。"""
        return config.CACHE_DIR / f"{self.dataset}__{self.stock_id}__{self.snapshot}.pkl"


def range_tag(history_days: Optional[int] = None, *,
              default_attr: str = "HISTORY_DAYS") -> str:
    """範圍戳的唯一格式來源（遷移腳本也用它,避免第二份字串格式）。

    用「正規化天數」而不是 start 日期當戳:live 模式（SNAPSHOT_END_DATE 為空）下
    start 每天都會漂,寫進檔名會天天 miss、把 FinMind 免費額度燒光;而
    (snapshot, days) 已足以唯一決定 [start, end]。
    """
    return f"d{_resolve_history_days(history_days, default_attr)}"


def cache_scope(dataset: str, stock_id: str, history_days: Optional[int] = None, *,
                default_attr: str = "HISTORY_DAYS") -> CacheScope:
    """歷史型資料集的 scope（同時給出 API 查詢視窗與含範圍的快取檔名）。"""
    days = _resolve_history_days(history_days, default_attr)
    start, end = _date_range(days)
    return CacheScope(
        dataset=dataset, stock_id=str(stock_id), snapshot=_snapshot_tag(),
        range_tag=range_tag(days), start=start, end=end, days=days,
    )


def rangeless_cache_scope(dataset: str, stock_id: str) -> CacheScope:
    """無查詢範圍資料集的 scope（見 `_RANGELESS_DATASETS`）。"""
    if dataset not in _RANGELESS_DATASETS:
        raise ValueError(
            f"{dataset} 有查詢範圍,請改用 cache_scope() 帶入 history_days"
        )
    return CacheScope(dataset=dataset, stock_id=str(stock_id),
                      snapshot=_snapshot_tag(), range_tag="")


def window_range_tag(start: str, end: str) -> str:
    """呼叫端自己指定 [start, end] 的資料集用的範圍戳(`w{start}_{end}`)。

    為什麼不共用 `range_tag()` 的「正規化天數」:那個戳只在「end = 快照日」時
    唯一決定視窗;處置/注意這類全市場表是呼叫端直接給起訖日的(而且常常從
    2021 年開始抓),天數戳無法表達。
    """
    s = str(pd.Timestamp(start).date())
    e = str(pd.Timestamp(end).date())
    if s > e:
        raise ValueError(f"快取視窗顛倒:start={s} > end={e}")
    return f"w{s}_{e}"


def window_cache_scope(dataset: str, stock_id: str, start: str, end: str, *,
                       snapshot: Optional[str] = None) -> CacheScope:
    """視窗由呼叫端指定的全市場表(處置/注意…)的 scope。

    2026-08-15 補的洞:P0-2 只把範圍推進 `data.fetch_*` 那一層,但
    `twse_disposition` / `tpex_disposition` 自己拼 `disposition__ALL__{snap}.pkl`,
    查詢範圍完全不進檔名。實測:先放一份只涵蓋 2026-05-01~05-10 的快取,再以
    `load_disposition('2021-01-01', '2026-06-22', [])` 請求 5 年半 → 一次都沒有
    重抓,直接回傳那 1 列,零警告。而這層資料決定回測的「處置期間禁新倉」,
    拿到只涵蓋近期的表 = 更早的期間全部被當成沒被處置而放行進場。

    檔名格式共用 `CacheScope`,不再讓每個模組自己拼字串 —— 這正是同一個 bug
    能在 data.py 之外重演的結構原因。
    """
    return CacheScope(
        dataset=dataset, stock_id=str(stock_id),
        snapshot=(snapshot or _snapshot_tag()),
        range_tag=window_range_tag(start, end),
        start=str(pd.Timestamp(start).date()), end=str(pd.Timestamp(end).date()),
    )


def parse_window_scope(path) -> Optional[dict]:
    """從 `window_cache_scope` 產生的檔名讀回 (dataset, stock_id, snapshot,
    start, end);不是這個格式就回 None。

    讀取端(例如 `execution.tradability`)必須能判斷「這份快取涵蓋哪一段」,
    否則又會退回「有檔案就用」的舊行為。
    """
    name = Path(path).name
    if not name.endswith(".pkl"):
        return None
    parts = name[:-4].split("__")
    if len(parts) != 4 or not parts[3].startswith("w"):
        return None
    window = parts[3][1:].split("_")
    if len(window) != 2:
        return None
    try:
        start, end = (str(pd.Timestamp(w).date()) for w in window)
    except Exception:
        return None
    return {"dataset": parts[0], "stock_id": parts[1], "snapshot": parts[2],
            "start": start, "end": end, "path": Path(path)}


def cache_glob(dataset: str, history_days: Optional[int] = None, *,
               default_attr: str = "HISTORY_DAYS") -> str:
    """掃某個 dataset 全部個股快取的 glob 字串（給直接讀 `_cache/` 的稽核腳本）。

    檔名規則只能有一份 —— 稽核腳本自己拼字串,就是下一次「範圍沒進 key」的來源。
    """
    return str(cache_scope(dataset, "*", history_days, default_attr=default_attr).path)


def _warn_legacy_cache_once(scope: CacheScope) -> None:
    """舊格式（不含範圍）檔案存在但新 key miss 時提醒一次。

    不得靜默使用範圍不符的檔案,但也不能讓使用者以為「快取憑空消失」:
    真要 bit-identical 重現舊數字,跑 `migrate_cache_range.py --apply`。
    """
    if not scope.range_tag or scope.dataset in _legacy_cache_warned:
        return
    if scope.legacy_path.exists():
        _legacy_cache_warned.add(scope.dataset)
        print(f"[data] {scope.dataset} 有舊格式快取（檔名不含範圍維度）:"
              f"{scope.legacy_path.name} —— 視為 miss,不當成任意範圍的有效命中。"
              f"要沿用舊檔請先跑 migrate_cache_range.py --apply")


def _load_cache(scope: CacheScope, max_age_hours: int = 12) -> Optional[pd.DataFrame]:
    """
    讀快取。當 config.SNAPSHOT_END_DATE 有值時：快取永久有效（鎖住資料快照，
    避免邊界漂移），且快照戳與範圍戳都已進檔名 → 不同快照／不同 history_days
    不會互相命中。SNAPSHOT_END_DATE 為空字串時退回原本的 max_age_hours 過期邏輯。

    注意：更長範圍的既有快取**不會**被切片重用。寧可多抓一次,也不要讓「檔名」
    與「內容範圍」脫鉤 —— 一旦脫鉤就無法從檔名判斷手上的資料是什麼範圍。
    """
    p = scope.path
    if not p.exists():
        _warn_legacy_cache_once(scope)
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


def _save_cache(scope: CacheScope, df: pd.DataFrame) -> None:
    try:
        with open(scope.path, "wb") as f:
            pickle.dump(df, f)
    except Exception:
        pass


# ── FinMind 低階呼叫 ────────────────────────────────────────────────────
def fetch_finmind_dataset(dataset: str, data_id: str,
                          start_date: str, end_date: str) -> pd.DataFrame:
    """打 FinMind API；瞬斷有界重試，耗盡或權限錯誤時 fail-closed。"""
    if not config.FINMIND_TOKEN:
        raise FinMindAPIError("FINMIND_TOKEN 未設定；拒絕回空表冒充無資料")

    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    headers = {"Authorization": f"Bearer {config.FINMIND_TOKEN}"}
    retries = max(1, int(getattr(config, "FINMIND_MAX_RETRIES", 3)))
    backoff = max(0.0, float(getattr(config, "FINMIND_RETRY_BACKOFF", 1.0)))
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(config.FINMIND_SLEEP + backoff * (attempt - 1))
            resp = _SESSION.get(
                config.FINMIND_BASE, params=params, headers=headers, timeout=30
            )
            code = resp.status_code
            # 401/402/403/其他 4xx 是憑證、額度或請求問題；重試不會改善。
            if 400 <= code < 500 and code != 429:
                raise FinMindAPIError(
                    f"FinMind {dataset} {data_id or 'ALL'} HTTP {code}；請檢查權限/額度/參數"
                )
            resp.raise_for_status()
            payload = resp.json()
            status = payload.get("status")
            if status is not None and int(status) != 200:
                raise FinMindAPIError(
                    f"FinMind {dataset} {data_id or 'ALL'} API status={status}"
                )
            rows = payload.get("data") or []
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except FinMindAPIError:
            raise
        except (requests.RequestException, ValueError, TypeError) as exc:
            last_error = exc
            if attempt < retries:
                print(f"[data] {dataset} {data_id or 'ALL'} 第 {attempt}/{retries} 次失敗:"
                      f"{type(exc).__name__}，準備重試")
    raise FinMindAPIError(
        f"FinMind {dataset} {data_id or 'ALL'} 重試 {retries} 次仍失敗:"
        f"{type(last_error).__name__}；拒絕回空表"
    )


# 舊內部名稱保留，避免外部研究腳本壞掉；新程式請用公開函式名。
_finmind_get = fetch_finmind_dataset


# ── 1. 股票清單 / 產業別 ────────────────────────────────────────────────
def fetch_stock_info() -> pd.DataFrame:
    """全市場股票基本資料（代號、名稱、產業別、類型）。"""
    scope = rangeless_cache_scope("info", "ALL")
    cached = _load_cache(scope, max_age_hours=24 * 7)
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
    _save_cache(scope, df)
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


def _maybe_self_adjust(stock_id: str, df: pd.DataFrame, dataset: str) -> pd.DataFrame:
    """未還原資料集 + SELF_ADJUST_PRICES 開啟時,用除權息結果自建還原價。

    快取存的是原始價,還原在讀取後套用 —— 這樣快照戳語意不變,且關掉旗標即可
    退回原始價做對照,不必清快取。
    """
    if not getattr(config, "SELF_ADJUST_PRICES", False):
        return df
    if dataset == "TaiwanStockPriceAdj":      # 官方已還原,不重複套
        return df
    if df is None or df.empty:
        return df
    try:
        import price_adjust
        out = price_adjust.adjust_price_frame(stock_id, df)
        out.attrs["price_dataset"] = f"{dataset}+selfadj"
        return out
    except Exception as e:
        raise RuntimeError(
            f"{stock_id} 自建還原價失敗:{type(e).__name__}；拒絕退回未還原價"
        ) from e


def fetch_price(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    日線資料，欄位：date, open, high, low, close, volume(股), turnover
    volume 用 Trading_Volume（成交股數）。
    """
    dataset = getattr(config, "PRICE_DATASET", "TaiwanStockPrice")
    cache_key = "price_adj" if dataset == "TaiwanStockPriceAdj" else "price"
    scope = cache_scope(cache_key, stock_id, history_days)
    cached = _load_cache(scope)
    if cached is not None:
        out = _clean_price_frame(cached)
        out.attrs["price_dataset"] = dataset
        return _maybe_self_adjust(stock_id, out, dataset)
    df = _finmind_get(dataset, stock_id, scope.start, scope.end)
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
    _save_cache(scope, df)   # 快取存**原始**價,還原在讀取後套用
    return _maybe_self_adjust(stock_id, df, dataset)


def fetch_price_limits(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """官方逐日參考價與漲跌停價：date/reference_price/limit_up/limit_down。

    這組欄位是 execution 的資料契約；缺欄位時必須 fail-closed，不能退回昨日收盤後
    還把來源標成 official。新上市無漲跌幅列的實際空值語意尚未完成真實 API 驗證，
    因此目前也不自行把空值解讀為豁免。
    """
    scope = cache_scope("price_limit", stock_id, history_days)
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    df = _finmind_get("TaiwanStockPriceLimit", stock_id, scope.start, scope.end)
    if df.empty:
        return df
    required = {"date", "reference_price", "limit_up", "limit_down"}
    missing = required - set(df.columns)
    if missing:
        raise FinMindAPIError(
            f"TaiwanStockPriceLimit {stock_id} schema 缺少 {sorted(missing)}"
        )
    out = df[["date", "reference_price", "limit_up", "limit_down"]].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for col in ("reference_price", "limit_up", "limit_down"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date").drop_duplicates(
        "date", keep="last").reset_index(drop=True)
    _save_cache(scope, out)
    return out


# ── 3. 三大法人買賣超 ───────────────────────────────────────────────────
def fetch_institutional(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    三大法人買賣超，整理成寬表：
      date, foreign_net, trust_net, dealer_net, inst_net (=foreign+trust，主力)
    單位：股（FinMind 原始為股數 buy/sell，net = buy - sell）。
    """
    scope = cache_scope("inst", stock_id, history_days)
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    raw = _finmind_get("TaiwanStockInstitutionalInvestorsBuySell",
                       stock_id, scope.start, scope.end)
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
    _save_cache(scope, out)
    return out


# ── 4. 融資融券 ────────────────────────────────────────────────────────
def fetch_margin(stock_id: str, history_days: int = None) -> pd.DataFrame:
    """
    融資融券，欄位整理：
      date, margin_balance(融資餘額,張), short_balance(融券餘額,張),
      margin_limit(融資限額), margin_change, short_change
    """
    scope = cache_scope("margin", stock_id, history_days)
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    df = _finmind_get("TaiwanStockMarginPurchaseShortSale",
                      stock_id, scope.start, scope.end)
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
    _save_cache(scope, df)
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
    scope = cache_scope("lending", stock_id, history_days)
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    raw = _finmind_get("TaiwanStockSecuritiesLending", stock_id, scope.start, scope.end)
    if raw.empty:
        return raw
    raw["date"] = pd.to_datetime(raw["date"])
    raw["volume"] = pd.to_numeric(raw.get("volume"), errors="coerce").fillna(0)
    # 逐筆 -> 每日聚合
    daily = raw.groupby("date", as_index=False)["volume"].sum()
    daily = daily.rename(columns={"volume": "lending_vol"}).sort_values("date").reset_index(drop=True)
    daily["lending_vol_5d"] = daily["lending_vol"].rolling(5, min_periods=1).sum()
    _save_cache(scope, daily)
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
    scope = cache_scope("fholding", stock_id, history_days)
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    df = _finmind_get("TaiwanStockShareholding", stock_id, scope.start, scope.end)
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
    _save_cache(scope, df)
    return df


# ── 市場層級：VIX 恐慌指數 ──────────────────────────────────────────────
# VIX 不是個股資料，獨立歸類為 "market"（快取檔 market__VIX__<snapshot>__d<days>.pkl），
# 與個股的 price/inst/margin 分開，避免混在同一命名空間。
# 台灣無穩定免費 VIX 來源 → 用美國 ^VIX 當市場恐慌的代理（台股高度跟隨美股情緒）。
def fetch_vix(history_days: int = None) -> pd.DataFrame:
    """
    回傳 VIX 日資料：date, vix_close, vix_high, vix_low。
    來源 yfinance ^VIX。失敗回空 DataFrame。
    """
    # history_days 會經 period 影響內容（2y vs 5y）→ 必須進 key。
    scope = cache_scope("market", "VIX", history_days)
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        period = "2y" if scope.days <= 730 else "5y"
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
        _save_cache(scope, out)
        return out
    except Exception as e:
        print(f"[data] VIX 抓取失敗：{e}")
        return pd.DataFrame()


# ── 市場層級：大盤加權指數（RS / 抗跌因子的基準）─────────────────────────
# 相對強勢 (relative strength)、下行 beta、抗跌度等因子都需要一條「大盤」序列
# 當基準。用 FinMind 的 TAIEX（發行量加權股價指數），full OHLCV、純 FinMind
# 來源（不引入 yfinance 個股）。快取檔 market__TAIEX__<snapshot>__d<days>.pkl，
# 與個股命名空間分開；範圍戳用 MARKET_HISTORY_DAYS（比個股長，MA200 暖身）。
def fetch_market_index(history_days: int = None) -> pd.DataFrame:
    """
    回傳大盤加權指數（TAIEX）日資料：date, open, high, low, close, volume。
    來源 FinMind TaiwanStockPrice / data_id=TAIEX。服務明確無資料才回空；API 失敗 raise。
    """
    # 大盤抓更長歷史（市場濾網 MA200 暖身用），預設 MARKET_HISTORY_DAYS。
    # 範圍先解析再查快取:TAIEX 與個股預設範圍不同,key 必須反映實際查詢視窗。
    scope = cache_scope("market", "TAIEX", history_days,
                        default_attr="MARKET_HISTORY_DAYS")
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    df = _finmind_get("TaiwanStockPrice", "TAIEX", scope.start, scope.end)
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
    _save_cache(scope, df)
    return df


# ── 市場層級：大盤**含息**報酬指數（口徑一致的比較基準）─────────────────────
# 為什麼要有第二條大盤序列:上面那條 TAIEX 是**價格指數**(不含息),而個股序列在
# 官方還原價或自建還原價下是**含息**的 → 拿它當基準等於用不含息的尺量含息的東西,
# 差額全部變成假超額。實測 2024-06-03~2026-06-20:價格指數算術年化 42.38%、含息
# 45.23%(差 2.86pp/年),Sharpe 1.677 vs 1.790(差 0.113);2015~2026 逐年差
# 2.41~4.81pp,**沒有一年為負** —— 是系統性偏誤,不是雜訊。
#
# 選擇邏輯不在這裡,在 `return_convention.py`(資料層只負責把序列取回來)。
# 快取檔 market__TAIEX_TR__<snapshot>__d<days>.pkl:與價格指數不同命名空間,
# 範圍戳同樣走 MARKET_HISTORY_DAYS(不變式 7:範圍必須進 key)。
def fetch_market_total_return_index(history_days: int = None) -> pd.DataFrame:
    """
    回傳大盤**含息**報酬指數（TAIEX Total Return）日資料：date, close。
    來源 FinMind TaiwanStockTotalReturnIndex / data_id=TAIEX（實測需 level 2）。
    服務明確無資料才回空；API 失敗 raise（不得回空表冒充無資料）。

    這個資料集只有一個價格欄位（實測欄位為 `price`，無 OHLCV、無成交量），所以
    它**只能**用於報酬比較,不能拿去當 OHLC 或成交量來源。
    """
    scope = cache_scope("market", "TAIEX_TR", history_days,
                        default_attr="MARKET_HISTORY_DAYS")
    cached = _load_cache(scope)
    if cached is not None:
        return cached
    df = _finmind_get("TaiwanStockTotalReturnIndex", "TAIEX", scope.start, scope.end)
    if df.empty:
        return df
    # 實測回傳欄位:price / stock_id / date。容忍 close 命名以免上游改欄位就靜默壞掉。
    if "close" not in df.columns and "price" in df.columns:
        df = df.rename(columns={"price": "close"})
    if "close" not in df.columns:
        raise FinMindAPIError(
            "TaiwanStockTotalReturnIndex 回傳沒有 price/close 欄位:"
            f"{sorted(df.columns)}；拒絕猜欄位"
        )
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    _save_cache(scope, df)
    return df


# ── 整合：一次取得單檔所有資料 ──────────────────────────────────────────
def fetch_bundle(stock_id: str, history_days: int = None,
                 include_extras: bool = False) -> dict:
    """
    回傳 {'price':df, 'inst':df, 'margin':df[, 'lending', 'fholding']}。
    任何一項抓不到就是空 DataFrame。

    2026-07-24:預設**不抓** lending / fholding。compute_factors 從不使用這兩類
    (全庫 grep 確認無消費者),照抓等於每檔多打 2/5 支 FinMind 免費配額、無因子價值,
    還提高中途 402 用盡風險(曾在整池刷新時觸發)。需要時傳 include_extras=True。
    """
    bundle = {
        "price": fetch_price(stock_id, history_days),
        "inst": fetch_institutional(stock_id, history_days),
        "margin": fetch_margin(stock_id, history_days),
    }
    if include_extras:
        bundle["lending"] = fetch_lending(stock_id, history_days)
        bundle["fholding"] = fetch_foreign_holding(stock_id, history_days)
    return bundle


if __name__ == "__main__":
    # 簡單自我測試
    print(f"FINMIND_TOKEN 設定：{'是' if config.FINMIND_TOKEN else '否'} (len={len(config.FINMIND_TOKEN)})")
    sid = "2330"
    b = fetch_bundle(sid)
    for k, v in b.items():
        print(f"  {k}: {len(v)} rows", list(v.columns) if not v.empty else "(空)")
