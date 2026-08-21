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
    """凍結的策略規則(三種 freeze 的第三種)。

    除了 hash,還要記住**規則本體**(`rules`,即 IS manifest 的 `candidate` 區塊)。
    只存 hash 的話,揭露前根本無法重算 hash —— 因為重算需要
    `eligibility_rule_id`,而它只有跑過訊號才知道。存了本體,揭露前就能用
    凍結時的 eligibility id 把 hash 算出來,把規則閘門移到載入 OS 之前。
    """

    strategy_id: str
    strategy_rule_hash: str
    frozen_at: str
    protocol_hash: str
    manifest_path: str = ""
    # 來自 IS manifest["candidate"](= CandidateSpec.rules())。
    rules: Dict[str, Any] = field(default_factory=dict)

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


def precompute_strategy_rule_hash(*, strategy_id: str,
                                  params: Optional[Mapping[str, Any]] = None,
                                  policy=None,
                                  eligibility_rule_id: str) -> str:
    """在**跑任何 run 之前**算出這次會得到的 `strategy_rule_hash`。

    可行的理由見 `golden_path.build_candidate_spec()`:規則身分的七個欄位全是
    宣告性的,與資料窗、fixture、快照都無關(改 is_ratio 或換 fixture,hash 不
    變 —— 那些只進 `evaluation_run_hash`)。所以「規則有沒有被改過」不需要先
    把 OS 算完才能回答。
    """
    from research.golden_path import build_candidate_spec

    return build_candidate_spec(
        strategy_id=strategy_id, params=params,
        policy_spec=(policy.spec if policy is not None else None),
        eligibility_rule_id=eligibility_rule_id).strategy_rule_hash()


def preflight_ledger(path=None) -> int:
    """在載入 OS **之前**驗證揭露紀錄可讀(鏈 + 指紋)且可寫,回傳現有列數。

    「看過 OS 但寫不進紀錄」是最糟的失敗狀態:資料已經消耗掉,帳上卻沒有痕跡,
    只能靠人事後憑記憶補登(`outputs/holdout_ledger.jsonl` 第 3 列的
    `manual_backfill_after_rule_hash_gate_failed_post_run` 就是這樣來的)。
    鏈壞掉、指紋對不上、目錄不可寫,這些全都是 run 開始前就已成立的既有狀態,
    因此可以、也必須在載入 OS 之前就驗一次。
    """
    import os as _os

    from evaluation.holdout import ledger_path as _ledger_path
    from evaluation.holdout import read_ledger

    rows = read_ledger(path)          # 鏈/指紋壞掉在這裡 fail-closed
    p = _ledger_path(path)
    if not p.exists():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HoldoutBoundaryError(
                f"[fail-closed] 揭露紀錄目錄建不起來:{exc}") from exc
    target = p if p.exists() else p.parent
    if not _os.access(target, _os.W_OK):
        raise HoldoutBoundaryError(
            f"[fail-closed] 揭露紀錄 {p} 不可寫。這在載入 OS 之前擋下 —— "
            "否則結果會是「OS 已經看過了、紀錄卻寫不進去」")
    return len(rows)


def os_reveal_status(*, strategy_rule_hash: str, protocol: SingleHoldoutProtocol,
                     strategy_id: str = "") -> Dict[str, Any]:
    return reveal_status(strategy_hash=strategy_rule_hash,
                         strategy_name=strategy_id or None,
                         os_start=protocol.os_start, os_end=protocol.os_end)


def record_os_reveal(*, strategy_rule_hash: str, strategy_id: str,
                     protocol: SingleHoldoutProtocol, source: str,
                     manifest: Optional[str] = None,
                     extra_context: Optional[Mapping[str, Any]] = None,
                     now=None, path=None) -> Dict[str, Any]:
    """把這次揭露 append 進 append-only 揭露紀錄(第二次會被標 previously_seen)。"""
    return record_reveal(
        strategy_hash=strategy_rule_hash, strategy_name=strategy_id,
        os_start=protocol.os_start, os_end=protocol.os_end,
        source=source, segment=SEGMENT_OS, manifest=manifest,
        is_window=[protocol.is_start, protocol.is_end],
        embargo_trading_days=int(protocol.embargo_trading_days),
        split_mode=protocol.split_mode,
        context={"protocol_hash": protocol.protocol_hash(),
                 "evaluator_version": protocol.evaluator_version,
                 **dict(extra_context or {})},
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
                     manifest_path: str = "",
                     rules: Optional[Mapping[str, Any]] = None
                     ) -> FrozenCandidate:
    """把 strategy rule 凍結成一個 hash + 規則本體。揭露 OS 前必須先有這個。

    `rules` 應該直接來自 IS run 的 `manifest["candidate"]`;少了它,揭露時就只能
    「先跑再比」。優先用 `freeze_from_is_manifest()`,避免有人手抄 hash。
    """
    return FrozenCandidate(
        strategy_id=strategy_id, strategy_rule_hash=str(strategy_rule_hash),
        frozen_at=str(frozen_at), protocol_hash=protocol.protocol_hash(),
        manifest_path=str(manifest_path), rules=dict(rules or {}))


