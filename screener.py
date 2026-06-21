# -*- coding: utf-8 -*-
"""
選股引擎
========
對 universe 中每檔股票：
  1. 抓資料 -> 算因子（factors.compute_factors）
  2. 取「指定日」那一列（預設最後一個交易日 = 今天選股）
  3. pre-filter：流動性、趨勢保護硬門檻
  4. 綜合評分（factors.composite_score）
  5. 依分數排序，輸出前 TOP_N

輸出：
  - DataFrame（給程式 / 回測用）
  - CSV（outputs/selection_YYYYMMDD.csv）
  - 易讀文字報告（print + outputs/selection_YYYYMMDD.txt）
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import pandas as pd

import config
import data
import factors
import universe as uni


def _build_reasons(row) -> str:
    """根據各因子分數，組出人看得懂的「選股理由」。"""
    tags = []
    if row.get("score_inst_mid", 0) >= 0.5 or row.get("score_inst_long", 0) >= 0.5:
        tags.append("法人持續買超")
    if row.get("score_inst_dip_buy", 0) >= 0.5:
        tags.append("跌時法人仍買(洗盤)")
    if row.get("score_bb_pullback", 0) >= 0.6:
        tags.append("回檔到月線買點")
    if row.get("score_ma_alignment", 0) >= 1.0:
        tags.append("均線多頭排列")
    if row.get("score_ma_squeeze", 0) >= 0.6:
        tags.append("均線糾結蓄勢")
    if row.get("score_vol_dryup", 0) >= 0.8:
        tags.append("量縮窒息")
    if row.get("score_margin_health", 0) >= 0.8:
        tags.append("資券結構健康")
    return "、".join(tags) if tags else "—"


def screen(
    symbols: Optional[List[str]] = None,
    as_of: Optional[str] = None,
    sample: bool = True,
    pool: Optional[int] = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    執行選股。
    - symbols：指定股票清單；None 則用 universe。
    - as_of：指定選股日 'YYYY-MM-DD'；None = 最後一個交易日。
    - pool：用「成交值前 N 大」池（需先跑 build_universe.py N），優先於 sample。
    - sample：pool 未指定時，universe 用小集合（True）或全市場（False）。
    """
    if symbols is None:
        if pool:
            symbols = uni.get_universe(top_n=pool)
        else:
            symbols = uni.get_universe(sample=sample)
    name_map = uni.get_name_map()
    industry_map = uni.get_industry_map()

    rows = []
    skipped = {"no_data": 0, "liquidity": 0, "trend": 0, "no_asof": 0, "excluded": 0}

    for sid in symbols:
        # pre-filter：排除金融 / ETF / 非普通股（任何 universe 模式都套用）
        industry = industry_map.get(sid, "")
        if config.EXCLUDE_FINANCE and ("金融" in industry or "保險" in industry):
            skipped["excluded"] += 1
            continue
        if "ETF" in industry or "ETN" in industry or sid.startswith("00"):
            skipped["excluded"] += 1
            continue

        bundle = data.fetch_bundle(sid)
        price = bundle.get("price")
        if price is None or price.empty:
            skipped["no_data"] += 1
            continue

        # pre-filter：流動性
        if not uni.passes_liquidity(price):
            skipped["liquidity"] += 1
            continue

        f = factors.compute_factors(bundle)
        if f.empty:
            skipped["no_data"] += 1
            continue

        # 取指定日（或最後一日）
        if as_of:
            as_of_ts = pd.to_datetime(as_of)
            sub = f[f["date"] <= as_of_ts]
            if sub.empty:
                skipped["no_asof"] += 1
                continue
            row = sub.iloc[-1]
        else:
            row = f.iloc[-1]

        # pre-filter：趨勢保護硬門檻
        if config.TREND_GUARD_ENABLED and not bool(row.get("trend_ok", False)):
            skipped["trend"] += 1
            continue

        score = factors.composite_score(row)
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, ""),
            "industry": industry_map.get(sid, ""),
            "date": row["date"].strftime("%Y-%m-%d"),
            "close": round(float(row["close"]), 2),
            "composite": score,
            "inst_6d": round(float(row.get("inst_6d", 0) or 0), 3),
            "inst_12d": round(float(row.get("inst_12d", 0) or 0), 3),
            "bb_pos": round(float(row.get("bb_pos", 0) or 0), 3),
            "near_high": round(float(row.get("near_high", 0) or 0), 3),
            "vol_ratio": round(float(row.get("vol_ratio", 0) or 0), 3),
            "margin_short_ratio": round(float(row.get("margin_short_ratio", 0) or 0), 1),
            # 保留各因子分數供報告
            "score_inst_mid": round(float(row.get("score_inst_mid", 0)), 3),
            "score_inst_long": round(float(row.get("score_inst_long", 0)), 3),
            "score_inst_dip_buy": round(float(row.get("score_inst_dip_buy", 0)), 3),
            "score_margin_health": round(float(row.get("score_margin_health", 0)), 3),
            "score_ma_alignment": round(float(row.get("score_ma_alignment", 0)), 3),
            "score_bb_pullback": round(float(row.get("score_bb_pullback", 0)), 3),
            "score_ma_squeeze": round(float(row.get("score_ma_squeeze", 0)), 3),
            "score_vol_dryup": round(float(row.get("score_vol_dryup", 0)), 3),
        })

    if not rows:
        if verbose:
            print(f"[screen] 無符合條件標的。skipped={skipped}")
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("composite", ascending=False).reset_index(drop=True)
    df["reason"] = df.apply(_build_reasons, axis=1)

    # 門檻過濾 + 取前 N
    selected = df[df["composite"] >= config.MIN_COMPOSITE].head(config.TOP_N).reset_index(drop=True)

    if verbose:
        _print_report(df, selected, skipped, as_of)
        _save_outputs(df, selected, as_of)

    return selected


def _print_report(full: pd.DataFrame, selected: pd.DataFrame, skipped: dict, as_of):
    day = as_of or (full["date"].iloc[0] if not full.empty else "—")
    print("=" * 72)
    print(f"  台股波段多因子選股  |  選股日：{day}")
    print("=" * 72)
    print(f"  掃描 {len(full)} 檔通過pre-filter；"
          f"略過：排除(金融/ETF) {skipped.get('excluded', 0)}、無資料 {skipped['no_data']}、"
          f"流動性 {skipped['liquidity']}、趨勢 {skipped['trend']}")
    print(f"  綜合分數 >= {config.MIN_COMPOSITE} 的標的：{len(selected)} 檔")
    print("-" * 72)
    if selected.empty:
        print("  （今日無達標標的）")
        # 仍列出分數最高前5供參考
        print("\n  分數最高前5（未達門檻，僅參考）：")
        for _, r in full.head(5).iterrows():
            print(f"    {r['stock_id']} {r['name']:<6} 分數 {r['composite']:>5}  {r['reason']}")
    else:
        for i, r in selected.iterrows():
            print(f"  {i+1:>2}. {r['stock_id']} {r['name']:<7} "
                  f"分數 {r['composite']:>5}  收 {r['close']:>7}  "
                  f"法人6d {r['inst_6d']:>6}  布林 {r['bb_pos']:>6}")
            print(f"       理由：{r['reason']}")
    print("=" * 72)


def _save_outputs(full: pd.DataFrame, selected: pd.DataFrame, as_of):
    stamp = (as_of or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    csv_path = config.OUTPUT_DIR / f"selection_{stamp}.csv"
    full.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  完整評分已存：{csv_path}")


if __name__ == "__main__":
    screen(sample=True, verbose=True)
