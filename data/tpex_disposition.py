# -*- coding: utf-8 -*-
"""
TPEx(上櫃)注意/處置資料層
==========================
補上 `twse_disposition.py` 缺的另一半市場。候選池裡上櫃約佔四分之一
(top100 22 檔 / top300 76 檔),而上櫃多為中小型冷門股、**更容易被列注意處置**,
少了這層等於保護在最需要的地方缺席。

資料源(免費,櫃買中心):
  - 歷史『處置』:https://www.tpex.org.tw/www/zh-tw/bulletin/disposal
      ?startDate=YYYY/MM/DD&endDate=YYYY/MM/DD&response=json
  - 歷史『注意』:https://www.tpex.org.tw/www/zh-tw/bulletin/attention(參數同上)

與 TWSE 的關鍵差異(對研究品質有利)
------------------------------------
TWSE 免費端點只給「當前」處置,歷史得從『注意』用「連續3日→處置10日」規則**推導**
(proxy,偏寬)。TPEx 的 disposal 端點**直接給歷史真實『處置起訖時間』**,不需推導。
所以上櫃這半邊的處置期間是 actual 而非 derived,`source` 欄位據實標示,兩邊混用時
不會把推導值誤當真實值。

PIT 安全性:處置公告的『公布日期』早於『處置起始日』(實測如公布 115/06/18 →
期間 115/06/22~07/03),故直接採用官方起訖日不引入未來資訊。

注意事項
--------
  - 代號含 5 碼轉換公司債(如 24552 全新二、61828 合晶八),`_is_stock` 只留 4 碼
    普通股,CB 自動排除。
  - 『證券名稱』欄位夾帶相對連結(如 "全新二(../../mainboard/...)"),需剝除。
  - 分段抓取務必去重:處置期間跨查詢邊界時,兩段都會回傳該筆(實測 2024 全年 415
    筆 = H1∪H2,交集 14 筆為邊界重複,非截斷)。
"""
from __future__ import annotations

import re
import time

import pandas as pd
import requests

import config
from data.twse_disposition import _is_stock, _roc_to_date, disposition_day_set  # noqa: F401

BULLETIN = "https://www.tpex.org.tw/www/zh-tw/bulletin"
DISPOSAL_PATH = "disposal"
ATTENTION_PATH = "attention"

# 空表也要用同一組欄位,否則下游 df['announce_date'] 會在「查無資料」時 KeyError。
DISP_COLUMNS = ["stock_id", "name", "announce_date", "disp_start", "disp_end",
                "measure", "reason", "source"]
ATT_COLUMNS = ["stock_id", "name", "notice_date", "cum_count"]
_SLEEP = 1.0  # 與 twse_disposition 一致的保守限流

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "https://www.tpex.org.tw/",
}

# 處置起訖:'115/06/22~115/07/03'(分隔可能是 ~ ～ -)
_PERIOD_RE = re.compile(
    r"\s*(\d{2,3}[/.\-]\d{1,2}[/.\-]\d{1,2})\s*[~～\-]\s*(\d{2,3}[/.\-]\d{1,2}[/.\-]\d{1,2})"
)


def _clean_cell(v) -> str:
    """剝掉欄位夾帶的相對連結與 HTML,例:'全新二(../../mainboard/...)' → '全新二'。"""
    s = str(v).strip()
    s = re.sub(r"\(\.{1,2}/[^)]*\)", "", s)  # 相對連結:(../..) 與 (./attention.html)
    s = re.sub(r"<[^>]+>", " ", s)            # <br> 等標籤
    return re.sub(r"\s+", " ", s).strip()


