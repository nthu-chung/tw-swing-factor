# -*- coding: utf-8 -*-
"""
弱市 / 抗跌 + 相對強勢因子研究
==============================
核心問題（在既有純動能底子上）：
    加入「相對強勢(RS) / 抗跌」的維度，能不能在『大盤走弱』時帶來增量 edge
    （降低 MaxDD / 改善弱市段報酬），而不是只跟 momentum 冗餘？

為什麼要「切弱市子期間」看？
    抗跌因子（下行 beta 低、下跌日相對抗跌）的天性是「大盤跌時少跌」。在多頭
    行情這種股會落後，所以「全期 IC」一定是負的（factor_audit 已證實）。它們真
    正該發光的地方是『大盤走弱』的那幾段。只看全期會直接把這個假設埋葬。

本腳本做四件事：
  (1) 大盤(TAIEX)regime 分類（全因果，只用 ≤當日資訊）：
        weak_ma20 / weak_ma60 / weak_dd5(回撤>5%) / weak_ret20(近20日負報酬)
  (2) regime-條件 IC：把每日橫斷面 IC 依「訊號日的大盤 regime」分成弱市 vs 強市
        兩組，看抗跌/RS 因子是否『在弱市翻正』。這是可交易的（regime 用過去資訊）。
  (3) 前瞻-regime 診斷 IC：依「未來20日大盤是否下跌」分組，直接檢驗抗跌因子的
        機械前提——大盤真的跌時，高抗跌分的股是否真的相對抗跌。（診斷用，非訊號）
  (4) 投組層級比較：純動能 vs 動能+RS/抗跌，分 IS/OS 報 MaxDD / Sharpe，以及 2025 關稅
        股災窗口（大盤 -28.7% 那段）的表現。看加抗跌是否真能壓低回撤。

防未來函數：
  - regime (2) 完全因果（MA/回撤/報酬都只看過去）。
  - 前瞻 regime (3) 明確標為「診斷」，不是可交易訊號。
  - fwd_ret 沿用 backtest 的未來20日收盤，不進因子。

用法：
  .venv/bin/python defensive_rs.py            # 用快取 panel（factor_audit 建的）
  .venv/bin/python defensive_rs.py --rebuild  # 重新建 panel
輸出：console + outputs/defensive_rs_*.csv（報告數字來源）
"""
from __future__ import annotations

import sys
import copy
import pickle

import numpy as np
import pandas as pd

import config
import data
import factors
import universe as uni
import backtest
import evaluation_split


PANEL_PATH = config.CACHE_DIR / (
    f"audit_panel_dynamic_top100_candidate{config.DYNAMIC_UNIVERSE_CANDIDATE_POOL}.pkl"
    if config.DYNAMIC_UNIVERSE_ENABLED else "audit_panel_static_top100.pkl"
)
H = config.BT_IC_HORIZON
MIN_CROSS = 5

# 研究的因子（動能為對照基準）
FACTORS = {
    "momentum": "score_momentum",
    "rs": "score_rs",
    "downside_resilience": "score_downside_resilience",
    "down_day_rs": "score_down_day_rs",
}


# ── Panel ───────────────────────────────────────────────────────────────
def get_panel(rebuild: bool, top_n: int = 100) -> pd.DataFrame:
    if not rebuild and PANEL_PATH.exists():
        print(f"[def-rs] 重用 panel：{PANEL_PATH}")
        panel = pickle.load(open(PANEL_PATH, "rb"))
    else:
        symbols = uni.get_research_candidates(universe_top_n=top_n)
        print(f"[def-rs] 建 panel：{len(symbols)} 檔 …")
        # research-only:候選池是單一日期排名(非 PIT)→ 顯式 static comparator。
        # members_only=True:本檔只做當日橫斷面統計(因子已在引擎內部於完整個股
        # 序列上算好),留成員日不影響結果;標籤會擋掉將來誤加的 ts_ 運算。
        panel = backtest.build_research_panel(
            symbols,
            dynamic_enabled=config.DYNAMIC_UNIVERSE_ENABLED,
            universe_top_n=top_n,
            static_universe_comparator=True,
            members_only=True,
        )
        panel["industry"] = panel["stock_id"].map(uni.get_industry_map()).fillna("")
        pickle.dump(panel, open(PANEL_PATH, "wb"))
    return panel.dropna(subset=["fwd_ret"]).reset_index(drop=True)


