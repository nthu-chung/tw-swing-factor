"""Causal market and universe-flow monitor for the Taiwan equity research stack.

The calculation layer is deliberately independent of data acquisition: pass it
long-format daily prices and institutional-flow data in tests or research jobs.
The command line wrapper is only a convenience fetcher for the official TWSE
endpoints already used by :mod:`current_watchlist`.

This is a research monitor, not an order-generation system.  A row dated ``T``
uses observations dated ``T`` or earlier only.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from price_integrity import detect_price_discontinuities


REQUIRED_PRICE_COLUMNS = {"date", "stock_id", "close", "turnover", "volume"}
REQUIRED_FLOW_COLUMNS = {"date", "stock_id"}


@dataclass(frozen=True)
class MarketFlowResult:
    """All causal monitor outputs, separated for easy CSV export and testing."""

    stock_metrics: pd.DataFrame
    market_breadth: pd.DataFrame
    membership_events: pd.DataFrame
    top_n_churn: pd.DataFrame


def _require_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"{label} is missing required columns: {', '.join(missing)}")


def _prepare_inputs(prices: pd.DataFrame, flows: pd.DataFrame | None) -> pd.DataFrame:
    """Validate and combine input panels without altering their time availability."""
    _require_columns(prices, REQUIRED_PRICE_COLUMNS, "prices")
    price = prices.copy()
    price["date"] = pd.to_datetime(price["date"]).dt.normalize()
    price["stock_id"] = price["stock_id"].astype(str)
    if price.duplicated(["date", "stock_id"]).any():
        raise ValueError("prices contains duplicate date/stock_id rows")
    for column in ("close", "turnover", "volume"):
        price[column] = pd.to_numeric(price[column], errors="coerce")
    price = price.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)

    if flows is None or flows.empty:
        price["institution_net"] = 0.0
        return price

    _require_columns(flows, REQUIRED_FLOW_COLUMNS, "flows")
    flow = flows.copy()
    flow["date"] = pd.to_datetime(flow["date"]).dt.normalize()
    flow["stock_id"] = flow["stock_id"].astype(str)
    if flow.duplicated(["date", "stock_id"]).any():
        raise ValueError("flows contains duplicate date/stock_id rows")
    if "institution_net" not in flow:
        available = [c for c in ("foreign_net", "trust_net", "dealer_net") if c in flow]
        if not available:
            raise ValueError("flows requires institution_net or a component net-flow column")
        flow["institution_net"] = flow[available].apply(pd.to_numeric, errors="coerce").sum(axis=1)
    flow["institution_net"] = pd.to_numeric(flow["institution_net"], errors="coerce").fillna(0.0)
    flow = flow[["date", "stock_id", "institution_net"]]
    return price.merge(flow, on=["date", "stock_id"], how="left", validate="one_to_one").assign(
        institution_net=lambda x: x["institution_net"].fillna(0.0)
    )


def _cross_section_zscore(values: pd.Series) -> pd.Series:
    """Population z-score within a date; a one-name/tied cross section is zero."""
    valid = values.notna()
    result = pd.Series(np.nan, index=values.index, dtype=float)
    if not valid.any():
        return result
    std = values[valid].std(ddof=0)
    if not np.isfinite(std) or std == 0:
        result.loc[valid] = 0.0
    else:
        result.loc[valid] = (values[valid] - values[valid].mean()) / std
    return result


def _add_price_integrity_flags(panel: pd.DataFrame) -> pd.DataFrame:
    """Flag discontinuities and causally quarantine their return horizon.

    The integrity detector never repairs raw prices.  A discontinuity on day
    ``T`` makes that observation plus the following 20 observations for the
    same stock unusable: the latter protects the 20-observation return window
    from spanning an unadjusted corporate action.  Rolling backwards from each
    row uses only information available on that row or earlier.
    """
    integrity_input = panel[["stock_id", "date", "close"]].copy()
    # The price panel's public contract does not require open.  Passing close
    # as open preserves the detector's close-to-prior-close safety check while
    # avoiding invented intraday data.
    integrity_input["open"] = panel["open"] if "open" in panel else panel["close"]
    discontinuities = detect_price_discontinuities(integrity_input)
    discontinuity_keys = discontinuities[["stock_id", "date"]].assign(
        price_discontinuity_flag=True
    )
    result = panel.merge(discontinuity_keys, on=["stock_id", "date"], how="left")
    result["price_discontinuity_flag"] = result["price_discontinuity_flag"].fillna(False).astype(bool)
    result = result.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)
    result["price_quarantine_flag"] = result.groupby("stock_id", sort=False)[
        "price_discontinuity_flag"
    ].transform(lambda flags: flags.rolling(21, min_periods=1).max().astype(bool))
    return result


def _build_membership_outputs(metrics: pd.DataFrame, top_n: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Produce exact top-N changes and churn from ranks already known at each close."""
    events: list[dict[str, object]] = []
    churn_rows: list[dict[str, object]] = []
    prior_top: set[str] | None = None
    for day, day_frame in metrics.groupby("date", sort=True):
        current = day_frame.loc[day_frame["top_n_flag"], ["stock_id", "universe_rank", "rank_delta_1d"]]
        current = current.sort_values(["universe_rank", "stock_id"], kind="stable")
        current_top = set(current["stock_id"])
        if prior_top is None:
            churn_rows.append(
                {"date": day, "top_n_count": len(current_top), "entrants": np.nan, "exits": np.nan, "top_n_churn": np.nan}
            )
        else:
            entrants = current_top.difference(prior_top)
            exits = prior_top.difference(current_top)
            denominator = min(top_n, len(prior_top), len(current_top))
            churn = len(entrants) / denominator if denominator else np.nan
            churn_rows.append(
                {"date": day, "top_n_count": len(current_top), "entrants": len(entrants), "exits": len(exits), "top_n_churn": churn}
            )
            by_stock = current.set_index("stock_id")
            for stock_id in sorted(entrants):
                events.append(
                    {"date": day, "event": "entrant", "stock_id": stock_id,
                     "universe_rank": int(by_stock.loc[stock_id, "universe_rank"]),
                     "rank_delta_1d": by_stock.loc[stock_id, "rank_delta_1d"]}
                )
            for stock_id in sorted(exits):
                events.append(
                    {"date": day, "event": "exit", "stock_id": stock_id,
                     "universe_rank": np.nan, "rank_delta_1d": np.nan}
                )
        prior_top = current_top
    event_columns = ["date", "event", "stock_id", "universe_rank", "rank_delta_1d"]
    event_frame = pd.DataFrame(events, columns=event_columns)
    # Keep the schema stable even when one run happens to have no exits.
    event_frame["universe_rank"] = pd.array(event_frame["universe_rank"], dtype="Int64")
    event_frame["rank_delta_1d"] = pd.array(event_frame["rank_delta_1d"], dtype="Float64")
    return event_frame, pd.DataFrame(churn_rows)


