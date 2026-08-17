# -*- coding: utf-8 -*-
"""回測引擎。**這裡有兩個引擎,呼叫端必須指名要哪一個。**

    from backtest import event_backtest   # 精確、慢,唯一可作正式證據
    from backtest import vec_backtest     # 近似、快,只能用於搜尋

這個 `__init__.py` **刻意不轉出任何東西**。若它轉出其中一個,`import backtest`
就會有一個「隱含的預設引擎」—— 而讀 `event_backtest.backtest_portfolio(...)` 的人看不出
那個數字是哪個引擎算的。兩個都必須指名,引擎身分才會出現在每一個呼叫點上。

  event_backtest  T+1 成交、漲跌停、一字鎖停、處置禁倉、整股張數、逐筆成本、
                  現金帳。慢(五相位約 35 秒),但它是唯一能產生正式證據的路徑。

  vec_backtest    向量化近似。丟掉逐日成交模擬以換取速度,供參數搜尋使用。
                  結果一律帶 `engine="vectorized_approximate"`,**結構上不可能**
                  被標成正式證據;任何要寫進 STRATEGY_REGISTRY.md 的數字都必須
                  由 event_backtest 重跑。兩者的差距要用對拍量出來,不是假設。
"""
