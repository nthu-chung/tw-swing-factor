# -*- coding: utf-8 -*-
"""
階段三：樣本外（OOS）「時間穩健性」檢查（非乾淨 OOS）
====================================================
⚠ 重要：本專案沒有真正乾淨的 OOS。權重/exit/門檻都已反覆看過同一段 2024-2026 資料
（全期 IC 選權重、config 演化史、WEIGHT_SETS 十幾組同資料比較）。因此本腳本測的是
「固定權重在後段沒被特別擬合的時間還行不行」＝時間穩健性，**不是**選股流程的純樣本外。
真正的乾淨 OOS 只能靠 freeze_manifest.py 凍結規則、forward_test.py 對凍結日之後的
新資料 forward-only 驗證（見那兩支）。

現行 baseline = config.FACTOR_WEIGHTS（momentum_only）；bootstrap 顯著性檢定對象
已對齊為 momentum_only，並強制與同期「等權買進持有」基準對照——OS 普漲時 CI 下界>0
只是 beta，不當作 edge。本腳本回答：

    這組權重的表現是時間穩健的，還是 in-sample 過擬合 / 只是普漲 beta？

做兩件事：
  (1) IS/OS 時間切分：前 70% 找/看績效（IS）、後 30% 純樣本外（OS），中間留
      embargo（= IC 視窗，避免訊號的未來報酬窗橫跨切點洩漏）。三組權重都跑，
      看 mom_quality 在 OS 是否仍贏 legacy_9、且 Sharpe/年化沒崩。
  (2) Block bootstrap：對 OS 的每日權益報酬做區塊重抽樣（保留自相關），算
      年化報酬 / Sharpe 的 90% 信賴區間。下界 > 0 才代表「不是運氣」。

誠實聲明（很重要，寫進報告）：
  權重是用「全期」IC 選的，所以嚴格說 OS 不是「選股流程的純樣本外」——它測的是
  「同一組固定權重，在它沒被特別擬合的後段時間，還行不行」＝時間穩健性。
  這仍是必要的過擬合防線，但不能宣稱是完全乾淨的 OOS。真正乾淨的 OOS 需「只用
  IS 資料重新跑 IC 選權重」，待資料更長時再做。

用法：
  .venv/bin/python validate_oos.py
  .venv/bin/python validate_oos.py --pool 100 --rebalance 5 --pick 5
輸出：outputs/OOS_VALIDATION_REPORT.md + outputs/oos_*.csv
"""
from __future__ import annotations

import sys
import copy

import numpy as np
import pandas as pd

import config
import backtest
import evaluation_split
import universe as uni


# ── 候選權重組 ──────────────────────────────────────────────────────────
# 2026-06-21 修權重輪：以 IS Sharpe 為篩選指標，找能擊敗 momentum_only(IS 1.40)
# 的組合。每個多因子組合都是「純動能 + 一個低相關補強因子」，每次只動一個東西,
# 才能歸因增量；多因子混雜（如 mom_quality）會讓「贏 / 輸是哪個因子的功勞」糊掉。
# key 必須是 factors.SCORE_COLUMNS 的鍵。
WEIGHT_SETS = {
    # ── 對照組（已知數字，用來驗證跑出來的結果跟先前一致）───────────
    "mom_quality(上線)": {  # 已知 IS Sharpe ≈ 0.41
        "momentum": 0.50, "ma_alignment": 0.20, "margin_health": 0.30,
    },
    "legacy_9(體檢前)": {   # 已知 IS Sharpe ≈ 0.24
        "momentum": 0.20, "inst_mid": 0.15, "inst_long": 0.15, "inst_dip_buy": 0.05,
        "margin_health": 0.05, "ma_alignment": 0.10, "bb_pullback": 0.10,
        "ma_squeeze": 0.10, "vol_dryup": 0.05,
    },
    "momentum_only": {"momentum": 1.0},  # 已知 IS Sharpe ≈ 1.40，本輪基準

    # ── 候選 A：mom + margin_health（margin 與 mom 較不共線）────────
    "mom80_margin20": {"momentum": 0.80, "margin_health": 0.20},
    "mom70_margin30": {"momentum": 0.70, "margin_health": 0.30},

    # ── 候選 B：mom + ma_alignment（共線 0.68，預期沒增量；當對照證偽）
    "mom80_maalign20": {"momentum": 0.80, "ma_alignment": 0.20},

    # ── 候選 C：mom + 法人（短中窗、且體檢 t≈1.99 接近顯著）─────────
    "mom80_instmid20":   {"momentum": 0.80, "inst_mid": 0.20},
    "mom80_instdip20":   {"momentum": 0.80, "inst_dip_buy": 0.20},

    # ── 候選 D：mom + 相對強勢 / 抗跌（弱市防禦研究，2026-07-21 加）──────
    #   假設：加 RS/抗跌能在弱市帶來增量 edge（降 MaxDD / 改善弱市段報酬）。
    #   體檢已知：rs 與 mom 共線 0.83（冗餘）；downside/down_day 全期 IC 反向。
    #   這裡用 IS/OS 檢驗它們在投組層級是否真的不拖累純動能。
    "mom80_rs20":        {"momentum": 0.80, "rs": 0.20},
    "mom80_downside20":  {"momentum": 0.80, "downside_resilience": 0.20},
    "mom80_downday20":   {"momentum": 0.80, "down_day_rs": 0.20},
    "mom70_downside30":  {"momentum": 0.70, "downside_resilience": 0.30},
}