def compute_market_flow(
    prices: pd.DataFrame,
    flows: pd.DataFrame | None = None,
    *,
    top_n: int = 20,
    universe_size: int = 300,
) -> MarketFlowResult:
    """Compute daily causal ranks, rank movement, top-N flow, and market breadth.

    ``momentum`` is the mean of 5- and 20-observation close returns.  Net
    institutional buying is normalized by contemporaneous 20-day average
    volume before its daily cross-sectional z-score.  Each day's scoring pool
    contains at most ``universe_size`` non-quarantined names with the largest
    20-observation average turnover available on that day.  There is no
    backward fill, forward fill, centered rolling window, or future-date
    merge.
    """
    if top_n < 1:
        raise ValueError("top_n must be at least one")
    if universe_size < 1:
        raise ValueError("universe_size must be at least one")
    if top_n > universe_size:
        raise ValueError("top_n cannot exceed universe_size")
    panel = _prepare_inputs(prices, flows)
    panel = panel.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)
    panel = _add_price_integrity_flags(panel)
    by_stock = panel.groupby("stock_id", group_keys=False, sort=False)
    panel["ret_5d"] = by_stock["close"].pct_change(5, fill_method=None)
    panel["ret_20d"] = by_stock["close"].pct_change(20, fill_method=None)
    panel["ma20"] = by_stock["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    panel["high_20d"] = by_stock["close"].transform(lambda s: s.rolling(20, min_periods=20).max())
    panel["avg_volume_20d"] = by_stock["volume"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    panel["avg_turnover_20d"] = by_stock["turnover"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    panel["inst_net_5d"] = by_stock["institution_net"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    panel["momentum"] = panel[["ret_5d", "ret_20d"]].mean(axis=1)
    panel.loc[panel[["ret_5d", "ret_20d"]].isna().any(axis=1), "momentum"] = np.nan
    panel["institution_intensity"] = panel["inst_net_5d"] / (panel["avg_volume_20d"] * 5)
    panel["above_ma20"] = panel["close"] > panel["ma20"]
    panel["new_high_20d"] = panel["close"] >= panel["high_20d"]

    # The liquidity pool is both causal and independent of the flow signal:
    # first rank only currently available trailing turnover among names that
    # are not quarantined, then use the selected names for every score/rank.
    # Stable stock-id ordering keeps cutoff ties deterministic.
    panel = panel.sort_values(["date", "stock_id"], kind="stable").reset_index(drop=True)
    liquidity_eligible = panel["avg_turnover_20d"].notna() & ~panel["price_quarantine_flag"]
    panel["liquidity_rank"] = np.nan
    panel.loc[liquidity_eligible, "liquidity_rank"] = panel.loc[liquidity_eligible].groupby("date")[
        "avg_turnover_20d"
    ].rank(ascending=False, method="first")
    panel["liquidity_rank"] = panel["liquidity_rank"].astype("Int64")
    panel["dynamic_universe_flag"] = panel["liquidity_rank"].le(universe_size).fillna(False)

    by_day = panel.groupby("date", group_keys=False, sort=False)
    # Pool membership is applied before grouping, so neither quarantined nor
    # lower-liquidity names can affect the selected cross-sectional scores.
    dynamic_eligible = panel["dynamic_universe_flag"]
    panel["momentum_z"] = by_day["momentum"].transform(
        lambda s: _cross_section_zscore(s.where(dynamic_eligible.loc[s.index]))
    )
    panel["turnover_z"] = by_day["turnover"].transform(
        lambda s: _cross_section_zscore(np.log1p(s.where(dynamic_eligible.loc[s.index])))
    )
    panel["institution_z"] = by_day["institution_intensity"].transform(
        lambda s: _cross_section_zscore(s.where(dynamic_eligible.loc[s.index]))
    )
    score_columns = ["momentum_z", "turnover_z", "institution_z"]
    panel["flow_score"] = panel[score_columns].mean(axis=1).where(dynamic_eligible)
    panel.loc[panel[score_columns].isna().any(axis=1), "flow_score"] = np.nan

    # Stable stock-id ordering makes ties and top-N membership deterministic.
    panel = panel.sort_values(["date", "stock_id"], kind="stable").reset_index(drop=True)
    panel["universe_rank"] = panel.groupby("date")["flow_score"].rank(
        ascending=False, method="first", na_option="bottom"
    )
    panel["universe_rank"] = panel["universe_rank"].where(panel["flow_score"].notna())
    panel["universe_rank"] = panel["universe_rank"].astype("Int64")
    panel["top_n_flag"] = panel["universe_rank"].le(top_n).fillna(False)
    panel = panel.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)
    panel["rank_delta_1d"] = panel.groupby("stock_id")["universe_rank"].shift(1) - panel["universe_rank"]
    panel["rank_delta_5d"] = panel.groupby("stock_id")["universe_rank"].shift(5) - panel["universe_rank"]
    metrics = panel.sort_values(["date", "universe_rank", "stock_id"], kind="stable").reset_index(drop=True)

    price_breadth_eligible = metrics["ma20"].notna() & ~metrics["price_quarantine_flag"]
    dynamic_breadth_eligible = metrics["dynamic_universe_flag"]
    breadth = metrics.groupby("date", sort=True).agg(
        universe_count=("stock_id", "size"),
        eligible_count=("ma20", lambda s: int(price_breadth_eligible.loc[s.index].sum())),
        above_ma20_pct=("above_ma20", lambda s: s.where(price_breadth_eligible.loc[s.index]).mean()),
        new_high_20d_pct=("new_high_20d", lambda s: s.where(price_breadth_eligible.loc[s.index]).mean()),
        institution_positive_pct=("inst_net_5d", lambda s: (s > 0).where(price_breadth_eligible.loc[s.index]).mean()),
        dynamic_universe_count=("stock_id", lambda s: int(dynamic_breadth_eligible.loc[s.index].sum())),
        dynamic_above_ma20_pct=("above_ma20", lambda s: s.where(dynamic_breadth_eligible.loc[s.index]).mean()),
        dynamic_new_high_20d_pct=("new_high_20d", lambda s: s.where(dynamic_breadth_eligible.loc[s.index]).mean()),
        dynamic_institution_positive_pct=(
            "inst_net_5d", lambda s: (s > 0).where(dynamic_breadth_eligible.loc[s.index]).mean()
        ),
    ).reset_index()
    events, churn = _build_membership_outputs(metrics, top_n)
    return MarketFlowResult(metrics, breadth, events, churn)


def fetch_twse_panels(as_of: date, calendar_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch official TWSE inputs using the same public functions as the screen."""
    from current_watchlist import fetch_flow_day, fetch_price_day

    session = requests.Session()
    prices, flows = [], []
    cursor = as_of
    while cursor >= as_of - timedelta(days=calendar_days):
        price = fetch_price_day(session, cursor)
        if not price.empty:
            prices.append(price)
            flow = fetch_flow_day(session, cursor)
            if not flow.empty:
                flows.append(flow)
        cursor -= timedelta(days=1)
    if not prices:
        raise RuntimeError("No TWSE daily price data was returned.")
    price_panel = pd.concat(prices, ignore_index=True)
    flow_panel = pd.concat(flows, ignore_index=True) if flows else pd.DataFrame(columns=["date", "stock_id", "institution_net"])
    return price_panel, flow_panel


def write_outputs(
    result: MarketFlowResult,
    output_dir: Path,
    as_of: pd.Timestamp,
    top_n: int,
    universe_size: int = 300,
) -> dict[str, Path]:
    """Write full CSVs plus a compact JSON latest-day snapshot."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = as_of.strftime("%Y%m%d")
    paths = {
        "metrics": output_dir / f"market_flow_metrics_{stamp}.csv",
        "breadth": output_dir / f"market_flow_breadth_{stamp}.csv",
        "events": output_dir / f"market_flow_events_{stamp}.csv",
        "churn": output_dir / f"market_flow_churn_{stamp}.csv",
        "integrity_audit": output_dir / f"market_flow_integrity_audit_{stamp}.csv",
        "summary": output_dir / f"market_flow_summary_{stamp}.json",
    }
    result.stock_metrics.to_csv(paths["metrics"], index=False, encoding="utf-8-sig")
    result.market_breadth.to_csv(paths["breadth"], index=False, encoding="utf-8-sig")
    result.membership_events.to_csv(paths["events"], index=False, encoding="utf-8-sig")
    result.top_n_churn.to_csv(paths["churn"], index=False, encoding="utf-8-sig")
    integrity_input = result.stock_metrics[["date", "stock_id", "close"]].copy()
    integrity_input["open"] = (
        result.stock_metrics["open"]
        if "open" in result.stock_metrics
        else result.stock_metrics["close"]
    )
    integrity_audit = detect_price_discontinuities(integrity_input)
    integrity_audit.to_csv(paths["integrity_audit"], index=False, encoding="utf-8-sig")
    latest_metrics = result.stock_metrics[result.stock_metrics["date"] == as_of]
    latest_breadth = result.market_breadth[result.market_breadth["date"] == as_of]
    summary = {
        "as_of": as_of.date().isoformat(),
        "top_n": top_n,
        "universe_size": universe_size,
        "latest_quarantine_count": int(latest_metrics["price_quarantine_flag"].sum()),
        "cumulative_quarantine_count": int(result.stock_metrics["price_quarantine_flag"].sum()),
        "quarantined_stock_count": int(
            result.stock_metrics.loc[
                result.stock_metrics["price_quarantine_flag"], "stock_id"
            ].nunique()
        ),
        "market_breadth": json.loads(latest_breadth.to_json(orient="records", date_format="iso")),
        "top_ranked": json.loads(latest_metrics[latest_metrics["top_n_flag"]].to_json(orient="records", date_format="iso")),
    }
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Causal TWSE market/universe flow monitor")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--calendar-days", type=int, default=90, help="calendar lookback to fetch (must cover 21 sessions)")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--universe-size", type=int, default=300, help="maximum daily liquidity-pool size")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "outputs")
    args = parser.parse_args()
    requested = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    prices, flows = fetch_twse_panels(requested, args.calendar_days)
    result = compute_market_flow(prices, flows, top_n=args.top_n, universe_size=args.universe_size)
    latest = result.stock_metrics["date"].max()
    paths = write_outputs(result, args.output_dir, latest, args.top_n, args.universe_size)
    print(f"TWSE market-flow monitor as of {latest.date()}")
    print(result.market_breadth[result.market_breadth["date"] == latest].to_string(index=False))
    print(result.stock_metrics[(result.stock_metrics["date"] == latest) & result.stock_metrics["top_n_flag"]][["universe_rank", "stock_id", "flow_score", "rank_delta_1d", "rank_delta_5d"]].to_string(index=False))
    print(f"Saved: {paths['summary']}")


if __name__ == "__main__":
    main()
