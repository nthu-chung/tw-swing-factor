# -*- coding: utf-8 -*-
"""引擎不得對趨勢有意見。

分界:**引擎強制市場強制你的事,策略宣告你相信的事。**

    市場強制(引擎)  T+1 才能賣、漲跌停買不到、處置股受限、整股 1000 股、現金不夠就是不夠
    你的看法(策略)  MA20 要不要在 MA60 之上

`trend_ok` 是三條均線條件 —— 那是看法。它曾經同時住在三個地方:

  1. `strategy_kit.signal_builder._member_mask()`  硬編碼,每支假說無條件繼承
  2. `backtest.event_backtest` 的 legacy 分支      全域 `config.TREND_GUARD_ENABLED`
  3. `screener.screen()`                            同上(live 選股路徑)

(1) 於 2026-08-16 改成 per-strategy 參數 `trend_guard`(進 rules hash);
(2) 於 2026-08-17 搬進 `factor_engine.legacy_factors.legacy_selection()`。

這份測試釘住搬完之後的狀態,防止它再長回引擎裡。真正要擋的失敗模式是:
**兩次 `strategy_rule_hash` 相同的 run,因為某個全域旗標而買到不同的股票** ——
那會讓凍結與 forward 的證據等級失去意義,而且不會報錯。
"""
from __future__ import annotations

import ast
import pathlib
import unittest

import factor_engine.legacy_factors as legacy

ENGINE = pathlib.Path(__file__).resolve().parents[1] / "backtest" / "event_backtest.py"


class EngineHasNoTrendOpinion(unittest.TestCase):

    def test_engine_source_does_not_filter_on_trend_ok(self):
        """引擎原始碼裡不得有任何以 trend_ok 為條件的過濾。

        用 AST 而不是字串搜尋:註解與 docstring 提到 trend_ok 是**應該**的
        (要解釋為什麼它不在這裡),被禁止的是真的拿它當運算元。
        """
        tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            # panel["trend_ok"] / df.trend_ok 這類取值
            hit = ((isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "trend_ok")
                   or (isinstance(node, ast.Attribute)
                       and node.attr == "trend_ok"))
            if hit:
                offenders.append(getattr(node, "lineno", "?"))
        self.assertEqual(
            offenders, [],
            f"event_backtest.py 第 {offenders} 行又出現 trend_ok 運算。"
            "趨勢是策略看法,不是引擎規則 —— 要用請放進策略層"
            "(signal_builder 的 trend_guard 參數,或 legacy_factors.legacy_selection)")

    def test_engine_does_not_read_the_global_trend_flag_for_filtering(self):
        """`config.TREND_GUARD_ENABLED` 只能被當成**傳給策略層的值**,不能直接當條件。

        允許:`factors.legacy_selection(..., trend_guard=config.TREND_GUARD_ENABLED)`
        禁止:`if config.TREND_GUARD_ENABLED:` —— 那就是引擎自己在決定。
        """
        tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp)):
                continue
            for sub in ast.walk(node.test):
                if isinstance(sub, ast.Attribute) and sub.attr == "TREND_GUARD_ENABLED":
                    bad.append(node.lineno)
        self.assertEqual(
            bad, [],
            f"event_backtest.py 第 {bad} 行拿 TREND_GUARD_ENABLED 當分支條件。"
            "全域旗標決定買什麼 = 相同 strategy_rule_hash 可以買到不同股票")

    def test_the_rule_still_exists_and_still_works_on_the_legacy_side(self):
        """搬家不等於刪功能:legacy 九因子的規則要原封不動地還在。"""
        import pandas as pd
        panel = pd.DataFrame({
            "stock_id": ["A", "B", "C", "D"],
            "composite": [90.0, 90.0, 10.0, 10.0],
            "trend_ok": [True, False, True, False],
        })
        on = legacy.legacy_selection(panel, min_composite=50, trend_guard=True)
        off = legacy.legacy_selection(panel, min_composite=50, trend_guard=False)
        self.assertEqual(list(on["stock_id"]), ["A"])
        self.assertEqual(list(off["stock_id"]), ["A", "B"])

    def test_missing_column_does_not_silently_drop_the_guard_for_legacy(self):
        """legacy 路徑刻意保留「缺欄位就略過」的舊行為,但要有意識地保留。

        與 `signal_builder` 的新規則不同(那邊缺欄位是 fail-closed)。差別的
        理由:legacy 策略已是 `rejected` 的偏誤對照組,改變它的行為只會讓舊
        對照數字對不上;新假說才需要嚴格保證「宣告了就一定套上」。
        """
        import pandas as pd
        panel = pd.DataFrame({"stock_id": ["A"], "composite": [90.0]})
        out = legacy.legacy_selection(panel, min_composite=50, trend_guard=True)
        self.assertEqual(list(out["stock_id"]), ["A"])


if __name__ == "__main__":
    unittest.main()
