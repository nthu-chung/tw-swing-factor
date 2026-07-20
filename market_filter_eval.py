# -*- coding: utf-8 -*-
"""
市場濾網 / 擇時 overlay 評估
============================
在 momentum_only 基線上疊加「大盤(TAIEX)走弱→降曝險」的濾網，誠實比較
『無濾網 vs 有濾網（多個少參數變體）』，回答 human 拍板的問題：

  這個強先驗規則在我們僅有的一次股災(2025 關稅)上幫多少？牛市段代價多少？
  濾網 vs 現有 trend 退場，是重複還是互補？

比較指標（分 IS/OS 看，不只全期）：
  MaxDD / 年化 / Sharpe / Calmar / 股災窗口報酬 / 最痛20日 /
  濾網出場次數(換手) / regime 切換次數(whipsaw)。

變體（全部列出，不 cherry-pick）：
  baseline(關) / ma200 / ma60 / ma20 / vol30，risk-off 全空手(0)；
  另加 ma200、ma60 的「減半(0.5)」版當溫和變體。

守鐵則：規則少參數、不 grid-search；回測鎖 config.SNAPSHOT_END_DATE；全因果
(濾網用訊號日收盤、T+1 開盤動作)。**資料只有 n=1 熊市，不能宣稱已驗證。**

用法：.venv/bin/python market_filter_eval.py [--pool 100 --rebalance 5 --pick 5]
輸出：console + outputs/market_filter_*.csv
"""
from __future__ import annotations

import sys
import copy

import numpy as np
import pandas as pd

import config
import backtest
import universe as uni


# 變體：(label, filter_enabled, rule, riskoff_weight)
VARIANTS = [
    ("baseline(無濾網)", False, None,    None),
    ("ma200_flat",       True,  "ma200", 0.0),
    ("ma60_flat",        True,  "ma60",  0.0),
    ("ma20_flat",        True,  "ma20",  0.0),
    ("vol30_flat",       True,  "vol",   0.0),
    ("ma200_half",       True,  "ma200", 0.5),
    ("ma60_half",        True,  "ma60",  0.5),
]

# 2025 關稅股災窗口（TAIEX 谷底 2025-04-09、-28.7%）
CRASH_START, CRASH_END = "2025-02-01", "2025-06-30"


def _set_filter(enabled, rule, weight):
    config.MARKET_FILTER_ENABLED = enabled
    if enabled:
        config.MARKET_FILTER_RULE = rule
        config.MARKET_FILTER_RISKOFF_WEIGHT = weight


def _metrics(eq: pd.DataFrame) -> dict:
    s = eq.set_index("date")["equity"] if "date" in eq.columns else eq["equity"]
    daily = s.pct_change().dropna()
    if len(daily) < 2:
        return {k: np.nan for k in ["cum", "ann", "sharpe", "mdd", "calmar", "crash", "worst20"]}
    ann = float(daily.mean() * 252)
    vol = float(daily.std(ddof=1) * np.sqrt(252))
    sharpe = ann / vol if vol > 0 else 0.0
    peak = s.cummax()
    mdd = float(((s - peak) / peak).min())
    calmar = ann / abs(mdd) if mdd < 0 else float("nan")
    seg = s[(s.index >= pd.to_datetime(CRASH_START)) & (s.index <= pd.to_datetime(CRASH_END))]
    crash = float(seg.iloc[-1] / seg.iloc[0] - 1.0) if len(seg) >= 2 else float("nan")
    worst20 = float(s.pct_change(20).min())
    return {"cum": float(s.iloc[-1] / s.iloc[0] - 1.0), "ann": ann, "sharpe": sharpe,
            "mdd": mdd, "calmar": calmar, "crash": crash, "worst20": worst20}


def _run(symbols, start, end, rebalance, pick):
    return backtest.backtest_portfolio(symbols=symbols, sample=False,
                                       start_date=start, end_date=end,
                                       rebalance_every=rebalance, top_n=pick)


def _split(symbols, rebalance, pick):
    _set_filter(False, None, None)
    res = _run(symbols, None, None, rebalance, pick)
    dates = pd.to_datetime(res["equity_curve"]["date"]).sort_values().reset_index(drop=True)
    n = len(dates)
    cut = int(n * config.IS_OS_SPLIT)
    os_i = min(n - 1, cut + config.EMBARGO_DAYS)
    return {"is": (str(dates.iloc[0].date()), str(dates.iloc[cut].date())),
            "os": (str(dates.iloc[os_i].date()), str(dates.iloc[-1].date())),
            "n": n, "eq_full": res["equity_curve"]}


