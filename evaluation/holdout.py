# -*- coding: utf-8 -*-
"""holdout 使用紀錄:誰、在什麼時候、看過哪一段 OS 資料(append-only 台帳)。

為什麼非有這個台帳不可
----------------------
IS/OS 的切點**完全由凍結資料自身的首尾日決定**(`evaluation/splits.py` 的 ratio
與 weeks 兩種模式都錨在 `dts[-1]`),而資料視窗的兩端會隨 `SNAPSHOT_END_DATE`
一起滑動(`data.py` 的 `start = end - HISTORY_DAYS`,730 天固定)。實測三個真實
快照:

    快照 2026-06-22 → OS = 2025-11-19 ~ 2026-06-18
    快照 2026-08-06 → OS 起點變成 2026-01-05

也就是 **2025-11-19 ~ 2026-01-04 這段從 OS 變成了 IS**。推進快照之後,同一支
腳本會拿一段「上次已經當成 OS 看過、而且參數是在看過它之後才定的」資料當成
新的 holdout,然後把結果報成 fresh OOS。系統原本沒有任何欄位記得「這段被看過」
——而 forward-only 已經是唯一剩下的證據升級路徑(見 `STRATEGY_REGISTRY.md` 的
S19:它的 OS 早已被評估窗洩漏污染)。

這份台帳就是那個記憶體:**每次正式揭露 OS 都 append 一列**,記策略 hash、OS
日期、揭露時間與 git commit。第二次揭露同一段 OS 不會被擋(重現既有結果是正當
需求),但一定會被標成 `holdout_previously_seen=True`,不得再稱 fresh OOS。

三個設計決定(每一個都對應一種會讓台帳失效的失敗模式)
------------------------------------------------------
1. **重疊即算看過,不是「日期字串相等」才算。** 上面的滑動窗讓兩次 OS 幾乎
   永遠不會完全相等;用等值比對等於這個台帳從第一天就永遠回報 fresh。所以
   比的是**區間交集**,並回報 `fresh_os_start`(這次真正沒被看過的起點)與
   `holdout_status`(fresh / partially_consumed / consumed)。
2. **雜湊鏈防靜默改寫。** append-only 的意義不在於「程式只用 'a' 模式開檔」,
   而在於**事後被改過看得出來**。每一列帶 `prev_sha256`,指向前一列的
   `record_sha256`;任何一列被改寫或抽掉,後面整條鏈就對不上,讀取時直接
   raise。台帳被靜默重寫的話,它記的東西就一文不值。
3. **reveal time 由呼叫端注入(`now=`)。** 需要時間戳,但不可引入不可重現的
   隨機性:測試必須能斷言同一份台帳的內容。時間戳不進任何策略 hash。

台帳裡**刻意不放績效數字**。它回答的是「這段未來資料被誰看過幾次」,不是
「跑出多少 Sharpe」;把績效放進來只會讓人有動機挑好看的那一列來引用。
forward 的執行結果另有 `outputs/forward_test_runs.jsonl`(每次 forward 一列,
帶 Sharpe 與基準),兩份用 `strategy_hash` + `output` 互相對照,語意不重疊:

    holdout_ledger.jsonl    → 揭露了哪一段 holdout(消耗紀錄)
    forward_test_runs.jsonl → 那次 forward 跑出什麼(執行紀錄)
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

import config
import provenance

try:                                     # POSIX 檔案鎖;沒有它就退回無鎖(見 _lock)
    import fcntl
except ImportError:                      # pragma: no cover - 本 repo 只跑 macOS/Linux
    fcntl = None                         # type: ignore[assignment]

LEDGER_NAME = "holdout_ledger.jsonl"
LEDGER_SCHEMA = 1

# 第一列的 prev 指標。用字串而不是 None,是為了讓「鏈的起點」與「欄位漏寫」
# 在驗證時區分得出來。
GENESIS = "genesis"


class HoldoutLedgerError(RuntimeError):
    """台帳讀寫或完整性問題。一律 fail-closed:寧可擋住,不可靜默接受被改過的台帳。"""


# ── 揭露前就已經被消耗掉的 holdout(程式碼層的既成事實宣告)────────────────
@dataclass(frozen=True)
class ConsumedHoldout:
    """在這個台帳存在**之前**就已經被看過的資料窗。

    為什麼要寫在程式碼裡而不是塞一列進台帳:`outputs/` 不進版控
    (`preflight.py` 會擋資料產物被追蹤),所以一份「事實上已經消耗」的宣告
    如果只存在於某台機器的 jsonl,換一台 clone 就變成 clean —— 那正是這裡
    要防的事。寫成模組常數之後,任何 clone、任何新 checkout 都會得到同一個
    答案,而且刪掉它會在 diff 裡看得見。
    """

    strategy: str
    seen_start: str          # 已看過的資料窗(不只 OS:IS 洩漏時整段都被看過)
    seen_end: str
    os_window: Tuple[str, str]   # 宣告當下那份報告的 OS 段
    status: str
    reason: str
    evidence: str
    declared_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "seen_window": [self.seen_start, self.seen_end],
            "os_window": list(self.os_window),
            "status": self.status,
            "reason": self.reason,
            "evidence": self.evidence,
            "declared_at": self.declared_at,
        }


# S19 的 OS 已經不是乾淨 holdout,這件事在台帳上線前就成立(見
# STRATEGY_REGISTRY.md 的 S19 條目與 strategies/s19_chip_momentum.py 的
# 「已作廢的數字」)。這裡把它寫死,任何 S19 的 OS 揭露都會被標成
# consumed / pseudo-OOS —— **不得**因為台帳是空的就重設成 clean。
KNOWN_CONSUMED_HOLDOUTS: Tuple[ConsumedHoldout, ...] = (
    ConsumedHoldout(
        strategy="s19_chip_momentum",
        # 資料窗下界取快照 2026-06-22 往前 HISTORY_DAYS(730)天,也就是那份
        # 報告實際看得到的最早一天;上界取 2026-08-06,因為 S19 的訊號在該快照
        # 下仍被逐日檢視(outputs/S19_top20_20260805.csv、S19_live_picks.csv)。
        seen_start="2024-06-23",
        seen_end="2026-08-06",
        os_window=("2025-11-19", "2026-06-18"),
        status="consumed_pseudo_oos",
        reason=(
            "評估窗洩漏:IS 權益曲線溢出切點 144 天、吃到 OS 段 +87.2%,"
            "而 16 格參數掃描是在那組被污染的數字上選的 —— 參數選擇已間接看過 OS;"
            "另有多輪權重/出場規則在同一段資料上反覆比較"
        ),
        evidence="STRATEGY_REGISTRY.md 的 S19 條目(證據等級 blocked)",
        declared_at="2026-08-15",
    ),
)


# ── 區間工具 ──────────────────────────────────────────────────────────────
def _day(value: Any) -> pd.Timestamp:
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        raise HoldoutLedgerError(f"無法解析日期 {value!r}:holdout 邊界不可含糊")
    return pd.Timestamp(ts).normalize()


def _window(start: Any, end: Any) -> Tuple[pd.Timestamp, pd.Timestamp]:
    a, b = _day(start), _day(end)
    if b < a:
        raise HoldoutLedgerError(f"holdout 視窗顛倒:{a.date()} > {b.date()}")
    return a, b


def _merge(intervals: Sequence[Tuple[pd.Timestamp, pd.Timestamp]]
           ) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """合併重疊或**相鄰**(差一天)的區間。

    相鄰也合併,否則 `fresh_os_start` 會指到一個其實已經被看過的日子:
    [1/1,1/10] 與 [1/11,1/20] 中間沒有任何未看過的日期。
    """
    out: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + pd.Timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _clip(intervals: Sequence[Tuple[pd.Timestamp, pd.Timestamp]],
          lo: pd.Timestamp, hi: pd.Timestamp
          ) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    out = []
    for a, b in intervals:
        a2, b2 = max(a, lo), min(b, hi)
        if a2 <= b2:
            out.append((a2, b2))
    return out


def _days(intervals: Sequence[Tuple[pd.Timestamp, pd.Timestamp]]) -> int:
    return int(sum((b - a).days + 1 for a, b in intervals))


# ── 台帳讀寫 ──────────────────────────────────────────────────────────────
def ledger_path(path: Optional[Any] = None) -> Path:
    return Path(path) if path is not None else (config.OUTPUT_DIR / LEDGER_NAME)


def _record_hash(record: Mapping[str, Any]) -> str:
    """一列的內容雜湊(不含 `record_sha256` 自己,含 `prev_sha256` → 形成鏈)。"""
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _lock(fh, exclusive: bool) -> None:
    if fcntl is None:                    # pragma: no cover
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _unlock(fh) -> None:
    if fcntl is None:                    # pragma: no cover
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _parse_and_verify(fh) -> List[Dict[str, Any]]:
    """讀出所有列並驗證雜湊鏈。被改寫/抽列/插列一律 raise。"""
    fh.seek(0)
    records: List[Dict[str, Any]] = []
    prev = GENESIS
    for lineno, line in enumerate(fh, start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HoldoutLedgerError(
                f"holdout 台帳第 {lineno} 行不是合法 JSON({exc});"
                "台帳是稽核紀錄,壞掉時不得當成空的繼續寫"
            ) from exc
        expected = _record_hash(rec)
        if rec.get("record_sha256") != expected:
            raise HoldoutLedgerError(
                f"[fail-closed] holdout 台帳第 {lineno} 行的內容與 record_sha256 "
                "對不上:這一列被事後改過。append-only 台帳的價值就在於改過看得見,"
                "請用版本控制/備份還原,不要覆蓋它"
            )
        if rec.get("prev_sha256") != prev:
            raise HoldoutLedgerError(
                f"[fail-closed] holdout 台帳第 {lineno} 行的 prev_sha256 接不上前一列:"
                "中間有列被刪除或插入(整條鏈是它存在的意義)"
            )
        prev = expected
        records.append(rec)
    return records


def read_ledger(path: Optional[Any] = None) -> List[Dict[str, Any]]:
    """讀台帳(順便驗鏈)。檔案不存在 = 還沒有任何揭露,回空 list。"""
    p = ledger_path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as fh:
        _lock(fh, exclusive=False)
        try:
            return _parse_and_verify(fh)
        finally:
            _unlock(fh)


def verify_ledger(path: Optional[Any] = None) -> int:
    """驗證整條鏈,回傳列數(壞掉會 raise)。稽核腳本用。"""
    return len(read_ledger(path))


# ── 「這段 holdout 被看過了嗎」────────────────────────────────────────────
def reveal_status(*, strategy_hash: str, strategy_name: Optional[str],
                  os_start: Any, os_end: Any,
                  records: Optional[Iterable[Mapping[str, Any]]] = None,
                  path: Optional[Any] = None) -> Dict[str, Any]:
    """這次要揭露的 OS 窗,有多少已經被同一套規則看過。

    覆蓋來源有兩個,都算數:
      1. 台帳裡**同一個 `strategy_hash`** 的既有揭露(規則沒變 = 同一套研究)。
      2. `KNOWN_CONSUMED_HOLDOUTS` 裡對**同一個策略名**的既成宣告(台帳上線前
         就被消耗掉的窗,例如 S19)。

    不同 `strategy_hash` 的揭露不會讓這次變成 previously_seen(那是另一套規則
    的樣本外),但會記進 `prior_reveals_other_rules` —— 同一段 holdout 被 30 套
    規則輪流看過是多重檢定問題,看得見比看不見好。
    """
    lo, hi = _window(os_start, os_end)
    rows = list(records) if records is not None else read_ledger(path)

    same: List[Mapping[str, Any]] = []
    other = 0
    for r in rows:
        try:
            a, b = _window(r.get("os_start"), r.get("os_end"))
        except HoldoutLedgerError:
            continue
        if b < lo or a > hi:              # 沒有交集
            continue
        if r.get("strategy_hash") == strategy_hash:
            same.append(r)
        else:
            other += 1

    declared = [c for c in KNOWN_CONSUMED_HOLDOUTS
                if strategy_name and c.strategy == strategy_name
                and not (_day(c.seen_end) < lo or _day(c.seen_start) > hi)]

    covered = _merge(
        [_window(r.get("os_start"), r.get("os_end")) for r in same]
        + [_window(c.seen_start, c.seen_end) for c in declared]
    )
    inside = _clip(covered, lo, hi)
    seen_days = _days(inside)
    total_days = int((hi - lo).days + 1)

    # 這次真正沒被看過的起點:第一個未被覆蓋的日子。
    fresh_start: Optional[pd.Timestamp]
    if not inside or inside[0][0] > lo:
        fresh_start = lo
    else:
        nxt = inside[0][1] + pd.Timedelta(days=1)
        fresh_start = nxt if nxt <= hi else None

    if seen_days <= 0:
        status = "fresh"
    elif seen_days >= total_days:
        status = "consumed"
    else:
        status = "partially_consumed"

    return {
        "os_window": [str(lo.date()), str(hi.date())],
        "os_window_days": total_days,
        "holdout_previously_seen": seen_days > 0,
        "holdout_status": status,
        "previously_seen_days": seen_days,
        "fresh_os_start": (None if fresh_start is None else str(fresh_start.date())),
        "fresh_oos_claim_allowed": seen_days <= 0,
        "prior_reveals_same_rules": [r.get("seq") for r in same],
        "prior_reveals_other_rules": other,
        "declared_consumed": [c.to_dict() for c in declared],
        "note": (
            "holdout_previously_seen=True 代表這段 OS 已經被同一套規則(或既成"
            "宣告)看過:可以為重現目的再跑,但不得再稱 fresh OOS。"
            if seen_days > 0 else
            "這段 OS 在本台帳裡是第一次被這套規則揭露。"
        ),
    }


# ── 揭露(append-only)────────────────────────────────────────────────────
def record_reveal(*, strategy_hash: str, strategy_name: Optional[str],
                  os_start: Any, os_end: Any, source: str,
                  segment: str = "OS",
                  label: Optional[str] = None,
                  manifest: Optional[str] = None,
                  is_window: Optional[Sequence[Any]] = None,
                  embargo_trading_days: Optional[int] = None,
                  split_mode: Optional[str] = None,
                  context: Optional[Mapping[str, Any]] = None,
                  now: Optional[datetime] = None,
                  path: Optional[Any] = None) -> Dict[str, Any]:
    """把一次 OS 揭露 append 進台帳,回傳寫進去的那一列。

    整段(讀 → 驗鏈 → 判 previously_seen → append)在**同一個排他檔案鎖**內完成:
    兩個 process 同時揭露時,不可以雙方都讀到「台帳是空的」而各自宣稱 fresh。

    `now` 由呼叫端注入,測試才能斷言台帳內容;時間戳不進任何策略 hash。
    """
    if not strategy_hash:
        raise HoldoutLedgerError(
            "揭露 holdout 必須帶 strategy_hash:沒有規則識別碼的紀錄無法回答"
            "「同一套規則看過這段沒有」"
        )
    lo, hi = _window(os_start, os_end)
    reveal_at = (now or datetime.now()).isoformat(timespec="seconds")

    p = ledger_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+", encoding="utf-8") as fh:
        _lock(fh, exclusive=True)
        try:
            records = _parse_and_verify(fh)
            status = reveal_status(strategy_hash=strategy_hash,
                                   strategy_name=strategy_name,
                                   os_start=lo, os_end=hi, records=records)
            record: Dict[str, Any] = {
                "ledger_schema": LEDGER_SCHEMA,
                "seq": len(records) + 1,
                "reveal_at": reveal_at,
                "source": source,
                "segment": segment,
                "strategy_hash": strategy_hash,
                "strategy_name": strategy_name,
                "label": label,
                "manifest": manifest,
                "os_start": str(lo.date()),
                "os_end": str(hi.date()),
                "is_window": ([str(_day(is_window[0]).date()),
                               str(_day(is_window[1]).date())]
                              if is_window else None),
                "embargo_trading_days": (None if embargo_trading_days is None
                                         else int(embargo_trading_days)),
                "split_mode": split_mode,
                "data_snapshot_end": getattr(config, "SNAPSHOT_END_DATE", ""),
                "history_days": getattr(config, "HISTORY_DAYS", None),
                "context": dict(context or {}),
            }
            record.update({k: v for k, v in status.items()
                           if k not in ("os_window", "note")})
            record.update(provenance.git_state())
            record["prev_sha256"] = (records[-1]["record_sha256"] if records
                                     else GENESIS)
            record["record_sha256"] = _record_hash(record)
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            _unlock(fh)
    return record


def rules_fingerprint(payload: Mapping[str, Any]) -> str:
    """規則 → 16 位識別碼(canonical JSON 的 sha256 前 16 碼)。

    `freeze_manifest.rules_hash` 與所有揭露點共用**這一份**實作:兩份實作遲早
    會分岔(排序、預設值、非 ASCII 逸出任一個不同就分岔),那樣台帳裡的
    `strategy_hash` 就對不上 manifest 的 `rules_sha256_16`,「這段 OS 是誰看的」
    也就再也答不出來。
    """
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ConsumedHoldout", "GENESIS", "HoldoutLedgerError", "KNOWN_CONSUMED_HOLDOUTS",
    "LEDGER_NAME", "LEDGER_SCHEMA", "ledger_path", "read_ledger", "record_reveal",
    "reveal_status", "rules_fingerprint", "verify_ledger",
]
