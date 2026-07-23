# -*- coding: utf-8 -*-
"""
選股池（universe）載入與 pre-filter。

- get_universe(sample=True)：快速原型用小集合（config.SAMPLE_UNIVERSE）。
- get_universe(sample=False)：全市場上市櫃普通股（去除 ETF / 金融，視 config）。
- apply_prefilter()：套用流動性 / 產業 / ETF 排除。
"""

from __future__ import annotations

from typing import List, Dict

import pandas as pd

import config
import data


def _is_normal_stock(stock_id: str, market_type: str) -> bool:
    """4 碼數字、上市或上櫃普通股。"""
    if not (len(stock_id) == 4 and stock_id.isdigit()):
        return False
    if config.EXCLUDE_ETF_PREFIX0 and stock_id.startswith("00"):
        return False
    return True


def get_universe(sample: bool = True, top_n: int = None) -> List[str]:
    """
    回傳股票代號清單（已去重）。
    - sample=True：小集合（config.SAMPLE_UNIVERSE）。
    - top_n 指定（如 100）：讀 build_universe.py 產生的「成交值前 N 大」池。
    - 否則：全市場上市櫃普通股。
    """
    if top_n:
        try:
            import build_universe
            ids = build_universe.load(top_n)
            if ids:
                seen, out = set(), []
                for s in ids:
                    if s not in seen:
                        seen.add(s); out.append(s)
                return out
            print(f"[universe] 找不到 top{top_n} 池，請先跑 build_universe.py")
        except Exception as e:
            print(f"[universe] 載入 top{top_n} 失敗：{e}")

    if sample:
        seen, out = set(), []
        for s in config.SAMPLE_UNIVERSE:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    info = data.fetch_stock_info()
    if info.empty:
        print("[universe] 無法取得全市場清單，改用 sample")
        return get_universe(sample=True)

    out = []
    for _, row in info.iterrows():
        sid = str(row.get("stock_id", "")).strip()
        mtype = str(row.get("market_type", "")).strip()
        industry = str(row.get("industry", "")).strip()
        if not _is_normal_stock(sid, mtype):
            continue
        if config.EXCLUDE_FINANCE and ("金融" in industry or "金control" in industry):
            continue
        out.append(sid)
    return sorted(set(out))


def get_research_candidates(universe_top_n: int = None,
                            candidate_pool_n: int = None) -> List[str]:
    """Return the candidate set used to build a daily dynamic universe.

    ``universe_top_n`` is the desired number of eligible names per date.  When
    dynamic-universe mode is enabled, candidates must be broader than that
    daily target; the current bootstrap uses the saved current top300 file.

    This removes direct *daily ranking* look-ahead, but it does not make a
    current top300 candidate file survivorship-free.  The backtest metadata
    reports this limitation explicitly.
    """
    target = universe_top_n or config.DYNAMIC_UNIVERSE_TOP_N
    if not config.DYNAMIC_UNIVERSE_ENABLED:
        return get_universe(top_n=target)

    pool = candidate_pool_n or config.DYNAMIC_UNIVERSE_CANDIDATE_POOL
    if pool < target:
        raise ValueError(
            f"動態 universe 候選池({pool})不可小於每日目標({target})"
        )
    ids = get_universe(top_n=pool)
    if len(ids) < target:
        raise ValueError(
            f"候選池只有 {len(ids)} 檔，少於動態 universe 目標 {target}；"
            f"請先跑 `.venv/bin/python build_universe.py {pool}`"
        )
    return ids


def get_industry_map() -> Dict[str, str]:
    """stock_id -> 產業別。"""
    info = data.fetch_stock_info()
    if info.empty:
        return {}
    return {str(r["stock_id"]).strip(): str(r.get("industry", "")).strip()
            for _, r in info.iterrows()}


def get_name_map() -> Dict[str, str]:
    """stock_id -> 名稱。"""
    info = data.fetch_stock_info()
    if info.empty:
        return {}
    return {str(r["stock_id"]).strip(): str(r.get("name", "")).strip()
            for _, r in info.iterrows()}


def passes_liquidity(price_df: pd.DataFrame) -> bool:
    """近 20 日均量（張）是否達門檻。volume 單位為股，/1000 = 張。"""
    if price_df is None or price_df.empty or "volume" not in price_df.columns:
        return False
    recent = price_df.tail(20)
    avg_lots = recent["volume"].mean() / 1000.0
    return avg_lots >= config.MIN_AVG_VOLUME_LOTS


if __name__ == "__main__":
    u = get_universe(sample=True)
    print(f"sample universe: {len(u)} 檔 -> {u}")
