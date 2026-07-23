"""Read-only price-integrity checks for research backtests.

The detector in this module deliberately does not repair prices or infer an
adjustment factor.  A large discontinuity can be caused by a split, par-value
change, capital reduction, bad source row, or a genuine no-limit session.  In
all of those cases an unadjusted-price backtest should stop and require an
auditable adjusted/PIT data source instead of guessing.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


# Taiwan stocks normally have a 10% daily price limit.  Twice that limit leaves
# room for rounding and ordinary limit moves while still catching the much
# larger discontinuities produced by splits and other corporate actions.
DEFAULT_DISCONTINUITY_THRESHOLD = 0.20

AUDIT_COLUMNS = [
    "stock_id",
    "date",
    "previous_close",
    "open",
    "close",
    "overnight_return",
    "close_return",
]


def detect_price_discontinuities(
    prices: pd.DataFrame,
    *,
    threshold: float = DEFAULT_DISCONTINUITY_THRESHOLD,
) -> pd.DataFrame:
    """Return rows whose open or close jumps abnormally from prior close.

    The function is causal and pure: each result uses only the current row and
    the immediately preceding row of the same stock.  It never mutates the
    input, writes files, repairs prices, or estimates corporate-action factors.
    """
    required = {"stock_id", "date", "open", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"price integrity 缺少欄位: {sorted(missing)}")
    if not np.isfinite(threshold) or threshold <= 0:
        raise ValueError("threshold 必須是有限且大於 0 的數值")
    if prices.empty:
        return pd.DataFrame(columns=AUDIT_COLUMNS)

    work = prices[list(required)].copy()
    work["stock_id"] = work["stock_id"].astype(str)
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["open"] = pd.to_numeric(work["open"], errors="coerce")
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)
    work["previous_close"] = work.groupby("stock_id", sort=False)["close"].shift(1)

    valid_previous = np.isfinite(work["previous_close"]) & (work["previous_close"] > 0)
    work["overnight_return"] = np.where(
        valid_previous & np.isfinite(work["open"]) & (work["open"] > 0),
        work["open"] / work["previous_close"] - 1.0,
        np.nan,
    )
    work["close_return"] = np.where(
        valid_previous & np.isfinite(work["close"]) & (work["close"] > 0),
        work["close"] / work["previous_close"] - 1.0,
        np.nan,
    )
    abnormal = (
        work["overnight_return"].abs().gt(threshold)
        | work["close_return"].abs().gt(threshold)
    )
    return (
        work.loc[abnormal, AUDIT_COLUMNS]
        .sort_values(["stock_id", "date"], kind="stable")
        .reset_index(drop=True)
    )


def audit_price_frames(
    price_frames: Mapping[str, pd.DataFrame],
    *,
    threshold: float = DEFAULT_DISCONTINUITY_THRESHOLD,
) -> pd.DataFrame:
    """Combine per-stock frames and run :func:`detect_price_discontinuities`."""
    records = []
    for stock_id, frame in price_frames.items():
        if frame is None or frame.empty:
            continue
        required = {"date", "open", "close"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{stock_id} price integrity 缺少欄位: {sorted(missing)}")
        part = frame[["date", "open", "close"]].copy()
        part["stock_id"] = str(stock_id)
        records.append(part)
    if not records:
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    return detect_price_discontinuities(
        pd.concat(records, ignore_index=True),
        threshold=threshold,
    )


def is_adjusted_price_dataset(dataset: str) -> bool:
    """Return whether the configured dataset explicitly identifies adjusted prices."""
    normalized = str(dataset or "").strip().lower()
    return normalized.endswith("adj") or "adjusted" in normalized


def should_block_unadjusted_backtest(dataset: str, audit: pd.DataFrame) -> bool:
    """Core fail-closed decision used by the rotation research entry point."""
    return not is_adjusted_price_dataset(dataset) and audit is not None and not audit.empty
