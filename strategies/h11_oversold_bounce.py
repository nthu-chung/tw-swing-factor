# -*- coding: utf-8 -*-
"""H11:純技術超賣反彈(oversold bounce)。

**假說**:RSI 低 + 布林位階低 = 短期超賣,價格會向中軌回歸。這是最古典的均值
回歸,不帶任何籌碼資訊。

**為什麼要跑一支「沒有故事」的**:同批的其他反向假說都在跌深之外疊了第二個機制。如果不先量出
**純技術超賣本身**值多少,就無法判斷它們的表現是來自各自的機制,還是來自
「買跌」這個共同成分。H11 是它們的共同基準,不是獨立的候選 —— 它的用途是
把「買跌」的部分扣掉。

**與 H3 的關係**:H3 用 5 日絕對報酬,H11 用波動正規化後的位置。同樣跌 10%,
高波動股可能只是日常、低波動股則是異常 —— RSI 與布林都內建這個正規化。所以
H11 是 H3 的「風險調整版」,兩者一起跑才知道正規化有沒有價值。

**kill 條件**:IS 相位中位 Sharpe < 0,或不優於 H3(正規化沒有增量價值)。

2026-08-17 IS 結果:**kill criterion 觸發,而且是反向族最差的一支。**
--------------------------------------------------------------
波動正規化**不但沒有增量價值,還比未正規化的絕對報酬更差**:RSI 與布林把
「跌得多但波動也大」的股票分數壓下去,留下的是低波動陰跌股 —— 在這個窗口
那是最壞的一組。週轉率也是同批最高之一。

這支的用途因此達成了:它把「買跌」這個共同成分的價值量了出來,而答案是
**負的**。同批其他反向假說若有任何超額,都不可能來自買跌本身。

(依 repo 的公開範圍,這裡不記錄績效數字;見 STRATEGY_REGISTRY.md 開頭。)
"""
from __future__ import annotations

from typing import Mapping

import pandas as pd

import factor_engine.operators as op
from strategy_kit.signal_builder import HypothesisStrategy


class H11OversoldBounce(HypothesisStrategy):
    name = "h11_oversold_bounce"
    version = "1.0.0"
    thesis = "RSI 與布林位階都低(波動正規化後的超賣)會向中軌回歸"
    kill_criterion = ("IS 相位中位 Sharpe < 0,或不優於 h3_short_reversal"
                      "(代表波動正規化沒有增量價值)")

    trend_guard = False

    required_columns = ("date", "stock_id", "close", "in_dynamic_universe")
    defaults = {"rsi_window": 14, "bb_window": 20, "rsi_weight": 0.5}
    bounds = {"rsi_window": (5, 60), "bb_window": (10, 60),
              "rsi_weight": (0.0, 1.0)}

    def score(self, panel: pd.DataFrame, ops: op.PanelOps,
              params: Mapping) -> pd.Series:
        close = pd.to_numeric(panel["close"], errors="coerce")
        rsi = ops.ts_rsi(close, int(params["rsi_window"]))          # [0,1]
        bb = ops.ts_bollinger_pos(close, int(params["bb_window"]))  # 0=中軌
        w = float(params["rsi_weight"])
        # 兩個都是「越低越超賣」,所以都取負號再 rank。
        return w * ops.cs_rank(-rsi) + (1.0 - w) * ops.cs_rank(-bb)
