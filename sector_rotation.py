# -*- coding: utf-8 -*-
"""
族群輪動選股策略（可回測）
==========================
把 sector_scan.py 的「掃描」升級成「可回測策略」。sector_scan 已證實族群動能有
樣本外延續性（OS_IC ret20=0.13 / inst6d=0.11 / breadth=0.07，全 >0.03）——這是
本 repo 唯一 OS 站得住的 edge。本腳本把它做成選股策略並嚴格驗證有沒有增量 edge。

策略流程（每個 rebalance 日，全因果）：
  1. 用族群動能(ret20 中位)+法人流(inst6d 中位)+廣度(breadth) 跨族群排序 → 取前 K
     強勢族群（每個特徵當日 rank 後等權合成 sector_score）。
  2. 在強勢族群「內」選股，兩種假設各測一版：
       leader（領漲）：族群內個股動能最高者 → 強者恆強（族群+個股雙確認）。
       laggard（補漲）：族群內個股動能『落後族群中位』(gap>0)、趨勢未壞者 → 族群
                        已動、個股還沒跟上（提早補漲）。
  3. 接 backtest.backtest_portfolio（注入 picks_by_date）：T+1 開盤進場、trend 退場、
     交易成本、逐日淨值。與 momentum_only 基線比 IS/OS Sharpe/MaxDD + 分散化。

防未來函數：族群聚合與個股特徵只用當日以前資料（mom_ret/inst_6d/breadth 皆因果）；
排序在訊號日 T、進場在 T+1 開盤；fwd_ret 不進策略。回測鎖 config.SNAPSHOT_END_DATE。

誠實提醒：僅 2 年單一多頭 + 一次股災；族群延續性可能含時代紅利。以 IS/OS 回測為準。

用法：
  .venv/bin/python sector_rotation.py                 # top100, K=3, reb5/pick5
  .venv/bin/python sector_rotation.py --pool 300 --ksec 3
輸出：console + outputs/SECTOR_ROTATION_STRATEGY_REPORT.md + outputs/sector_rot_*.csv
"""
from __future__ import annotations

import sys
import copy

import numpy as np
import pandas as pd

import config
import data
import evaluation_split
import factors
import universe as uni
import backtest
import validate_oos as vo   # 重用 block bootstrap / buyhold baseline

MIN_SECTOR_N = 5     # 少於此檔數的產業不算族群
TOP_K_SECTORS = 3    # 取前 K 強勢族群


# ── 載入個股因子面板（族群特徵用；不需 market → 跳過 RS 計算較快）──────────
def _load_panels(pool: int):
    symbols = uni.get_universe(top_n=pool)
    imap = uni.get_industry_map()
    nmap = uni.get_name_map()
    print(f"[rot] 研究池 top{pool}：{len(symbols)} 檔，載入中 …")
    rows = []
    kept = []
    for i, sid in enumerate(symbols, 1):
        ind = imap.get(sid, "")
        if config.EXCLUDE_FINANCE and ("金融" in ind or "保險" in ind):
            continue
        if "ETF" in ind or "ETN" in ind or sid.startswith("00"):
            continue
        f = factors.compute_factors(data.fetch_bundle(sid))  # 不注入 market（族群特徵不需 RS）
        if f.empty or not uni.passes_liquidity(data.fetch_price(sid)):
            continue
        d = f[["date", "close", "ma_short", "mom_ret", "inst_6d", "trend_ok"]].copy()
        d["sid"] = sid
        d["name"] = nmap.get(sid, "")
        d["industry"] = ind or "(未知)"
        d["above_ma20"] = (d["close"] > d["ma_short"]).astype(float)
        rows.append(d)
        kept.append(sid)
        if i % 50 == 0:
            print(f"  … {i}/{len(symbols)}")
    long = pd.concat(rows, ignore_index=True)
    return long, kept


# ── 每日族群排序 → 強勢族群 ─────────────────────────────────────────────
def _hot_sectors(long: pd.DataFrame, top_k: int) -> pd.DataFrame:
    sec = long.groupby(["date", "industry"]).agg(
        sec_ret20=("mom_ret", "median"),
        breadth=("above_ma20", "mean"),
        sec_inst6d=("inst_6d", "median"),
        n=("mom_ret", "count"),
    ).reset_index()
    sec = sec[sec["n"] >= MIN_SECTOR_N].copy()
    # 每個交易日跨族群 rank（pct），三特徵等權合成 sector_score
    sec["r1"] = sec.groupby("date")["sec_ret20"].rank(pct=True)
    sec["r2"] = sec.groupby("date")["breadth"].rank(pct=True)
    sec["r3"] = sec.groupby("date")["sec_inst6d"].rank(pct=True)
    sec["sector_score"] = sec[["r1", "r2", "r3"]].mean(axis=1)
    sec["sec_rank"] = sec.groupby("date")["sector_score"].rank(ascending=False, method="first")
    hot = sec[sec["sec_rank"] <= top_k][["date", "industry", "sector_score", "sec_ret20"]]
    return hot


