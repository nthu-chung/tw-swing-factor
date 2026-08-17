# -*- coding: utf-8 -*-
"""研究層的 request 契約:`CandidateSpec` / `EvaluationProtocol` / `BacktestRequest`。

兩層 identity(研究規格 §8.2)——**不得只做一個混合 hash**:

  `strategy_rule_hash`   凍結的是哪一套交易規則(策略 + 參數 + 投組 + 退出 +
                         universe 規則)。同一套規則推進資料做 forward 時,
                         這個 hash **必須不變**,否則 forward 永遠累積不起來。
  `evaluation_run_hash`  這套規則在哪一次實驗中如何被評估(資料快照、切割、
                         phase、成本、seed、evaluator 版本)。

混成一個 hash 的後果:推進快照就換 hash,forward 斷掉;或是不同 fold 的
metrics 共用同一個 id,campaign 無法歸因。兩者都會讓「這個數字屬於哪一套規則」
變成不可回答的問題。

`BacktestRequest` 是 frozen 的,而且 runner 自己擁有的安全欄位
(`REQUEST_OWNED_KEYS`)**不接受**任何 `engine_kwargs` 覆寫 —— 那些欄位正是
PIT provider、資金情境、評估窗與 provenance 的來源,能被一個 dict 靜默蓋掉的話,
前面所有閘門都等於沒有。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

import config

# runner 擁有、不得由呼叫端 kwargs 覆寫的欄位。
# `segment` 在這裡的理由和其他欄位一樣但更直接:它決定這次結果被記成 IS 還是
# OS。能被 kwargs 蓋掉的話,一份 IS run 可以自稱 OS(或反過來),而 holdout
# 揭露紀錄記的就是這個欄位 —— 整個單次揭露的紀律會建立在一個可以被覆寫的字串上。
REQUEST_OWNED_KEYS: Tuple[str, ...] = (
    "signal_frame", "strategy_position_policy", "symbols", "universe_provider",
    "sample", "dynamic_enabled", "initial_capital", "order_size_mode",
    "minimum_commission", "start_date", "end_date", "picks_by_date",
    "segment",
)


def _stable_hash(payload: Mapping[str, Any], *, length: int = 16) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str,
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:length]


@dataclass(frozen=True)
class CandidateSpec:
    """「要驗證的是哪一套規則」。只有這裡的東西可以被搜尋器變動(§8.3)。"""

    strategy_id: str
    strategy_version: str
    signal_params: Mapping[str, Any] = field(default_factory=dict)
    portfolio_params: Mapping[str, Any] = field(default_factory=dict)
    exit_params: Mapping[str, Any] = field(default_factory=dict)
    universe_rule: str = "monthly_pit_dynamic_topn"
    eligibility_rule_id: str = "unspecified"
    code_fingerprint: str = ""

    def __post_init__(self) -> None:
        # defensive copy:frozen dataclass 只擋「重新指派欄位」,擋不住呼叫端
        # 拿著原本那個 dict 繼續改。參數在 run 開始後被改掉的話,manifest 記的
        # 是一組參數、實際跑的是另一組,而 rule hash 早就算完了。
        # 這裡刻意只做一層 dict 複製(§7:不建立 deep-immutable framework)——
        # 參數值是純量,一層就夠;真有巢狀結構,canonical serialization 也會把
        # 差異反映在 hash 上。
        for field_name in ("signal_params", "portfolio_params", "exit_params"):
            object.__setattr__(self, field_name,
                               dict(getattr(self, field_name) or {}))

    def rules(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "signal_params": dict(self.signal_params),
            "portfolio_params": dict(self.portfolio_params),
            "exit_params": dict(self.exit_params),
            "universe_rule": self.universe_rule,
            "eligibility_rule_id": self.eligibility_rule_id,
            "code_fingerprint": self.code_fingerprint,
        }

    def strategy_rule_hash(self) -> str:
        """**不含**資料快照、fold 與 evaluator 版本(§8.2)。"""
        return _stable_hash(self.rules())


@dataclass(frozen=True)
class EvaluationProtocol:
    """「這一次要怎麼評估」。campaign 內固定,**不得成為 genome**(§8.3)。"""

    data_snapshot: str = ""
    price_dataset: str = ""
    adjustment_anchor: str = ""
    fixture: str = "synthetic"
    capital_scenario: str = "research"
    initial_capital: float = 0.0
    order_size_mode: str = ""
    minimum_commission: float = 0.0
    phases: int = 5
    decision_frequency: str = "weekly"
    benchmark: str = "daily_equal_weight_rebalanced_eligible"
    # 名稱要描述真正的算法(§3.3):這是每日對當日 eligible 母體等權
    # 再平衡,不是買進持有。兩者在成分變動時會分岔,叫錯名字會讓人
    # 以為超額報酬是拿 buy-and-hold 當基礎算的。
    return_convention: str = "unspecified"
    seed: int = 0
    evaluator_version: str = "golden_path_v1"
    segment: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BacktestRequest:
    """immutable:candidate + protocol + runner 擁有的執行參數。"""

    candidate: CandidateSpec
    protocol: EvaluationProtocol
    engine_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clashes = sorted(set(self.engine_kwargs) & set(REQUEST_OWNED_KEYS))
        if clashes:
            raise ValueError(
                f"[fail-closed] engine_kwargs 想覆寫 runner 擁有的安全欄位 "
                f"{clashes}。這些欄位是 PIT provider、資金情境、評估窗與 "
                "provenance 的來源,能被 kwargs 靜默蓋掉的話,前面所有閘門都失效")

    def strategy_rule_hash(self) -> str:
        return self.candidate.strategy_rule_hash()

    def evaluation_run_hash(self) -> str:
        """含 rule hash + 這次評估協定;同規則不同快照/fold → 不同 run hash。"""
        return _stable_hash({
            "strategy_rule_hash": self.strategy_rule_hash(),
            "protocol": self.protocol.to_dict(),
        })

    def manifest(self) -> Dict[str, Any]:
        # 五項必要 provenance(goal §3.4)全部在這一份 manifest 裡:
        #   1. strategy_id / strategy_version   → 下面兩行
        #   2. 完整 effective parameters        → candidate.rules()
        #   3. git_commit + dirty state         → provenance 區塊
        #   4. 快照 / 資料集 / 還原錨            → protocol + config_snapshot
        #   5. segment 與日期                    → protocol.segment(+ audit 的
        #                                          segment_boundary)
        import provenance                                   # 延後 import:
        # contracts 是純資料層,不希望 import 它就順帶跑 git 子行程。
        git = provenance.git_state()
        return {
            "strategy_id": self.candidate.strategy_id,
            "strategy_version": self.candidate.strategy_version,
            "strategy_rule_hash": self.strategy_rule_hash(),
            "evaluation_run_hash": self.evaluation_run_hash(),
            "candidate": self.candidate.rules(),
            "protocol": self.protocol.to_dict(),
            "engine_kwargs": dict(self.engine_kwargs),
            "fixture": self.protocol.fixture,
            "capital_scenario": self.protocol.capital_scenario,
            "provenance": dict(git),
            # 工作樹 dirty = 這份結果對不到任何 commit。放在頂層是因為它是
            # 「這個數字能不能被重現」的第一個問題,不該埋在巢狀 dict 裡。
            "git_commit": git.get("git_commit"),
            "git_dirty": git.get("git_dirty"),
            "config_snapshot": {
                "SNAPSHOT_END_DATE": getattr(config, "SNAPSHOT_END_DATE", ""),
                "PRICE_DATASET": getattr(config, "PRICE_DATASET", ""),
                "PRICE_ADJUST_ANCHOR": getattr(config, "PRICE_ADJUST_ANCHOR", ""),
                "SELF_ADJUST_PRICES": bool(getattr(config, "SELF_ADJUST_PRICES", False)),
            },
        }
