# -*- coding: utf-8 -*-
"""正式歷史策略取得候選池的**唯一最短路徑**。

為什麼要有這一層:在這之前,新策略要拿一組 symbols,最短的路徑是
`universe.get_research_candidates()` —— 但那讀的是 `outputs/universe_top*.json`,
是**單一日期**的成交值排名。把它回套整段歷史 = 用今天知道誰熱門去決定兩年前能
選誰(AGENTS.md 陷阱 4;實測舊池 283 檔有 83 檔在回測起點連前 200 名都排不進去)。

所以這裡把「月頻 PIT 候選池」做成預設且最短的入口:

    from universes import historical_pit_universe

    pit = historical_pit_universe()
    res = backtest.backtest_portfolio(**pit.backtest_kwargs(),
                                      start_date=is_start, end_date=is_end)

`backtest_kwargs()` 一次帶齊 `symbols` / `universe_provider` / `sample=False` /
`dynamic_enabled=True`,呼叫端沒有機會只帶一半而讓引擎誤判意圖 —— 引擎那邊的
強制點(`backtest._resolve_universe_source`)在缺 provider 時是 fail-closed raise。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import config

from .monthly_pit import MonthlyPITUniverseProvider


@dataclass(frozen=True)
class PITUniverse:
    """一組 PIT 候選池 + 它的成員聯集(可扣掉黑名單)。

    `symbols` 只允許是 provider 聯集的**子集**:唯一正當的縮小理由是資料品質
    黑名單(例如 `outputs/price_integrity_excluded.json`)。多出聯集以外的股票
    代表候選池已經不是由 PIT 規則決定,引擎會 raise。
    """

    provider: MonthlyPITUniverseProvider
    symbols: List[str]
    excluded: Tuple[str, ...] = field(default=())

    def backtest_kwargs(self) -> Dict:
        """展開成 backtest 引擎的關鍵字參數(正式歷史回測的完整意圖)。"""
        return {
            "symbols": list(self.symbols),
            "sample": False,
            "dynamic_enabled": True,
            "universe_provider": self.provider,
        }

    def metadata(self) -> Dict:
        meta = dict(self.provider.metadata())
        meta["candidate_symbols_excluded"] = len(self.excluded)
        return meta


def historical_pit_universe(*, candidate_pool_n: Optional[int] = None,
                            min_obs: Optional[int] = None,
                            exclude: Optional[Iterable[str]] = None,
                            start: Optional[str] = None) -> PITUniverse:
    """回傳正式歷史回測用的月頻 PIT 候選池。

    - `candidate_pool_n`:每月取成交值前 N(預設 `DYNAMIC_UNIVERSE_CANDIDATE_POOL`)。
    - `min_obs`:該月至少要有幾天交易資料才算候選(預設
      `DYNAMIC_UNIVERSE_MONTHLY_MIN_OBS`)。
    - `exclude`:資料品質黑名單(例如未還原價殘留斷點的股票)。
    - `start`:PIT 逐日快照的起始日;不傳就用 `MonthlyPITUniverseProvider.from_cache`
      的預設值(刻意不硬編,免得凍結測試對 kwargs 的斷言失效)。
    """
    kwargs = {
        "top_n": candidate_pool_n or config.DYNAMIC_UNIVERSE_CANDIDATE_POOL,
        "min_obs": (config.DYNAMIC_UNIVERSE_MONTHLY_MIN_OBS
                    if min_obs is None else int(min_obs)),
    }
    if start is not None:
        kwargs["start"] = start
    provider = MonthlyPITUniverseProvider.from_cache(**kwargs)

    union = sorted(set(provider.all_symbols))
    drop = sorted(set(exclude or ()) & set(union))
    symbols = [s for s in union if s not in set(drop)]
    if not symbols:
        raise ValueError(
            "PIT 候選池扣掉黑名單後為空;拒絕降級成靜態池或 sample universe"
        )
    return PITUniverse(provider=provider, symbols=symbols, excluded=tuple(drop))