def freeze_from_is_manifest(*, manifest: Mapping[str, Any],
                            protocol: SingleHoldoutProtocol, frozen_at: str,
                            manifest_path: str = "") -> FrozenCandidate:
    """直接用 IS run 的 manifest 凍結 —— 唯一建議的凍結入口。

    手抄 hash 的版本有兩個失敗模式:抄錯(閘門變成噪音),以及只抄 hash 沒抄
    規則本體(閘門無法前移到載入 OS 之前)。這支兩個都堵掉。
    """
    return freeze_candidate(
        strategy_id=str(manifest["strategy_id"]),
        strategy_rule_hash=str(manifest["strategy_rule_hash"]),
        rules=dict(manifest.get("candidate") or {}),
        protocol=protocol, frozen_at=frozen_at, manifest_path=manifest_path)


def reject_unverifiable_rule(**kwargs) -> None:
    """揭露 OS 時**不接受** `signal_frame=` —— 那是一條完整的旁路。

    2026-08-21 對抗式驗證實測:傳一份用 `mom_window` 加倍算出來的 signal_frame
    (已確認與凍結參數的訊號不同),前置閘門看 `kwargs["params"] is None` 而去算
    `default_parameters()` 的 hash、`run_golden_path` 因為有 signal_frame 也不會
    呼叫 `make_signals`,於是 `candidate.signal_params` 仍是 defaults ——
    **兩道 hash 閘門都放行,一套沒凍結的規則吃掉了 locked OS,而紀錄記的是
    凍結那套的 hash。** 等於可以無限次「免費看」同一段 OS。

    根因是 SignalFrame 只帶 `strategy_id` / `strategy_version`,**不帶產生它的
    參數**,所以規則身分在這條路上結構性地無法驗證。既然驗不了就不准走 ——
    IS 研究要用 signal_frame 沒問題(`research_run` 不受此限),那裡沒有不可逆的
    資源可燒。
    """
    if kwargs.get("signal_frame") is not None:
        raise HoldoutBoundaryError(
            "[fail-closed] 揭露 locked OS 不接受 signal_frame= —— SignalFrame 不帶"
            "產生它的參數,所以 strategy_rule_hash 無法驗證(實測可繞過兩道閘門)。"
            "請改成傳 strategy_id + params,讓引擎自己算訊號")


def preflight_run_inputs(*, fixture_name: str, protocol: SingleHoldoutProtocol,
                         output_dir) -> None:
    """把**能事前判定**的錯誤全部擋在寫揭露紀錄之前。

    為什麼重要:紀錄一旦寫下就撤不回(append-only),而 `reveal_status` 不看
    `phase`。所以「`fixture_name` 打錯一個字母」這種零資料載入的失敗,若排在
    紀錄之後,會讓該候選**永久失去 fresh OOS 宣稱** —— 專案只有一段 locked OS,
    一次手滑就沒了。

    誠實的殘留:run 目錄撞名**無法**事前檢查,因為 run id 由 run 內部的
    evaluation hash 決定。這裡只能確認 output_dir 可建、可寫。
    """
    import os as _os
    from pathlib import Path as _Path

    from research.fixtures import KNOWN_FIXTURES
    from research.golden_path import CAPITAL_SCENARIOS

    if fixture_name not in KNOWN_FIXTURES:
        raise HoldoutBoundaryError(
            f"[fail-closed] 未知的 fixture_name={fixture_name!r};只接受 "
            f"{list(KNOWN_FIXTURES)}。這道檢查刻意排在寫揭露紀錄之前 —— "
            "打錯字不該燒掉一次 fresh OOS 宣稱")
    if protocol.capital_scenario not in CAPITAL_SCENARIOS:
        raise HoldoutBoundaryError(
            f"[fail-closed] 未知資金情境 {protocol.capital_scenario!r};"
            f"可用 {sorted(CAPITAL_SCENARIOS)}")
    out = _Path(str(output_dir))
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HoldoutBoundaryError(
            f"[fail-closed] output_dir 建不起來:{exc}") from exc
    if not _os.access(out, _os.W_OK):
        raise HoldoutBoundaryError(f"[fail-closed] output_dir 不可寫:{out}")