# ── (1) 大盤 regime（全因果）────────────────────────────────────────────
def market_regime() -> pd.DataFrame:
    m = data.fetch_market_index().sort_values("date").reset_index(drop=True)
    m["date"] = m["date"].astype("datetime64[ns]")
    c = m["close"]
    m["ma20"] = c.rolling(20).mean()
    m["ma60"] = c.rolling(60).mean()
    m["dd"] = c / c.cummax() - 1.0                 # 距歷史高點回撤（因果：cummax 只看過去）
    m["ret20"] = c / c.shift(20) - 1.0             # 近20日報酬
    reg = pd.DataFrame({"date": m["date"]})
    reg["weak_ma20"] = (c < m["ma20"]).values
    reg["weak_ma60"] = (c < m["ma60"]).values
    reg["weak_dd5"] = (m["dd"] < -0.05).values
    reg["weak_ret20"] = (m["ret20"] < 0).values
    reg["mkt_close"] = c.values
    reg["mkt_dd"] = m["dd"].values
    # 前瞻診斷用：未來20日大盤報酬（非因果，僅供 (3)）
    reg["mkt_fwd20"] = (c.shift(-H) / c - 1.0).values
    return reg


# ── 每日橫斷面 IC（可選產業中性、可限定日期集合）────────────────────────
def _daily_ic(panel: pd.DataFrame, col: str, neutralize: bool, dates=None) -> np.ndarray:
    ics = []
    g = panel if dates is None else panel[panel["date"].isin(dates)]
    for d, grp in g.groupby("date"):
        sub = grp[[col, "fwd_ret", "industry"]].dropna(subset=[col, "fwd_ret"])
        if len(sub) < MIN_CROSS or sub[col].nunique() < 2:
            continue
        x = sub[col].astype(float).copy()
        y = sub["fwd_ret"].astype(float).copy()
        if neutralize:
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
    sd = float(ics.std(ddof=1))
    ir = mean_ic / sd if sd > 0 else np.nan
    n_eff = max(1.0, len(ics) / H)      # 重疊校正
    t = ir * np.sqrt(n_eff) if pd.notna(ir) else np.nan
    return {"mean_ic": mean_ic, "t_stat": t, "n_days": len(ics)}


# ── (2) regime-條件 IC ──────────────────────────────────────────────────
def regime_conditional_ic(panel: pd.DataFrame, reg: pd.DataFrame,
                          regime_col: str, neutralize: bool = True) -> pd.DataFrame:
    weak_dates = set(reg.loc[reg[regime_col], "date"])
    strong_dates = set(reg.loc[~reg[regime_col], "date"])
    rows = []
    for name, col in FACTORS.items():
        w = _ic_stats(_daily_ic(panel, col, neutralize, weak_dates))
        s = _ic_stats(_daily_ic(panel, col, neutralize, strong_dates))
        rows.append({
            "factor": name,
            "ic_weak": w["mean_ic"], "t_weak": w["t_stat"], "nd_weak": w["n_days"],
            "ic_strong": s["mean_ic"], "t_strong": s["t_stat"], "nd_strong": s["n_days"],
            "delta_weak_minus_strong": (w["mean_ic"] - s["mean_ic"])
            if pd.notna(w["mean_ic"]) and pd.notna(s["mean_ic"]) else np.nan,
        })
    return pd.DataFrame(rows)


# ── (3) 前瞻-regime 診斷 IC（大盤未來跌 vs 漲）──────────────────────────
def forward_regime_ic(panel: pd.DataFrame, reg: pd.DataFrame,
                      neutralize: bool = True) -> pd.DataFrame:
    down_dates = set(reg.loc[reg["mkt_fwd20"] < 0, "date"])
    up_dates = set(reg.loc[reg["mkt_fwd20"] >= 0, "date"])
    rows = []
    for name, col in FACTORS.items():
        d = _ic_stats(_daily_ic(panel, col, neutralize, down_dates))
        u = _ic_stats(_daily_ic(panel, col, neutralize, up_dates))
        rows.append({
            "factor": name,
            "ic_mkt_down": d["mean_ic"], "t_mkt_down": d["t_stat"], "nd_down": d["n_days"],
            "ic_mkt_up": u["mean_ic"], "t_mkt_up": u["t_stat"], "nd_up": u["n_days"],
        })
    return pd.DataFrame(rows)


