"""Forward-ready E06 Quiet Sponsor Compression event screen.

This module is deliberately a *descriptive event study*, not a portfolio or
an order generator.  A signal is evaluated after the close of day ``T`` using
only ``T`` and earlier observations.  Its hypothetical entry is the next
available session's open, subject to a 4% positive-gap no-chase rule.

The input is the stock-level ``market_flow_metrics`` CSV.  In particular, the
causal liquidity rank, dynamic-universe flag, and price-quarantine flag are
mandatory.  E06 uses the first 200 liquidity ranks from the monitor's wider
top-300 research pool and never tries to infer a corporate-action repair from
price history.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from rank_flow_strategy import HORIZONS, event_study, event_summary
from price_integrity import detect_price_discontinuities


REQUIRED_COLUMNS = {
    "date",
    "stock_id",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "institution_net",
    "liquidity_rank",
    "dynamic_universe_flag",
    "price_quarantine_flag",
}
REQUIRED_HISTORY = 120
FLOW_LOOKBACK = 6
VOLUME_LOOKBACK = 20
ATR_LOOKBACK = 10


def _as_bool(values: pd.Series) -> pd.Series:
    """Parse monitor CSV flags without treating unknown text as true."""
    return values.map(
        lambda value: value
        if isinstance(value, (bool, np.bool_))
        else str(value).strip().lower() in {"1", "true", "yes"}
    ).fillna(False).astype(bool)


def _prepare_metrics(stock_metrics: pd.DataFrame) -> pd.DataFrame:
    """Validate the monitor contract and normalize a single causal panel."""
    missing = sorted(REQUIRED_COLUMNS.difference(stock_metrics.columns))
    if missing:
        raise ValueError(
            "market_flow_metrics missing required columns: " + ", ".join(missing)
        )
    metrics = stock_metrics.copy()
    metrics["date"] = pd.to_datetime(metrics["date"]).dt.normalize()
    metrics["stock_id"] = metrics["stock_id"].astype(str)
    if metrics.duplicated(["date", "stock_id"]).any():
        raise ValueError("market_flow_metrics contains duplicate date/stock_id rows")
    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "institution_net",
        "liquidity_rank",
    ):
        metrics[column] = pd.to_numeric(metrics[column], errors="coerce")
    for column in ("dynamic_universe_flag", "price_quarantine_flag"):
        metrics[column] = _as_bool(metrics[column])
    return metrics.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)


def _add_causal_features(metrics: pd.DataFrame) -> pd.DataFrame:
    """Add only same-day or strictly trailing features to a validated panel."""
    result = metrics.copy()
    by_stock = result.groupby("stock_id", sort=False, group_keys=False)

    # The trailing volume denominator is deliberately shifted: T's volume
    # cannot help make T look quiet or active.
    result["prior20_avg_volume"] = by_stock["volume"].transform(
        lambda s: s.shift(1).rolling(VOLUME_LOOKBACK, min_periods=VOLUME_LOOKBACK).mean()
    )
    result["institution_positive_days_6"] = by_stock["institution_net"].transform(
        lambda s: s.gt(0).rolling(FLOW_LOOKBACK, min_periods=FLOW_LOOKBACK).sum()
    )
    result["institution_net_6d"] = by_stock["institution_net"].transform(
        lambda s: s.rolling(FLOW_LOOKBACK, min_periods=FLOW_LOOKBACK).sum()
    )
    result["flow_to_prior_volume_6d"] = result["institution_net_6d"] / (
        result["prior20_avg_volume"] * FLOW_LOOKBACK
    )
    result["return_10d"] = by_stock["close"].pct_change(10, fill_method=None)
    result["ma20"] = by_stock["close"].transform(
        lambda s: s.rolling(20, min_periods=20).mean()
    )
    result["ma20_slope_5d"] = result["ma20"] - by_stock["ma20"].shift(5)
    result["prior10_high"] = by_stock["high"].transform(
        lambda s: s.shift(1).rolling(10, min_periods=10).max()
    )
    result["volume_ratio"] = result["volume"] / result["prior20_avg_volume"]
    result["prior_close"] = by_stock["close"].shift(1)
    result["overnight_gap"] = result["open"] / result["prior_close"] - 1.0

    # ATR(T) uses T's realized range and close(T-1), both available at T's
    # close.  Its threshold uses the 120 *previous* ATR observations, with a
    # hard min_periods=120.  It therefore cannot shorten itself for a 62-day
    # input or use the current/future ATR in its own percentile.
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - result["prior_close"]).abs(),
            (result["low"] - result["prior_close"]).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=False)
    result["atr10"] = true_range.groupby(result["stock_id"], sort=False).transform(
        lambda s: s.rolling(ATR_LOOKBACK, min_periods=ATR_LOOKBACK).mean()
    )
    result["atr10_to_close"] = result["atr10"] / result["close"]
    # A row that was quarantined is not a "valid observation" for the
    # 120-session compression baseline.  Requiring 120 non-null values inside
    # the trailing 120 rows means the threshold only reappears after a full,
    # contiguous clean history; an old corporate-action spike cannot silently
    # remain in the reference distribution.
    clean_atr_ratio = result["atr10_to_close"].where(
        ~result["price_quarantine_flag"]
    )
    result["atr10_prior120_p30"] = clean_atr_ratio.groupby(
        result["stock_id"], sort=False
    ).transform(
        lambda s: s.shift(1).rolling(
            REQUIRED_HISTORY, min_periods=REQUIRED_HISTORY
        ).quantile(0.30)
    )
    result["atr_history_count"] = clean_atr_ratio.groupby(
        result["stock_id"], sort=False
    ).transform(
        lambda s: s.shift(1).rolling(REQUIRED_HISTORY, min_periods=1).count()
    )

    # Rank only legal names from T's supplied dynamic pool.  Quarantined names
    # must not affect the percentile cutoff as they are not investable here.
    dynamic_legal = (
        result["dynamic_universe_flag"]
        & result["liquidity_rank"].le(200)
        & ~result["price_quarantine_flag"]
        & np.isfinite(result["flow_to_prior_volume_6d"])
    )
    result["flow_cross_section_pct"] = np.nan
    result.loc[dynamic_legal, "flow_cross_section_pct"] = result.loc[
        dynamic_legal
    ].groupby("date", sort=False)["flow_to_prior_volume_6d"].rank(
        ascending=False, method="first", pct=True
    )
    return result


def build_quiet_sponsor_signals(stock_metrics: pd.DataFrame) -> pd.DataFrame:
    """Return E06 signals known at close ``T`` from monitor metrics only.

    The cross-sectional sponsor-intensity test is top 20% within the
    contemporaneous ADV20 top-200 strategy universe.  Every rolling
    calculation is stock-sorted and trailing; no fills or centered windows
    are used.
    """
    metrics = _add_causal_features(_prepare_metrics(stock_metrics))
    condition = (
        metrics["dynamic_universe_flag"]
        & metrics["liquidity_rank"].le(200)
        & ~metrics["price_quarantine_flag"]
        & metrics["institution_positive_days_6"].ge(4)
        & metrics["flow_cross_section_pct"].le(0.20)
        & metrics["return_10d"].between(-0.03, 0.05, inclusive="both")
        & metrics["atr10_to_close"].le(metrics["atr10_prior120_p30"])
        & metrics["close"].gt(metrics["ma20"])
        & metrics["ma20_slope_5d"].gt(0)
        & metrics["close"].gt(metrics["prior10_high"])
        & metrics["volume_ratio"].between(1.10, 1.80, inclusive="both")
        & metrics["overnight_gap"].abs().le(0.11)
    )
    columns = [
        "date",
        "stock_id",
        *(["name"] if "name" in metrics else []),
        "open",
        "close",
        "liquidity_rank",
        "institution_positive_days_6",
        "institution_net_6d",
        "prior20_avg_volume",
        "flow_to_prior_volume_6d",
        "flow_cross_section_pct",
        "return_10d",
        "atr10_to_close",
        "atr10_prior120_p30",
        "atr_history_count",
        "ma20",
        "ma20_slope_5d",
        "prior10_high",
        "volume_ratio",
        "overnight_gap",
    ]
    signals = metrics.loc[condition, columns].copy()
    signals["signal"] = "quiet_sponsor_compression"
    signals["reason"] = (
        "E06 quiet sponsor compression: positive institutional flow "
        + signals["institution_positive_days_6"].astype(int).astype(str)
        + "/6 days; dynamic-pool flow percentile "
        + signals["flow_cross_section_pct"].map(lambda x: f"{x:.1%}")
        + "; ATR10/close at or below prior-120 p30"
    )
    signals["research_scope"] = "forward_only_descriptive_non_portfolio"
    return signals.sort_values(["date", "stock_id"], kind="stable").reset_index(drop=True)


# Short alias for notebook callers, while preserving the explicit public name.
build_signals = build_quiet_sponsor_signals


def quiet_sponsor_summary(
    stock_metrics: pd.DataFrame,
    signals: pd.DataFrame,
    events: pd.DataFrame,
) -> dict[str, object]:
    """Describe coverage and events without turning the screen into a portfolio."""
    featured = _add_causal_features(_prepare_metrics(stock_metrics))
    legal = (
        featured["dynamic_universe_flag"]
        & featured["liquidity_rank"].le(200)
        & ~featured["price_quarantine_flag"]
    )
    historical = legal & featured["atr10_prior120_p30"].notna()
    status = "sufficient_history_available" if historical.any() else "insufficient_history"
    summary = event_summary(events, horizons=HORIZONS)
    summary.update(
        {
            "signal_count": int(len(signals)),
            "required_history": REQUIRED_HISTORY,
            "insufficient_history_status": status,
            "insufficient_history": {
                "status": status,
                "dynamic_legal_rows": int(legal.sum()),
                "rows_with_prior_120_atr_observations": int(historical.sum()),
                "rows_without_prior_120_atr_observations": int((legal & ~historical).sum()),
            },
            "research_scope": "forward_only_descriptive_non_portfolio",
            "execution_policy": {
                "signal_time": "T_close",
                "entry": "T_plus_1_next_available_open",
                "max_positive_entry_gap": 0.04,
                "positive_gap_above_limit": "do_not_chase",
            },
            "price_integrity": {
                "policy": "require_supplied_causal_quarantine_flag",
                "quarantined_rows": int(featured["price_quarantine_flag"].sum()),
            },
        }
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E06 Quiet Sponsor Compression forward-only event study (not a portfolio)"
    )
    parser.add_argument("--metrics", type=Path, required=True, help="market_flow_metrics_YYYYMMDD.csv")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parent / "outputs"
    )
    parser.add_argument("--keep-overlaps", action="store_true", help="retain overlapping same-stock events")
    args = parser.parse_args()

    metrics = pd.read_csv(args.metrics)
    # Validate before producing any output.  Missing quarantine flags are a
    # hard integrity block rather than permission to infer repaired prices.
    prepared = _prepare_metrics(metrics)
    audit = detect_price_discontinuities(prepared)
    if not audit.empty:
        quarantine_keys = set(
            map(
                tuple,
                prepared.loc[
                    prepared["price_quarantine_flag"], ["stock_id", "date"]
                ].itertuples(index=False, name=None),
            )
        )
        unprotected = [
            (str(row.stock_id), row.date)
            for row in audit.itertuples(index=False)
            if (str(row.stock_id), row.date) not in quarantine_keys
        ]
        if unprotected:
            raise RuntimeError(
                "price discontinuity is not covered by the supplied causal "
                f"quarantine flags: {unprotected[:5]}"
            )
    signals = build_quiet_sponsor_signals(prepared)
    try:
        events = event_study(
            signals,
            prepared,
            horizons=HORIZONS,
            deduplicate_overlaps=not args.keep_overlaps,
            max_entry_gap=0.04,
        )
    except ValueError as error:
        raise ValueError(
            "market_flow_metrics is not compatible with descriptive event study "
            "(requires monitor universe_rank and above_ma20 columns): "
            f"{error}"
        ) from error
    if not events.empty:
        events["research_scope"] = "forward_only_descriptive_non_portfolio"
    summary = quiet_sponsor_summary(prepared, signals, events)

    latest = prepared["date"].max().strftime("%Y%m%d")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    signal_path = args.output_dir / f"quiet_sponsor_signals_{latest}.csv"
    event_path = args.output_dir / f"quiet_sponsor_event_study_{latest}.csv"
    summary_path = args.output_dir / f"quiet_sponsor_summary_{latest}.json"
    signals.to_csv(signal_path, index=False, encoding="utf-8-sig")
    events.to_csv(event_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Forward-only descriptive event study; not a portfolio backtest.")
    print(f"signals: {signal_path}")
    print(f"events: {event_path}")
    print(f"summary: {summary_path}")


if __name__ == "__main__":
    main()
