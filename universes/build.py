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
import security_type

TWSE_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_ALL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"


def _to_float(x) -> float:
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _is_normal_4digit(code: str) -> bool:
    """代號**形狀**前篩(4 碼純數字、非 00 開頭)。

    這只擋權證/CB/特別股與 00 開頭 ETF,**不是**證券別判定 —— DR(91xx)與興櫃的
    代號同樣是 4 碼數字。真正的證券別白名單在 `build()` 裡用 TaiwanStockInfo 套。
    形狀規則共用 `security_type` 的那一份,免得四個檔案各寫一次。
    """
    return security_type.is_plausible_equity_code(code)


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
    print(f"[universe] 全市場 4 碼代號：{len(rows)} 檔")

    # 證券別白名單:openapi 只給代號,分不出 DR / 創新板 / ETF,一定要配
    # TaiwanStockInfo 的 type + industry_category(判定共用 `security_type`)。
    # `on_unknown="exclude"`:這是 live 的池建構,當天剛掛牌的股票可能還沒進
    # stock_info —— 排除並記數(印出來)比 raise 掉整次建池合理,而且方向是保守的。
    registry = security_type.build_registry(data_mod.fetch_stock_info())
    eligible = set(security_type.filter_ids(
        (r["stock_id"] for r in rows),
        registry=registry, source="build_universe.build", on_unknown="exclude"))
    dropped = [r["stock_id"] for r in rows if r["stock_id"] not in eligible]
    if dropped:
        print(f"[universe] 證券別排除(興櫃/DR/創新板/ETF/未知)：{len(dropped)} 檔"
              f"（例：{', '.join(sorted(dropped)[:8])}）")

    filtered = []
    n_fin = 0
    for r in rows:
        if r["stock_id"] not in eligible:
            continue
        ind = registry.get(r["stock_id"], ("", "", ""))[1]
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