# ── (4) 投組層級比較 ────────────────────────────────────────────────────
WEIGHT_SETS = {
    "momentum_only":        {"momentum": 1.0},
    "mom80_rs20":           {"momentum": 0.80, "rs": 0.20},
    "mom80_downside20":     {"momentum": 0.80, "downside_resilience": 0.20},
    "mom80_downday20":      {"momentum": 0.80, "down_day_rs": 0.20},
    "mom70_downside30":     {"momentum": 0.70, "downside_resilience": 0.30},
    "mom60_ds20_dd20":      {"momentum": 0.60, "downside_resilience": 0.20, "down_day_rs": 0.20},
}


def _equity_metrics(eq: pd.DataFrame, crash_start, crash_end) -> dict:
    s = eq.set_index("date")["equity"]
    daily = s.pct_change().dropna()
    cum = float(s.iloc[-1] / s.iloc[0] - 1.0)
    ann = float(daily.mean() * 252)
    vol = float(daily.std(ddof=1) * np.sqrt(252))
    sharpe = ann / vol if vol > 0 else 0.0
    peak = s.cummax()
    mdd = float(((s - peak) / peak).min())
    calmar = ann / abs(mdd) if mdd < 0 else float("nan")
    # 股災窗口報酬
    seg = s[(s.index >= pd.to_datetime(crash_start)) & (s.index <= pd.to_datetime(crash_end))]
    crash_ret = float(seg.iloc[-1] / seg.iloc[0] - 1.0) if len(seg) >= 2 else float("nan")
    # 最痛 20 日滾動報酬
    worst20 = float(s.pct_change(20).min())
    return {"cum": cum, "ann": ann, "sharpe": sharpe, "mdd": mdd,
            "calmar": calmar, "crash_ret": crash_ret, "worst20": worst20}


def portfolio_compare(symbols, rebalance, pick, crash_start, crash_end,
                      split) -> pd.DataFrame:
    orig = copy.deepcopy(config.FACTOR_WEIGHTS)
    rows = []
    for label, w in WEIGHT_SETS.items():
        config.FACTOR_WEIGHTS = copy.deepcopy(w)
        for segment, (start, end) in {"IS": split.is_window,
                                      "OS": split.os_window}.items():
            res = backtest.backtest_portfolio(
                symbols=symbols, sample=False, start_date=start, end_date=end,
                rebalance_every=rebalance, top_n=pick,
                static_universe_comparator=True,   # research-only:非 PIT 候選池
            )
            if "equity_curve" not in res:
                rows.append({"weights": label, "segment": segment,
                             "error": res.get("error", "?")})
                continue
            m = _equity_metrics(res["equity_curve"], crash_start, crash_end)
            s = res.get("summary", {})
            rows.append({"weights": label, "segment": segment,
                         "n_trades": s.get("n_trades"),
                         "win_rate": s.get("win_rate"), **m})
    config.FACTOR_WEIGHTS = orig
    return pd.DataFrame(rows)


# ── 輸出 ────────────────────────────────────────────────────────────────
def _f(x, p=4):
    return f"{x:+.{p}f}" if (x is not None and pd.notna(x)) else "   n/a"