# ── 建 picks_by_date（leader / laggard）──────────────────────────────────
def build_picks(long: pd.DataFrame, mode: str, top_k: int) -> dict:
    hot = _hot_sectors(long, top_k)
    m = long.merge(hot, on=["date", "industry"], how="inner")
    m = m[(m["trend_ok"] == True)].dropna(subset=["mom_ret"])  # noqa: E712
    if mode == "leader":
        m = m.assign(score=m["mom_ret"])
    elif mode == "laggard":
        m = m.assign(gap=m["sec_ret20"] - m["mom_ret"])
        m = m[m["gap"] > 0].assign(score=lambda x: x["gap"])
    else:
        raise ValueError(mode)
    picks = {}
    for d, g in m.groupby("date"):
        g = g.sort_values("score", ascending=False)
        picks[d] = list(zip(g["sid"], g["score"], g["name"]))
    return picks


# ── 指標 ────────────────────────────────────────────────────────────────
def _metrics(eq: pd.DataFrame) -> dict:
    s = eq.set_index("date")["equity"] if "date" in eq.columns else eq["equity"]
    daily = s.pct_change().dropna()
    if len(daily) < 2:
        return {k: np.nan for k in ["cum", "ann", "sharpe", "mdd", "calmar"]}
    ann = float(daily.mean() * 252)
    vol = float(daily.std(ddof=1) * np.sqrt(252))
    sharpe = ann / vol if vol > 0 else 0.0
    peak = s.cummax(); mdd = float(((s - peak) / peak).min())
    return {"cum": float(s.iloc[-1] / s.iloc[0] - 1.0), "ann": ann, "sharpe": sharpe,
            "mdd": mdd, "calmar": (ann / abs(mdd) if mdd < 0 else float("nan"))}


def _run_strategy(symbols, picks, start, end, reb, pick):
    return backtest.backtest_portfolio(symbols=symbols, sample=False,
                                       start_date=start, end_date=end,
                                       rebalance_every=reb, top_n=pick,
                                       picks_by_date=picks)


def _daily_ret(eq):
    s = eq.set_index("date")["equity"]
    return s.pct_change().dropna()


def run(pool=100, top_k=3, reb=5, pick=5):
    global TOP_K_SECTORS
    TOP_K_SECTORS = top_k
    orig_w = copy.deepcopy(config.FACTOR_WEIGHTS)
    config.FACTOR_WEIGHTS = {"momentum": 1.0}  # 基線=上線純動能
    # 若市場濾網存在(PR#2 已合)則暫時關掉，純比策略；不存在則略過（與 main 相容）。
    filt_orig = getattr(config, "MARKET_FILTER_ENABLED", None)
    if filt_orig is not None:
        config.MARKET_FILTER_ENABLED = False

    symbols_full = uni.get_universe(top_n=pool)
    long, kept = _load_panels(pool)
    print(f"[rot] 有效個股 {len(kept)}；族群數 {long['industry'].nunique()}")

    picks_leader = build_picks(long, "leader", top_k)
    picks_laggard = build_picks(long, "laggard", top_k)
    print(f"[rot] leader 有訊號日 {len(picks_leader)}；laggard 有訊號日 {len(picks_laggard)}")

    # 用基線全期取得交易日 → IS/OS 切點
    base_full = backtest.backtest_portfolio(symbols=symbols_full, sample=False,
                                            rebalance_every=reb, top_n=pick)
    split = evaluation_split.build_evaluation_split(base_full["equity_curve"]["date"])
    n = split.n_total
    IS = split.is_window
    OS = split.os_window
    print(f"[rot] 全期 {IS[0]}~{OS[1]}（{n}日）| IS {IS[0]}~{IS[1]} | OS {OS[0]}~{OS[1]}")

    strategies = {
        "momentum_only(基線)": None,
        "sector_leader(領漲)": picks_leader,
        "sector_laggard(補漲)": picks_laggard,
    }

    rows = []
    eq_full = {}
    for label, picks in strategies.items():
        for seg, (st, en) in {"full": (None, None), "IS": IS, "OS": OS}.items():
            if picks is None:
                res = backtest.backtest_portfolio(symbols=symbols_full, sample=False,
                                                  start_date=st, end_date=en,
                                                  rebalance_every=reb, top_n=pick)
            else:
                res = _run_strategy(symbols_full, picks, st, en, reb, pick)
            if "equity_curve" not in res:
                rows.append({"strategy": label, "seg": seg, "error": res.get("error", "?")})
                continue
            m = _metrics(res["equity_curve"])
            s = res.get("summary", {})
            rows.append({"strategy": label, "seg": seg, "n_trades": s.get("n_trades"),
                         "win_rate": s.get("win_rate"), **m})
            if seg == "full":
                eq_full[label] = res["equity_curve"]

    df = pd.DataFrame(rows)

    # 分散化：各策略 vs 基線 的每日報酬相關（全期）
    base_r = _daily_ret(eq_full["momentum_only(基線)"])
    corr = {}
    for label, eq in eq_full.items():
        if label == "momentum_only(基線)":
            continue
        r = _daily_ret(eq)
        j = pd.concat([base_r, r], axis=1, join="inner").dropna()
        corr[label] = float(j.iloc[:, 0].corr(j.iloc[:, 1])) if len(j) > 2 else np.nan

    # OS bootstrap（每個策略）+ OS 買持基準
    boot = {}
    for label, picks in strategies.items():
        if picks is None:
            res = backtest.backtest_portfolio(symbols=symbols_full, sample=False,
                                              start_date=OS[0], end_date=OS[1],
                                              rebalance_every=reb, top_n=pick)
        else:
            res = _run_strategy(symbols_full, picks, OS[0], OS[1], reb, pick)
        boot[label] = vo._block_bootstrap_ci(res["equity_curve"]) if "equity_curve" in res else None
    bh = vo._buyhold_baseline(symbols_full, pd.to_datetime(OS[0]), pd.to_datetime(OS[1]))

    config.FACTOR_WEIGHTS = orig_w
    if filt_orig is not None:
        config.MARKET_FILTER_ENABLED = filt_orig
    _report(df, corr, boot, bh, IS, OS, n, pool, top_k, reb, pick)
    return df


