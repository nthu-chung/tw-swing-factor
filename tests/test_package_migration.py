# -*- coding: utf-8 -*-
"""套件邊界的回歸測試。

原本這支是在守四個**舊匯入路徑相容 shim**(`factors.py` / `operators.py` /
`evaluation_split.py` / `chip_momentum_strategy.py`)。那四支已於 2026-08-16 刪除,
所有呼叫端改為直接指向真正的模組,所以「shim 還能用」的斷言失去意義 —— 現在
`operators.PanelOps is PanelOps` 只是同一個物件跟自己比。

留下來的是**跨套件的責任邊界**:引擎必須把可交易性判定委派給 `execution/`,
而不是自己再寫一份漲跌停與處置規則。這條在 shim 消失之後仍然成立,也仍然會被
違反(有人為了「就差一點」在引擎裡補一個 if)。
"""
from __future__ import annotations

import unittest

from backtest import event_backtest
from execution import detect_limit_lock, load_disposition_days


class PackageBoundaryTest(unittest.TestCase):
    def test_engine_delegates_tradability_to_execution_package(self):
        """漲跌停與處置禁倉只能有一份實作,而且住在 `execution/`。"""
        self.assertIs(event_backtest._limit_lock, detect_limit_lock)
        self.assertIs(event_backtest._load_disposition_days, load_disposition_days)

    def test_no_legacy_shim_modules_remain(self):
        """四支相容 shim 不得復活 —— 它們只會讓同一個東西有兩條匯入路徑。"""
        import importlib.util

        for name in ("factors", "operators", "evaluation_split",
                     "chip_momentum_strategy"):
            with self.subTest(module=name):
                self.assertIsNone(
                    importlib.util.find_spec(name),
                    f"根目錄的 {name}.py 相容 shim 已刪除,不應再出現")


if __name__ == "__main__":
    unittest.main()