def main():
    argv = sys.argv[1:]
    rebuild = "--rebuild" in argv
    pool, rebalance, pick = 100, 5, 5

    panel = get_panel(rebuild, pool)
    reg = market_regime()
    symbols = uni.get_research_candidates(universe_top_n=pool)

    # 大盤股災窗口（TAIEX 2025 關稅股災：谷底 2025-04-09、-28.7%）
    crash_start, crash_end = "2025-02-01", "2025-06-30"

    print(f"\n資料快照 {config.SNAPSHOT_END_DATE}｜top{pool}｜"
          f"全期 {str(panel['date'].min())[:10]} ~ {str(panel['date'].max())[:10]}")
    for rc in ["weak_ma20", "weak_ma60", "weak_dd5", "weak_ret20"]:
        share = reg[rc].mean()
        print(f"  regime {rc}: 弱市日佔比 {share:.0%}")

    # (2) regime-條件 IC（用 weak_ma20 為主，另存 weak_dd5 / weak_ret20 對照）
    print("\n" + "=" * 92)
    print("  (2) regime-條件 IC（產業中性；訊號日大盤 regime 分組；可交易）")
    print("=" * 92)
    all_reg = {}
    for rc in ["weak_ma20", "weak_ma60", "weak_dd5", "weak_ret20"]:
        df = regime_conditional_ic(panel, reg, rc, neutralize=True)
        all_reg[rc] = df
        print(f"\n  ── regime = {rc} ──")
        print(f"  {'因子':<20}{'IC弱市':>10}{'t弱':>7}{'天數':>6}{'IC強市':>10}{'t強':>7}{'天數':>6}{'Δ(弱-強)':>10}")
        for _, r in df.iterrows():
            print(f"  {r['factor']:<20}{_f(r['ic_weak']):>10}{_f(r['t_weak'],2):>7}"
                  f"{int(r['nd_weak']):>6}{_f(r['ic_strong']):>10}{_f(r['t_strong'],2):>7}"
                  f"{int(r['nd_strong']):>6}{_f(r['delta_weak_minus_strong']):>10}")

    # (3) 前瞻-regime 診斷 IC
    print("\n" + "=" * 92)
    print("  (3) 前瞻-regime 診斷 IC（大盤未來20日 跌 vs 漲；診斷抗跌前提，非可交易訊號）")
    print("=" * 92)
    fr = forward_regime_ic(panel, reg, neutralize=True)
    print(f"  {'因子':<20}{'IC(大盤未來跌)':>16}{'t':>7}{'天數':>6}{'IC(大盤未來漲)':>16}{'t':>7}{'天數':>6}")
    for _, r in fr.iterrows():
        print(f"  {r['factor']:<20}{_f(r['ic_mkt_down']):>16}{_f(r['t_mkt_down'],2):>7}"
              f"{int(r['nd_down']):>6}{_f(r['ic_mkt_up']):>16}{_f(r['t_mkt_up'],2):>7}{int(r['nd_up']):>6}")

    # (4) 投組層級比較
    print("\n" + "=" * 92)
    split = evaluation_split.build_evaluation_split(panel["date"])
    print(f"  (4) 投組比較：純動能 vs 動能+RS/抗跌（IS/OS + 股災窗口 {crash_start}~{crash_end}）")
    print("=" * 92)
    pc = portfolio_compare(symbols, rebalance, pick, crash_start, crash_end, split)
    print(f"  {'權重組':<20}{'段':<4}{'筆數':>5}{'年化':>9}{'Sharpe':>8}{'MaxDD':>9}{'Calmar':>8}{'股災報酬':>10}{'最痛20日':>10}")
    for _, r in pc.iterrows():
        if "error" in r and isinstance(r.get("error"), str) and pd.notna(r.get("error")):
            print(f"  {r['weights']:<20}{r['segment']:<4} ERROR {r['error']}"); continue
        print(f"  {r['weights']:<20}{r['segment']:<4}{int(r['n_trades']) if pd.notna(r['n_trades']) else 0:>5}"
              f"{r['ann']:>+8.1%}{r['sharpe']:>8.2f}{r['mdd']:>+8.1%}"
              f"{(r['calmar'] if pd.notna(r['calmar']) else 0):>8.2f}"
              f"{r['crash_ret']:>+9.1%}{r['worst20']:>+9.1%}")
    print("=" * 92)

    # 存 CSV
    for rc, df in all_reg.items():
        df.to_csv(config.OUTPUT_DIR / f"defensive_rs_regime_ic_{rc}.csv", index=False, encoding="utf-8-sig")
    fr.to_csv(config.OUTPUT_DIR / "defensive_rs_fwd_regime_ic.csv", index=False, encoding="utf-8-sig")
    pc.to_csv(config.OUTPUT_DIR / "defensive_rs_portfolio_compare.csv", index=False, encoding="utf-8-sig")
    print("\n[def-rs] 已存 outputs/defensive_rs_*.csv")


if __name__ == "__main__":
    main()
