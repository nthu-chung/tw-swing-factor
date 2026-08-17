# -*- coding: utf-8 -*-
"""跑在**最後**的守衛:確認沒有測試把 mock 留在整個 process 裡。

檔名的 `zz` 是刻意的 —— `unittest discover` 依模組名排序,這支必須最後執行才驗得到
「跑完整套之後,引擎還是不是原本那個引擎」。

為什麼需要它(2026-08-16 實例):`tests/test_pit_universe_boundary.py` 的
`_PanelEnv.__enter__` 逐一 `p.start()`,中途失敗時前面已啟動的 patch 沒有被停掉;
而 `__enter__` 拋出代表 with 區塊沒進去、`__exit__` 永遠不會跑。其中一個 patch 是
`_load_disposition_days -> lambda: {}`,一旦洩漏,字母序在後面的
`test_tpex_disposition` 就會拿到空字典而失敗 —— **而且失敗訊息完全看不出跟那裡有關**
(CI 偶發紅燈、macOS 無法重現,查了很久)。

這種 bug 的特徵正是本 repo 最在意的那種:不會 crash,只會讓另一支測試的保護
靜默失效。所以擋它的方式不是「記得寫對」,是讓它在套件結束時被抓到。
"""
from __future__ import annotations

import unittest


class NoPatchLeakTest(unittest.TestCase):
    def test_engine_hooks_are_not_left_patched_after_the_whole_suite(self):
        import execution.tradability as tradability
        from backtest import event_backtest

        leaks = []
        if event_backtest._load_disposition_days is not tradability.load_disposition_days:
            leaks.append(
                "_load_disposition_days 仍被 patch 著 → "
                f"{event_backtest._load_disposition_days!r};"
                "後面任何依賴處置禁倉的測試都會拿到假資料")

        for name in ("_assert_price_integrity", "backtest_portfolio", "factor_ic",
                     "_prepare_panel"):
            fn = getattr(event_backtest, name, None)
            if fn is None:
                leaks.append(f"{name} 不見了(被 patch 掉且沒還原?)")
                continue
            if getattr(fn, "__module__", None) != "backtest.event_backtest":
                leaks.append(f"{name} 的來源模組是 "
                             f"{getattr(fn, '__module__', None)!r},不是引擎自己")

        self.assertEqual(leaks, [], "偵測到跨測試的 patch 洩漏:\n  - "
                                    + "\n  - ".join(leaks))

    def test_process_level_registries_are_reset(self):
        """`security_type` 的 registry 是 process 級全域,同樣會跨測試污染。"""
        import security_type

        self.assertIsNone(
            security_type._REGISTRY_CACHE,
            "security_type registry 沒有被還原;後面的證券別判定會用到某個"
            "測試塞進去的假名冊,而那不會報錯,只會讓白名單閘門失去意義")


if __name__ == "__main__":
    unittest.main()