def reveal_locked_os(*, strategy_id: str, protocol: SingleHoldoutProtocol,
                     frozen: Optional[FrozenCandidate], authorization: str,
                     output_dir, fixture_name: str = "synthetic",
                     stamp: str = "os", now=None, ledger_path=None, **kwargs):
    """揭露入口:需要 owner 獨立授權 + 已凍結的 rule + 揭露紀錄。

    順序刻意是「先擋、後跑」:授權、**不可驗證的規則**、凍結、**規則 hash**、
    揭露紀錄可寫性、**可事前判定的輸入錯誤**,六道全部在建立任何 OS panel **之前**,所以擋下來的呼叫連 OS 資料都不會被載入
    (規格 §8.5)。

    2026-08-17 的真實事故就是這個順序寫錯的後果:`control_h4` 的 git commit 與
    `signal_params`(多了 `trend_guard=True`)都與凍結時不同,但 hash 比對排在
    `run_golden_path()` **之後** —— 整段 locked OS 已經被載入並算完,才拋出
    「規則變了」。事後只能人工補登揭露紀錄
    (`source=manual_backfill_after_rule_hash_gate_failed_post_run`)。
    OS 的消耗不可逆,所以偵測必須前移到消耗之前,而不是在事後宣告失敗。
    """
    authorize_reveal(authorization)
    reject_unverifiable_rule(**kwargs)            # ⓪ 驗不了的規則不准走
    if frozen is None:
        raise HoldoutBoundaryError(
            "[fail-closed] 揭露 locked OS 前必須先凍結 strategy rule"
            "(freeze_candidate);未凍結就等於還能回頭改規則")
    if frozen.protocol_hash != protocol.protocol_hash():
        raise HoldoutBoundaryError(
            "[fail-closed] protocol 與凍結時不同:換考卷等於重新選一次切割")

    # ① 規則閘門 —— 在載入任何 OS 之前完成。
    expected_rules = dict(frozen.rules or {})
    if not expected_rules.get("eligibility_rule_id"):
        raise HoldoutBoundaryError(
            "[fail-closed] frozen 沒有記錄 candidate rules,無法在載入 OS 之前"
            "重算 strategy_rule_hash。請改用 freeze_from_is_manifest() 依 IS "
            "run 的 manifest 重新凍結 —— 只有 hash 的舊 frozen 會逼這道閘門"
            "退回「先跑再擋」,而那正是 2026-08-17 燒掉 OS 的原因")
    assert_rule_unchanged(frozen, precompute_strategy_rule_hash(
        strategy_id=strategy_id, params=kwargs.get("params"),
        policy=kwargs.get("policy"),
        eligibility_rule_id=str(expected_rules["eligibility_rule_id"])))

    # ② 揭露紀錄壞掉/不可寫、以及**任何能事前判定的輸入錯誤**,都要在看見 OS
    #    之前擋:同一類問題(狀態在 run 之前就已成立),同一種處理。
    preflight_ledger(ledger_path)
    preflight_run_inputs(fixture_name=fixture_name, protocol=protocol,
                         output_dir=output_dir)

    # ③ 先記錄、再跑。語意上「決定要載入 OS」就等於「要看」,紀錄不可以取決於
    #    後面還會不會出錯(④ 的比對、artifacts 寫檔、KeyboardInterrupt、OOM)。
    #    代價是 run 在載入 OS 前就失敗也會留下一列;所以標 phase=pre_run,
    #    run 完成的證據是 run 目錄的 audit.json(有 seq 可對回這一列)。
    ledger = record_os_reveal(
        strategy_rule_hash=frozen.strategy_rule_hash, strategy_id=strategy_id,
        protocol=protocol, source="research.holdout.reveal_locked_os",
        manifest=str(output_dir),
        extra_context={"phase": "pre_run", "stamp": str(stamp),
                       "frozen_at": str(frozen.frozen_at)},
        now=now, path=ledger_path)

    from research.golden_path import run_golden_path

    result = run_golden_path(
        strategy_id=strategy_id, fixture_name=fixture_name,
        capital=protocol.capital_scenario, output_dir=output_dir,
        stamp=stamp, holdout_protocol=protocol, segment=SEGMENT_OS, **kwargs)

    # ④ run 之後再比一次(defense in depth):抓 `eligibility_rule_id` 這種只有
    #    跑過才知道的漂移。此時 OS 已被看過,但 ③ 已經留下紀錄。
    assert_rule_unchanged(frozen, result.manifest["strategy_rule_hash"])
    result.audit["os_reveal"] = ledger
    result.audit["frozen_candidate"] = frozen.to_dict()
    from research import artifacts
    artifacts.write_json(
        artifacts.RunDirectory(path=__import__("pathlib").Path(result.run_dir),
                               run_id=""), "audit", result.audit)
    return result