def _f(x, pct=True):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "  n/a"
    return f"{x:+.1%}" if pct else f"{x:.2f}"


def _report(df, corr, boot, bh, IS, OS, n, pool, top_k, reb, pick):
    print("\n" + "=" * 96)
    print(f"  族群輪動 vs 純動能（top{pool}｜前{top_k}強勢族群｜reb{reb}/pick{pick}｜snapshot {config.SNAPSHOT_END_DATE}）")
    print("=" * 96)
    print(f"  {'策略':<22}{'段':<5}{'筆數':>5}{'勝率':>7}{'年化':>9}{'Sharpe':>8}{'MaxDD':>9}{'Calmar':>8}")
    for _, r in df.iterrows():
        if "error" in r and isinstance(r.get("error"), str) and pd.notna(r.get("error")):
            print(f"  {r['strategy']:<22}{r['seg']:<5}  ERROR {r['error']}"); continue
        print(f"  {r['strategy'] if r['seg']=='full' else '':<22}{r['seg']:<5}"
              f"{int(r['n_trades']) if pd.notna(r['n_trades']) else 0:>5}"
              f"{(r['win_rate'] if pd.notna(r['win_rate']) else 0):>7.0%}"
              f"{_f(r['ann']):>9}{_f(r['sharpe'],0):>8}{_f(r['mdd']):>9}{_f(r['calmar'],0):>8}")
        if r["seg"] == "OS":
            print("  " + "-" * 90)
    print("\n  分散化（各策略 vs 基線 每日報酬相關，全期；越低越有分散價值）：")
    for k, v in corr.items():
        print(f"    {k:<22} corr = {v:+.2f}")
    print("\n  OS Block Bootstrap（年化 90% CI；OS 為普漲行情，CI>0 是 beta 非 alpha）：")
    for k, b in boot.items():
        if b:
            print(f"    {k:<22} 年化中位 {b['ann_med']:+.0%}  CI[{b['ann_lo']:+.0%},{b['ann_hi']:+.0%}]  Sharpe中位 {b['sharpe_med']:.2f}")
    if bh:
        print(f"    ⚠️ OS top{pool} 買持基準：平均 {bh['mean']:+.0%} / 上漲家數 {bh['up_ratio']:.0%}")
    print("=" * 96)

    df.to_csv(config.OUTPUT_DIR / "sector_rot_compare.csv", index=False, encoding="utf-8-sig")
    _write_md(df, corr, boot, bh, IS, OS, n, pool, top_k, reb, pick)
    print("[rot] 已存 outputs/sector_rot_compare.csv + SECTOR_ROTATION_STRATEGY_REPORT.md")


