"""Exploratory, causal rank-flow event studies.

This module turns the daily outputs of :mod:`market_flow_monitor` into a
small set of falsifiable *signals*.  It deliberately does not construct a
portfolio, select position sizes, model costs, or make investment claims.
The event study measures a single name from the next available session's open
to a later close; it is a hypothesis screen, not a backtest.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


REQUIRED_METRIC_COLUMNS = {"date", "stock_id", "open", "close", "universe_rank"}
REQUIRED_BREADTH_COLUMNS = {
    "date", "above_ma20_pct", "institution_positive_pct",
}
HORIZONS = (5, 10, 20)


def _validate_inputs(stock_metrics: pd.DataFrame, market_breadth: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Normalize monitor output and reject ambiguous duplicate observations."""
    metric_missing = sorted(REQUIRED_METRIC_COLUMNS.difference(stock_metrics.columns))
    breadth_missing = sorted(REQUIRED_BREADTH_COLUMNS.difference(market_breadth.columns))
    if metric_missing:
        raise ValueError(f"stock_metrics missing required columns: {', '.join(metric_missing)}")
    if breadth_missing:
        raise ValueError(f"market_breadth missing required columns: {', '.join(breadth_missing)}")

    metrics = stock_metrics.copy()
    metrics["date"] = pd.to_datetime(metrics["date"]).dt.normalize()
    metrics["stock_id"] = metrics["stock_id"].astype(str)
    if metrics.duplicated(["date", "stock_id"]).any():
        raise ValueError("stock_metrics contains duplicate date/stock_id rows")
    for column in (
        "open",
        "close",
        "universe_rank",
        "flow_score",
        "institution_intensity",
        "ma20",
    ):
        if column not in metrics:
            continue
        metrics[column] = pd.to_numeric(metrics[column], errors="coerce")
    for column in ("inst_net_5d", "rank_delta_5d"):
        if column in metrics:
            metrics[column] = pd.to_numeric(metrics[column], errors="coerce")
    if "above_ma20" not in metrics:
        raise ValueError("stock_metrics missing required column: above_ma20")
    metrics["above_ma20"] = metrics["above_ma20"].fillna(False).astype(bool)
    for column in ("price_discontinuity_flag", "price_quarantine_flag"):
        if column in metrics:
            metrics[column] = metrics[column].map(
                lambda value: (
                    value
                    if isinstance(value, (bool, np.bool_))
                    else str(value).strip().lower() in {"1", "true", "yes"}
                )
            )
    metrics = metrics.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)

    breadth = market_breadth.copy()
    breadth["date"] = pd.to_datetime(breadth["date"]).dt.normalize()
    if breadth.duplicated("date").any():
        raise ValueError("market_breadth contains duplicate date rows")
    for column in ("above_ma20_pct", "institution_positive_pct"):
        breadth[column] = pd.to_numeric(breadth[column], errors="coerce")
    breadth = breadth.sort_values("date", kind="stable").reset_index(drop=True)
    return metrics, breadth


