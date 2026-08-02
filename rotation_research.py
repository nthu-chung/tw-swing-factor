# -*- coding: utf-8 -*-
"""Causal long-only sector-rotation and breakout research.

This module deliberately separates three decisions:

1. point-in-time liquidity eligibility (handled by ``dynamic_universe``);
2. group/industry strength and institutional-flow pre-filtering;
3. stock-level price-volume confirmation and T+1 execution.

The current data only contains a coarse, current industry classification.
Consequently this is an implementation pilot, not a clean historical test of
fine-grained themes such as DRAM or passive components.  The report generated
by this module states that limitation explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

import backtest
import config
import data
import price_integrity
import universe as uni


MIN_GROUP_SIZE = 5
TOP_GROUPS = 3
MIN_SIGNAL_SCORE = 0.50
BREAKOUT_LOOKBACK = 20
VOLUME_LOOKBACK = 20
BREAKOUT_VOLUME_RATIO = 1.20
PRICE_INTEGRITY_AUDIT = "price_integrity_audit.csv"


@dataclass(frozen=True)
class ExitSpec:
    name: str
    ma_window: Optional[int] = 20
    max_hold: int = 120
    hard_stop: float = 0.08


EXIT_SPECS = (
    ExitSpec("ma10", ma_window=10),
    ExitSpec("ma20", ma_window=20),
    ExitSpec("hold20", ma_window=None, max_hold=20),
    ExitSpec("hold40", ma_window=None, max_hold=40),
)

THEME_CASES = {
    "記憶體": ["2408", "2344", "2337", "2451", "3260", "4967", "8271", "8299"],
    "被動元件": [
        "2327", "2492", "2375", "2478", "3090", "6173",
        "6207", "8043", "5328", "2428",
    ],
}


def build_research_panel(
    symbols: Optional[list[str]] = None,
    *,
    universe_top_n: int = 100,
) -> pd.DataFrame:
    """Return a point-in-time panel with group and entry-trigger fields."""
    if symbols is None:
        symbols = uni.get_universe(top_n=config.DYNAMIC_UNIVERSE_CANDIDATE_POOL)
    panel = backtest._prepare_panel(
        symbols,
        config.MIN_COMPOSITE,
        None,
        None,
        dynamic_enabled=True,
        universe_top_n=universe_top_n,
    )
    if panel.empty:
        return panel

    industry_map = uni.get_industry_map()
    out = panel.copy()
    out["industry"] = out["stock_id"].map(industry_map).fillna("未分類")
    out = out.sort_values(["stock_id", "date"]).reset_index(drop=True)

    grouped = out.groupby("stock_id", sort=False)
    prior_high = grouped["high"].transform(
        lambda s: s.shift(1).rolling(BREAKOUT_LOOKBACK, min_periods=BREAKOUT_LOOKBACK).max()
    )
    prior_volume = grouped["volume"].transform(
        lambda s: s.shift(1).rolling(VOLUME_LOOKBACK, min_periods=VOLUME_LOOKBACK).mean()
    )
    out["breakout_20"] = out["close"] > prior_high
    out["breakout_volume_ratio"] = out["volume"] / prior_volume.replace(0, np.nan)
    out["positive_day_share_20"] = grouped["close"].transform(
        lambda s: s.pct_change().gt(0).rolling(20, min_periods=20).mean()
    )
    return attach_group_scores(out)


def _pct_rank(s: pd.Series) -> pd.Series:
    if s.notna().sum() <= 1:
        return pd.Series(0.5, index=s.index, dtype=float)
    return s.rank(pct=True, method="average")


def attach_group_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """Attach same-day group breadth/flow scores using only causal fields."""
    required = {
        "date", "industry", "stock_id", "rs_excess", "mom_ret",
        "near_high", "inst_6d",
    }
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"group score 缺少欄位: {sorted(missing)}")

    group_daily = (
        panel.groupby(["date", "industry"], as_index=False)
        .agg(
            group_n=("stock_id", "nunique"),
            group_rs=("rs_excess", "median"),
            group_momentum=("mom_ret", "median"),
            group_near_high_breadth=("near_high", lambda s: float((s >= 0.95).mean())),
            group_inst_breadth=("inst_6d", lambda s: float((s > 0).mean())),
        )
    )
    group_daily["group_eligible"] = group_daily["group_n"] >= MIN_GROUP_SIZE

    rank_fields = [
        "group_rs", "group_momentum",
        "group_near_high_breadth", "group_inst_breadth",
    ]
    for col in rank_fields:
        group_daily[f"{col}_rank"] = (
            group_daily.groupby("date", group_keys=False)[col].apply(_pct_rank)
        )

    # Equal-weight, pre-registered combination.  No 2026 theme-specific weight
    # is fitted here.
    group_daily["group_price_score"] = group_daily[
        ["group_rs_rank", "group_momentum_rank", "group_near_high_breadth_rank"]
    ].mean(axis=1)
    group_daily["group_combo_score"] = (
        group_daily["group_price_score"] + group_daily["group_inst_breadth_rank"]
    ) / 2.0
    group_daily.loc[~group_daily["group_eligible"], "group_combo_score"] = np.nan
    group_daily["group_rank"] = (
        group_daily.groupby("date")["group_combo_score"]
        .rank(ascending=False, method="first")
    )

    merge_cols = [
        "date", "industry", "group_n", "group_rs", "group_momentum",
        "group_near_high_breadth", "group_inst_breadth",
        "group_price_score", "group_combo_score", "group_rank",
    ]
    return panel.merge(group_daily[merge_cols], on=["date", "industry"], how="left")


def build_signal_table(panel: pd.DataFrame, variant: str) -> pd.DataFrame:
    """Build causal close-of-day rankings for one strategy variant."""
    out = panel.copy()
    for col in ["score_momentum", "rs_excess", "inst_6d",
                "breakout_volume_ratio", "positive_day_share_20"]:
        out[f"{col}_rank"] = (
            out.groupby("date", group_keys=False)[col].apply(_pct_rank)
        )

    trend = out["trend_ok"].fillna(False)
    momentum_ok = out["score_momentum"] >= MIN_SIGNAL_SCORE

    if variant == "momentum":
        eligible = trend & momentum_ok
        out["signal_score"] = out["score_momentum"]
    elif variant == "rotation":
        eligible = trend & momentum_ok & (out["group_rank"] <= TOP_GROUPS)
        out["signal_score"] = out[
            ["score_momentum_rank", "rs_excess_rank", "inst_6d_rank"]
        ].mean(axis=1)
    elif variant == "rotation_breakout":
        eligible = (
            trend
            & (out["group_rank"] <= TOP_GROUPS)
            & (out["inst_6d"] > 0)
            & out["breakout_20"].fillna(False)
            & (out["breakout_volume_ratio"] >= BREAKOUT_VOLUME_RATIO)
        )
        out["signal_score"] = out[
            [
                "score_momentum_rank", "rs_excess_rank", "inst_6d_rank",
                "breakout_volume_ratio_rank", "positive_day_share_20_rank",
            ]
        ].mean(axis=1)
    else:
        raise ValueError(f"未知 variant: {variant}")

    return (
        out[eligible]
        .sort_values(["date", "signal_score", "stock_id"],
                     ascending=[True, False, True])
        .reset_index(drop=True)
    )


def _price_cache(symbols: Iterable[str], ma_windows: Iterable[int]) -> Dict[str, pd.DataFrame]:
    cache: Dict[str, pd.DataFrame] = {}
    for sid in symbols:
        price = data.fetch_price(sid)
        if price is None or price.empty:
            continue
        price = price.sort_values("date").reset_index(drop=True).copy()
        for win in ma_windows:
            price[f"ma{win}"] = price["close"].rolling(win, min_periods=win).mean()
        cache[sid] = price
    return cache


def run_portfolio(
    signals: pd.DataFrame,
    symbols: list[str],
    *,
    exit_spec: ExitSpec,
    start_date: str,
    end_date: str,
    max_positions: int = 5,
    price_frames: Optional[Dict[str, pd.DataFrame]] = None,
) -> dict:
    """Daily-fill, long-only portfolio with T+1 entry and T+1 MA exits."""
    ma_windows = [exit_spec.ma_window] if exit_spec.ma_window else []
    prices = price_frames if price_frames is not None else _price_cache(symbols, ma_windows)
    lookup = {
        sid: {d: i for i, d in enumerate(frame["date"])}
        for sid, frame in prices.items()
    }
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    all_dates = sorted({
        d for frame in prices.values()
        for d in frame.loc[(frame["date"] >= start) & (frame["date"] <= end), "date"]
    })
    if len(all_dates) < 2:
        return {"error": "回測日期不足"}

    by_date = {
        d: list(g[["stock_id", "name", "signal_score", "industry"]]
                .itertuples(index=False, name=None))
        for d, g in signals[
            (signals["date"] >= start) & (signals["date"] <= end)
        ].groupby("date")
    }

    def bar(sid: str, d: pd.Timestamp):
        idx = lookup.get(sid, {}).get(d)
        if idx is None:
            return None, None
        return prices[sid].iloc[idx], idx

    cash = 1.0
    equity = 1.0
    positions: Dict[str, dict] = {}
    pending_exit: Dict[str, str] = {}
    trades: list[dict] = []
    curve: list[tuple] = []
    fee = config.BT_FEE
    sell_cost = config.BT_FEE + config.BT_TAX

    def close_position(sid: str, d: pd.Timestamp, px: float, reason: str, idx: int):
        nonlocal cash
        pos = positions[sid]
        proceeds = pos["shares"] * px * (1 - sell_cost)
        cash += proceeds
        trades.append({
            "stock_id": sid,
            "name": pos["name"],
            "industry": pos["industry"],
            "signal_date": pos["signal_date"],
            "entry_date": pos["entry_date"],
            "exit_date": d,
            "entry_price": pos["entry_price"],
            "exit_price": px,
            "hold_bars": idx - pos["entry_idx"],
            "ret": proceeds / pos["cost"] - 1.0,
            "exit_reason": reason,
            "signal_score": pos["signal_score"],
        })
        positions.pop(sid)
        pending_exit.pop(sid, None)

    for di, d in enumerate(all_dates):
        # Close-confirmed exits are executed at today's open.
        for sid in list(pending_exit):
            if sid not in positions:
                pending_exit.pop(sid, None)
                continue
            row, idx = bar(sid, d)
            if row is not None and float(row["open"]) > 0:
                close_position(sid, d, float(row["open"]), pending_exit[sid], idx)

        # Hard stop is executable intraday; gaps use the worse opening price.
        for sid in list(positions):
            row, idx = bar(sid, d)
            if row is None or idx <= positions[sid]["entry_idx"]:
                continue
            stop = positions[sid]["entry_price"] * (1 - exit_spec.hard_stop)
            if float(row["open"]) <= stop:
                close_position(sid, d, float(row["open"]), "hard_stop_gap", idx)
            elif float(row["low"]) <= stop:
                close_position(sid, d, stop, "hard_stop", idx)

        # Yesterday's close signal enters today at the open.  Rank the full
        # queue, then keep filling after already-held/untradable names.
        if di > 0:
            signal_date = all_dates[di - 1]
            for sid, name, score, industry in by_date.get(signal_date, []):
                if len(positions) >= max_positions:
                    break
                if sid in positions:
                    continue
                row, idx = bar(sid, d)
                if row is None or float(row["open"]) <= 0:
                    continue
                allocation = min(equity / max_positions, cash)
                if allocation <= 0 or cash < allocation * 0.5:
                    break
                entry = float(row["open"])
                shares = allocation * (1 - fee) / entry
                cash -= allocation
                positions[sid] = {
                    "name": name,
                    "industry": industry,
                    "signal_date": signal_date,
                    "entry_date": d,
                    "entry_idx": idx,
                    "entry_price": entry,
                    "cost": allocation,
                    "shares": shares,
                    "signal_score": float(score),
                }

        mtm = cash
        for sid, pos in positions.items():
            row, _ = bar(sid, d)
            px = float(row["close"]) if row is not None else pos["entry_price"]
            mtm += pos["shares"] * px
        equity = mtm
        curve.append((d, equity))

        # Schedule close-confirmed exits for the next available open.
        for sid, pos in list(positions.items()):
            row, idx = bar(sid, d)
            if row is None or idx <= pos["entry_idx"]:
                continue
            held = idx - pos["entry_idx"]
            if held >= exit_spec.max_hold:
                pending_exit[sid] = "max_hold"
            elif exit_spec.ma_window:
                ma = row.get(f"ma{exit_spec.ma_window}")
                if pd.notna(ma) and float(row["close"]) < float(ma):
                    pending_exit[sid] = f"ma{exit_spec.ma_window}"

    eq = pd.DataFrame(curve, columns=["date", "equity"])
    trade_df = pd.DataFrame(trades)
    return {
        "summary": performance_metrics(eq, trade_df),
        "equity_curve": eq,
        "trades": trade_df,
    }


def performance_metrics(eq: pd.DataFrame, trades: pd.DataFrame) -> dict:
    if eq.empty:
        return {}
    series = eq.set_index("date")["equity"]
    daily = series.pct_change().dropna()
    years = max(len(daily) / 252.0, 1 / 252.0)
    cagr = float((series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1)
    vol = float(daily.std(ddof=1) * np.sqrt(252)) if len(daily) > 1 else np.nan
    sharpe = float(daily.mean() * 252 / vol) if vol and np.isfinite(vol) else np.nan
    drawdown = series / series.cummax() - 1
    return {
        "n_days": int(len(daily)),
        "n_trades": int(len(trades)),
        "cum_ret": float(series.iloc[-1] / series.iloc[0] - 1),
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "win_rate": float((trades["ret"] > 0).mean()) if len(trades) else np.nan,
        "avg_trade": float(trades["ret"].mean()) if len(trades) else np.nan,
        "median_trade": float(trades["ret"].median()) if len(trades) else np.nan,
        "avg_hold": float(trades["hold_bars"].mean()) if len(trades) else np.nan,
    }


def benchmark_metrics(start_date: str, end_date: str) -> dict:
    market = data.fetch_market_index().copy()
    market["date"] = pd.to_datetime(market["date"])
    market = market[
        (market["date"] >= pd.Timestamp(start_date))
        & (market["date"] <= pd.Timestamp(end_date))
    ].sort_values("date")
    if len(market) < 2:
        return {}
    eq = pd.DataFrame({
        "date": market["date"],
        "equity": market["close"] / market["close"].iloc[0],
    })
    return performance_metrics(eq, pd.DataFrame())


def market_relative_metrics(eq: pd.DataFrame, start_date: str, end_date: str) -> dict:
    """Return simple daily CAPM beta/alpha and relative terminal wealth."""
    if eq is None or eq.empty:
        return {}
    market = data.fetch_market_index().copy()
    market["date"] = pd.to_datetime(market["date"])
    market = market[
        (market["date"] >= pd.Timestamp(start_date))
        & (market["date"] <= pd.Timestamp(end_date))
    ][["date", "close"]].sort_values("date")
    joined = eq[["date", "equity"]].merge(market, on="date", how="inner")
    if len(joined) < 3:
        return {}
    returns = joined.set_index("date").pct_change().dropna()
    y = returns["equity"].to_numpy(dtype=float)
    x = returns["close"].to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(x)), x])
    alpha_daily, beta = np.linalg.lstsq(design, y, rcond=None)[0]
    strategy_wealth = joined["equity"].iloc[-1] / joined["equity"].iloc[0]
    market_wealth = joined["close"].iloc[-1] / joined["close"].iloc[0]
    return {
        "relative_wealth": float(strategy_wealth / market_wealth - 1),
        "ann_alpha": float(alpha_daily * 252),
        "beta": float(beta),
    }


def split_dates(panel: pd.DataFrame) -> dict:
    dates = pd.Series(sorted(panel["date"].unique()))
    cut = int(len(dates) * config.IS_OS_SPLIT)
    os_idx = min(len(dates) - 1, cut + config.EMBARGO_DAYS)
    return {
        "is_start": str(pd.Timestamp(dates.iloc[0]).date()),
        "is_end": str(pd.Timestamp(dates.iloc[cut - 1]).date()),
        "os_start": str(pd.Timestamp(dates.iloc[os_idx]).date()),
        "os_end": str(pd.Timestamp(dates.iloc[-1]).date()),
        "n_dates": int(len(dates)),
    }


def theme_case_audit(
    panel: pd.DataFrame,
    symbols: list[str],
    *,
    as_of_start: str = "2026-01-01",
) -> pd.DataFrame:
    """Audit named themes without using them to fit the generic strategy."""
    name_map = uni.get_name_map()
    pool_rank = {sid: i + 1 for i, sid in enumerate(symbols)}
    strict = build_signal_table(panel, "rotation_breakout")
    stock_trigger = panel[
        panel["trend_ok"].fillna(False)
        & (panel["inst_6d"] > 0)
        & panel["breakout_20"].fillna(False)
        & (panel["breakout_volume_ratio"] >= BREAKOUT_VOLUME_RATIO)
    ].copy()
    prices = _price_cache(symbols, [])
    market = data.fetch_market_index().copy()
    market["date"] = pd.to_datetime(market["date"])
    market = market.sort_values("date")
    start = pd.Timestamp(as_of_start)
    rows = []

    for theme, ids in THEME_CASES.items():
        for sid in ids:
            member = panel[(panel["stock_id"] == sid) & (panel["date"] >= start)]
            momentum = member[
                member["trend_ok"].fillna(False)
                & (member["score_momentum"] >= MIN_SIGNAL_SCORE)
            ]
            trigger = stock_trigger[
                (stock_trigger["stock_id"] == sid)
                & (stock_trigger["date"] >= start)
            ]
            strict_sid = strict[
                (strict["stock_id"] == sid)
                & (strict["date"] >= start)
            ]
            first_trigger = trigger["date"].min() if len(trigger) else pd.NaT

            audit = {
                "theme": theme,
                "stock_id": sid,
                "name": name_map.get(sid, ""),
                "current_pool_rank": pool_rank.get(sid),
                "first_dynamic_member": (
                    member["date"].min() if len(member) else pd.NaT
                ),
                "first_momentum_signal": (
                    momentum["date"].min() if len(momentum) else pd.NaT
                ),
                "first_stock_breakout_flow": first_trigger,
                "first_rotation_breakout": (
                    strict_sid["date"].min() if len(strict_sid) else pd.NaT
                ),
                "ret_20d": np.nan,
                "ret_40d": np.nan,
                "mfe_120d": np.nan,
                "taiex_20d": np.nan,
            }
            price = prices.get(sid)
            if pd.notna(first_trigger) and price is not None and len(price):
                future = price[price["date"] > first_trigger].reset_index(drop=True)
                if len(future):
                    entry = float(future.iloc[0]["open"])
                    audit["entry_date"] = future.iloc[0]["date"]
                    audit["entry_price"] = entry
                    if len(future) > 20:
                        audit["ret_20d"] = float(future.iloc[20]["close"] / entry - 1)
                    if len(future) > 40:
                        audit["ret_40d"] = float(future.iloc[40]["close"] / entry - 1)
                    audit["mfe_120d"] = float(
                        future.head(120)["high"].max() / entry - 1
                    )
                    market_future = market[market["date"] >= future.iloc[0]["date"]]
                    if len(market_future) > 20:
                        audit["taiex_20d"] = float(
                            market_future.iloc[20]["close"]
                            / market_future.iloc[0]["close"]
                            - 1
                        )
            rows.append(audit)
    return pd.DataFrame(rows)


def evaluate(
    *,
    candidate_pool: int = 300,
    universe_top_n: int = 100,
    max_positions: int = 5,
    panel: Optional[pd.DataFrame] = None,
    symbols: Optional[list[str]] = None,
) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    symbols = symbols or uni.get_universe(top_n=candidate_pool)
    if panel is None:
        panel = build_research_panel(symbols, universe_top_n=universe_top_n)
    split = split_dates(panel)
    price_frames = _price_cache(
        symbols,
        [spec.ma_window for spec in EXIT_SPECS if spec.ma_window],
    )
    rows = []
    trade_frames = []
    for variant in ("momentum", "rotation", "rotation_breakout"):
        signals = build_signal_table(panel, variant)
        for spec in EXIT_SPECS:
            for segment, start, end in (
                ("IS", split["is_start"], split["is_end"]),
                ("OOS", split["os_start"], split["os_end"]),
            ):
                result = run_portfolio(
                    signals,
                    symbols,
                    exit_spec=spec,
                    start_date=start,
                    end_date=end,
                    max_positions=max_positions,
                    price_frames=price_frames,
                )
                row = {
                    "variant": variant,
                    "exit": spec.name,
                    "segment": segment,
                    **result.get("summary", {}),
                    **market_relative_metrics(
                        result.get("equity_curve"),
                        start,
                        end,
                    ),
                }
                rows.append(row)
                trades = result.get("trades")
                if trades is not None and not trades.empty:
                    t = trades.copy()
                    t["variant"] = variant
                    t["exit"] = spec.name
                    t["segment"] = segment
                    trade_frames.append(t)

    result_df = pd.DataFrame(rows)
    benchmark = {
        "IS": benchmark_metrics(split["is_start"], split["is_end"]),
        "OOS": benchmark_metrics(split["os_start"], split["os_end"]),
    }
    all_trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    return result_df, {"split": split, "benchmark": benchmark}, all_trades


def main():
    symbols = uni.get_universe(top_n=config.DYNAMIC_UNIVERSE_CANDIDATE_POOL)
    price_frames = _price_cache(symbols, [])
    integrity_threshold = float(
        getattr(
            config,
            "PRICE_INTEGRITY_RETURN_THRESHOLD",
            price_integrity.DEFAULT_DISCONTINUITY_THRESHOLD,
        )
    )
    integrity_audit = price_integrity.audit_price_frames(
        price_frames,
        threshold=integrity_threshold,
    )
    if getattr(config, "ALLOW_UNADJUSTED_BACKTEST", False) and not (
        price_integrity.is_adjusted_price_dataset(config.PRICE_DATASET)
    ):
        print("[rotation_research] ⚠ 未還原價逃生門開啟(SWING_ALLOW_UNADJUSTED=1):"
              "結果含公司行動污染,非真實績效,請勿當已驗證數字引用。")
    elif price_integrity.should_block_unadjusted_backtest(
        config.PRICE_DATASET,
        integrity_audit,
    ):
        audit_path = config.OUTPUT_DIR / PRICE_INTEGRITY_AUDIT
        integrity_audit.to_csv(audit_path, index=False, encoding="utf-8-sig")
        raise RuntimeError(
            f"Price integrity fail-closed: {config.PRICE_DATASET} is an unadjusted "
            f"dataset. The discontinuity scan is diagnostic only and flagged "
            f"{len(integrity_audit)} rows; an empty scan would not mean clean prices, "
            "because ex-dividend gaps sit below the daily limit and are invisible to "
            f"it. Audit: {audit_path}. "
            "Do not estimate adjustment factors; rerun with an audited adjusted price "
            "dataset and survivorship-free PIT data."
        )
    panel = build_research_panel(
        symbols,
        universe_top_n=config.DYNAMIC_UNIVERSE_TOP_N,
    )
    result, meta, trades = evaluate(panel=panel, symbols=symbols)
    theme_audit = theme_case_audit(panel, symbols)
    result.to_csv(
        config.OUTPUT_DIR / "rotation_is_oos.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trades.to_csv(
        config.OUTPUT_DIR / "rotation_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    theme_audit.to_csv(
        config.OUTPUT_DIR / "theme_case_audit.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(meta)
    print(result.to_string(index=False))
    print(theme_audit.to_string(index=False))


if __name__ == "__main__":
    main()
