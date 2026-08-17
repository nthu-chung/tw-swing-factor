# -*- coding: utf-8 -*-
"""H3:短期反轉(**預期會失敗的對照組**)。

**假說**:近 N 日跌最多的股票會反彈。

**為什麼要跑一個預期失敗的**:repo 既有結論是台股 2024–2026 為動能市
(`factor_audit` 的分層報酬顯示買弱反向、`winner_dna` 的超跌反彈規則 OS lift 僅
1.04)。所以 H3 應該要**輸**。它的用途是當管線的對照:如果連反轉都跑出漂亮
Sharpe,要懷疑的是資料或管線,不是台股突然變成反轉市。

**kill 條件**:預期就是被 kill。若它反而顯著勝出,先查資料與執行層,不要先
相信結論。
"""
from __future__ import annotations

from typing import Mapping

import pandas as pd

import factor_engine.operators as op
from strategy_kit.signal_builder import HypothesisStrategy


class H3ShortReversal(HypothesisStrategy):
    name = "h3_short_reversal"
    version = "1.0.0"
    thesis = "近 N 日跌最多者反彈(預期失敗的對照組)"
    kill_criterion = "預期被 kill;若勝出,先懷疑資料與執行層"

    defaults = {"lookback": 5}
    bounds = {"lookback": (2, 60)}

    def score(self, panel: pd.DataFrame, ops: op.PanelOps,
              params: Mapping) -> pd.Series:
        close = pd.to_numeric(panel["close"], errors="coerce")
        past = ops.ts_delay(close, int(params["lookback"]))
        recent = close / past.replace(0, float("nan")) - 1.0
        # 跌越多分數越高 —— 反轉假說的定義。
        return ops.cs_rank(-recent)
