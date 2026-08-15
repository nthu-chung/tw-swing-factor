# -*- coding: utf-8 -*-
"""
因子體檢（factor audit）
========================
不改動既有選股/回測邏輯，獨立做一次「誠實的因子健康檢查」，回答：

  1. 冗餘？     因子兩兩相關矩陣 —— 哪些因子根本在講同一件事。
  2. 真 alpha？ 產業中性化 IC —— 把產業 beta 扣掉後，因子還剩多少預測力。
                （momentum 的正 IC 很可能只是「這兩年半導體大漲」的產業效應）
  3. 形狀？     分層(quintile)平均未來報酬 —— IC 只看單調相關，分層看「買最高分
                那群到底賺不賺、是否單調」，更直觀。順便驗證「台股買強 vs 買弱」。
  4. 穩定？     前後兩個子期間，IC 是否同號同量級 —— 排除「只是某一段行情的假象」。

所有分析都建立在同一個 panel 上（backtest._prepare_panel），只抓一次資料、算一次因子。
Panel 會 pickle 到 _cache/audit_panel_top{N}.pkl 供重複分析（--reuse）。

防未來函數：完全沿用 backtest._prepare_panel（fwd_ret 用未來 BT_IC_HORIZON 日收盤、
因子全因果），這裡只做「橫斷面統計」，不引入任何新的未來資訊。

用法：
  .venv/bin/python factor_audit.py            # top100，重新建 panel
  .venv/bin/python factor_audit.py --reuse    # 用上次 pickle 的 panel（快）
  .venv/bin/python factor_audit.py --top 100
"""
from __future__ import annotations

import sys
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

import config
import factors
import universe as uni
import backtest


SCORE_COLS = list(factors.SCORE_COLUMNS.values())
H = config.BT_IC_HORIZON
MIN_CROSS = 5  # 每日橫斷面至少 N 檔才算一個有效的橫斷面 IC


# ──────────────────────────────────────────────────────────────────────
# Panel 建立 / 快取
# ──────────────────────────────────────────────────────────────────────
def panel_cache_path(top_n: int):
    """稽核 panel 的快取路徑(**含快照與歷史長度**)。

    原 bug(2026-08-15 修):檔名只有 `audit_panel_{mode}.pkl`,而 `--reuse` 就
    直接吃它。換 `SNAPSHOT_END_DATE` 或 `HISTORY_DAYS` 後重跑會靜默重用上一個
    範圍建出來的 panel —— 與 P0-2 在 `data.py` 立的規則相同的洞,只是發生在
    自己拼字串的衍生快取上。
    """
    mode = (
        f"dynamic_top{top_n}_candidate{config.DYNAMIC_UNIVERSE_CANDIDATE_POOL}"
        if config.DYNAMIC_UNIVERSE_ENABLED else f"static_top{top_n}"
    )
    snap = getattr(config, "SNAPSHOT_END_DATE", "").strip() or "live"
    return config.CACHE_DIR / f"audit_panel_{mode}__{snap}__d{config.HISTORY_DAYS}.pkl"


def build_panel(top_n: int, reuse: bool) -> pd.DataFrame:
    cache_path = panel_cache_path(top_n)
    if reuse and cache_path.exists():
        print(f"[audit] 重用 panel：{cache_path}")
        return pickle.load(open(cache_path, "rb"))

    symbols = uni.get_research_candidates(universe_top_n=top_n)
    print(f"[audit] universe = {len(symbols)} 檔，建立 panel（會抓資料/算因子，請稍候）...")
    # research-only:候選池來自單一日期的 top-N 排名(非 PIT),所以顯式宣告成
    # static comparator;這裡的 IC 只能當發掘層線索,不可當正式證據。
    # members_only=True:本檔全部是「當日橫斷面」統計(IC、分位、產業中性化),
    # 因子本身已在引擎內部於完整個股序列上算好,所以只留成員日不影響結果 ——
    # 但 panel 會被標成 members_only,將來有人在它上面加 ts_ 會 fail-closed。
    panel = backtest.build_research_panel(
        symbols,
        dynamic_enabled=config.DYNAMIC_UNIVERSE_ENABLED,
        universe_top_n=top_n,
        static_universe_comparator=True,
        members_only=True,
    )

    # 補上產業別（產業中性化要用）
    ind_map = uni.get_industry_map()
    panel["industry"] = panel["stock_id"].map(ind_map).fillna("")

    pickle.dump(panel, open(cache_path, "wb"))
    print(f"[audit] panel：{len(panel)} 列、{panel['stock_id'].nunique()} 檔、"
          f"{panel['date'].nunique()} 天；已快取 {cache_path}")
    return panel


