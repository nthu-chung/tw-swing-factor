# -*- coding: utf-8 -*-
"""候選池規則與 point-in-time provider。

新策略請用 `historical_pit_universe()`(月頻 PIT,正式歷史回測的預設入口);
`universe.get_research_candidates()` 的單日靜態池只能當顯式對照組。
"""

from .entry import PITUniverse, historical_pit_universe
from .monthly_pit import MonthlyPITUniverseProvider

__all__ = [
    "MonthlyPITUniverseProvider",
    "PITUniverse",
    "historical_pit_universe",
]
