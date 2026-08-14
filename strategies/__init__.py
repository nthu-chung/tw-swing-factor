# -*- coding: utf-8 -*-
"""可重複執行的選股策略；研究腳本與策略實作分開保存。

策略的**全部**可調參數要放進 `spec.StrategySpec`(訊號 + 投組),否則
`freeze_manifest.py` 凍不到它們 —— 那就等於 forward 驗證的規則與凍結的規則不同。
"""

from .spec import KNOWN_STRATEGIES, StrategySpec, load_spec, load_strategy_module

__all__ = [
    "KNOWN_STRATEGIES",
    "StrategySpec",
    "load_spec",
    "load_strategy_module",
]
