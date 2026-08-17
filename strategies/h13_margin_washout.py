# -*- coding: utf-8 -*-
"""H13:融資斷頭出清(margin washout)。

**假說**:股價跌深、且融資餘額同時大幅下降,代表槓桿浮額已被強制清洗。融資
追繳與斷頭是**非自願賣壓** —— 它與看法無關,清完就沒了。所以「跌深 + 融資
大減」應該比「跌深 + 融資還掛在上面」更接近底部。

**為什麼這一支特別值得測**:這是台股結構性的東西,不是照搬美股因子。台股融資
維持率不足會被券商強制回補,形成短時間、非資訊性的集中賣壓;賣完之後那批
籌碼不會再出現。美股沒有等價機制(margin call 是券商逐戶處理,不公告餘額),
所以這個因子在文獻裡找不到,也不容易被同一批人擁擠交易。

**要小心的反向解讀**:融資餘額下降也可能是**還沒跌完**的中途站。這支要測的
是「清洗完成」與「清洗進行中」能不能分開 —— 如果分不開,IS 就會是負的,
那也是有用的答案。

**為什麼 `trend_guard = False`**:被斷頭清洗過的股票必然在均線之下。

**kill 條件**:IS 相位中位 Sharpe < 0,或「跌深」與「融資減」兩個成分拆開之後,
融資那一半沒有正貢獻(那代表撐住結果的只是跌深,融資維度是多餘的)。

2026-08-17 IS 結果:**kill criterion 觸發。**
--------------------------------------------------
成分拆解(`margin_weight` = 1.0 / 0.5 / 0.0)顯示 **「故事」那一半是拖累**:
單獨用融資餘額變化是負的,把權重調到 0(等於只剩跌深)反而最好。融資餘額下降
沒有分出「清洗完成」與「還在跌」。

而剩下的跌深成分也不是 edge:它系統性選中高波動股,超額集中在單一反彈區間,
在波動三分位內中性化之後幾乎消失。**兩個成分都沒有留下來的理由。**

要救這個假說需要的不是調權重,是**分點或逐筆的強制回補資料**來直接辨識斷頭,
而免費層沒有(見 DATA_SOURCES.md §5)。在那之前這個方向沒有可測的下一步。

(依 repo 的公開範圍,這裡不記錄績效數字;見 STRATEGY_REGISTRY.md 開頭。)
"""
from __future__ import annotations

from typing import Mapping

import pandas as pd

import factor_engine.operators as op
from strategy_kit.signal_builder import HypothesisStrategy


class H13MarginWashout(HypothesisStrategy):
    name = "h13_margin_washout"
    version = "1.0.0"
    thesis = "跌深 + 融資餘額大減 = 槓桿浮額已清洗,賣壓結構性減少"
    kill_criterion = ("IS 相位中位 Sharpe < 0;或成分拆解後融資那一半沒有正貢獻"
                      "(代表撐住結果的只是跌深,融資維度是多餘的)")

    trend_guard = False

    required_columns = ("date", "stock_id", "close", "in_dynamic_universe")
    defaults = {"margin_window": 20, "drawdown_window": 60,
                "margin_weight": 0.5}
    bounds = {"margin_window": (5, 60), "drawdown_window": (20, 250),
              "margin_weight": (0.0, 1.0)}

    def score(self, panel: pd.DataFrame, ops: op.PanelOps,
              params: Mapping) -> pd.Series:
        close = pd.to_numeric(panel["close"], errors="coerce")
        margin = pd.to_numeric(panel.get("margin_balance"), errors="coerce")

        # 用自身近期水準正規化:融資餘額的絕對張數在大小型股之間差好幾個級距。
        d = int(params["margin_window"])
        base = ops.ts_mean(margin, d)
        margin_change = ops.ts_delta(margin, d) / base.replace(0, float("nan"))

        peak = ops.ts_max(close, int(params["drawdown_window"]))
        drawdown = close / peak.replace(0, float("nan")) - 1.0

        w = float(params["margin_weight"])
        # 兩個都是「越負越符合假說」。
        return (w * ops.cs_rank(-margin_change)
                + (1.0 - w) * ops.cs_rank(-drawdown))