# ──────────────────────────────────────────────────────────────────────
# (1) 因子相關矩陣
# ──────────────────────────────────────────────────────────────────────
def corr_matrix(panel: pd.DataFrame) -> pd.DataFrame:
    sub = panel[SCORE_COLS].dropna()
    # Spearman：因子分數多為非線性壓縮過，用 rank 相關較穩健
    cm = sub.corr(method="spearman")
    cm.index = [c.replace("score_", "") for c in cm.index]
    cm.columns = [c.replace("score_", "") for c in cm.columns]
    return cm


def print_corr(cm: pd.DataFrame):
    print("\n" + "=" * 78)
    print("  (1) 因子相關矩陣（Spearman；|r|>0.5 代表高度冗餘，講同一件事）")
    print("=" * 78)
    names = list(cm.columns)
    hdr = "".join(f"{n[:6]:>8}" for n in names)
    print(f"  {'':<14}{hdr}")
    for r in names:
        cells = "".join(f"{cm.loc[r, c]:>8.2f}" for c in names)
        print(f"  {r:<14}{cells}")
    # 列出高相關配對
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = cm.loc[a, b]
            if abs(v) >= 0.4:
                pairs.append((abs(v), a, b, v))
    pairs.sort(reverse=True)
    if pairs:
        print("\n  高相關配對（|r|>=0.4，冗餘候選）：")
        for _, a, b, v in pairs:
            print(f"    {a:<14} ↔ {b:<14} r = {v:+.2f}")


# ──────────────────────────────────────────────────────────────────────
# 通用：每日橫斷面 IC（可選擇產業中性化），含重疊校正 t 值
# ──────────────────────────────────────────────────────────────────────
def _daily_ic(panel: pd.DataFrame, col: str, neutralize_industry: bool) -> np.ndarray:
    ics = []
    for d, grp in panel.groupby("date"):
        sub = grp[[col, "fwd_ret", "industry"]].dropna(subset=[col, "fwd_ret"])
        if len(sub) < MIN_CROSS or sub[col].nunique() < 2:
            continue
        x = sub[col].astype(float).copy()
        y = sub["fwd_ret"].astype(float).copy()
        if neutralize_industry:
            # 在「當日 × 產業」內各自 demean，扣掉產業共同漂移後再算相關
            x = x - sub.groupby("industry")[col].transform("mean")
            y = y - sub.groupby("industry")["fwd_ret"].transform("mean")
            if x.nunique() < 2:
                continue
        ic = x.corr(y, method="spearman")
        if pd.notna(ic):
            ics.append(ic)
    return np.array(ics)


def _ic_stats(ics: np.ndarray) -> dict:
    if len(ics) < 2:
        return {"mean_ic": np.nan, "t_stat": np.nan, "n_days": len(ics)}
    mean_ic = float(ics.mean())
    ic_std = float(ics.std(ddof=1))
    ic_ir = mean_ic / ic_std if ic_std > 0 else np.nan
    n_eff = max(1.0, len(ics) / H)  # 重疊校正：有效獨立樣本 ≈ 天數 / 視窗
    t_stat = ic_ir * np.sqrt(n_eff) if pd.notna(ic_ir) else np.nan
    return {"mean_ic": mean_ic, "t_stat": t_stat, "n_days": len(ics)}


