# -*- coding: utf-8 -*-
"""上個曆月成交值候選池。

這是正式回測的候選池 provider。某月 M 的成員永遠由 M-1 的交易所逐日快照
決定；當月行情或現在的熱門股名單都不能回頭改寫歷史成員。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import pandas as pd

from universes import pit_snapshots as pit_universe


@dataclass(frozen=True)
class MonthlyPITUniverseProvider:
    """以生效日索引的不可變月頻 PIT 候選池。"""

    pools: Dict[pd.Timestamp, List[str]]
    top_n: int
    min_obs: int
    history_start: pd.Timestamp
    history_end: pd.Timestamp

    @classmethod
    def from_history(cls, history: pd.DataFrame, *, top_n: int,
                     min_obs: int = 1) -> "MonthlyPITUniverseProvider":
        if history.empty:
            raise ValueError("PIT history 為空，不能建立正式候選池")
        dates = pd.to_datetime(history["date"])
        pools = pit_universe.build_pit_pools(
            history, top_n=top_n, freq="M", min_obs=min_obs,
        )
        if not pools:
            raise ValueError("PIT history 至少要涵蓋兩個相鄰月份")
        return cls(
            pools=pools,
            top_n=int(top_n),
            min_obs=int(min_obs),
            history_start=pd.Timestamp(dates.min()),
            history_end=pd.Timestamp(dates.max()),
        )

    @classmethod
    def from_cache(cls, *, top_n: int, min_obs: int = 1,
                   start: str = "2024-06-01") -> "MonthlyPITUniverseProvider":
        history = pit_universe.load_history_cached(start=start, require_complete=True)
        return cls.from_history(history, top_n=top_n, min_obs=min_obs)

    @property
    def all_symbols(self) -> List[str]:
        return sorted({sid for members in self.pools.values() for sid in members})

    def members_on(self, day) -> List[str]:
        return pit_universe.pool_for_date(self.pools, day)

    def candidate_mask(self, panel: pd.DataFrame) -> pd.Series:
        """對稠密 panel 標出當月候選；不刪列，避免 ts_ rolling 變稀疏。"""
        if not {"date", "stock_id"}.issubset(panel.columns):
            raise ValueError("candidate_mask 需要 date 與 stock_id 欄位")
        mask = pd.Series(False, index=panel.index, dtype=bool)
        for day, idx in panel.groupby("date", sort=False).groups.items():
            members = set(self.members_on(day))
            if members:
                mask.loc[idx] = panel.loc[idx, "stock_id"].isin(members).to_numpy()
        return mask

    def metadata(self) -> dict:
        return {
            "candidate_source": f"twse_tpex_previous_calendar_month_top{self.top_n}",
            "candidate_rule": "month_M_uses_only_calendar_month_M_minus_1",
            "candidate_frequency": "monthly",
            "candidate_rank_field": "mean_daily_turnover",
            "candidate_min_obs": self.min_obs,
            "candidate_pool_top_n": self.top_n,
            "candidate_pool_asof": str(self.history_end.date()),
            "candidate_history_start": str(self.history_start.date()),
            "candidate_effective_dates": len(self.pools),
            # 候選名單本身含當時上市、後來下市者；但正式價格仍由 FinMind 逐檔取，
            # 少數下市股可能缺完整還原序列，所以不能把整套回測冒充完全 survival-free。
            "candidate_membership_survivorship_free": True,
            "price_history_survivorship_free": False,
            "survivorship_free": False,
        }
