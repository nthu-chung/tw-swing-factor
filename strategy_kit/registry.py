# -*- coding: utf-8 -*-
"""allowlisted 策略註冊表:`strategy_id` → factory。

為什麼是 allowlist 而不是「manifest 裡寫 import path」:研究規格 §5.5 明訂
「plugin 必須從 repo 內的策略 registry 解析,不接受 JSON 直接傳任意 Python
path」。一份 JSON 若能決定正式驗證流程要 import 什麼,凍結的 manifest 就不再
描述一套固定規則 —— 換一個 import path 就換了策略,而 rules hash 完全看不出來。

新增策略 = 在這裡註冊一行。註冊本身不代表任何證據等級;`evidence_status`
只是把策略自己的宣告帶出來,讓 runner 能把 fixture 與正式策略分開對待。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

_REGISTRY: Dict[str, Callable[[], Any]] = {}


def register(strategy_id: str, factory: Callable[[], Any]) -> None:
    if not strategy_id or not isinstance(strategy_id, str):
        raise ValueError("strategy_id 必須是非空字串")
    if strategy_id in _REGISTRY:
        raise ValueError(f"strategy_id 重複註冊:{strategy_id}")
    _REGISTRY[strategy_id] = factory


def available() -> List[str]:
    return sorted(_REGISTRY)


def resolve(strategy_id: str):
    """取得策略實例;未註冊一律 fail-closed(不猜、不動態 import)。"""
    if strategy_id not in _REGISTRY:
        raise KeyError(
            f"[fail-closed] 未註冊的 strategy_id={strategy_id!r}。"
            f"可用:{available()}。正式入口只從 registry 解析,"
            "不接受任意 import path(研究規格 §5.5)")
    strategy = _REGISTRY[strategy_id]()
    for attr in ("name", "version", "make_signals", "data_requirements",
                 "default_parameters"):
        if not hasattr(strategy, attr):
            raise TypeError(
                f"[fail-closed] {strategy_id} 缺少策略介面成員 {attr!r}")
    if str(getattr(strategy, "name", "")) != strategy_id:
        raise ValueError(
            f"[fail-closed] {strategy_id} 的 strategy.name="
            f"{getattr(strategy, 'name', None)!r} 與註冊 id 不一致;"
            "兩者必須相同,否則 manifest 記的策略與實際跑的不是同一個")
    return strategy


def evidence_status(strategy_id: str) -> str:
    """策略自己宣告的證據狀態(fixture 與正式策略要分得開)。"""
    return str(getattr(resolve(strategy_id), "evidence_status", "unspecified"))


def _register_builtin() -> None:
    """逐檔顯式註冊。

    刻意**不**自動掃描 `strategies/` 目錄:allowlist 的意義就是「有人明確決定
    這支可以被正式流程跑」。自動掃描等於把「放一個檔案進資料夾」變成註冊動作,
    那和 §5.5 禁止的「JSON 指定任意 import path」是同一個問題的兩種寫法。

    每一行的 module 路徑就是那支策略的檔案位置,一眼看得出對應關係。
    """
    # s19:legacy 籌碼×風險調整動能。台帳 `blocked`(IS 20 相位只有 3/20 勝過
    # 被動基準),留在公開是因為它同時是**平台的管線驗收載體** —— 9 份測試靠它
    # 證明 make_signals → validator → 五相位 → 事件引擎 → artifacts 這條鏈是通的。
    from strategies.s19_reference import S19ReferenceStrategy
    register("s19_reference_make_signals", S19ReferenceStrategy)

    # 假說策略在下一個 commit 加入。這裡先只有管線驗收載體 —— registry 是
    # allowlist,新增策略 = 明確加一行,不自動掃描目錄。


_register_builtin()