# bootstrap 設定
N_BOOT = 2000
BLOCK = 10          # 區塊長度（交易日）——保留報酬序列自相關
CI = 0.90           # 信賴水準


def _metrics_from_equity(eq: pd.DataFrame) -> dict:
    """由每日權益曲線算績效（與 backtest.py 同口徑）。"""
    s = eq.set_index("date")["equity"] if "date" in eq.columns else eq["equity"]
    daily = s.pct_change().dropna()
    if len(daily) < 2:
        return {"cum_ret": np.nan, "ann_ret": np.nan, "sharpe": np.nan,
                "max_dd": np.nan, "n_days": len(daily)}
    cum = float(s.iloc[-1] / s.iloc[0] - 1.0)
    ann = float(daily.mean() * 252)
    vol = float(daily.std(ddof=1) * np.sqrt(252))
    sharpe = ann / vol if vol > 0 else 0.0
    peak = s.cummax()
    mdd = float(((s - peak) / peak).min())
    return {"cum_ret": cum, "ann_ret": ann, "sharpe": sharpe,
            "max_dd": mdd, "n_days": len(daily)}


def _run(symbols, weights, start, end, rebalance, pick, universe_top_n):
    config.FACTOR_WEIGHTS = copy.deepcopy(weights)
    # research-only:候選池是單一日期排名(非 PIT)→ 顯式 static comparator。
    # 這支腳本的 OOS 數字因此不是 PIT 正式證據(summary 會標
    # formal_evidence_eligible=False);正式路徑見 universes.historical_pit_universe。
    res = backtest.backtest_portfolio(symbols=symbols, sample=False,
                                      start_date=start, end_date=end,
                                      rebalance_every=rebalance, top_n=pick,
                                      dynamic_enabled=config.DYNAMIC_UNIVERSE_ENABLED,
                                      universe_top_n=universe_top_n,
                                      static_universe_comparator=True)
    return res


def _split_dates(symbols, rebalance, pick, universe_top_n):
    """先跑一次全期，取得所有交易日，算 IS/OS 切點與 embargo。"""
    res = _run(
        symbols, WEIGHT_SETS["mom_quality(上線)"], None, None,
        rebalance, pick, universe_top_n,
    )
    if "equity_curve" not in res:
        return None
    return evaluation_split.build_evaluation_split(
        res["equity_curve"]["date"]
    ).to_dict()


def _block_bootstrap_ci(eq: pd.DataFrame):
    """對每日報酬做 moving-block bootstrap，回傳年化報酬 / Sharpe 的 CI。"""
    s = eq.set_index("date")["equity"] if "date" in eq.columns else eq["equity"]
    r = s.pct_change().dropna().values
    n = len(r)
    if n < BLOCK * 3:
        return None
    n_blocks = int(np.ceil(n / BLOCK))
    # 用固定種子讓結果可復現（Date/random 在 workflow 受限，但這是一般 CLI，OK）
    rng = np.random.default_rng(20260620)
    starts_pool = np.arange(0, n - BLOCK + 1)
    ann_samples, sharpe_samples = [], []
    for _ in range(N_BOOT):
        picks = rng.choice(starts_pool, size=n_blocks, replace=True)
        seq = np.concatenate([r[st:st + BLOCK] for st in picks])[:n]
        mu, sd = seq.mean(), seq.std(ddof=1)
        ann_samples.append(mu * 252)
        sharpe_samples.append((mu * 252) / (sd * np.sqrt(252)) if sd > 0 else 0.0)
    lo, hi = (1 - CI) / 2 * 100, (1 + CI) / 2 * 100
    return {
        "ann_lo": float(np.percentile(ann_samples, lo)),
        "ann_hi": float(np.percentile(ann_samples, hi)),
        "ann_med": float(np.median(ann_samples)),
        "sharpe_lo": float(np.percentile(sharpe_samples, lo)),
        "sharpe_hi": float(np.percentile(sharpe_samples, hi)),
        "sharpe_med": float(np.median(sharpe_samples)),
        "p_ann_pos": float(np.mean(np.array(ann_samples) > 0)),
    }