def _fetch(path: str, start: str, end: str, session: requests.Session, retries: int = 3):
    """抓一段區間,回傳 (fields, data);全部重試失敗會 raise。

    重試是必要的:實測會偶發 ChunkedEncodingError(連線中斷)。沒有重試時,
    一次瞬斷會讓整個年度區間靜默變成 0 筆 —— 那是「看起來成功的資料遺漏」,
    比直接報錯更危險。
    """
    params = {
        "startDate": pd.Timestamp(start).strftime("%Y/%m/%d"),
        "endDate": pd.Timestamp(end).strftime("%Y/%m/%d"),
        "response": "json",
    }
    last = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(_SLEEP * attempt)   # 退避
            r = session.get(f"{BULLETIN}/{path}", params=params, timeout=30)
            r.raise_for_status()
            tables = r.json().get("tables") or []
            if not tables:
                return [], []
            t = tables[0]
            return t.get("fields", []), t.get("data", [])
        except Exception as e:
            last = e
            print(f"[tpex] {path} {params['startDate']}~{params['endDate']} "
                  f"第 {attempt}/{retries} 次失敗:{type(e).__name__}")
    raise RuntimeError(
        f"[tpex] {path} {params['startDate']}~{params['endDate']} 重試 {retries} 次仍失敗:"
        f"{type(last).__name__}。拒絕回傳空結果冒充『該期間無處置』。"
    )


def _year_chunks(start_date: str, end_date: str):
    """切成年度區間(整年查詢實測不截斷,請求數最少)。"""
    s, e = pd.Timestamp(start_date), pd.Timestamp(end_date)
    cur = s
    while cur <= e:
        year_end = min(pd.Timestamp(cur.year, 12, 31), e)
        yield cur, year_end
        cur = year_end + pd.Timedelta(days=1)


def _col(fields, *keys):
    """依欄位名關鍵字找 index(避免寫死位置)。"""
    for i, f in enumerate(fields):
        if any(k in str(f) for k in keys):
            return i
    return None


# ── (1) 歷史處置(真實起訖,免推導)──────────────────────────────────────
def fetch_disposal_history(start_date: str, end_date: str) -> pd.DataFrame:
    """抓 [start, end] 的上櫃**真實**處置期間。

    回傳欄位與 twse_disposition.build_disposition_periods 對齊:
    stock_id / disp_start / disp_end / measure / reason / source。
    """
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    rows = []
    for s, e in _year_chunks(start_date, end_date):
        fields, data_rows = _fetch(DISPOSAL_PATH, s, e, sess)
        if not data_rows:
            continue
        i_code = _col(fields, "證券代號", "代號")
        i_name = _col(fields, "證券名稱", "名稱")
        i_period = _col(fields, "處置起訖")
        i_reason = _col(fields, "處置原因")
        i_measure = _col(fields, "處置內容")
        i_ann = _col(fields, "公布日期", "公告日期")
        if i_code is None or i_period is None:
            print(f"[tpex] disposal 欄位異常,跳過 {s.date()}~{e.date()}:{fields}")
            continue
        for r in data_rows:
            code = _clean_cell(r[i_code])
            if not _is_stock(code):
                continue           # 排除 5 碼 CB / 權證 / ETF
            m = _PERIOD_RE.match(_clean_cell(r[i_period]))
            if not m:
                continue
            ds, de = _roc_to_date(m.group(1)), _roc_to_date(m.group(2))
            if pd.isna(ds) or pd.isna(de):
                continue
            rows.append({
                "stock_id": code,
                "name": _clean_cell(r[i_name]) if i_name is not None else "",
                "announce_date": _roc_to_date(_clean_cell(r[i_ann])) if i_ann is not None else pd.NaT,
                "disp_start": ds,
                "disp_end": de,
                "measure": _clean_cell(r[i_measure]) if i_measure is not None else "",
                "reason": _clean_cell(r[i_reason]) if i_reason is not None else "",
                "source": "tpex_disposal_actual",
            })
        print(f"[tpex] 處置 {s.date()}~{e.date()}:累計 {len(rows)} 筆")
    if not rows:
        return pd.DataFrame(columns=DISP_COLUMNS)
    out = pd.DataFrame(rows)
    # 跨查詢邊界的期間會在兩段各出現一次 → 必須去重
    out = out.drop_duplicates(["stock_id", "disp_start", "disp_end"])
    s, e = pd.Timestamp(start_date), pd.Timestamp(end_date)
    out = out[(out["disp_start"] <= e) & (out["disp_end"] >= s)]
    return out.sort_values(["stock_id", "disp_start"]).reset_index(drop=True)


