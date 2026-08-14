# -*- coding: utf-8 -*-
"""
權重實驗：依因子體檢結論，比較不同因子權重的回測績效
=====================================================
體檢（factor_audit.py）的結論：

  真 alpha（產業中性後仍 |t|>2、分層單調、子期間穩定）：
    - momentum     ★ 最強：中性後 IC +0.098/t2.45、分層單調 +0.90、子期間穩定
    - ma_alignment ★ 中性後 t2.10、子期間穩定（但與 momentum 相關 0.68，冗餘）
    - margin_health★ 中性後 t2.30（但分層單調僅 +0.30、前半子期間弱）

  該砍（反向 / 翻號 / 無效）：
    - ma_squeeze   ✗ IC 反向、分層 Q5-Q1 −3.39%/單調 −1.00（完美反向！買糾結在台股反而虧）
    - vol_dryup    ✗ 子期間兩半翻號、IC≈0
    - bb_pullback    弱、無單調、與 ma_alignment 冗餘 0.54
    - inst_long    ✗ 子期間翻號，且與 inst_mid 冗餘 0.74

  邊際保留（小權重）：
    - inst_mid     法人短中窗，弱但同號
    - inst_dip_buy 中性後 t1.99 接近顯著

核心發現：台股這兩年是「動能市」——買強(momentum/ma_alignment)有效，
買弱/拉回(ma_squeeze/vol_dryup/bb_pullback)無效甚至反向。原 config 卻把
35% 權重給「買弱」群，等於跟有效訊號對賭。本實驗驗證「砍掉買弱群、
動能傾斜」能否改善 Sharpe / MaxDD。

用法：
  .venv/bin/python experiment_weights.py
"""
from __future__ import annotations

import copy

import pandas as pd

import config
import backtest
import evaluation_split
import universe as uni


# ── 候選權重組（key 必須是 factors.SCORE_COLUMNS 的鍵）─────────────────
WEIGHT_SETS = {
    "baseline(原始)": {
        "momentum": 0.20, "inst_mid": 0.15, "inst_long": 0.15,
        "inst_dip_buy": 0.05, "margin_health": 0.05,
        "ma_alignment": 0.10, "bb_pullback": 0.10,
        "ma_squeeze": 0.10, "vol_dryup": 0.05,
    },
    # 砍掉反向/翻號/冗餘，動能傾斜，保留品質(margin)與少量法人
    "audit_lean(精簡)": {
        "momentum": 0.35, "ma_alignment": 0.15, "margin_health": 0.20,
        "inst_mid": 0.15, "inst_dip_buy": 0.15,
    },
    # 更純的動能+品質（連法人都先拿掉，看法人到底有沒有加分）
    "mom_quality(動能+品質)": {
        "momentum": 0.50, "ma_alignment": 0.20, "margin_health": 0.30,
    },
    # 純動能（單因子對照，看其他因子是否真的有增量貢獻）
    "momentum_only(純動能)": {
        "momentum": 1.0,
    },
}


def run_one(label: str, weights: dict, symbols, rebalance: int, top_n: int,
            start: str, end: str) -> dict:
    config.FACTOR_WEIGHTS = copy.deepcopy(weights)
    # research-only:候選池是單一日期排名(非 PIT)→ 顯式 static comparator。
    res = backtest.backtest_portfolio(symbols=symbols, sample=False,
                                      start_date=start, end_date=end,
                                      rebalance_every=rebalance, top_n=top_n,
                                      static_universe_comparator=True)
    if "summary" not in res:
        return {"label": label, "error": res.get("error", "?")}
    s = res["summary"]
    return {
        "label": label,
        "segment": "IS",
        "n_trades": s["n_trades"],
        "win_rate": s["win_rate"],
        "payoff": s["payoff_ratio"],
        "cum_ret": s["cum_ret"],
        "ann_ret": s["ann_ret"],
        "sharpe": s["sharpe"],
        "sortino": s.get("sortino"),
        "calmar": s.get("calmar"),
        "max_dd": s["max_drawdown"],
    }


def main():
    top_n = 100
    rebalance = 5
    pick = 5
    symbols = uni.get_research_candidates(universe_top_n=top_n)
    calendar_run = backtest.backtest_portfolio(
        symbols=symbols, sample=False, rebalance_every=rebalance, top_n=pick,
        static_universe_comparator=True,
    )
    if "equity_curve" not in calendar_run:
        raise RuntimeError(calendar_run.get("error", "無法建立研究交易日曆"))
    split = evaluation_split.build_evaluation_split(calendar_run["equity_curve"]["date"])
    start, end = split.is_window
    print(f"[experiment] universe={len(symbols)} 檔，rebalance={rebalance}日，最多持有{pick}檔")
    print(f"[experiment] 參數比較只使用 IS {start}~{end}；OS {split.os_window[0]} 起不在本腳本揭露\n")

    orig = copy.deepcopy(config.FACTOR_WEIGHTS)
    rows = []
    for label, w in WEIGHT_SETS.items():
        print(f"  跑：{label} ...")
        rows.append(run_one(label, w, symbols, rebalance, pick, start, end))
    config.FACTOR_WEIGHTS = orig  # 還原

    df = pd.DataFrame(rows)
    print("\n" + "=" * 92)
    print(f"  權重實驗對比（IS only {start}~{end} / trend 退場）")
    print("=" * 92)
    hdr = f"  {'權重組':<22}{'筆數':>5}{'勝率':>7}{'賺賠':>6}{'累積':>8}{'年化':>8}{'Sharpe':>8}{'Sortino':>8}{'Calmar':>8}{'MaxDD':>8}"
    print(hdr)
    print("  " + "-" * 88)
    for _, r in df.iterrows():
        if "error" in r and pd.notna(r.get("error")):
            print(f"  {r['label']:<22}  ERROR: {r['error']}")
            continue
        print(f"  {r['label']:<22}{int(r['n_trades']):>5}{r['win_rate']:>7.1%}"
              f"{r['payoff']:>6.2f}{r['cum_ret']:>8.1%}{r['ann_ret']:>8.1%}"
              f"{r['sharpe']:>8.2f}{r['sortino']:>8.2f}{r['calmar']:>8.2f}{r['max_dd']:>8.1%}")
    print("=" * 92)

    df.to_csv(config.OUTPUT_DIR / "experiment_weights.csv", index=False, encoding="utf-8-sig")
    print(f"\n[experiment] 已存 outputs/experiment_weights.csv")


if __name__ == "__main__":
    main()
