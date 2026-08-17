# -*- coding: utf-8 -*-
"""**策略本體**:一個檔案一支策略,裡面只有假說與它的可調參數空間。

這個套件刻意**不放任何機器**。機器在 `strategy_kit/`:

    strategy_kit/signal_builder.py   分數 → 合格 SignalFrame 的翻譯層
    strategy_kit/contracts.py        DataRequirements / SignalContext
    strategy_kit/registry.py         allowlist:strategy_id → factory
    strategy_kit/spec.py             凍結用的 StrategySpec
    strategy_kit/position_policy.py  分數 → 想要的部位

為什麼要分開:之後要用基因演算法調參數,搜尋器必須能**列舉出所有策略**並讀出
每一支的參數空間。如果策略跟基礎設施混在同一個套件裡,搜尋器就得一直排除
「這個是機器不是策略」;更糟的是,有人會順手把共用邏輯寫進某支策略,讓它變成
別人的相依。這裡的規則很簡單:**這個資料夾裡的每一個 .py 都是一支策略。**

一支策略要提供的東西(見 `strategy_kit/signal_builder.py`):

    name / version          身分,要與 registry 的 id 一致
    thesis / kill_criterion 假說本身,以及什麼情況算它被推翻
    defaults / bounds       參數與上下界 —— **GA 只能動這裡**
    score(panel, ops, params) -> pd.Series    唯一要自己寫的函式

`score()` 只回答一件事:**這一天,哪一檔比較好?** 其餘(eligible 遮罩、名次、
母體大小、ties、provenance 欄位)全部由 `signal_builder` 統一處理,所以每支策略
不可能各自把它們弄錯。
"""

__all__: list = []