def _dd_episode(eq: pd.DataFrame):
    """回傳 (peak_date, trough_date, mdd) 供報告說明最大回撤發生在何時。"""
    s = eq.set_index("date")["equity"]
    peak = s.cummax()
    dd = (s - peak) / peak
    ti = dd.idxmin()
    pk = s[:ti].idxmax()
    return str(pk.date()), str(ti.date()), float(dd.min())


def run(pool, rebalance, pick):
    orig_w = copy.deepcopy(config.FACTOR_WEIGHTS)
    orig_filter = (config.MARKET_FILTER_ENABLED, config.MARKET_FILTER_RULE,
                   config.MARKET_FILTER_RISKOFF_WEIGHT)
    config.FACTOR_WEIGHTS = {"momentum": 1.0}  # 基線=上線純動能

    symbols = uni.get_universe(top_n=pool)
    print(f"[mf] universe top{pool}={len(symbols)} 檔｜rebalance {rebalance}日/持有{pick}檔｜"
          f"snapshot {config.SNAPSHOT_END_DATE}")

    sp = _split(symbols, rebalance, pick)
    pk, tr, mdd = _dd_episode(sp["eq_full"])
    print(f"[mf] 全期 {sp['is'][0]} ~ {sp['os'][1]}（{sp['n']} 日）")
    print(f"[mf] IS {sp['is'][0]}~{sp['is'][1]} | embargo {config.EMBARGO_DAYS}日 | OS {sp['os'][0]}~{sp['os'][1]}")
    print(f"[mf] 基線最大回撤 {mdd:+.1%}：波峰 {pk} → 谷底 {tr}\n")

    rows = []
    for label, en, rule, w in VARIANTS:
        _set_filter(en, rule, w)
        rec = {"variant": label}
        for seg, (st, ed) in {"full": (None, None), "IS": sp["is"], "OS": sp["os"]}.items():
            res = _run(symbols, st, ed, rebalance, pick)
            if "equity_curve" not in res:
                continue
            m = _metrics(res["equity_curve"])
            s = res["summary"]; mf = s["market_filter"]
            for k, v in m.items():
                rec[f"{seg}_{k}"] = v
            if seg == "full":
                rec["n_trades"] = s["n_trades"]
                rec["filter_exits"] = mf["n_filter_exits"]
                rec["switches"] = mf["n_regime_switches"]
                rec["exit_breakdown"] = s["exit_breakdown"]
        rows.append(rec)

    config.FACTOR_WEIGHTS = orig_w
    _set_filter(*orig_filter)
    df = pd.DataFrame(rows)
    _report(df, sp, pk, tr, mdd, pool, rebalance, pick)
    return df


def _f(x, pct=True):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "  n/a"
    return f"{x:+.1%}" if pct else f"{x:.2f}"


def _report(df, sp, pk, tr, mdd, pool, rebalance, pick):
    print("=" * 108)
    print(f"  {'變體':<16}{'段':<5}{'年化':>9}{'Sharpe':>8}{'MaxDD':>9}{'Calmar':>8}"
          f"{'股災窗':>9}{'最痛20':>9}{'濾網出場':>9}{'切換':>6}")
    print("=" * 108)
    for _, r in df.iterrows():
        for seg in ["full", "IS", "OS"]:
            extra = ""
            if seg == "full":
                extra = f"{int(r.get('filter_exits',0)):>9}{int(r.get('switches',0)):>6}"
            print(f"  {r['variant'] if seg=='full' else '':<16}{seg:<5}"
                  f"{_f(r.get(f'{seg}_ann')):>9}{_f(r.get(f'{seg}_sharpe'),0):>8}"
                  f"{_f(r.get(f'{seg}_mdd')):>9}{_f(r.get(f'{seg}_calmar'),0):>8}"
                  f"{_f(r.get(f'{seg}_crash')):>9}{_f(r.get(f'{seg}_worst20')):>9}{extra}")
        print("  " + "-" * 104)
    print(f"\n  基線最大回撤 {mdd:+.1%}：波峰 {pk} → 谷底 {tr}")
    print("  股災窗口 =", CRASH_START, "~", CRASH_END, "（TAIEX 谷底 2025-04-09, -28.7%）")

    df.to_csv(config.OUTPUT_DIR / "market_filter_eval.csv", index=False, encoding="utf-8-sig")
    print("\n[mf] 已存 outputs/market_filter_eval.csv")


def _parse(argv):
    pool, reb, pick = 100, 5, 5
    i = 0
    while i < len(argv):
        if argv[i] == "--pool" and i + 1 < len(argv):
            pool = int(argv[i + 1]); i += 1
        elif argv[i] == "--rebalance" and i + 1 < len(argv):
            reb = int(argv[i + 1]); i += 1
        elif argv[i] == "--pick" and i + 1 < len(argv):
            pick = int(argv[i + 1]); i += 1
        i += 1
    return pool, reb, pick


if __name__ == "__main__":
    run(*_parse(sys.argv[1:]))