def build_rank_flow_signals(stock_metrics: pd.DataFrame, market_breadth: pd.DataFrame) -> pd.DataFrame:
    """Build four causal rank-flow signal types dated at the signal close.

    Definitions, all known at the close of date ``T``:

    * ``confirmed_entrant``: on ``T-1`` the name improved from rank >50 to
      rank <=20; it remains rank <=30 at ``T``.
    * ``persistent_leader``: at least three of T-4..T are top-30, T is top-20,
      rank improved over five observations, institutional 5-day flow is
      positive, and price is above MA20.
    * ``breadth_expansion``: MA20 breadth rose >=5 percentage points from
      T-5 and institutional-positive breadth is >=55%; select current top-20
      names whose institutional 5-day flow is positive.
    * ``rank_flow_persistence``: current top-50, five-observation improvement
      of at least 30 ranks, repeated positive flow score, non-weakening
      institutional intensity, above MA20 without >25% extension, and no
      abnormal overnight gap.

    The function has no look-ahead: shifts/rolls only point backward and the
    breadth merge is same-date only.  A single stock can emit multiple named
    hypotheses on one date; event-study de-duplication is a separate choice.
    """
    metrics, breadth = _validate_inputs(stock_metrics, market_breadth)
    by_stock = metrics.groupby("stock_id", sort=False, group_keys=False)

    # Rank at T-2 and T-1 let us verify yesterday's actual entry instead of
    # inferring it from a possibly absent rank_delta column.
    metrics["rank_t_minus_1"] = by_stock["universe_rank"].shift(1)
    metrics["rank_t_minus_2"] = by_stock["universe_rank"].shift(2)
    metrics["top30_days_5"] = by_stock["universe_rank"].transform(
        lambda s: s.le(30).rolling(5, min_periods=5).sum()
    )
    metrics["rank_t_minus_5"] = by_stock["universe_rank"].shift(5)
    metrics["rank_improvement_5d"] = metrics["rank_t_minus_5"] - metrics["universe_rank"]
    metrics["top100_days_5"] = by_stock["universe_rank"].transform(
        lambda s: s.le(100).rolling(5, min_periods=5).sum()
    )
    flow_score = metrics.get("flow_score", pd.Series(np.nan, index=metrics.index))
    metrics["positive_flow_score_days_3"] = flow_score.groupby(
        metrics["stock_id"], sort=False
    ).transform(lambda s: s.gt(0).rolling(3, min_periods=3).sum())
    institution_intensity = metrics.get(
        "institution_intensity", pd.Series(np.nan, index=metrics.index)
    )
    metrics["institution_intensity_t_minus_3"] = institution_intensity.groupby(
        metrics["stock_id"], sort=False
    ).shift(3)
    metrics["previous_close"] = by_stock["close"].shift(1)
    metrics["overnight_gap"] = metrics["open"] / metrics["previous_close"] - 1.0
    ma20 = metrics.get("ma20", pd.Series(np.nan, index=metrics.index))
    metrics["ma20_extension"] = metrics["close"] / ma20

    # The entrant condition is evaluated on yesterday: rank(T-2)>50,
    # rank(T-1)<=20, then today's confirmation rank(T)<=30.
    entrant = (
        metrics["rank_t_minus_2"].gt(50)
        & metrics["rank_t_minus_1"].le(20)
        & metrics["universe_rank"].le(30)
    )
    positive_flow = metrics.get("inst_net_5d", pd.Series(np.nan, index=metrics.index)).gt(0)
    leader = (
        metrics["top30_days_5"].ge(3)
        & metrics["universe_rank"].le(20)
        & metrics["rank_improvement_5d"].gt(0)
        & positive_flow
        & metrics["above_ma20"]
    )
    not_quarantined = ~metrics.get(
        "price_quarantine_flag",
        pd.Series(False, index=metrics.index),
    ).fillna(True).astype(bool)
    rank_flow_persistence = (
        metrics["universe_rank"].le(50)
        & metrics["rank_improvement_5d"].ge(30)
        & metrics["top100_days_5"].ge(3)
        & metrics["positive_flow_score_days_3"].eq(3)
        & institution_intensity.gt(0)
        & institution_intensity.ge(metrics["institution_intensity_t_minus_3"])
        & metrics["above_ma20"]
        & metrics["ma20_extension"].le(1.25)
        & metrics["overnight_gap"].abs().le(0.11)
        & not_quarantined
    )
    entrant &= not_quarantined
    leader &= not_quarantined
    metrics["_positive_flow_flag"] = positive_flow
    metrics["_not_quarantined_flag"] = not_quarantined

    breadth["above_ma20_pct_t_minus_5"] = breadth["above_ma20_pct"].shift(5)
    breadth["breadth_change_5d_pp"] = 100 * (
        breadth["above_ma20_pct"] - breadth["above_ma20_pct_t_minus_5"]
    )
    metrics = metrics.merge(
        breadth[["date", "institution_positive_pct", "breadth_change_5d_pp"]],
        on="date", how="left", validate="many_to_one",
    )
    expansion = (
        metrics["breadth_change_5d_pp"].ge(5.0)
        & metrics["institution_positive_pct"].ge(0.55)
        & metrics["universe_rank"].le(20)
        & metrics["_positive_flow_flag"]
        & metrics["_not_quarantined_flag"]
    )

    base_columns = [
        column for column in ("date", "stock_id", "name", "open", "close", "universe_rank", "inst_net_5d")
        if column in metrics
    ]
    events: list[pd.DataFrame] = []
    for signal, condition, reason in (
        (
            "confirmed_entrant", entrant,
            lambda x: (
                "yesterday entered top20 from outside top50 "
                f"(rank {int(x['rank_t_minus_2'])}->{int(x['rank_t_minus_1'])}); "
                f"confirmed today at rank {int(x['universe_rank'])}"
            ),
        ),
        (
            "persistent_leader", leader,
            lambda x: (
                f"top30 on {int(x['top30_days_5'])}/5 days; rank improved "
                f"{int(x['rank_t_minus_5'])}->{int(x['universe_rank'])}; "
                "institutional 5d flow positive and above MA20"
            ),
        ),
        (
            "breadth_expansion", expansion,
            lambda x: (
                f"MA20 breadth +{x['breadth_change_5d_pp']:.1f}pp in 5 days; "
                f"institution-positive breadth {x['institution_positive_pct']:.1%}; "
                f"top20 rank {int(x['universe_rank'])} with positive institutional 5d flow"
            ),
        ),
        (
            "rank_flow_persistence", rank_flow_persistence,
            lambda x: (
                f"rank improved {int(x['rank_t_minus_5'])}->{int(x['universe_rank'])}; "
                f"top100 on {int(x['top100_days_5'])}/5 days; "
                f"positive flow score {int(x['positive_flow_score_days_3'])}/3 days; "
                f"MA20 extension {x['ma20_extension']:.2f}"
            ),
        ),
    ):
        selected = metrics.loc[condition, base_columns + [
            "rank_t_minus_2", "rank_t_minus_1", "rank_t_minus_5", "top30_days_5",
            "rank_improvement_5d", "breadth_change_5d_pp", "institution_positive_pct",
            "top100_days_5", "positive_flow_score_days_3",
            "institution_intensity_t_minus_3", "overnight_gap", "ma20_extension",
        ]].copy()
        if selected.empty:
            continue
        selected["signal"] = signal
        selected["reason"] = selected.apply(reason, axis=1)
        events.append(selected)
    columns = base_columns + [
        "signal", "reason", "rank_t_minus_2", "rank_t_minus_1", "rank_t_minus_5",
        "top30_days_5", "rank_improvement_5d", "breadth_change_5d_pp", "institution_positive_pct",
        "top100_days_5", "positive_flow_score_days_3",
        "institution_intensity_t_minus_3", "overnight_gap", "ma20_extension",
    ]
    if not events:
        return pd.DataFrame(columns=columns)
    return pd.concat(events, ignore_index=True).sort_values(
        ["date", "stock_id", "signal"], kind="stable"
    ).reset_index(drop=True)[columns]