# ── (2) 歷史注意(供研究/與上市對稱)──────────────────────────────────────
def fetch_attention_history(start_date: str, end_date: str) -> pd.DataFrame:
    """抓 [start, end] 的上櫃注意事件,欄位對齊 twse_disposition.fetch_notice_history。"""
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    rows = []
    for s, e in _year_chunks(start_date, end_date):
        fields, data_rows = _fetch(ATTENTION_PATH, s, e, sess)
        if not data_rows:
            continue
        i_code = _col(fields, "證券代號", "代號")
        i_name = _col(fields, "證券名稱", "名稱")
        i_date = _col(fields, "公告日期", "日期")
        i_cum = _col(fields, "累計")
        if i_code is None or i_date is None:
            print(f"[tpex] attention 欄位異常,跳過 {s.date()}~{e.date()}:{fields}")
            continue
        for r in data_rows:
            code = _clean_cell(r[i_code])
            if not _is_stock(code):
                continue
            nd = _roc_to_date(_clean_cell(r[i_date]))
            if pd.isna(nd):
                continue
            rows.append({
                "stock_id": code,
                "name": _clean_cell(r[i_name]) if i_name is not None else "",
                "notice_date": nd,
                "cum_count": pd.to_numeric(r[i_cum], errors="coerce") if i_cum is not None else 1,
            })
        print(f"[tpex] 注意 {s.date()}~{e.date()}:累計 {len(rows)} 筆")
    if not rows:
        return pd.DataFrame(columns=ATT_COLUMNS)
    out = pd.DataFrame(rows).drop_duplicates(["stock_id", "notice_date"])
    s, e = pd.Timestamp(start_date), pd.Timestamp(end_date)
    out = out[(out["notice_date"] >= s) & (out["notice_date"] <= e)]
    return out.sort_values(["stock_id", "notice_date"]).reset_index(drop=True)


# ── 快取包裝(與 twse_disposition.load_disposition 對稱)────────────────────
DATASET = "disposition_tpex"
ATTENTION_DATASET = "attention_tpex"


def cache_path(start_date: str, end_date: str):
    """這份快取涵蓋 [start_date, end_date];範圍進檔名(與 TWSE 對稱)。"""
    import data

    return data.window_cache_scope(DATASET, "ALL", start_date, end_date).path


def load_disposition(start_date: str, end_date: str, refresh: bool = False) -> pd.DataFrame:
    """抓上櫃真實處置期間,快取(含快照戳**與查詢範圍**)。

    不需要 trading_days:官方直接給起訖日,無需像 TWSE 那樣對齊交易日曆推導。

    原 bug 與 `twse_disposition.load_disposition` 相同(2026-08-15 一起修):
    舊檔名 `disposition_tpex__ALL__{snapshot}.pkl` 不含查詢範圍,短範圍快取會
    靜默回應長範圍請求,而回測用它決定處置期間禁新倉。
    """
    import data

    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"
    cache = cache_path(start_date, end_date)
    if cache.exists() and not refresh:
        try:
            return pd.read_pickle(cache)
        except Exception:
            pass
    disp = fetch_disposal_history(start_date, end_date)
    if disp.empty:
        print("[tpex] 無處置資料。")
        return disp
    disp.to_pickle(cache)
    att = fetch_attention_history(start_date, end_date)
    if not att.empty:
        att.to_pickle(data.window_cache_scope(
            ATTENTION_DATASET, "ALL", start_date, end_date, snapshot=snap).path)
    print(f"[tpex] 真實處置期間 {len(disp)} 段 / {disp['stock_id'].nunique()} 檔"
          f"(存 {cache.name});注意事件 {len(att)} 筆")
    return disp


if __name__ == "__main__":
    import data

    m = data.fetch_market_index()
    start = str(m["date"].min())[:10]
    end = str(m["date"].max())[:10]
    print(f"抓上櫃注意/處置:{start} ~ {end}")
    disp = load_disposition(start, end, refresh=True)
    if not disp.empty:
        print(disp.head(10).to_string(index=False))
        print(f"\n共 {disp['stock_id'].nunique()} 檔、{len(disp)} 段(全部為官方真實期間)")
