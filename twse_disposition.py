# -*- coding: utf-8 -*-
"""
TWSE 注意/處置(disposition)資料層
==================================
處置股在處置期間改分盤集合競價(每5/20分)、預收款券、停信用交易,流動性劇降,
動能策略最容易追進正要被列注意/處置的暴漲股 → 回測若當普通股正常成交會高估。

資料源(免費,TWSE):
  - 歷史『注意(notice)』:https://www.twse.com.tw/rwd/zh/announcement/notice
      ?startDate=YYYYMMDD&endDate=YYYYMMDD&response=json  ← startDate/endDate 有效,可抓歷史
  - 當前『處置(punish)』:https://openapi.twse.com.tw/v1/announcement/punish
      (date 參數被忽略,只回當前;含真實處置起迄,用來驗證我們從注意推導的期間)

台股規則(要點 FL007225):連續 3 個營業日達注意標準 → 次一營業日起處置,通常 10 個
營業日(當沖過高型 12 日)。處置期間收盤後才公告(當日假設可交易屬前視)。

本模組:
  (1) fetch_notice_history:逐月抓歷史注意事件(真實)。
  (2) fetch_current_punish:抓當前真實處置名單(驗證用)。
  (3) build_disposition_periods:由注意事件依升級規則**推導處置期間**(標為 derived),
      對齊交易日曆;處置公告在收盤後 → 期間從『注意達標日的次一交易日』起,PIT 安全。
  (4) is_disposed_map:回傳 {stock_id -> set(處置交易日)} 供回測/事件研究用。

⚠ 推導是 proxy(用真實注意資料 + 規則),非逐筆官方處置檔;會與實際有出入,報告須標明。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import config

NOTICE_URL = "https://www.twse.com.tw/rwd/zh/announcement/notice"
PUNISH_OPENAPI = "https://openapi.twse.com.tw/v1/announcement/punish"
_SLEEP = 1.0  # TWSE 限流(約 3 req/5s),保守 sleep

DISP_DAYS = 10          # 處置期間(交易日);當沖型 12,這裡取通例 10
CONSEC_NOTICE = 3       # 連續達注意標準次數 → 升級處置


def _roc_to_date(s: str):
    """民國日期 '115/08/03'、'114.08.27'、'1150803' → Timestamp(分隔可為 . / -)。"""
    s = str(s).strip()
    m = re.match(r"(\d{2,3})[./\-]?(\d{1,2})[./\-]?(\d{1,2})$", s)
    if not m:
        return pd.NaT
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    try:
        return pd.Timestamp(y, mo, d)
    except ValueError:
        return pd.NaT


def _is_stock(code: str) -> bool:
    """4 碼普通股,排除權證(6碼)、00 開頭 ETF。"""
    c = str(code).strip()
    return len(c) == 4 and c.isdigit() and not c.startswith("00")


# ── (1) 歷史注意(逐月)──────────────────────────────────────────────────
def fetch_notice_month(yyyymm: str, session: requests.Session,
                       retries: int = 3) -> pd.DataFrame:
    y, m = int(yyyymm[:4]), int(yyyymm[4:6])
    start = f"{y}{m:02d}01"
    end_dt = (pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(1))
    end = end_dt.strftime("%Y%m%d")
    last = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(_SLEEP * attempt)
            r = session.get(
                NOTICE_URL,
                params={"startDate": start, "endDate": end, "response": "json"},
                timeout=30,
            )
            r.raise_for_status()
            j = r.json()
            break
        except Exception as exc:
            last = exc
            print(f"[disp] notice {yyyymm} 第 {attempt}/{retries} 次失敗:"
                  f"{type(exc).__name__}")
    else:
        raise RuntimeError(
            f"[disp] notice {yyyymm} 重試 {retries} 次仍失敗:{type(last).__name__}；"
            "拒絕回空表"
        )
    if j.get("stat") != "OK" or not j.get("data"):
        return pd.DataFrame()
    fields = j["fields"]
    df = pd.DataFrame(j["data"], columns=fields)
    # 欄位:編號/證券代號/證券名稱/累計次數/注意交易資訊/日期/收盤價/本益比
    code_col = next((c for c in fields if "代號" in c), None)
    date_col = next((c for c in fields if c == "日期" or "日期" in c), None)
    name_col = next((c for c in fields if "名稱" in c), None)
    cum_col = next((c for c in fields if "累計" in c), None)
    out = pd.DataFrame({
        "stock_id": df[code_col].astype(str).str.strip(),
        "name": df[name_col].astype(str).str.strip() if name_col else "",
        "notice_date": df[date_col].map(_roc_to_date) if date_col else pd.NaT,
        "cum_count": pd.to_numeric(df[cum_col], errors="coerce") if cum_col else 1,
    })
    out = out[out["stock_id"].map(_is_stock) & out["notice_date"].notna()]
    return out.reset_index(drop=True)


def fetch_notice_history(start_date: str, end_date: str) -> pd.DataFrame:
    """抓 [start_date, end_date] 範圍內所有月份的注意事件(真實)。"""
    s = pd.Timestamp(start_date); e = pd.Timestamp(end_date)
    months = pd.date_range(s.replace(day=1), e, freq="MS").strftime("%Y%m")
    sess = requests.Session()
    frames = []
    for i, ym in enumerate(months, 1):
        d = fetch_notice_month(ym, sess)
        if not d.empty:
            frames.append(d)
        if i % 6 == 0:
            print(f"[disp] 注意抓取 {i}/{len(months)} 月 …")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["stock_id", "notice_date"])
    out = out[(out["notice_date"] >= s) & (out["notice_date"] <= e)]
    return out.sort_values(["stock_id", "notice_date"]).reset_index(drop=True)


# ── (2) 當前真實處置(驗證用)────────────────────────────────────────────
def fetch_current_punish(retries: int = 3) -> pd.DataFrame:
    last = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(_SLEEP * attempt)
            r = requests.get(PUNISH_OPENAPI, timeout=30)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as exc:
            last = exc
            print(f"[disp] punish 第 {attempt}/{retries} 次失敗:{type(exc).__name__}")
    else:
        raise RuntimeError(
            f"[disp] punish 重試 {retries} 次仍失敗:{type(last).__name__}；拒絕回空表"
        )
    rows = []
    for it in data:
        code = str(it.get("Code", "")).strip()
        if not _is_stock(code):
            continue
        period = str(it.get("DispositionPeriod", ""))
        m = re.match(r"\s*(\d{2,3}/\d{1,2}/\d{1,2})\s*[~～-]\s*(\d{2,3}/\d{1,2}/\d{1,2})", period)
        if not m:
            continue
        rows.append({
            "stock_id": code, "name": str(it.get("Name", "")).strip(),
            "disp_start": _roc_to_date(m.group(1)), "disp_end": _roc_to_date(m.group(2)),
            "measure": str(it.get("DispositionMeasures", "")).strip(),
            "reason": str(it.get("ReasonsOfDisposition", "")).strip(),
            "source": "twse_punish_actual",
        })
    return pd.DataFrame(rows)


# ── (3) 由注意升級推導處置期間(對齊交易日曆)──────────────────────────────
def build_disposition_periods(notice_df: pd.DataFrame, trading_days) -> pd.DataFrame:
    """連續 CONSEC_NOTICE 個交易日達注意 → 次一交易日起處置 DISP_DAYS 個交易日。

    trading_days:排序好的交易日 Timestamp 陣列(用 TAIEX 日曆)。處置期間從『達標日的
    次一交易日』起 → PIT 安全(注意收盤後才公告)。回傳每檔的處置期間 [disp_start, disp_end]。
    """
    td = pd.DatetimeIndex(sorted(pd.to_datetime(trading_days)))
    pos = {d: i for i, d in enumerate(td)}
    rows = []
    for sid, g in notice_df.groupby("stock_id"):
        dates = sorted(d for d in g["notice_date"] if d in pos)
        idxs = [pos[d] for d in dates]
        i = 0
        while i < len(idxs):
            # 從 i 起找『連續(交易日連號)』的注意日長度
            run = 1
            while i + run < len(idxs) and idxs[i + run] == idxs[i + run - 1] + 1:
                run += 1
            if run >= CONSEC_NOTICE:
                trig_pos = idxs[i + CONSEC_NOTICE - 1]      # 第3次(達標)交易日
                start_pos = min(trig_pos + 1, len(td) - 1)  # 次一交易日起處置
                end_pos = min(start_pos + DISP_DAYS - 1, len(td) - 1)
                rows.append({
                    "stock_id": sid,
                    "disp_start": td[start_pos], "disp_end": td[end_pos],
                    "measure": "derived_from_notice", "reason": f"連續{run}日注意",
                    "source": "derived",
                })
                i += run
            else:
                i += 1
    return pd.DataFrame(rows, columns=["stock_id", "disp_start", "disp_end",
                                       "measure", "reason", "source"])


def disposition_day_set(disp_df: pd.DataFrame, trading_days) -> dict:
    """{stock_id -> set(處置交易日 Timestamp)},供回測 O(1) 查詢。"""
    td = pd.DatetimeIndex(sorted(pd.to_datetime(trading_days)))
    out = {}
    for _, r in disp_df.iterrows():
        mask = (td >= r["disp_start"]) & (td <= r["disp_end"])
        out.setdefault(r["stock_id"], set()).update(td[mask])
    return out


# ── 快取包裝 ────────────────────────────────────────────────────────────
def load_disposition(start_date: str, end_date: str, trading_days,
                     refresh: bool = False) -> pd.DataFrame:
    """抓注意→推導處置期間,快取(含快照戳)。回傳處置期間表。"""
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"
    cache = config.CACHE_DIR / f"disposition__ALL__{snap}.pkl"
    if cache.exists() and not refresh:
        try:
            return pd.read_pickle(cache)
        except Exception:
            pass
    notice = fetch_notice_history(start_date, end_date)
    if notice.empty:
        print("[disp] 無注意資料。")
        return pd.DataFrame()
    disp = build_disposition_periods(notice, trading_days)
    disp.to_pickle(cache)
    notice.to_pickle(config.CACHE_DIR / f"notice__ALL__{snap}.pkl")
    print(f"[disp] 注意事件 {len(notice)} 筆 → 推導處置期間 {len(disp)} 段(存 {cache.name})")
    return disp


if __name__ == "__main__":
    import data
    m = data.fetch_market_index()
    td = m["date"].tolist()
    start = str(m["date"].min())[:10]; end = str(m["date"].max())[:10]
    print(f"抓注意/推導處置:{start} ~ {end}")
    disp = load_disposition(start, end, td, refresh=True)
    if not disp.empty:
        print(disp.head(10).to_string(index=False))
        print(f"\n共 {disp['stock_id'].nunique()} 檔曾被推導處置、{len(disp)} 段")
    # 驗證:當前真實處置 vs 推導
    cur = fetch_current_punish()
    print(f"\n當前真實處置(punish)股票 {len(cur)} 檔:", cur["stock_id"].tolist() if not cur.empty else "(無)")
