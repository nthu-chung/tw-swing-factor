# -*- coding: utf-8 -*-
"""研究層:把 Python 策略接到唯一事件引擎的正式路徑。

責任邊界(見 `CROSS_SECTIONAL_STRATEGY_RESEARCH_SPEC.md` §4.1):

  contracts.py         CandidateSpec / EvaluationProtocol / immutable BacktestRequest
  signal_validation.py 所有 repo 與 external SignalFrame 共用的唯一 validator
  fixtures.py          synthetic(離線)與 local frozen data 兩種資料來源
  golden_path.py       API + CLI orchestration —— **不是第二套引擎**
  artifacts.py         run directory、atomic write、manifest 與表格輸出

這一層不算損益:所有績效都來自 `backtest.backtest_portfolio()`。
"""
