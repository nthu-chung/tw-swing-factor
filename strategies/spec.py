# -*- coding: utf-8 -*-
"""可凍結的策略規格(`StrategySpec`)。

為什麼要有這一層
----------------
S19 真正決定績效的參數(`ts_ir` 視窗、訊號權重、持股數、再平衡天數、MA 出場、
硬停損)原本是**模組常數**,而且投組那半是在 manifest 產生**之後**才由
`_apply_portfolio_config()` 臨時寫進 `config` —— 於是 `freeze_manifest.py`
一個都沒凍到。實測後果:兩份 `rules_sha256_16` 相同的 manifest 可以對應到
完全不同的策略(改 `PORT_MAX_POSITIONS` 10→3、`PORT_REBALANCE_DAYS` 20→5,
hash 一個字都不會變),而 `forward_test.py` 又是吃 `backtest_portfolio` 的簽章
預設 `rebalance_every=5 / top_n=3` 在跑 —— forward 驗證的規則跟凍結的規則
根本是兩套。

把這些參數收進 frozen dataclass 之後:
  1. manifest 有明確、完整的東西可以凍(進 `rules["strategy"]`,也進 hash)。
  2. forward 能把凍結值原封套回策略,而不是靠引擎預設值。
  3. 改任一參數 → hash 一定變 → 舊 forward 紀錄不會被冒用。

設計上刻意**不把 label 放進規格**:label 只是人給的名字,同一組規則換 label
不該產生新的 hash(否則「同規則兩次凍結」會被誤認為兩套規則)。label 只進
檔名與 manifest 的 metadata。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Dict, Mapping, Tuple

# 策略名 → 模組路徑。manifest 只存**名字**,由這張表解析成模組。
# 不讓 manifest 直接夾帶 import 路徑:manifest 是會被讀進正式驗證路徑的檔案,
# 讓它決定 import 什麼等於把任意程式碼執行權交給一份 JSON。
KNOWN_STRATEGIES: Dict[str, str] = {
    "s19_chip_momentum": "strategies.s19_chip_momentum",
}

# forward 正式驗證路徑會呼叫的策略介面。策略模組缺任何一個就不該進 forward
# (缺 `evaluate` = 沒有相位掃描、缺 `equal_weight_baseline` = 沒有基準)。
STRATEGY_PROTOCOL: Tuple[str, ...] = (
    "SPEC", "build_panel", "evaluate", "equal_weight_baseline",
)


@dataclass(frozen=True)
class StrategySpec:
    """一個策略單元的**全部**可調參數(訊號 + 投組),可 hash、可序列化。

    `signal` / `portfolio` 分開放,是因為兩者的失效方式不同:訊號參數變了是
    換了一個 alpha;投組參數變了是同一個 alpha 換了執行方式。兩者都會改變
    績效,所以兩者都必須進 hash。

    `required_*` 是**該策略自己宣告**的必要 key。從 manifest 反序列化時會再檢查
    一次:少了任何一個就 raise,而不是拿預設值頂替(拿預設值頂替 = forward 用
    的參數跟凍結的不同,正是這次要修的 bug)。
    """

    name: str
    signal: Mapping[str, Any]
    portfolio: Mapping[str, Any]
    required_signal: Tuple[str, ...] = ()
    required_portfolio: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in KNOWN_STRATEGIES:
            raise ValueError(
                f"未知策略 {self.name!r};要新增請註冊進 spec.KNOWN_STRATEGIES"
                f"(現有:{sorted(KNOWN_STRATEGIES)})"
            )
        missing_s = [k for k in self.required_signal if k not in self.signal]
        missing_p = [k for k in self.required_portfolio if k not in self.portfolio]
        if missing_s or missing_p:
            raise ValueError(
                f"[fail-closed] {self.name} 規格不完整:缺訊號參數 {missing_s}、"
                f"投組參數 {missing_p};拒絕用預設值頂替(那會讓 forward 跑的"
                "規則與凍結的規則不同)"
            )
        # 存進來的 dict 另外複製一份:呼叫端事後改自己那份 dict 不會偷偷改動
        # 已經算過 hash 的規格(dataclass 的 frozen 只擋 rebind,不擋 mutate)。
        object.__setattr__(self, "signal", dict(self.signal))
        object.__setattr__(self, "portfolio", dict(self.portfolio))

    # ── 取值(缺 key 一律 fail-closed,不給預設值)──────────────────────
    def sig(self, key: str) -> Any:
        if key not in self.signal:
            raise KeyError(f"{self.name} 訊號參數缺 {key!r}(凍結規格不完整)")
        return self.signal[key]

    def port(self, key: str) -> Any:
        if key not in self.portfolio:
            raise KeyError(f"{self.name} 投組參數缺 {key!r}(凍結規格不完整)")
        return self.portfolio[key]

    # ── 序列化 ────────────────────────────────────────────────────────
    def rules(self) -> Dict[str, Any]:
        """進 manifest 與 rules hash 的內容(**不含** label)。"""
        return {
            "name": self.name,
            "signal": dict(sorted(self.signal.items())),
            "portfolio": dict(sorted(self.portfolio.items())),
        }

    def to_dict(self) -> Dict[str, Any]:
        d = self.rules()
        d["required_signal"] = list(self.required_signal)
        d["required_portfolio"] = list(self.required_portfolio)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StrategySpec":
        """從 manifest 還原。必要 key 以**策略模組當下宣告的**為準。

        為什麼不信 manifest 裡的 `required_*`:那是舊 manifest 寫下的期望值,
        若今天的策略多了一個 load-bearing 參數,舊 manifest 就是**不完整**的,
        必須被擋下來(不得冒充可靠凍結版本),而不是用它自己較寬鬆的清單放行。
        """
        unknown = set(d) - {"name", "signal", "portfolio",
                            "required_signal", "required_portfolio"}
        if unknown:
            raise ValueError(f"manifest strategy 段有未知欄位:{sorted(unknown)}")
        name = d.get("name")
        if not name:
            raise ValueError("manifest strategy 段缺 name")
        current = load_spec(str(name))
        return cls(
            name=str(name),
            signal=dict(d.get("signal") or {}),
            portfolio=dict(d.get("portfolio") or {}),
            required_signal=tuple(current.required_signal),
            required_portfolio=tuple(current.required_portfolio),
        )

    def replace(self, *, signal: Mapping[str, Any] | None = None,
                portfolio: Mapping[str, Any] | None = None) -> "StrategySpec":
        """產生改了某些參數的新規格(給參數研究用;改了就是新規則、新 hash)。"""
        return StrategySpec(
            name=self.name,
            signal={**self.signal, **(signal or {})},
            portfolio={**self.portfolio, **(portfolio or {})},
            required_signal=self.required_signal,
            required_portfolio=self.required_portfolio,
        )

    def module(self) -> ModuleType:
        return load_strategy_module(self.name)


def load_strategy_module(name: str) -> ModuleType:
    """依註冊名 import 策略模組,並檢查它有 forward 需要的介面。"""
    if name not in KNOWN_STRATEGIES:
        raise ValueError(
            f"未知策略 {name!r};forward 只接受註冊過的策略"
            f"(現有:{sorted(KNOWN_STRATEGIES)})"
        )
    mod = importlib.import_module(KNOWN_STRATEGIES[name])
    missing = [a for a in STRATEGY_PROTOCOL if not hasattr(mod, a)]
    if missing:
        raise AttributeError(
            f"[fail-closed] 策略 {name} 缺 forward 需要的介面 {missing};"
            "沒有相位掃描或基準的策略不得走正式 forward 驗證"
        )
    return mod


def load_spec(name: str) -> StrategySpec:
    """取得策略**目前**的預設規格(freeze 時凍的就是這個)。"""
    spec = getattr(load_strategy_module(name), "SPEC")
    if not isinstance(spec, StrategySpec):
        raise TypeError(f"{name}.SPEC 必須是 StrategySpec")
    return spec
