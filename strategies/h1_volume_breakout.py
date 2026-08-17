# -*- coding: utf-8 -*-
"""H1:量能確認的突破。

**假說**:創 N 日新高、而且當天量能相對前期明顯放大的股票,未來數週延續機率
較高。經濟機制是「突破需要成交量背書」—— 沒有量的突破多半是薄量拉抬,隔天就
被賣回去。

**與 S19 的差異**:S19 疊了多個成分;H1 刻意只用價量兩個維度,
只看價格位置與量能,所以兩者的訊號來源是分開的。

**kill 條件**:IS 五個相位的 Sharpe 中位數若不明顯優於同期等權買進持有,
或最差相位為負且幅度接近中位數,視為沒有穩健 edge。
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

import factor_engine.operators as op
from strategy_kit.signal_builder import HypothesisStrategy


class H1VolumeBreakout(HypothesisStrategy):
    name = "h1_volume_breakout"
    version = "1.0.0"
    thesis = "創 N 日新高且量能放大者延續機率較高"
    kill_criterion = "IS 相位中位 Sharpe 不優於等權買進持有,或最差相位深度為負"

    defaults = {"high_window": 20, "vol_window": 20, "w_breakout": 0.6}
    bounds = {"high_window": (5, 120), "vol_window": (5, 120),
              "w_breakout": (0.0, 1.0)}

    def score(self, panel: pd.DataFrame, ops: op.PanelOps,
              params: Mapping) -> pd.Series:
        close = pd.to_numeric(panel["close"], errors="coerce")
        volume = pd.to_numeric(panel["volume"], errors="coerce")

        # 突破幅度:今天收盤相對「**不含今天**的前 N 日最高」。shift(1) 是因果性
        # 的關鍵 —— 含今天的話,今天的高點會把自己的突破洗掉。
        prior_high = ops.ts_max(ops.ts_delay(close, 1), int(params["high_window"]))
        breakout = close / prior_high.replace(0, np.nan) - 1.0

        # 量能比:今天量 / 前 N 日均量(同樣不含今天)。
        prior_vol = ops.ts_mean(ops.ts_delay(volume, 1), int(params["vol_window"]))
        vol_ratio = volume / prior_vol.replace(0, np.nan)

        w = float(params["w_breakout"])
        return w * ops.cs_rank(breakout) + (1.0 - w) * ops.cs_rank(vol_ratio)