def event_study(
    signals: pd.DataFrame,
    stock_metrics: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = HORIZONS,
    deduplicate_overlaps: bool = True,
    max_entry_gap: float | None = 0.05,
) -> pd.DataFrame:
    """Measure next-open entries and later-close exits for exploratory events.

    ``horizon=5`` means enter at the first available open after T, then exit
    at the close five available stock sessions after that entry.  Events with
    incomplete future observations are retained with null exit fields.  With
    ``deduplicate_overlaps=True``, a stock's first signal suppresses later
    signals of the *same hypothesis* through the largest requested holding
    window.  Different signal types remain separate event studies; otherwise
    alphabetical signal ordering could arbitrarily assign an overlapping
    observation to one hypothesis and bias the per-signal comparison.  When
    ``max_entry_gap`` is set, a next-session open more than that fraction above
    the signal close is treated as an unfilled chase and omitted.
    """
    metrics, _ = _validate_inputs(stock_metrics, pd.DataFrame({
        "date": pd.to_datetime(stock_metrics["date"]).drop_duplicates(),
        "above_ma20_pct": np.nan,
        "institution_positive_pct": np.nan,
    }))
    required = {"date", "stock_id", "signal"}
    missing = sorted(required.difference(signals.columns))
    if missing:
        raise ValueError(f"signals missing required columns: {', '.join(missing)}")
    if not horizons or any(int(horizon) < 1 for horizon in horizons):
        raise ValueError("horizons must contain positive trading-session counts")
    if max_entry_gap is not None and (
        not np.isfinite(max_entry_gap) or max_entry_gap < 0
    ):
        raise ValueError("max_entry_gap must be non-negative or None")

    work = signals.copy()
    work["date"] = pd.to_datetime(work["date"]).dt.normalize()
    work["stock_id"] = work["stock_id"].astype(str)
    work = work.sort_values(["stock_id", "date", "signal"], kind="stable").reset_index(drop=True)
    metrics_by_stock = {
        stock_id: frame.sort_values("date", kind="stable").reset_index(drop=True)
        for stock_id, frame in metrics.groupby("stock_id", sort=False)
    }
    metrics_by_date = {
        day: frame.set_index("stock_id", drop=False)
        for day, frame in metrics.groupby("date", sort=False)
    }
    if "price_quarantine_flag" in metrics:
        quarantine_matrix = metrics.pivot(
            index="date",
            columns="stock_id",
            values="price_quarantine_flag",
        ).fillna(False).astype(bool)
    else:
        quarantine_matrix = pd.DataFrame()
    rows: list[dict[str, object]] = []
    max_horizon = max(int(h) for h in horizons)
    active_until: dict[tuple[str, str], pd.Timestamp] = {}
    for signal_row in work.itertuples(index=False):
        stock_id, signal_date = signal_row.stock_id, signal_row.date
        hypothesis_key = (stock_id, signal_row.signal)
        if stock_id not in metrics_by_stock:
            continue
        frame = metrics_by_stock[stock_id]
        available = frame.index[frame["date"].gt(signal_date)]
        if not len(available):
            continue
        entry_idx = int(available[0])
        entry_date = frame.loc[entry_idx, "date"]
        if (
            deduplicate_overlaps
            and hypothesis_key in active_until
            and signal_date <= active_until[hypothesis_key]
        ):
            continue
        entry_open = frame.loc[entry_idx, "open"]
        if not np.isfinite(entry_open) or entry_open <= 0:
            continue
        if (
            "price_quarantine_flag" in frame
            and bool(frame.loc[entry_idx, "price_quarantine_flag"])
        ):
            continue
        signal_matches = frame.index[frame["date"].eq(signal_date)]
        if not len(signal_matches):
            continue
        signal_close = frame.loc[int(signal_matches[0]), "close"]
        if not np.isfinite(signal_close) or signal_close <= 0:
            continue
        entry_gap = float(entry_open) / float(signal_close) - 1.0
        if max_entry_gap is not None and entry_gap > max_entry_gap:
            continue
        record = {
            "signal_date": signal_date,
            "stock_id": stock_id,
            "signal": signal_row.signal,
            "entry_date": entry_date,
            "entry_open": float(entry_open),
            "entry_gap": entry_gap,
        }
        for horizon in horizons:
            exit_idx = entry_idx + int(horizon)
            prefix = f"h{int(horizon)}"
            if exit_idx < len(frame) and np.isfinite(frame.loc[exit_idx, "close"]):
                exit_date = frame.loc[exit_idx, "date"]
                event_path_blocked = (
                    "price_quarantine_flag" in frame
                    and bool(
                        frame.loc[
                            entry_idx:exit_idx, "price_quarantine_flag"
                        ].any()
                    )
                )
                record[f"{prefix}_integrity_blocked"] = event_path_blocked
                if event_path_blocked:
                    record[f"{prefix}_exit_date"] = pd.NaT
                    record[f"{prefix}_exit_close"] = np.nan
                    record[f"{prefix}_return"] = np.nan
                    record[f"{prefix}_benchmark_n"] = 0
                    record[f"{prefix}_benchmark_return"] = np.nan
                    record[f"{prefix}_excess_return"] = np.nan
                    continue
                exit_close = float(frame.loc[exit_idx, "close"])
                record[f"{prefix}_exit_date"] = exit_date
                record[f"{prefix}_exit_close"] = exit_close
                record[f"{prefix}_return"] = exit_close / float(entry_open) - 1.0

                # Descriptive equal-weight control: names eligible at the
                # signal close, bought at the same next-session open and
                # measured at the same exit close.  This is not TAIEX and it
                # does not cure a non-PIT source universe, but it prevents a
                # broad market rally from being mistaken for stock-selection
                # alpha in this exploratory event screen.
                signal_day = metrics_by_date.get(signal_date)
                entry_day = metrics_by_date.get(entry_date)
                exit_day = metrics_by_date.get(exit_date)
                if signal_day is not None and entry_day is not None and exit_day is not None:
                    eligible_ids = set(
                        signal_day.loc[signal_day["universe_rank"].notna(), "stock_id"]
                    )
                    peer_ids = (
                        eligible_ids
                        .intersection(entry_day.index)
                        .intersection(exit_day.index)
                    )
                    if stock_id in peer_ids:
                        peer_ids.remove(stock_id)
                    if peer_ids:
                        peer_list = sorted(peer_ids)
                        peer_entry = pd.to_numeric(
                            entry_day.loc[peer_list, "open"], errors="coerce"
                        )
                        peer_exit = pd.to_numeric(
                            exit_day.loc[peer_list, "close"], errors="coerce"
                        )
                        valid_peers = (
                            np.isfinite(peer_entry)
                            & np.isfinite(peer_exit)
                            & peer_entry.gt(0)
                        )
                        if not quarantine_matrix.empty:
                            path_quarantine = quarantine_matrix.loc[
                                (quarantine_matrix.index >= entry_date)
                                & (quarantine_matrix.index <= exit_date),
                                quarantine_matrix.columns.intersection(peer_list),
                            ].any(axis=0)
                            contaminated_peers = set(
                                path_quarantine[path_quarantine].index.astype(str)
                            )
                            valid_peers &= ~valid_peers.index.to_series().isin(
                                contaminated_peers
                            )
                        peer_returns = (
                            peer_exit.loc[valid_peers] / peer_entry.loc[valid_peers] - 1.0
                        )
                    else:
                        peer_returns = pd.Series(dtype=float)
                else:
                    peer_returns = pd.Series(dtype=float)
                benchmark = (
                    np.nan
                    if peer_returns.empty
                    else float(peer_returns.mean())
                )
                record[f"{prefix}_benchmark_n"] = int(len(peer_returns))
                record[f"{prefix}_benchmark_return"] = benchmark
                record[f"{prefix}_excess_return"] = (
                    np.nan
                    if not np.isfinite(benchmark)
                    else record[f"{prefix}_return"] - benchmark
                )
            else:
                record[f"{prefix}_integrity_blocked"] = False
                record[f"{prefix}_exit_date"] = pd.NaT
                record[f"{prefix}_exit_close"] = np.nan
                record[f"{prefix}_return"] = np.nan
                record[f"{prefix}_benchmark_n"] = 0
                record[f"{prefix}_benchmark_return"] = np.nan
                record[f"{prefix}_excess_return"] = np.nan
        rows.append(record)
        if deduplicate_overlaps:
            cutoff_idx = min(entry_idx + max_horizon, len(frame) - 1)
            active_until[hypothesis_key] = frame.loc[cutoff_idx, "date"]
    return pd.DataFrame(rows).sort_values(["signal_date", "stock_id", "signal"], kind="stable").reset_index(drop=True) if rows else pd.DataFrame()


