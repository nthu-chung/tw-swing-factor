# -*- coding: utf-8 -*-
"""
Regime 策略比較台（regime_strategy_lab）
========================================
針對「當前大盤 regime」比較多條選股策略,全部走 backtest.backtest_portfolio 的
**同一執行/成本模型**(T+1 開盤進場、trend 退場、手續費+證交稅、逐日淨值),
只差在 picks_by_date 的產生邏輯,確保公平比較。

比較的五條(全部扣在專案已驗證發現上,不亂發明):
  S0 momentum        純動能基線(trend_ok,依 mom_ret 排序)——專案上線 baseline。
  S1 momentum+throttle 同 S0,但開 config.MARKET_FILTER(vol 規則)在高波動時降曝險。
  S2 sector_leader   族群輪動領漲(重用 sector_rotation.build_picks;唯一 OS 過的線)。
  S3 break_retest    創高後拉回不破、趨勢未壞再進(near_high 0.88~0.97 & >MA60↑ & mom>0)。
  S4 buyweak_rs      嚴格買弱:回檔破 MA20 但守 MA60↑ 且相對大盤仍強(rs_excess>0)。

Regime-fit 指標:用 TAIEX 標記「回檔日(收<MA20)」與「高波動日(20d 年化波動>門檻)」,
比較各策略在這些子期間的表現(當前 regime = 高波動回檔,誰在這種日子最抗跌 = 最適合現在)。

限制(誠實):未還原價回測會被 fail-closed 擋,需 SWING_ALLOW_UNADJUSTED=1,結果標
integrity_bypassed、僅供**相對排名/方向**,絕對值不可信;僅單一多頭窗+一次股災。
最終要升級結論須 freeze_manifest + forward_test。

用法:SWING_ALLOW_UNADJUSTED=1 SWING_SNAPSHOT_END=2026-07-24 \
      .venv/bin/python regime_strategy_lab.py --pool 300 --pick 5 --reb 5 --vol-th 0.25
"""
from __future__ import annotations

import sys
import copy

import numpy as np
import pandas as pd

import config
import data
import backtest
import evaluation_split
import universe as uni
import sector_rotation as sr


# ── Regime 標記(TAIEX) ─────────────────────────────────────────────────
def market_regime(vol_th: float = 0.25) -> pd.DataFrame:
    m = data.fetch_market_index()
    if m is None or m.empty:
        return pd.DataFrame()
    m = m.sort_values("date").reset_index(drop=True)
    c = m["close"]
    ma20 = c.rolling(20).mean()
    vol20 = c.pct_change().rolling(20).std() * np.sqrt(252)
    out = pd.DataFrame({
        "date": m["date"],
        "pullback": (c < ma20),                         # 收盤跌破 MA20 = 回檔
        "highvol": (vol20 > vol_th),                    # 高波動
    })
    out["stress"] = out["pullback"] | out["highvol"]    # 現在這種 regime
    return out


# ── 從 panel 建各策略 picks_by_date ─────────────────────────────────────
def _picks_from_mask(panel: pd.DataFrame, mask: pd.Series, score_col: str) -> dict:
    sub = panel[mask].dropna(subset=[score_col])
    picks = {}
    for d, g in sub.groupby("date"):
        g = g.sort_values(score_col, ascending=False)
        picks[d] = list(zip(g["stock_id"], g[score_col], g["name"]))
    return picks


def build_all_picks(panel: pd.DataFrame) -> dict:
    p = panel
    strategies = {}

    # S0 momentum:趨勢 OK,依 60 日動能排序
    strategies["S0_momentum"] = _picks_from_mask(
        p, p["trend_ok"] == True, "mom_ret")            # noqa: E712

    # S3 break_retest:創高後拉回 3~12%(near_high 0.88~0.97)、守 MA60 且 MA60 上揚、動能仍正
    m3 = (
        (p["near_high"] >= 0.88) & (p["near_high"] <= 0.97)
        & (p["close"] > p["ma_long"]) & (p["ma_long_slope"] > 0)
        & (p["mom_ret"] > 0)
    )
    strategies["S3_break_retest"] = _picks_from_mask(p, m3, "mom_ret")

    # S4 buyweak_rs:回檔破 MA20 但守 MA60↑、相對大盤仍強(rs_excess>0);依 RS 排序
    m4 = (
        (p["close"] < p["ma_short"]) & (p["close"] > p["ma_long"])
        & (p["ma_long_slope"] > 0) & (p["rs_excess"] > 0)
    )
    strategies["S4_buyweak_rs"] = _picks_from_mask(p, m4, "rs_excess")

    # S2 sector_leader:重用 sector_rotation 的 hot-sector + leader 邏輯
    long = p[["date", "stock_id", "name", "close", "ma_short", "mom_ret",
              "inst_6d", "trend_ok"]].copy()
    long = long.rename(columns={"stock_id": "sid"})
    long["industry"] = long["sid"].map(uni.get_industry_map()).fillna("(未知)")
    long["above_ma20"] = (long["close"] > long["ma_short"]).astype(float)
    try:
        strategies["S2_sector_leader"] = sr.build_picks(long, "leader", top_k=3)
    except Exception as e:
        print(f"[lab] S2 sector 建 picks 失敗:{type(e).__name__}: {e}")
        strategies["S2_sector_leader"] = {}
    return strategies


