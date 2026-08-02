# -*- coding: utf-8 -*-
"""Compare legacy static-current universe with point-in-time daily ranking.

The strategy remains long-only in both variants.  The dynamic variant ranks a
broader candidate set by trailing turnover on each signal date.
"""
from __future__ import annotations

import argparse
import time

import pandas as pd

import backtest
import config
import universe as uni


def _cagr(eq: pd.DataFrame) -> float:
    s = eq.sort_values("date")["equity"]
    n = max(1, len(s) - 1)
    years = n / 252.0
    if years <= 0 or s.iloc[0] <= 0 or s.iloc[-1] <= 0:
        return float("nan")
    return float((s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0)


def _run(label: str, symbols: list[str], dynamic: bool,
         universe_top: int, pick: int, rebalance: int) -> tuple[dict, pd.DataFrame]:
    started = time.time()
    res = backtest.backtest_portfolio(
        symbols=symbols,
        sample=False,
        rebalance_every=rebalance,
        top_n=pick,
        dynamic_enabled=dynamic,
        universe_top_n=universe_top,
    )
    if "summary" not in res:
        return {"variant": label, "error": res.get("error", "?")}, pd.DataFrame()
    s = res["summary"]
    u = s.get("universe", {})
    row = {
        "variant": label,
        "direction": "long_only",
        "n_trades": s["n_trades"],
        "cum_ret": s["cum_ret"],
        "cagr": _cagr(res["equity_curve"]),
        "ann_mean_return": s["ann_ret"],
        "sharpe": s["sharpe"],
        "max_drawdown": s["max_drawdown"],
        "candidate_symbols": u.get("n_candidate_symbols", len(symbols)),
        "member_symbols_ever": u.get("n_member_symbols_ever"),
        "members_per_day_median": u.get("members_per_day_median"),
        "survivorship_free": u.get("survivorship_free", False),
        "seconds": round(time.time() - started, 1),
    }
    return row, res["equity_curve"]


def run(static_top: int = 100, candidate_pool: int = 300,
        universe_top: int = 100, pick: int = 5, rebalance: int = 5) -> pd.DataFrame:
    static_symbols = uni.get_universe(top_n=static_top)
    dynamic_symbols = uni.get_universe(top_n=candidate_pool)
    rows = []
    static, _ = _run(
        f"static_current_top{static_top}", static_symbols, False,
        universe_top, pick, rebalance,
    )
    rows.append(static)
    dynamic, _ = _run(
        f"dynamic_top{universe_top}_within_current_top{candidate_pool}",
        dynamic_symbols, True, universe_top, pick, rebalance,
    )
    rows.append(dynamic)
    out = pd.DataFrame(rows)
    out.to_csv(
        config.OUTPUT_DIR / "dynamic_universe_comparison.csv",
        index=False, encoding="utf-8-sig",
    )
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--static-top", type=int, default=100)
    p.add_argument("--candidate-pool", type=int, default=300)
    p.add_argument("--universe-top", type=int, default=100)
    p.add_argument("--pick", type=int, default=5)
    p.add_argument("--rebalance", type=int, default=5)
    a = p.parse_args()
    out = run(
        a.static_top, a.candidate_pool, a.universe_top, a.pick, a.rebalance
    )
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