# ──────────────────────────────────────────────────────────────────────
# (2) 產業中性化 IC：原始 vs 產業中性
# ──────────────────────────────────────────────────────────────────────
def industry_neutral_ic(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in SCORE_COLS:
        raw = _ic_stats(_daily_ic(panel, col, neutralize_industry=False))
        neu = _ic_stats(_daily_ic(panel, col, neutralize_industry=True))
        rows.append({
            "factor": col.replace("score_", ""),
            "ic_raw": raw["mean_ic"], "t_raw": raw["t_stat"],
            "ic_neutral": neu["mean_ic"], "t_neutral": neu["t_stat"],
            "ic_decay": (raw["mean_ic"] - neu["mean_ic"]) if pd.notna(raw["mean_ic"]) else np.nan,
        })
    out = pd.DataFrame(rows).sort_values("ic_neutral", ascending=False,
                                         key=lambda s: s.abs(), na_position="last")
    return out.reset_index(drop=True)


def print_neutral(df: pd.DataFrame):
    print("\n" + "=" * 78)
    print("  (2) 產業中性化 IC：扣掉產業 beta 後，因子還剩多少真 alpha？")
    print("=" * 78)
    print(f"  {'因子':<14}{'IC原始':>9}{'t原始':>7}{'IC中性':>9}{'t中性':>7}{'衰減':>8}  判讀")
    print("  " + "-" * 74)
    for _, r in df.iterrows():
        def f(x, p=4): return f"{x:+.{p}f}" if pd.notna(x) else "  n/a"
        # 判讀：中性化後仍 |t|>2 = 真 alpha；衰減大 = 多半是產業效應
        tn = r["t_neutral"]
        icn = r["ic_neutral"]
        if pd.notna(tn) and abs(tn) > 2 and abs(icn) > 0.03:
            v = "★ 真 alpha（產業中性後仍顯著）"
        elif pd.notna(r["ic_raw"]) and pd.notna(icn) and abs(r["ic_raw"]) > 0.03 and abs(icn) < 0.02:
            v = "⚠ 多為產業beta（中性後消失）"
        elif pd.notna(icn) and icn < -0.02:
            v = "✗ 反向"
        else:
            v = "弱/無"
        print(f"  {r['factor']:<14}{f(r['ic_raw']):>9}{f(r['t_raw'],2):>7}"
              f"{f(r['ic_neutral']):>9}{f(r['t_neutral'],2):>7}{f(r['ic_decay']):>8}  {v}")
    print("  " + "-" * 74)
    print("  註：t 已對 fwd_ret 重疊保守校正(有效樣本=天數/視窗)；|t|>2 才算顯著。")


# ──────────────────────────────────────────────────────────────────────
# (3) 分層(quintile)平均未來報酬
# ──────────────────────────────────────────────────────────────────────
def quintile_returns(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in SCORE_COLS:
        # 每日把因子分數切 5 層，取各層 fwd_ret 均值，再對時間平均
        layer_rets = {q: [] for q in range(1, 6)}
        for d, grp in panel.groupby("date"):
            sub = grp[[col, "fwd_ret"]].dropna()
            if len(sub) < 10 or sub[col].nunique() < 5:
                continue
            try:
                sub = sub.copy()
                sub["q"] = pd.qcut(sub[col].rank(method="first"), 5, labels=False) + 1
            except ValueError:
                continue
            for q in range(1, 6):
                vals = sub.loc[sub["q"] == q, "fwd_ret"]
                if len(vals):
                    layer_rets[q].append(vals.mean())
        means = {q: (np.mean(v) if v else np.nan) for q, v in layer_rets.items()}
        spread = means[5] - means[1] if pd.notna(means[5]) and pd.notna(means[1]) else np.nan
        # 單調性：Spearman(層序, 各層均報酬)
        qs = [q for q in range(1, 6) if pd.notna(means[q])]
        mono = (pd.Series([means[q] for q in qs]).corr(pd.Series(qs), method="spearman")
                if len(qs) >= 3 else np.nan)
        rows.append({"factor": col.replace("score_", ""),
                     "Q1": means[1], "Q2": means[2], "Q3": means[3],
                     "Q4": means[4], "Q5": means[5],
                     "Q5-Q1": spread, "mono": mono})
    return pd.DataFrame(rows).sort_values("Q5-Q1", ascending=False,
                                          key=lambda s: s.abs(), na_position="last").reset_index(drop=True)


def print_quintile(df: pd.DataFrame):
    print("\n" + "=" * 78)
    print(f"  (3) 分層平均未來 {H} 日報酬（Q5=因子分數最高層；想看到 Q5>Q1 且單調遞增）")
    print("=" * 78)
    print(f"  {'因子':<14}{'Q1':>8}{'Q2':>8}{'Q3':>8}{'Q4':>8}{'Q5':>8}{'Q5-Q1':>9}{'單調':>7}")
    print("  " + "-" * 74)
    for _, r in df.iterrows():
        def p(x): return f"{x:+.2%}" if pd.notna(x) else "   n/a"
        mono = f"{r['mono']:+.2f}" if pd.notna(r['mono']) else " n/a"
        print(f"  {r['factor']:<14}{p(r['Q1']):>8}{p(r['Q2']):>8}{p(r['Q3']):>8}"
              f"{p(r['Q4']):>8}{p(r['Q5']):>8}{p(r['Q5-Q1']):>9}{mono:>7}")
    print("  " + "-" * 74)
    print("  讀法：Q5-Q1>0 且 單調≈+1 = 因子方向對；Q5-Q1<0 = 反向；忽高忽低 = 雜訊。")


# ──────────────────────────────────────────────────────────────────────
# (4) 子期間穩定性
# ──────────────────────────────────────────────────────────────────────
def subperiod_stability(panel: pd.DataFrame) -> pd.DataFrame:
    dates = np.sort(panel["date"].unique())
    mid = dates[len(dates) // 2]
    p1 = panel[panel["date"] < mid]
    p2 = panel[panel["date"] >= mid]
    rows = []
    for col in SCORE_COLS:
        s1 = _ic_stats(_daily_ic(p1, col, False))
        s2 = _ic_stats(_daily_ic(p2, col, False))
        ic1, ic2 = s1["mean_ic"], s2["mean_ic"]
        same_sign = (pd.notna(ic1) and pd.notna(ic2) and np.sign(ic1) == np.sign(ic2))
        rows.append({"factor": col.replace("score_", ""),
                     "ic_first": ic1, "ic_second": ic2,
                     "same_sign": same_sign})
    out = pd.DataFrame(rows)
    return out, str(mid)[:10]


def print_stability(df: pd.DataFrame, mid: str):
    print("\n" + "=" * 78)
    print(f"  (4) 子期間穩定性（前半 vs 後半，分界 {mid}；同號才可信）")
    print("=" * 78)
    print(f"  {'因子':<14}{'IC前半':>10}{'IC後半':>10}{'同號?':>8}  判讀")
    print("  " + "-" * 74)
    for _, r in df.iterrows():
        def f(x): return f"{x:+.4f}" if pd.notna(x) else "   n/a"
        if r["same_sign"] and pd.notna(r["ic_first"]) and min(abs(r["ic_first"]), abs(r["ic_second"])) > 0.02:
            v = "穩定"
        elif r["same_sign"]:
            v = "同號但弱"
        else:
            v = "✗ 兩半翻號（不可信）"
        ss = "是" if r["same_sign"] else "否"
        print(f"  {r['factor']:<14}{f(r['ic_first']):>10}{f(r['ic_second']):>10}{ss:>8}  {v}")


# ──────────────────────────────────────────────────────────────────────
def main():
    argv = sys.argv[1:]
    reuse = "--reuse" in argv
    top_n = 100
    if "--top" in argv:
        top_n = int(argv[argv.index("--top") + 1])

    panel = build_panel(top_n, reuse)
    panel = panel.dropna(subset=["fwd_ret"]).reset_index(drop=True)

    cm = corr_matrix(panel); print_corr(cm)
    neu = industry_neutral_ic(panel); print_neutral(neu)
    qt = quintile_returns(panel); print_quintile(qt)
    st, mid = subperiod_stability(panel); print_stability(st, mid)

    # 存檔
    neu.to_csv(config.OUTPUT_DIR / f"audit_neutral_ic_top{top_n}.csv", index=False, encoding="utf-8-sig")
    qt.to_csv(config.OUTPUT_DIR / f"audit_quintile_top{top_n}.csv", index=False, encoding="utf-8-sig")
    cm.to_csv(config.OUTPUT_DIR / f"audit_corr_top{top_n}.csv", encoding="utf-8-sig")
    print(f"\n[audit] 結果已存 outputs/audit_*_top{top_n}.csv")


if __name__ == "__main__":
    main()