# ── 指標 ────────────────────────────────────────────────────────────────
def _daily(eq: pd.DataFrame) -> pd.Series:
    s = eq.set_index("date")["equity"]
    return s.pct_change().dropna()


def _metrics(eq: pd.DataFrame, regime: pd.DataFrame) -> dict:
    s = eq.set_index("date")["equity"]
    dr = s.pct_change().dropna()
    if len(dr) < 3:
        return {}
    ann = float(dr.mean() * 252); vol = float(dr.std(ddof=1) * np.sqrt(252))
    sharpe = ann / vol if vol > 0 else 0.0
    peak = s.cummax(); mdd = float(((s - peak) / peak).min())
    out = {
        "n_days": len(dr), "cum": float(s.iloc[-1] / s.iloc[0] - 1),
        "ann": ann, "sharpe": sharpe, "mdd": mdd,
        "calmar": (ann / abs(mdd) if mdd < 0 else float("nan")),
    }
    # regime-fit:壓力日(回檔/高波動)的平均日報酬 vs 平順日
    if not regime.empty:
        rr = dr.reset_index().merge(regime, on="date", how="left")
        stress = rr[rr["stress"] == True]["equity"]          # noqa: E712
        calm = rr[rr["stress"] != True]["equity"]            # noqa: E712
        out["stress_days"] = int(len(stress))
        out["ret_stress_bp"] = float(stress.mean() * 1e4) if len(stress) else float("nan")
        out["ret_calm_bp"] = float(calm.mean() * 1e4) if len(calm) else float("nan")
    return out


def _run(symbols, picks, filt=False, reb=5, pick=5, start=None, end=None):
    filt_orig = getattr(config, "MARKET_FILTER_ENABLED", False)
    if filt:
        config.MARKET_FILTER_ENABLED = True
        config.MARKET_FILTER_RULE = "vol"
    try:
        return backtest.backtest_portfolio(
            symbols=symbols, sample=False, start_date=start, end_date=end,
            rebalance_every=reb, top_n=pick,
            dynamic_enabled=True, picks_by_date=picks)
    finally:
        config.MARKET_FILTER_ENABLED = filt_orig


def run(pool=300, pick=5, reb=5, vol_th=0.25):
    symbols = uni.get_research_candidates(universe_top_n=config.DYNAMIC_UNIVERSE_TOP_N,
                                          candidate_pool_n=pool)
    print(f"[lab] 候選池 {len(symbols)} 檔｜snapshot {config.SNAPSHOT_END_DATE}｜建 panel …")
    panel = backtest._prepare_panel(symbols, 0.0, None, None,
                                    dynamic_enabled=True,
                                    universe_top_n=config.DYNAMIC_UNIVERSE_TOP_N)
    if panel.empty:
        print("[lab] panel 為空,結束。"); return
    print(f"[lab] panel {len(panel)} 列、{panel['date'].nunique()} 交易日、"
          f"{panel['stock_id'].nunique()} 檔成員")

    regime = market_regime(vol_th)
    split = evaluation_split.build_evaluation_split(panel["date"])
    picks = build_all_picks(panel)
    for k, v in picks.items():
        print(f"[lab] {k}: {len(v)} 個有訊號日")

    # 五個配置:S0/S2/S3/S4 用各自 picks;S1 = S0 picks + vol throttle
    configs = [
        ("S0_momentum", picks["S0_momentum"], False),
        ("S1_momentum_volthrottle", picks["S0_momentum"], True),
        ("S2_sector_leader", picks.get("S2_sector_leader", {}), False),
        ("S3_break_retest", picks["S3_break_retest"], False),
        ("S4_buyweak_rs", picks["S4_buyweak_rs"], False),
    ]

    rows, eqs = [], {}
    for label, pk, filt in configs:
        for segment, (start, end) in {"IS": split.is_window,
                                      "OS": split.os_window}.items():
            if not pk:
                rows.append({"strategy": label, "segment": segment,
                             "error": "無訊號"}); continue
            res = _run(symbols, pk, filt=filt, reb=reb, pick=pick,
                       start=start, end=end)
            if "equity_curve" not in res:
                rows.append({"strategy": label, "segment": segment,
                             "error": res.get("error", "?")}); continue
            s = res["summary"]; m = _metrics(res["equity_curve"], regime)
            eqs[f"{label}:{segment}"] = res["equity_curve"]
            rows.append({
                "strategy": label, "segment": segment, "n_trades": s["n_trades"],
                "win_rate": s["win_rate"], "bypass": s["data"].get("integrity_bypassed"),
                **m,
            })

    df = pd.DataFrame(rows)
    _report(df, eqs, pool, pick, reb, vol_th)
    return df


