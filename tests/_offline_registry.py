# -*- coding: utf-8 -*-
"""離線測試共用 fixture:把測試代號宣告成上市普通股的證券別 registry。

為什麼需要(2026-08-15,證券別閘門第二輪)
------------------------------------------
`backtest_portfolio` 的兩條**繞過 panel** 的外部訊號路徑(`picks_by_date` 與
`strategy_position_policy` 的 `signal_frame`)現在都有證券別閘門,而閘門是
fail-closed:證券別判不出來(代號不在 TaiwanStockInfo)一律 raise,不預設放行
—— 「缺資訊就當可交易」正是那道閘門要修掉的 bug。

測試絕不打網路,所以每個走這兩條路徑的測試都必須**顯式宣告**它的代號是什麼證券。
這裡集中一份,免得十幾個測試各寫一次(寫法一分岔,閘門的語意就會被稀釋)。

這是 fixture,不是逃生門
------------------------
只有「宣告成上市/上櫃普通股、代號形狀也對」的代號會通過。興櫃 / DR / 創新板 /
ETF / 特別股 / 非 4 碼代號照樣被白名單擋掉 —— 閘門本身的行為釘在
`tests/test_security_type_filter.py`,不是這裡。

另外:測試代號一律用 4 碼數字。舊測試用 "A" / "B" / "HELD" 這種假代號,那種代號
連台股的證券別規則都套不上去(真實的 DR 9103、興櫃 6775 也都是 4 碼數字),等於
讓測試在一個「不可能出現非普通股」的假世界裡驗證引擎。
"""
from __future__ import annotations

import contextlib
from typing import Dict, Iterator, Tuple

import security_type


def common_stock_registry(*stock_ids) -> Dict[str, Tuple[str, str, str]]:
    """回傳 {stock_id -> (market_type, industry, name)},全部宣告成上市普通股。"""
    return {str(s): ("twse", "半導體業", f"測試{s}") for s in stock_ids}


def use_common_stocks(test_case, *stock_ids) -> None:
    """把 stock_ids 宣告成上市普通股;測試結束自動還原 process 級 registry。"""
    security_type.set_registry(common_stock_registry(*stock_ids))
    test_case.addCleanup(security_type.reset_registry)


@contextlib.contextmanager
def common_stocks(*stock_ids) -> Iterator[None]:
    """`use_common_stocks` 的 context manager 版(給不在 TestCase 裡的 helper 用)。"""
    security_type.set_registry(common_stock_registry(*stock_ids))
    try:
        yield
    finally:
        security_type.reset_registry()