def _buyhold_baseline(symbols, start, end):
    """OS 段 universe 等權買進持有基準——用來證明 OS 是不是普漲行情(beta)。"""
    import data
    rets = []
    for sid in symbols:
        p = data.fetch_price(sid)
        if p is None or p.empty:
            continue
        p = p.copy()
        p["date"] = pd.to_datetime(p["date"])
        seg = p[(p["date"] >= start) & (p["date"] <= end)].sort_values("date")
        if len(seg) < 2:
            continue
        c0, c1 = seg["close"].iloc[0], seg["close"].iloc[-1]
        if c0 > 0:
            rets.append(c1 / c0 - 1.0)
    if not rets:
        return None
    r = np.array(rets)
    return {"mean": float(r.mean()), "median": float(np.median(r)),
            "up_ratio": float((r > 0).mean()), "n": len(r)}


def run(pool, rebalance, pick):
    symbols = uni.get_research_candidates(universe_top_n=pool)
    print(f"[oos] universe top{pool} = {len(symbols)} 檔；rebalance={rebalance}日 / 持有{pick}檔")
    orig = copy.deepcopy(config.FACTOR_WEIGHTS)

    sp = _split_dates(symbols, rebalance, pick, pool)
    if sp is None:
        print("[oos] 無法取得交易日，結束。")
        return
    print(f"[oos] 全期 {sp['is_start']} ~ {sp['os_end']}（{sp['n_total']} 交易日）")
    print(f"[oos] IS: {sp['is_start']} ~ {sp['is_end']}  |  embargo  |  "
          f"OS: {sp['os_start']} ~ {sp['os_end']}")

    # (1) 三組權重 × (IS/OS) 回測
    rows = []
    os_equity_for_boot = None
    for label, w in WEIGHT_SETS.items():
        print(f"\n[oos] 跑 {label} …")
        for seg, (st, en) in {
            "IS": (sp["is_start"], sp["is_end"]),
            "OS": (sp["os_start"], sp["os_end"]),
        }.items():
            res = _run(symbols, w, st, en, rebalance, pick, pool)
            if "equity_curve" not in res:
                rows.append({"weights": label, "seg": seg, "error": res.get("error", "?")})
                continue
            m = _metrics_from_equity(res["equity_curve"])
            s = res.get("summary", {})
            rows.append({
                "weights": label, "seg": seg,
                "n_trades": s.get("n_trades"), "win_rate": s.get("win_rate"),
                **m,
            })
            # bootstrap 綁「現行 baseline momentum_only」（= config.FACTOR_WEIGHTS），
            # 不再綁已被否決的 mom_quality。顯著性檢定對象要與實際在用的權重一致。
            if label == "momentum_only" and seg == "OS":
                os_equity_for_boot = res["equity_curve"]

    config.FACTOR_WEIGHTS = orig  # 還原
    df = pd.DataFrame(rows)

    # (2) OS bootstrap（只對上線權重）
    boot = _block_bootstrap_ci(os_equity_for_boot) if os_equity_for_boot is not None else None

    # OS 買進持有基準（判斷 OS 是否普漲行情）
    bh = _buyhold_baseline(symbols, sp["os_start"], sp["os_end"])

    _print_and_save(df, boot, bh, sp, pool, rebalance, pick)


def _fmt(x, pct=True):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "  n/a"
    return f"{x:+.1%}" if pct else f"{x:.2f}"