def _mdrow(r):
    if "error" in r and isinstance(r.get("error"), str) and pd.notna(r.get("error")):
        return f"| {r['strategy']} | {r['seg']} | — | — | — | — | — | ERR |"
    return (f"| {r['strategy']} | {r['seg']} | {int(r['n_trades']) if pd.notna(r['n_trades']) else 0} "
            f"| {(r['win_rate'] if pd.notna(r['win_rate']) else 0):.0%} | {_f(r['ann'])} | {_f(r['sharpe'],0)} "
            f"| {_f(r['mdd'])} | {_f(r['calmar'],0)} |")


def _write_md(df, corr, boot, bh, IS, OS, n, pool, top_k, reb, pick):
    lines = [
        "# 族群輪動選股策略 — 研究報告",
        "",
        f"> 2026-07-21 接力研究。資料快照 `SNAPSHOT_END_DATE={config.SNAPSHOT_END_DATE}`｜"
        f"universe top{pool}｜前 {top_k} 強勢族群｜rebalance {reb}日 / 持有 {pick} 檔｜trend 退場。",
        f"> 對應程式：`sector_rotation.py`（本檔自動產生此報告）；引擎 `backtest.py`（注入 picks_by_date）。",
        f"> 全期 {IS[0]} ~ {OS[1]}（{n} 交易日）｜**IS** {IS[0]}~{IS[1]}｜embargo {config.EMBARGO_DAYS}日｜**OS** {OS[0]}~{OS[1]}",
        "",
        "## 策略定義",
        "",
        "每 rebalance 日用族群動能(ret20中位)+法人流(inst6d)+廣度(breadth) 跨族群排序 → 取前"
        f" {top_k} 強勢族群；在強勢族群『內』選股：",
        "- **領漲(leader)**：族群內個股動能最高者（強者恆強，族群+個股雙確認）。",
        "- **補漲(laggard)**：族群內個股動能『落後族群中位』(gap>0)、趨勢未壞者（族群已動、個股還沒跟上）。",
        "",
        "## IS / OS 績效（與 momentum_only 基線比；OS 為普漲 beta，關鍵看 IS）",
        "",
        "| 策略 | 段 | 筆數 | 勝率 | 年化 | Sharpe | MaxDD | Calmar |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [_mdrow(r) for _, r in df.iterrows()]
    lines += [
        "",
        "## 分散化（各策略 vs 基線 每日報酬相關，全期）",
        "",
        "| 策略 | 與基線相關 | 判讀 |",
        "|---|---|---|",
    ]
    for k, v in corr.items():
        note = "高度冗餘" if v > 0.8 else ("中度相關" if v > 0.6 else "有分散價值")
        lines.append(f"| {k} | {v:+.2f} | {note} |")
    lines += [
        "",
        "## OS Block Bootstrap（年化 90% CI）",
        "",
        "| 策略 | 年化中位 | 90% CI | Sharpe 中位 |",
        "|---|---|---|---|",
    ]
    for k, b in boot.items():
        if b:
            lines.append(f"| {k} | {b['ann_med']:+.0%} | [{b['ann_lo']:+.0%}, {b['ann_hi']:+.0%}] | {b['sharpe_med']:.2f} |")
    if bh:
        lines += ["",
                  f"> ⚠️ OS 段 top{pool} 等權買進持有：平均 {bh['mean']:+.0%}、上漲家數 {bh['up_ratio']:.0%}。"
                  f"OS 是普漲行情，所有策略的 OS 高 Sharpe 都是 beta 不是 alpha，**決策看 IS**。"]
    lines += [
        "",
        "## 誠實限制",
        "",
        "1. 僅 2 年單一多頭 + 一次股災；族群延續性（sector_scan OS_IC ret20=0.13）可能含時代紅利。",
        "2. 族群用 FinMind 官方粗分類，抓不到主題概念股（功率元件等橫跨多產業）。",
        "3. IS 樣本小；小樣本小差距（Sharpe <0.05）視為噪音，不據以改決策。",
        "4. 交易成本（手續費 0.1425%/證交稅 0.3%）已計入；回測鎖 snapshot、防未來函數。",
        "5. 本報告為研究用途，非投資建議。",
    ]
    (config.OUTPUT_DIR / "SECTOR_ROTATION_STRATEGY_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def _parse(argv):
    pool, ksec, reb, pick = 100, 3, 5, 5
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--pool" and i + 1 < len(argv):
            pool = int(argv[i + 1]); i += 1
        elif a == "--ksec" and i + 1 < len(argv):
            ksec = int(argv[i + 1]); i += 1
        elif a == "--reb" and i + 1 < len(argv):
            reb = int(argv[i + 1]); i += 1
        elif a == "--pick" and i + 1 < len(argv):
            pick = int(argv[i + 1]); i += 1
        i += 1
    return pool, ksec, reb, pick


if __name__ == "__main__":
    pool, ksec, reb, pick = _parse(sys.argv[1:])
    run(pool, ksec, reb, pick)
