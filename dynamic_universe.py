# -*- coding: utf-8 -*-
"""Point-in-time dynamic universe helpers.

This module is deliberately strategy-agnostic.  It only determines whether a
stock was eligible on a signal date.  Portfolio direction remains long-only.

The ranking uses trailing observations ending on the signal date, so appending
future rows cannot change past membership.  A dynamic universe is only as
survivorship-free as its candidate set; callers must report the candidate-set
source separately.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {"stock_id", "date", "turnover", "volume", "close"}


def add_membership(
    panel: pd.DataFrame,
    *,
    top_n: int,
    lookback: int,
    min_obs: Optional[int] = None,
    min_avg_volume_lots: float = 0.0,
    min_avg_turnover: float = 0.0,
    candidate_mask: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Add trailing-liquidity fields, daily rank and ``in_dynamic_universe``.

    Ranking key is trailing average turnover (descending), then stock id for a
    deterministic tie-break.  All rolling windows include the current signal
    day and never use future observations.
    """
    missing = REQUIRED_COLUMNS - set(panel.columns)
    if missing:
        raise ValueError(f"dynamic universe 缺少欄位: {sorted(missing)}")
    if top_n <= 0:
        raise ValueError("top_n 必須 > 0")
    if lookback <= 0:
        raise ValueError("lookback 必須 > 0")

    min_obs = lookback if min_obs is None else min_obs
    if not 1 <= min_obs <= lookback:
        raise ValueError("min_obs 必須介於 1 與 lookback 之間")

    out = panel.copy()
    if candidate_mask is None:
        out["in_candidate_pool"] = True
    else:
        if len(candidate_mask) != len(panel):
            raise ValueError("candidate_mask 長度必須與 panel 相同")
        # 先依原 index 對齊再排序；不能在 sort 後直接塞 numpy，否則成員會錯配。
        aligned = pd.Series(candidate_mask, index=panel.index).fillna(False).astype(bool)
        out["in_candidate_pool"] = aligned
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values(["stock_id", "date"]).reset_index(drop=True)

    for col in ["turnover", "volume", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    valid_bar = (
        np.isfinite(out["close"]) & (out["close"] > 0)
        & np.isfinite(out["volume"]) & (out["volume"] > 0)
        & np.isfinite(out["turnover"]) & (out["turnover"] > 0)
    )
    out["_valid_turnover"] = out["turnover"].where(valid_bar)
    out["_valid_volume"] = out["volume"].where(valid_bar)

    grouped = out.groupby("stock_id", sort=False)
    out["universe_avg_turnover"] = grouped["_valid_turnover"].transform(
        lambda s: s.rolling(lookback, min_periods=min_obs).mean()
    )
    out["universe_avg_volume_lots"] = grouped["_valid_volume"].transform(
        lambda s: s.rolling(lookback, min_periods=min_obs).mean() / 1000.0
    )
    out["universe_obs"] = grouped["_valid_turnover"].transform(
        lambda s: s.rolling(lookback, min_periods=1).count()
    )

    eligible = (
        valid_bar
        & out["in_candidate_pool"]
        & (out["universe_obs"] >= min_obs)
        & (out["universe_avg_volume_lots"] >= min_avg_volume_lots)
        & (out["universe_avg_turnover"] >= min_avg_turnover)
    )
    out["universe_eligible"] = eligible
    out["universe_rank"] = np.nan
    out["in_dynamic_universe"] = False

    eligible_idx = out.index[eligible]
    if len(eligible_idx):
        ranked = (
            out.loc[eligible_idx, ["date", "stock_id", "universe_avg_turnover"]]
            .sort_values(
                ["date", "universe_avg_turnover", "stock_id"],
                ascending=[True, False, True],
            )
        )
        ranked["universe_rank"] = ranked.groupby("date").cumcount() + 1
        out.loc[ranked.index, "universe_rank"] = ranked["universe_rank"].astype(float)
        member_idx = ranked.index[ranked["universe_rank"] <= top_n]
        out.loc[member_idx, "in_dynamic_universe"] = True

    return out.drop(columns=["_valid_turnover", "_valid_volume"])


def membership_summary(panel: pd.DataFrame) -> dict:
    """Compact diagnostics for a panel returned by :func:`add_membership`."""
    if panel.empty or "in_dynamic_universe" not in panel:
        return {}
    members = panel[panel["in_dynamic_universe"]]
    by_day = members.groupby("date")["stock_id"].nunique()
    candidate = (
        panel["in_candidate_pool"].astype(bool)
        if "in_candidate_pool" in panel
        else pd.Series(True, index=panel.index)
    )
    return {
        "n_candidate_symbols": int(panel["stock_id"].nunique()),
        "n_monthly_candidate_symbols_ever": int(
            panel.loc[candidate, "stock_id"].nunique()
        ),
        "n_member_symbols_ever": int(members["stock_id"].nunique()),
        "n_dates": int(panel["date"].nunique()),
        "members_per_day_min": int(by_day.min()) if len(by_day) else 0,
        "members_per_day_median": float(by_day.median()) if len(by_day) else 0.0,
        "members_per_day_max": int(by_day.max()) if len(by_day) else 0,
    }