def _print_and_save(df, boot, bh, sp, pool, rebalance, pick):
    print("\n" + "=" * 90)
    print("  (1) IS / OS 績效對比（三組權重；OS 才是檢驗真 edge 的關鍵）")
    print("=" * 90)
    print(f"  {'權重組':<20}{'段':<4}{'筆數':>5}{'勝率':>7}{'累積':>9}{'年化':>9}{'Sharpe':>8}{'MaxDD':>9}")
    print("  " + "-" * 86)
    for _, r in df.iterrows():
        if "error" in r and isinstance(r.get("error"), str):
            print(f"  {r['weights']:<20}{r['seg']:<4}  ERROR: {r['error']}")
            continue
        wr = r['win_rate']
        print(f"  {r['weights']:<20}{r['seg']:<4}{int(r['n_trades']) if pd.notna(r['n_trades']) else 0:>5}"
              f"{(wr if pd.notna(wr) else 0):>7.0%}{_fmt(r['cum_ret']):>9}{_fmt(r['ann_ret']):>9}"
              f"{_fmt(r['sharpe'],0):>8}{_fmt(r['max_dd']):>9}")
    print("=" * 90)

    if boot:
        print("\n" + "=" * 90)
        print(f"  (2) OS 期間 Block Bootstrap（n={N_BOOT}, block={BLOCK}日, {int(CI*100)}% CI）")
        print("=" * 90)
        print(f"  年化報酬：中位 {boot['ann_med']:+.1%}   "
              f"{int(CI*100)}% CI = [{boot['ann_lo']:+.1%}, {boot['ann_hi']:+.1%}]")
        print(f"  Sharpe ：中位 {boot['sharpe_med']:.2f}   "
              f"{int(CI*100)}% CI = [{boot['sharpe_lo']:.2f}, {boot['sharpe_hi']:.2f}]")
        if bh is not None:
            print(f"\n  ⚠️ OS 段 top{pool} 買進持有(等權)基準：平均 {bh['mean']:+.0%} / "
                  f"中位 {bh['median']:+.0%} / 上漲家數 {bh['up_ratio']:.0%}")
            print(f"  → bootstrap CI 下界 > 0 不代表 edge：OS 是普漲行情，beta 而非 alpha。")
            print(f"     真正能區分權重的是 IS 段（見上表 Sharpe 欄）。")
        print("=" * 90)

    # 存檔
    df.to_csv(config.OUTPUT_DIR / "oos_isos_compare.csv", index=False, encoding="utf-8-sig")

    # markdown 報告
    def mdrow(r):
        if "error" in r and isinstance(r.get("error"), str):
            return f"| {r['weights']} | {r['seg']} | — | — | — | — | — | ERR |"
        return (f"| {r['weights']} | {r['seg']} | {int(r['n_trades']) if pd.notna(r['n_trades']) else 0} "
                f"| {(r['win_rate'] if pd.notna(r['win_rate']) else 0):.0%} | {_fmt(r['cum_ret'])} "
                f"| {_fmt(r['ann_ret'])} | {_fmt(r['sharpe'],0)} | {_fmt(r['max_dd'])} |")

    lines = [
        "# 階段三：樣本外（OOS）驗證報告",
        "",
        "> ⚠️ **本檔由 `validate_oos.py` 每次跑都會自動覆寫**。下方候選裡的",
        "> 「mom_quality(上線)」是 WEIGHT_SETS dict key 的歷史殘留標籤,實際",
        "> 上線權重以 `config.FACTOR_WEIGHTS` 為準。最新決策見",
        "> [WEIGHT_FIX_REPORT.md](./WEIGHT_FIX_REPORT.md)。",
        ">",
        f"> 資料快照 {getattr(config, 'SNAPSHOT_END_DATE', 'now') or 'now'}｜"
        f"每日動態 universe top{pool}（候選 current top{config.DYNAMIC_UNIVERSE_CANDIDATE_POOL}）"
        f"｜rebalance {rebalance}日 / 持有{pick}檔｜trend 退場",
        "> ⚠ 候選池仍是 current-pool bootstrap，`survivorship_free=False`；"
        "這不是最終論文級 PIT OOS。",
        f"> 全期 {sp['is_start']} ~ {sp['os_end']}（{sp['n_total']} 交易日）",
        f"> **IS** {sp['is_start']} ~ {sp['is_end']}　|　embargo {config.EMBARGO_DAYS}日　|　"
        f"**OS** {sp['os_start']} ~ {sp['os_end']}",
        "",
        "## 為什麼做這個",
        "",
        "驗證上線權重在後段「沒被特別擬合」的時間（OS）是否仍有 edge，避免把",
        "in-sample 過擬合的權重拿去實盤。",
        "",
        "## (1) IS / OS 績效對比",
        "",
        "| 權重組 | 段 | 筆數 | 勝率 | 累積 | 年化 | Sharpe | MaxDD |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [mdrow(r) for _, r in df.iterrows()]
    lines += [
        "",
        "> 看點：① mom_quality 在 **OS** 是否仍優於 legacy_9；② OS 的 Sharpe/年化是否",
        "> 沒有相對 IS 崩掉（崩掉=過擬合）。",
        "",
    ]
    if boot:
        # beta 判定：OS 若是普漲行情（等權買進持有也大賺、上漲家數極高），bootstrap
        # CI 下界 > 0 只反映 beta，不代表選股 alpha。這裡把作者原本只印到 console 的
        # buy&hold 基準寫進報告，並讓結論在 beta 情境下自動降級，不再無條件背書。
        beta_like = bh is not None and (
            bh.get("up_ratio", 0.0) >= 0.80
            or (np.isfinite(bh.get("mean", np.nan)) and boot["ann_med"] <= bh["mean"])
        )
        if beta_like:
            verdict = (
                f"> ⚠ **這不是 alpha 的證據**：同期 top{pool} 等權買進持有平均 "
                f"{bh['mean']:+.0%}、上漲家數 {bh['up_ratio']:.0%}，OS 為普漲行情（beta）。"
                "bootstrap CI 下界 > 0 幾乎必然，只反映普漲、不代表選股有超額 edge。"
                "真正的鑑別看 IS 段與『報酬明顯超越買進持有』的幅度。"
            )
        elif boot["ann_lo"] > 0:
            verdict = ("> ✓ **年化 CI 下界 > 0 且非明顯普漲**：樣本外有初步正 edge"
                       "（仍受單一多頭窗 + 候選池倖存者偏誤限制，非乾淨 OOS）。")
        else:
            verdict = ("> ⚠ **年化 CI 下界 ≤ 0**：樣本外未達顯著，不能排除是運氣。")
        lines += [
            f"## (2) OS Block Bootstrap（momentum_only baseline；n={N_BOOT}, "
            f"block={BLOCK}日, {int(CI*100)}% CI）",
            "",
            "對 OS 每日報酬做區塊重抽樣（保留自相關），估現行 baseline 樣本外績效的不確定性。",
            "",
            "| 指標 | 中位數 | " + f"{int(CI*100)}% 信賴區間 |",
            "|---|---|---|",
            f"| 年化報酬 | {boot['ann_med']:+.1%} | [{boot['ann_lo']:+.1%}, {boot['ann_hi']:+.1%}] |",
            f"| Sharpe | {boot['sharpe_med']:.2f} | [{boot['sharpe_lo']:.2f}, {boot['sharpe_hi']:.2f}] |",
            f"| P(年化>0) | {boot['p_ann_pos']:.1%} | — |",
            "",
        ]
        if bh is not None:
            lines += [
                f"> **OS 段 top{pool} 等權買進持有基準**：平均 {bh['mean']:+.1%} / 中位 "
                f"{bh['median']:+.1%} / 上漲家數 {bh['up_ratio']:.0%}（n={bh['n']}）。"
                "策略 OS 報酬須**明顯超越此基準**才算 alpha，否則只是 beta。",
                "",
            ]
        lines += [verdict, ""]
    lines += [
        "## 重要保留（誠實聲明）",
        "",
        "1. **權重是用全期 IC 選的**，故此 OS 嚴格說測的是「固定權重的時間穩健性」，",
        "   不是「選股流程的純樣本外」。真正乾淨的 OOS 要『只用 IS 重算 IC 選權重』，",
        "   待 FinMind 資料更長（>3年）再做。",
        "2. 全期僅 2024-2026 單一多頭，OS 段更短、樣本更少，CI 會偏寬。",
        "3. 動能策略在行情反轉時會集體失靈；需搭配市場濾網（VIX/大盤 MA200）。",
        "4. 本報告為研究用途，非投資建議。",
    ]
    (config.OUTPUT_DIR / "OOS_VALIDATION_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[oos] 報告已存：outputs/OOS_VALIDATION_REPORT.md")


def _parse(argv):
    pool, reb, pick = 100, 5, 5
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--pool" and i + 1 < len(argv):
            pool = int(argv[i + 1]); i += 1
        elif a == "--rebalance" and i + 1 < len(argv):
            reb = int(argv[i + 1]); i += 1
        elif a == "--pick" and i + 1 < len(argv):
            pick = int(argv[i + 1]); i += 1
        i += 1
    return pool, reb, pick


if __name__ == "__main__":
    pool, reb, pick = _parse(sys.argv[1:])
    run(pool, reb, pick)