def _f(x, pct=True):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "n/a"
    return f"{x:+.1%}" if pct else f"{x:.2f}"


def _report(df, eqs, pool, pick, reb, vol_th):
    print("\n" + "=" * 104)
    print(f"  Regime 策略比較（top{pool}｜reb{reb}/pick{pick}｜vol_th {vol_th:.0%}｜"
          f"snapshot {config.SNAPSHOT_END_DATE}）")
    print("  ⚠ 未還原價 upper-bound(integrity_bypassed)、僅相對排名可參考")
    print("=" * 104)
    hdr = f"  {'策略':<26}{'段':<4}{'筆':>4}{'勝率':>7}{'年化':>9}{'Sharpe':>8}{'MaxDD':>9}{'Calmar':>8}{'壓力日bp':>9}{'平順日bp':>9}"
    print(hdr)
    print("  " + "-" * 100)
    for _, r in df.iterrows():
        if "error" in r and isinstance(r.get("error"), str) and pd.notna(r.get("error")):
            print(f"  {r['strategy']:<26}{r['segment']:<4} ERROR {r['error']}"); continue
        print(f"  {r['strategy']:<26}{r['segment']:<4}{int(r['n_trades']):>4}{r['win_rate']:>7.0%}"
              f"{_f(r['ann']):>9}{_f(r['sharpe'],0):>8}{_f(r['mdd']):>9}{_f(r['calmar'],0):>8}"
              f"{r.get('ret_stress_bp',float('nan')):>9.1f}{r.get('ret_calm_bp',float('nan')):>9.1f}")
    print("=" * 104)
    print("  壓力日bp = 回檔/高波動日的平均日報酬(基點);越高=越適合現在的高波動回檔 regime。")

    # 存 md
    lines = [
        "# Regime 策略比較報告",
        "",
        f"> snapshot `{config.SNAPSHOT_END_DATE}`｜top{pool}｜reb{reb}/pick{pick}｜vol_th {vol_th:.0%}｜"
        "同一 backtest 引擎(T+1開盤/trend退場/含成本)。",
        "> ⚠ **未還原價 upper-bound**(integrity_bypassed=True):僅供**相對排名/方向**,"
        "絕對 Sharpe/報酬不可信;僅單一多頭窗。升級結論須 freeze_manifest + forward_test。",
        "",
        "## 當前 regime(見 console / 手動判讀)",
        "長多(遠在 MA200 上)中的高波動短線回檔:MA20 翻下、20d 波動偏高。故重點看**壓力日報酬**。",
        "",
        "## 策略比較",
        "",
        "| 策略 | 段 | 筆數 | 勝率 | 年化 | Sharpe | MaxDD | Calmar | 壓力日bp | 平順日bp |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in df.iterrows():
        if "error" in r and isinstance(r.get("error"), str) and pd.notna(r.get("error")):
            lines.append(f"| {r['strategy']} | {r['segment']} | — | — | — | — | — | — | — | ERR |"); continue
        lines.append(f"| {r['strategy']} | {r['segment']} | {int(r['n_trades'])} | {r['win_rate']:.0%} | "
                     f"{_f(r['ann'])} | {_f(r['sharpe'],0)} | {_f(r['mdd'])} | {_f(r['calmar'],0)} | "
                     f"{r.get('ret_stress_bp',float('nan')):.1f} | {r.get('ret_calm_bp',float('nan')):.1f} |")
    lines += [
        "",
        "## 判讀指引",
        "- **壓力日bp** 最高、**MaxDD** 最淺者 = 最適合當前高波動回檔 regime。",
        "- S1(vol throttle)若 MaxDD 明顯優於 S0 而年化沒崩太多 → 節流在回檔有價值。",
        "- S3/S4 是「回檔中進場」的兩種假設;若壓力日 bp 仍為負,代表台股回檔中接刀依舊危險(符合專案先驗)。",
        "- 這是**相對**比較;任何絕對數字待還原價 + forward 重驗。",
    ]
    (config.OUTPUT_DIR / "REGIME_STRATEGY_LAB_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    df.to_csv(config.OUTPUT_DIR / "regime_strategy_lab.csv", index=False, encoding="utf-8-sig")
    print("[lab] 已存 outputs/REGIME_STRATEGY_LAB_REPORT.md + regime_strategy_lab.csv")


def _parse(argv):
    pool, pick, reb, vth = 300, 5, 5, 0.25
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--pool" and i + 1 < len(argv): pool = int(argv[i + 1]); i += 1
        elif a == "--pick" and i + 1 < len(argv): pick = int(argv[i + 1]); i += 1
        elif a == "--reb" and i + 1 < len(argv): reb = int(argv[i + 1]); i += 1
        elif a == "--vol-th" and i + 1 < len(argv): vth = float(argv[i + 1]); i += 1
        i += 1
    return pool, pick, reb, vth


if __name__ == "__main__":
    pool, pick, reb, vth = _parse(sys.argv[1:])
    run(pool, pick, reb, vth)
