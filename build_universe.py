# -*- coding: utf-8 -*-
"""
建立「成交值前 N 大」選股池
============================
為什麼用成交值排名（而非市值）？
  - FinMind 免費版不能一次抓全市場，必須先有名單才能逐檔抓歷史 → 雞生蛋問題。
  - TWSE / TPEX 官方 OpenAPI 可「一次」抓全市場當日資料（免費、不限流）。
  - 成交值（TradeValue）= 當日資金關注度，且天然保證「進得去、出得來」的流動性
    —— 對波段選股比純市值更實用。

流程：抓 TWSE 全上市 + TPEX 全上櫃當日 → 排除 ETF/權證/金融 → 取成交值前 N →
      存成 outputs/universe_top{N}.json（供 config / 回測讀取）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import requests

import config
import data as data_mod

TWSE_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_ALL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"


def _to_float(x) -> float:
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _is_normal_4digit(code: str) -> bool:
    """4 碼純數字普通股；排除 ETF(00開頭)、權證/特殊(>4碼或含字母)。"""
    return len(code) == 4 and code.isdigit() and not code.startswith("00")


def fetch_twse_rows() -> list[dict]:
    r = requests.get(TWSE_ALL, timeout=30)
    r.raise_for_status()
    out = []
    for it in r.json():
        code = str(it.get("Code", "")).strip()
        if not _is_normal_4digit(code):
            continue
        out.append({
            "stock_id": code,
            "name": str(it.get("Name", "")).strip(),
            "trade_value": _to_float(it.get("TradeValue")),
            "close": _to_float(it.get("ClosingPrice")),
            "market": "TWSE",
        })
    return out


def fetch_tpex_rows() -> list[dict]:
    r = requests.get(TPEX_ALL, timeout=30)
    r.raise_for_status()
    out = []
    for it in r.json():
        code = str(it.get("SecuritiesCompanyCode", "")).strip()
        if not _is_normal_4digit(code):
            continue
        out.append({
            "stock_id": code,
            "name": str(it.get("CompanyName", "")).strip(),
            "trade_value": _to_float(it.get("TransactionAmount")),
            "close": _to_float(it.get("Close")),
            "market": "TPEX",
        })
    return out


def build(top_n: int = 100, exclude_finance: bool = True) -> list[dict]:
    rows = fetch_twse_rows() + fetch_tpex_rows()
    print(f"[universe] 全市場普通股(4碼)：{len(rows)} 檔")

    # 用 FinMind 的 stock_info 取得產業別，過濾金融保險
    industry_map = {}
    if exclude_finance:
        info = data_mod.fetch_stock_info()
        if not info.empty:
            industry_map = {str(r["stock_id"]).strip(): str(r.get("industry", "")).strip()
                            for _, r in info.iterrows()}

    filtered = []
    n_fin = 0
    for r in rows:
        ind = industry_map.get(r["stock_id"], "")
        if exclude_finance and ("金融" in ind or "保險" in ind or "金control" in ind):
            n_fin += 1
            continue
        r["industry"] = ind
        filtered.append(r)
    if exclude_finance:
        print(f"[universe] 排除金融保險：{n_fin} 檔")

    ranked = sorted(filtered, key=lambda x: x["trade_value"], reverse=True)[:top_n]
    # 建構日 provenance：openapi 只給「當日」全市場,故池的 as_of = 實際建構日。
    # 之後回測會檢查 as_of 是否晚於資料快照(晚=未來池 look-ahead)。
    as_of = datetime.now().strftime("%Y-%m-%d")
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
        r["as_of"] = as_of
    return ranked


def save(ranked: list[dict], top_n: int) -> Path:
    path = config.OUTPUT_DIR / f"universe_top{top_n}.json"
    path.write_text(json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load(top_n: int = 100) -> list[str]:
    """讀回先前建立的池，回傳 stock_id 清單（給回測/選股用）。"""
    path = config.OUTPUT_DIR / f"universe_top{top_n}.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [r["stock_id"] for r in rows]


def load_asof(top_n: int = 100) -> str | None:
    """回傳候選池的建構日 as_of(無 provenance 時回 None)。"""
    path = config.OUTPUT_DIR / f"universe_top{top_n}.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text(encoding="utf-8"))
    if rows and isinstance(rows[0], dict) and rows[0].get("as_of"):
        return str(rows[0]["as_of"])
    return None


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    ranked = build(top_n=n)
    p = save(ranked, n)
    print(f"\n[universe] 成交值前 {n} 大（前 15 名）：")
    for r in ranked[:15]:
        print(f"  {r['rank']:>3}. {r['stock_id']} {r['name']:<8} "
              f"成交值 {r['trade_value']/1e8:>8.1f} 億  {r['industry']}")
    print(f"\n已存：{p}")
