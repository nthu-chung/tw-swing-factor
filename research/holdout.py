# -*- coding: utf-8 -*-
"""單次 IS / embargo / locked-OS 的資料閘門(見 `research/docs/` 的兩份規格)。

這一層要保證的不是「CLI 跑得完」,而是:

  **研究程序根本沒有取得 locked OS**,而且事件引擎與 artifacts 都沒有越過
  當前 segment 的邊界。

三個入口刻意分開,因為它們的授權層級不同:

  `research_run()`     研究:只能建立並傳入 `[warmup_start, is_end]`
  `freeze_candidate()` 凍結:把 strategy rule 固定成一個 hash
  `reveal_locked_os()` 揭露:需要**獨立的 owner 授權**,一般 `mode="os"` 不等於授權

誠實聲明(對齊 `EVALUATION_DATA_BOUNDARY_SPEC.md` §2.2):這是**程序性**閘門,
不是物理沙盒。它擋的是「IS 研究流程偷看 locked OS」,不是「任意 Python 在 IS 內
寫 `shift(-1)`」——後者靠因果算子、prefix-invariance 測試與 code review。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from evaluation.holdout import record_reveal, reveal_status
from evaluation.splits import EvaluationSplit, build_evaluation_split

SEGMENT_IS = "IS"
SEGMENT_OS = "OS"

# owner 的獨立授權字串。刻意不是 bool,也刻意不叫 mode —— 規格 §4.4:
# 「一般 run(mode="os") 不得等價於授權」。要打錯字很難、要不小心傳到更難。
REVEAL_AUTHORIZATION = "owner-authorized-single-holdout-reveal"


class HoldoutBoundaryError(RuntimeError):
    """違反 single-holdout 資料邊界;一律 fail-closed,不得降級成 warning。"""


def _stable_hash(payload: Mapping[str, Any], *, length: int = 16) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str,
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:length]


@dataclass(frozen=True)
class SingleHoldoutProtocol:
    """固定的 IS / embargo / OS 邊界與評估協定(三種 freeze 的第二種)。

    這些欄位**不得**由 strategy params 或 engine_kwargs 覆寫(規格 §2.1 末段):
    能改考卷的人不能同時是考生。
    """

    snapshot: str
    is_start: str
    is_end: str
    os_start: str
    os_end: str
    embargo_trading_days: int
    warmup_bars: int
    phases: int
    benchmark: str
    capital_scenario: str
    initial_capital: float
    order_size_mode: str
    minimum_commission: float
    segment_end_policy: str = "mtm_at_segment_end_no_next_segment_price"
    split_mode: str = ""
    evaluator_version: str = "single_holdout_v1"

    @classmethod
    def from_dates(cls, dates: Sequence, *, snapshot: str, warmup_bars: int,
                   phases: int, capital_scenario: str, initial_capital: float,
                   order_size_mode: str, minimum_commission: float,
                   benchmark: str = "daily_equal_weight_rebalanced_eligible",
                   minimum_embargo_days: int = 0,
                   **split_kwargs) -> "SingleHoldoutProtocol":
        """用 `evaluation/splits.py` 建切割 —— **不另寫 split**(goal 明文要求)。"""
        split: EvaluationSplit = build_evaluation_split(
            dates, minimum_embargo_days=minimum_embargo_days, **split_kwargs)
        return cls(
            snapshot=str(snapshot),
            is_start=str(pd.Timestamp(split.is_start).date()),
            is_end=str(pd.Timestamp(split.is_end).date()),
            os_start=str(pd.Timestamp(split.os_start).date()),
            os_end=str(pd.Timestamp(split.os_end).date()),
            embargo_trading_days=int(split.n_embargo),
            warmup_bars=int(warmup_bars), phases=int(phases),
            benchmark=benchmark, capital_scenario=capital_scenario,
            initial_capital=float(initial_capital),
            order_size_mode=str(order_size_mode),
            minimum_commission=float(minimum_commission),
            split_mode=str(split.mode),
        )

    def __post_init__(self) -> None:
        order = [self.is_start, self.is_end, self.os_start, self.os_end]
        if any(pd.Timestamp(a) > pd.Timestamp(b)
               for a, b in zip(order, order[1:])):
            raise HoldoutBoundaryError(
                f"[fail-closed] 切割日期順序不合法:{order}")
        if int(self.embargo_trading_days) < 0:
            raise HoldoutBoundaryError("embargo 不得為負")
        if int(self.phases) < 1:
            raise HoldoutBoundaryError("phases 至少為 1")

    def protocol_hash(self) -> str:
        return _stable_hash(asdict(self))

    def window(self, segment: str) -> Tuple[str, str]:
        """該 segment 允許**載入**的資料窗(含因果 warmup)。"""
        if segment == SEGMENT_IS:
            start = (pd.Timestamp(self.is_start)
                     - pd.tseries.offsets.BDay(int(self.warmup_bars) + 5))
            return (str(start.date()), self.is_end)
        if segment == SEGMENT_OS:
            start = (pd.Timestamp(self.os_start)
                     - pd.tseries.offsets.BDay(int(self.warmup_bars) + 5))
            return (str(start.date()), self.os_end)
        raise HoldoutBoundaryError(
            f"[fail-closed] 未知 segment={segment!r};只接受 {SEGMENT_IS}/{SEGMENT_OS}")

    def scoring_window(self, segment: str) -> Tuple[str, str]:
        """該 segment 允許**計分**的窗(比載入窗窄:warmup 不計分)。"""
        if segment == SEGMENT_IS:
            return (self.is_start, self.is_end)
        if segment == SEGMENT_OS:
            return (self.os_start, self.os_end)
        raise HoldoutBoundaryError(f"[fail-closed] 未知 segment={segment!r}")

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["protocol_hash"] = self.protocol_hash()
        return out


# ── segment 邊界稽核 ──────────────────────────────────────────────────────
_DATE_COLUMNS = ("date", "exit_date", "entry_date", "signal_date")


def _frame_max_date(frame) -> Optional[pd.Timestamp]:
    if frame is None or len(frame) == 0:
        return None
    df = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    best: Optional[pd.Timestamp] = None
    for col in _DATE_COLUMNS:
        if col not in df.columns:
            continue
        vals = pd.to_datetime(df[col], errors="coerce").dropna()
        if len(vals):
            top = vals.max()
            best = top if best is None else max(best, top)
    return best


def boundary_audit(*, protocol: SingleHoldoutProtocol, segment: str,
                   panel_input: Tuple[Any, Any],
                   tables: Mapping[str, Any]) -> Dict[str, Any]:
    """記錄策略實際 input 範圍與每一張輸出表的最大日期,並檢查沒有越界。

    規格 §6 要求「artifact 必須記錄 strategy 實際 input 日期範圍與所有事件輸出
    的最大日期」—— 因為「輸出看起來沒越界」與「資料根本沒進來」是兩件事,
    只有前者的話,任何裁切 bug 都會偽裝成合規。
    """
    load_start, load_end = protocol.window(segment)
    score_start, score_end = protocol.scoring_window(segment)
    limit = pd.Timestamp(score_end)

    per_table: Dict[str, Optional[str]] = {}
    violations: List[str] = []
    for name, frame in tables.items():
        top = _frame_max_date(frame)
        per_table[name] = None if top is None else str(top.date())
        if top is not None and top > limit:
            violations.append(
                f"{name} 的最大日期 {top.date()} 越過 segment 結尾 {limit.date()}")

    in_min, in_max = panel_input
    if in_max is not None and pd.Timestamp(in_max) > pd.Timestamp(load_end):
        violations.append(
            f"strategy input 最大日期 {pd.Timestamp(in_max).date()} 越過"
            f"允許載入窗 {load_end}")
    if in_min is not None and pd.Timestamp(in_min) < pd.Timestamp(load_start):
        violations.append(
            f"strategy input 最小日期 {pd.Timestamp(in_min).date()} 早於"
            f"允許載入窗 {load_start}")

    return {
        "segment": segment,
        "load_window": [load_start, load_end],
        "scoring_window": [score_start, score_end],
        "strategy_input_min": (None if in_min is None
                               else str(pd.Timestamp(in_min).date())),
        "strategy_input_max": (None if in_max is None
                               else str(pd.Timestamp(in_max).date())),
        "output_max_dates": per_table,
        "violations": violations,
        "within_segment": not violations,
    }


def assert_within_segment(audit: Mapping[str, Any]) -> None:
    if not audit.get("within_segment"):
        raise HoldoutBoundaryError(
            "[fail-closed] 輸出越過 segment 邊界:\n  - "
            + "\n  - ".join(audit.get("violations") or []))


# ── OS 揭露授權 ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FrozenCandidate:
    """凍結的策略規則(三種 freeze 的第三種)。"""

    strategy_id: str
    strategy_rule_hash: str
    frozen_at: str
    protocol_hash: str
    manifest_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def authorize_reveal(token: str) -> None:
    """檢查 owner 的獨立授權。`mode="os"` 這種一般參數**不等於**授權。"""
    if token != REVEAL_AUTHORIZATION:
        raise HoldoutBoundaryError(
            "[fail-closed] locked OS 需要 owner 的獨立授權。"
            f"預期 authorization={REVEAL_AUTHORIZATION!r};"
            "一般的 mode/segment 參數不構成授權(規格 §4.4)")


def assert_rule_unchanged(frozen: FrozenCandidate, current_hash: str) -> None:
    if str(frozen.strategy_rule_hash) != str(current_hash):
        raise HoldoutBoundaryError(
            "[fail-closed] OS run 的 strategy_rule_hash 與凍結時不同 "
            f"({current_hash} != {frozen.strategy_rule_hash})。"
            "看完 OS 回頭改規則正是這條閘門要擋的事(規格 §7)")


def os_reveal_status(*, strategy_rule_hash: str, protocol: SingleHoldoutProtocol,
                     strategy_id: str = "") -> Dict[str, Any]:
    return reveal_status(strategy_hash=strategy_rule_hash,
                         strategy_name=strategy_id or None,
                         os_start=protocol.os_start, os_end=protocol.os_end)


def record_os_reveal(*, strategy_rule_hash: str, strategy_id: str,
                     protocol: SingleHoldoutProtocol, source: str,
                     manifest: Optional[str] = None,
                     now=None, path=None) -> Dict[str, Any]:
    """把這次揭露 append 進 append-only 台帳(第二次會被標 previously_seen)。"""
    return record_reveal(
        strategy_hash=strategy_rule_hash, strategy_name=strategy_id,
        os_start=protocol.os_start, os_end=protocol.os_end,
        source=source, segment=SEGMENT_OS, manifest=manifest,
        is_window=[protocol.is_start, protocol.is_end],
        embargo_trading_days=int(protocol.embargo_trading_days),
        split_mode=protocol.split_mode,
        context={"protocol_hash": protocol.protocol_hash(),
                 "evaluator_version": protocol.evaluator_version},
        now=now, path=path)


# ── 兩個分開的入口(授權層級不同)────────────────────────────────────────
def research_run(*, strategy_id: str, protocol: SingleHoldoutProtocol,
                 output_dir, fixture_name: str = "synthetic",
                 stamp: str = "is", **kwargs):
    """研究入口:**只能**跑 IS。這裡沒有任何參數可以要到 OS 資料。

    OS 之所以在 research mode 建不出來,不是因為被檢查擋掉,而是因為
    `protocol.window(SEGMENT_IS)` 根本不會回傳 OS 的日期 —— 資料窗在建 panel
    之前就被決定了,`reveal_locked_os()` 是唯一會傳 `SEGMENT_OS` 的地方。
    """
    from research.golden_path import run_golden_path

    return run_golden_path(
        strategy_id=strategy_id, fixture_name=fixture_name,
        capital=protocol.capital_scenario, output_dir=output_dir,
        stamp=stamp, holdout_protocol=protocol, segment=SEGMENT_IS, **kwargs)


def freeze_candidate(*, strategy_id: str, strategy_rule_hash: str,
                     protocol: SingleHoldoutProtocol, frozen_at: str,
                     manifest_path: str = "") -> FrozenCandidate:
    """把 strategy rule 凍結成一個 hash。揭露 OS 前必須先有這個。"""
    return FrozenCandidate(
        strategy_id=strategy_id, strategy_rule_hash=str(strategy_rule_hash),
        frozen_at=str(frozen_at), protocol_hash=protocol.protocol_hash(),
        manifest_path=str(manifest_path))


def reveal_locked_os(*, strategy_id: str, protocol: SingleHoldoutProtocol,
                     frozen: Optional[FrozenCandidate], authorization: str,
                     output_dir, fixture_name: str = "synthetic",
                     stamp: str = "os", now=None, ledger_path=None, **kwargs):
    """揭露入口:需要 owner 獨立授權 + 已凍結的 rule + 台帳。

    順序刻意是「先擋、後跑」:授權與凍結檢查都在建立任何 OS panel **之前**,
    所以未授權的呼叫連 OS 資料都不會被載入(規格 §8.5)。
    """
    authorize_reveal(authorization)
    if frozen is None:
        raise HoldoutBoundaryError(
            "[fail-closed] 揭露 locked OS 前必須先凍結 strategy rule"
            "(freeze_candidate);未凍結就等於還能回頭改規則")
    if frozen.protocol_hash != protocol.protocol_hash():
        raise HoldoutBoundaryError(
            "[fail-closed] protocol 與凍結時不同:換考卷等於重新選一次切割")

    from research.golden_path import run_golden_path

    result = run_golden_path(
        strategy_id=strategy_id, fixture_name=fixture_name,
        capital=protocol.capital_scenario, output_dir=output_dir,
        stamp=stamp, holdout_protocol=protocol, segment=SEGMENT_OS, **kwargs)

    assert_rule_unchanged(frozen, result.manifest["strategy_rule_hash"])
    ledger = record_os_reveal(
        strategy_rule_hash=frozen.strategy_rule_hash, strategy_id=strategy_id,
        protocol=protocol, source="research.holdout.reveal_locked_os",
        manifest=result.run_dir, now=now, path=ledger_path)
    result.audit["os_reveal"] = ledger
    result.audit["frozen_candidate"] = frozen.to_dict()
    from research import artifacts
    artifacts.write_json(
        artifacts.RunDirectory(path=__import__("pathlib").Path(result.run_dir),
                               run_id=""), "audit", result.audit)
    return result