def event_summary(events: pd.DataFrame, horizons: tuple[int, ...] = HORIZONS) -> dict[str, object]:
    """Compact descriptive output; no portfolio aggregation or significance claim."""
    summary: dict[str, object] = {
        "event_count": int(len(events)),
        "independent_signal_dates": (
            0 if events.empty else int(events["signal_date"].nunique())
        ),
        "by_signal": {},
    }
    if events.empty:
        return summary
    for signal, frame in events.groupby("signal", sort=True):
        values: dict[str, object] = {
            "count": int(len(frame)),
            "signal_date_cohorts": int(frame["signal_date"].nunique()),
        }
        for horizon in horizons:
            returns = frame.get(f"h{int(horizon)}_return", pd.Series(dtype=float)).dropna()
            excess = frame.get(
                f"h{int(horizon)}_excess_return", pd.Series(dtype=float)
            ).dropna()
            cohort_excess = (
                frame.dropna(subset=[f"h{int(horizon)}_excess_return"])
                .groupby("signal_date")[f"h{int(horizon)}_excess_return"]
                .mean()
            )
            values[f"h{int(horizon)}"] = {
                "observations": int(len(returns)),
                "mean_return": None if returns.empty else float(returns.mean()),
                "median_return": None if returns.empty else float(returns.median()),
                "win_rate": None if returns.empty else float(returns.gt(0).mean()),
                "q25_return": None if returns.empty else float(returns.quantile(0.25)),
                "q75_return": None if returns.empty else float(returns.quantile(0.75)),
                "excess_observations": int(len(excess)),
                "mean_excess_return": (
                    None if excess.empty else float(excess.mean())
                ),
                "median_excess_return": (
                    None if excess.empty else float(excess.median())
                ),
                "excess_win_rate": (
                    None if excess.empty else float(excess.gt(0).mean())
                ),
                "cohort_observations": int(len(cohort_excess)),
                "mean_cohort_excess_return": (
                    None if cohort_excess.empty else float(cohort_excess.mean())
                ),
            }
        summary["by_signal"][signal] = values
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Exploratory causal rank-flow event studies (not a portfolio backtest)")
    parser.add_argument("--metrics", type=Path, required=True, help="market_flow_metrics_YYYYMMDD.csv")
    parser.add_argument("--breadth", type=Path, required=True, help="market_flow_breadth_YYYYMMDD.csv")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "outputs")
    parser.add_argument("--keep-overlaps", action="store_true", help="retain overlapping same-stock events")
    args = parser.parse_args()

    metrics = pd.read_csv(args.metrics)
    breadth = pd.read_csv(args.breadth)
    # Do not invent a corporate-action adjustment.  A current monitor output
    # carries causal quarantine flags and has already recomputed its ranks
    # without contaminated rows.  Older files without those flags fall back
    # to the more conservative whole-stock exclusion.
    from price_integrity import detect_price_discontinuities

    audit = detect_price_discontinuities(metrics)
    blocked_stocks = sorted(audit["stock_id"].astype(str).unique().tolist())
    if "price_quarantine_flag" in metrics:
        clean_metrics = metrics.copy()
        integrity_policy = "causal_21_observation_quarantine"
    else:
        clean_metrics = metrics[
            ~metrics["stock_id"].astype(str).isin(blocked_stocks)
        ].copy()
        integrity_policy = "excluded_whole_discontinuity_stock_fallback"
    signals = build_rank_flow_signals(clean_metrics, breadth)
    events = event_study(signals, clean_metrics, deduplicate_overlaps=not args.keep_overlaps)
    summary = event_summary(events)
    summary["signal_count"] = int(len(signals))
    summary["execution_policy"] = {
        "entry": "next_available_open",
        "max_positive_entry_gap": 0.05,
        "overlap_deduplication": (
            "none" if args.keep_overlaps else "within_stock_and_signal"
        ),
    }
    summary["price_integrity"] = {
        "policy": integrity_policy,
        "detected_stock_count": len(blocked_stocks),
        "detected_stock_ids": blocked_stocks,
        "discontinuity_count": int(len(audit)),
    }
    latest = pd.to_datetime(metrics["date"]).max().strftime("%Y%m%d")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    signal_path = args.output_dir / f"rank_flow_signals_{latest}.csv"
    event_path = args.output_dir / f"rank_flow_event_study_{latest}.csv"
    summary_path = args.output_dir / f"rank_flow_event_summary_{latest}.json"
    signals.to_csv(signal_path, index=False, encoding="utf-8-sig")
    events.to_csv(event_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Exploratory event study only; this is not a portfolio backtest.")
    if blocked_stocks:
        print(
            "Detected unadjusted discontinuity stocks under "
            f"{integrity_policy}: " + ", ".join(blocked_stocks)
        )
    print(f"signals: {signal_path}")
    print(f"events: {event_path}")
    print(f"summary: {summary_path}")


if __name__ == "__main__":
    main()
