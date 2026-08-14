# -*- coding: utf-8 -*-
"""
因子批量掃描（factor_scan）— 用 operators 組合候選因子,篩台股有 edge 的
====================================================================
把 operators.py 的算子用起來:對一組**候選因子表達式**(含 z-score / rank /
產業中性化 / 迴歸中性化 / 時序變換)批量算橫斷面 IC(對未來 BT_IC_HORIZON 日報酬),
用**重疊校正的 t 值**判顯著性,並看分層單調性、IS/OS 穩定性、因子間相關(冗餘)。

這是**因子『發掘/生成』層,不是驗證層**:IC 顯著只代表值得進一步做嚴格回測 +
freeze/forward;不可據此直接上線(見 RESEARCH_OPERATING_PROTOCOL §4 證據等級)。

防過擬合:
  - 全期 + IS/OS 分開看(只看全期會被普漲騙);
  - 重疊視窗自相關 → Newey-West 式有效樣本 n_eff=n_days/h,t 值保守;
  - 報因子間相關矩陣,高相關(>0.8)= 冗餘,別當獨立發現;
  - 掃了幾個因子就有多重檢定,|t|>2 之外另標 Bonferroni 臨界。

限制:未還原價下需 SWING_ALLOW_UNADJUSTED=1,IC 為方向性上界;候選池倖存者未消。

用法:SWING_ALLOW_UNADJUSTED=1 .venv/bin/python factor_scan.py --pool 300
      SWING_SNAPSHOT_END=2026-07-24 SWING_ALLOW_UNADJUSTED=1 .venv/bin/python factor_scan.py --pool 121
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import config
import backtest
import evaluation_split
import universe as uni
import operators as op


# ── 候選因子庫:name -> f(panel, ops, ind) -> Series(當日橫斷面 alpha)──────
# 全部用 operators 組合,示範算子的用法;raw 欄位來自 _prepare_panel(因果計算)。
def build_factor_library():
    lib = {}

    # 原始(當基準對照)
    lib["mom_ret_raw"] = lambda p, o, ind: p["mom_ret"]
    lib["inst_6d_raw"] = lambda p, o, ind: p["inst_6d"]
    lib["near_high_raw"] = lambda p, o, ind: p["near_high"]
    lib["rs_excess_raw"] = lambda p, o, ind: p["rs_excess"]

    # 橫斷面正規化(z-score / rank)——同一資訊不同標準化
    lib["mom_cs_zscore"] = lambda p, o, ind: o.cs_zscore(p["mom_ret"])
    lib["mom_cs_rank"] = lambda p, o, ind: o.cs_rank(p["mom_ret"])
    lib["inst_cs_zscore"] = lambda p, o, ind: o.cs_zscore(p["inst_6d"])

    # 產業中性化(剝掉族群 beta,留個股相對族群的超額)
    lib["mom_ind_neutral"] = lambda p, o, ind: o.cs_zscore(o.group_neutralize(p["mom_ret"], ind))
    lib["inst_ind_neutral"] = lambda p, o, ind: o.cs_zscore(o.group_neutralize(p["inst_6d"], ind))

    # 迴歸中性化:動能對『規模代理(近20日均量)』與『rs』迴歸後的殘差
    lib["mom_neut_size"] = lambda p, o, ind: o.regression_neut(
        o.cs_zscore(p["mom_ret"]), o.cs_zscore(np.log1p(p["avg_vol_lots"].clip(lower=0))))
    lib["mom_neut_rs"] = lambda p, o, ind: o.regression_neut(
        o.cs_zscore(p["mom_ret"]), o.cs_zscore(p["rs_excess"].fillna(0)))

    # 多因子中性化:動能對 [rs, 規模, 量比] 一起迴歸後殘差
    lib["mom_multineut"] = lambda p, o, ind: o.multi_regression(
        o.cs_zscore(p["mom_ret"]),
        [o.cs_zscore(p["rs_excess"].fillna(0)),
         o.cs_zscore(np.log1p(p["avg_vol_lots"].clip(lower=0))),
         o.cs_zscore(p["vol_ratio"].fillna(1.0))])

    # 非線性:signed_power 壓尾保方向、s_log_1p 壓量級
    lib["mom_signed_pow"] = lambda p, o, ind: o.cs_zscore(op.signed_power(p["mom_ret"], 0.5))
    lib["inst_slog"] = lambda p, o, ind: o.cs_zscore(op.s_log_1p(p["inst_6d"]))

    # 時序:個股層 20 日動能 z-score(近期加速)、突破位階
    lib["mom_ts_zscore20"] = lambda p, o, ind: o.cs_zscore(o.ts_zscore(p["close"], 20))
    lib["breakout_pos"] = lambda p, o, ind: o.cs_rank(p["close"] / p["roll_high"])

    # 組合:動能 + 法人(等權 z-score 相加)
    lib["mom_plus_inst"] = lambda p, o, ind: o.cs_zscore(
        o.cs_zscore(p["mom_ret"]) + o.cs_zscore(p["inst_6d"]))
    # 品質動能:動能 × (量縮健康度) —— 用 rank 相乘避免尺度問題
    lib["mom_x_dryup"] = lambda p, o, ind: o.cs_rank(
        o.cs_rank(p["mom_ret"]) * (1 - o.cs_rank(p["vol_ratio"].fillna(1.0))))
    return lib


# ── 單因子橫斷面 IC(全期 + 指定日期子集)──────────────────────────────────
def _daily_ic(panel: pd.DataFrame, fac: pd.Series, dates=None) -> np.ndarray:
    df = pd.DataFrame({"f": fac, "fwd": panel["fwd_ret"], "date": panel["date"]}).dropna(subset=["f", "fwd"])
    if dates is not None:
        df = df[df["date"].isin(dates)]
    ics = []
    for _, g in df.groupby("date"):
        if len(g) >= 5 and g["f"].nunique() >= 2:
            ic = g["f"].corr(g["fwd"], method="spearman")
            if pd.notna(ic):
                ics.append(ic)
    return np.array(ics)


def _ic_stats(ics: np.ndarray, h: int) -> dict:
    if len(ics) < 2:
        return {"mean_ic": np.nan, "ic_ir": np.nan, "t_stat": np.nan, "n_days": len(ics)}
    m = float(ics.mean()); sd = float(ics.std(ddof=1))
    ir = m / sd if sd > 0 else np.nan
    n_eff = max(1.0, len(ics) / h)                     # 重疊校正
    t = ir * np.sqrt(n_eff) if pd.notna(ir) else np.nan
    return {"mean_ic": round(m, 4), "ic_ir": round(ir, 3) if pd.notna(ir) else np.nan,
            "t_stat": round(t, 2) if pd.notna(t) else np.nan, "n_days": len(ics)}


def _quintile_spread(panel: pd.DataFrame, fac: pd.Series) -> float:
    """Q5-Q1 未來報酬價差(當日分 5 層,取每日各層均值再平均)。"""
    df = pd.DataFrame({"f": fac, "fwd": panel["fwd_ret"], "date": panel["date"]}).dropna(subset=["f", "fwd"])
    spreads = []
    for _, g in df.groupby("date"):
        if len(g) < 10 or g["f"].nunique() < 5:
            continue
        q = pd.qcut(g["f"].rank(method="first"), 5, labels=False)
        top = g["fwd"][q == 4].mean(); bot = g["fwd"][q == 0].mean()
        if pd.notna(top) and pd.notna(bot):
            spreads.append(top - bot)
    return float(np.mean(spreads)) if spreads else np.nan


def run(pool=300):
    symbols = uni.get_research_candidates(candidate_pool_n=pool)
    print(f"[scan] 候選池 {len(symbols)} 檔｜snapshot {config.SNAPSHOT_END_DATE}｜建 panel …")
    # build_research_panel 預設稠密:保留全 panel(連續個股序列)→ ts_ 算子不在稀疏
    # 成員日 rolling(避免『20日窗其實跨非連續成員日』的失真);IC 再過濾到
    # in_dynamic_universe 成員。
    # research-only:候選池是單一日期排名(非 PIT)→ 顯式 static comparator。
    panel = backtest.build_research_panel(symbols,
                                          dynamic_enabled=True,
                                          static_universe_comparator=True)
    if panel.empty:
        print("[scan] panel 為空。"); return
    panel = panel.dropna(subset=["fwd_ret"]).reset_index(drop=True)
    ind = panel["stock_id"].map(uni.get_industry_map()).fillna("(未知)")
    ops = op.PanelOps(panel["date"], panel["stock_id"])    # 綁全 panel(連續)
    h = max(1, config.BT_IC_HORIZON)

    # IC/分層只在動態池成員上算(算子已在連續序列上算好,這裡只挑成員列評估)
    member_idx = (panel.index[panel["in_dynamic_universe"].fillna(False)]
                  if "in_dynamic_universe" in panel else panel.index)
    pmem = panel.loc[member_idx]

    # IS/OS 切分(時間;用成員列的日期)
    dates = np.sort(pmem["date"].unique())
    split = evaluation_split.build_evaluation_split(
        dates, minimum_embargo_days=config.BT_IC_HORIZON
    )
    cut, os_start = split.is_end, split.os_start
    is_dates = set(dates[dates <= cut]); os_dates = set(dates[dates >= os_start])

    lib = build_factor_library()
    rows = {}
    fac_series = {}
    for name, fn in lib.items():
        try:
            fac = fn(panel, ops, ind).astype(float)   # 在連續全 panel 上算(含 ts_)
        except Exception as e:
            print(f"[scan] {name} 失敗:{type(e).__name__}: {e}"); continue
        facm = fac.loc[member_idx]                     # 評估只取動態池成員列
        fac_series[name] = facm
        full = _ic_stats(_daily_ic(pmem, facm), h)
        isc = _ic_stats(_daily_ic(pmem, facm, is_dates), h)
        osc = _ic_stats(_daily_ic(pmem, facm, os_dates), h)
        rows[name] = {
            "factor": name,
            "ic": full["mean_ic"], "t": full["t_stat"], "n": full["n_days"],
            "ic_is": isc["mean_ic"], "t_is": isc["t_stat"],
            "ic_os": osc["mean_ic"], "t_os": osc["t_stat"],
            "q5q1": round(_quintile_spread(pmem, facm), 4),
        }

    df = pd.DataFrame(rows.values())
    df["absic"] = df["ic"].abs()
    df = df.sort_values("absic", ascending=False).drop(columns="absic").reset_index(drop=True)

    # 多重檢定臨界(Bonferroni,雙尾 α=0.05):α'=0.05/k 對應的 |t| 臨界
    k = len(df)
    bonf_t = round(_z_two_sided(0.05 / max(k, 1)), 2)

    _report(df, fac_series, panel, pool, bonf_t)
    return df


def _z_two_sided(alpha):
    # 反查雙尾臨界 z(不依賴 scipy;二分法逼近標準常態 ppf)
    from math import erf, sqrt
    target = 1 - alpha / 2
    lo, hi = 0.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        cdf = 0.5 * (1 + erf(mid / sqrt(2)))
        if cdf < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _report(df, fac_series, panel, pool, bonf_t):
    print("\n" + "=" * 96)
    print(f"  因子掃描（top{pool}｜snapshot {config.SNAPSHOT_END_DATE}｜IC vs 未來{config.BT_IC_HORIZON}日）")
    print("  ⚠ 未還原價方向性上界;IC 顯著=值得嚴格回測,非可上線")
    print("=" * 96)
    print(f"  {'因子':<20}{'IC':>8}{'t':>7}{'IC_IS':>8}{'t_IS':>7}{'IC_OS':>8}{'t_OS':>7}{'Q5-Q1':>9}  判讀")
    print("  " + "-" * 92)
    for _, r in df.iterrows():
        sig_full = pd.notna(r["t"]) and abs(r["t"]) > 2
        sig_os = pd.notna(r["t_os"]) and abs(r["t_os"]) > 2
        same_sign = pd.notna(r["ic_is"]) and pd.notna(r["ic_os"]) and np.sign(r["ic_is"]) == np.sign(r["ic_os"])
        if sig_full and sig_os and same_sign:
            v = "★ IS/OS 同向且顯著"
        elif sig_full and abs(r["t"]) > bonf_t:
            v = "✓ 過多重檢定"
        elif sig_full:
            v = "△ 全期顯著(未過MHT/OS)"
        else:
            v = "—"
        def s(x, p=True): return (f"{x:+.4f}" if p else f"{x:+.2f}") if pd.notna(x) else "  n/a"
        print(f"  {r['factor']:<20}{s(r['ic'])}{s(r['t'],0):>7}{s(r['ic_is'])}{s(r['t_is'],0):>7}"
              f"{s(r['ic_os'])}{s(r['t_os'],0):>7}{s(r['q5q1']):>9}  {v}")
    print("=" * 96)
    print(f"  |t|>2 才算顯著;多重檢定 Bonferroni 臨界 |t|>{bonf_t}(掃了 {len(df)} 個因子)。")

    # 因子相關矩陣(每日 IC 序列相關 → 冗餘偵測),取前 8 強
    top = df.head(8)["factor"].tolist()
    print("\n  前 8 強因子的『值』橫斷面相關(>0.8=冗餘,別當獨立發現):")
    M = pd.DataFrame({n: fac_series[n] for n in top}).corr(method="spearman")
    print(M.round(2).to_string())

    # 存檔
    df.to_csv(config.OUTPUT_DIR / "factor_scan.csv", index=False, encoding="utf-8-sig")
    lines = [
        "# 因子掃描報告（operators 批量組合）",
        "",
        f"> snapshot `{config.SNAPSHOT_END_DATE}`｜候選池 top{pool}｜IC vs 未來 {config.BT_IC_HORIZON} 日｜"
        f"重疊校正 t(n_eff=天數/{config.BT_IC_HORIZON})。",
        "> ⚠ 未還原價方向性上界;**這是因子發掘層,IC 顯著≠可上線**,需嚴格回測 + freeze/forward。",
        "",
        "## 因子 IC（全期 + IS/OS）",
        "",
        "| 因子 | IC | t | IC_IS | t_IS | IC_OS | t_OS | Q5-Q1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, r in df.iterrows():
        def m(x): return f"{x:+.4f}" if pd.notna(x) else "n/a"
        def mt(x): return f"{x:+.2f}" if pd.notna(x) else "n/a"
        lines.append(f"| {r['factor']} | {m(r['ic'])} | {mt(r['t'])} | {m(r['ic_is'])} | {mt(r['t_is'])} "
                     f"| {m(r['ic_os'])} | {mt(r['t_os'])} | {m(r['q5q1'])} |")
    lines += [
        "",
        f"> 判讀:★=IS/OS 同向且 |t|>2(最強);✓=過 Bonferroni |t|>{bonf_t};△=全期顯著但未過多重檢定或 OS。",
        "> Q5-Q1 = 最高分組減最低分組的未來報酬(正且大且單調才好)。",
        "",
        "## 防過擬合保留",
        "1. 未還原價 → IC 是方向性上界,絕對值待還原價重算。",
        f"2. 掃了 {len(df)} 個因子,多重檢定下期望假陽性 ≈ {len(df)*0.05:.1f} 個;只信過 Bonferroni + OS 同向者。",
        "3. 候選池倖存者未消(current top-N);單一多頭窗。",
        "4. 這是發掘層;任何『★』因子要進 validate_oos 嚴格回測 + freeze_manifest/forward_test 才算數。",
    ]
    (config.OUTPUT_DIR / "FACTOR_SCAN_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n[scan] 已存 outputs/FACTOR_SCAN_REPORT.md + factor_scan.csv")


def _parse(argv):
    pool = 300
    for i, a in enumerate(argv):
        if a == "--pool" and i + 1 < len(argv):
            pool = int(argv[i + 1])
    return pool


if __name__ == "__main__":
    run(_parse(sys.argv[1:]))
