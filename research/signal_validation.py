# -*- coding: utf-8 -*-
"""SignalFrame 的**唯一** validator(研究規格 §5.6 / §6)。

repo 內註冊的 Python 策略與外部序列化訊號走同一支,**不得為 S19 開特例**。
理由很直接:外部訊號之所以危險,不是因為它來自別處,而是因為沒人替它檢查
key 唯一性、排名母體、快照完整性與未來資料邊界;如果 repo 內的策略走另一條
比較寬鬆的路,那條路遲早會變成大家用來繞過檢查的門。

每一條檢查都對應一個實際會產生假結果的失敗:
  - key 不唯一        → 同一天同一檔兩個 rank,選股結果取決於列順序
  - rank 不連續       → 「第 10 名」不再等於第 10 名,entry buffer 語意壞掉
  - 排名母體對不上    → 非成員混進 cs 排名(§3.1 的不變式)
  - 快照不完整卻宣稱完整 → 未出現的持股會被誤判成「掉出榜外」而賣掉
  - 越過 as-of        → 用還沒發生的資料決策
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = (
    "date", "stock_id", "eligible", "raw_score", "rank",
    "ranking_universe_count", "snapshot_complete",
)
# §6.1 要求但允許由 validator 補齊(補齊時會記進 warnings,不是靜默)。
DERIVABLE_COLUMNS = ("alpha_score", "rank_pct", "thesis_ok", "hard_exit",
                     "reason_codes", "eligibility_rule_id")
PROVENANCE_COLUMNS = ("strategy_id", "strategy_version")


class SignalValidationError(ValueError):
    """SignalFrame 不符契約;一律 fail-closed,不得降級成 warning。"""


# ── 型別強制:pandas 的預設轉型會把「壞資料」變成「看起來合理的資料」 ─────────
def _strict_bool(series: pd.Series, *, who: str, col: str) -> pd.Series:
    """把一欄轉成真 bool,但**拒絕**會靜默翻轉語意的輸入。

    `pd.Series(["False"]).astype(bool)` 是 `True` —— 字串非空即為真。所以一份
    把 bool 存成字串的 CSV 讀回來,`snapshot_complete="False"` 會變成「宣告完整」,
    於是持股沒出現在快照裡就被判 `not_ranked` 賣掉。這正是 snapshot_complete
    這個旗標存在要防的事,不能讓它被一次 dtype 轉換抵銷。

    只接受:真 bool、numpy bool_、整數 0/1、以及缺值(缺值 → False,方向與
    §9B.1 一致:不知道就是不完整)。
    """
    if series.dtype == bool:
        return series
    out: List[bool] = []
    for val in series.tolist():
        if isinstance(val, (bool, np.bool_)):
            out.append(bool(val))
            continue
        if val is None or val is pd.NaT or (
                isinstance(val, float) and math.isnan(val)):
            out.append(False)
            continue
        if isinstance(val, (int, np.integer)) and int(val) in (0, 1):
            out.append(bool(int(val)))
            continue
        if isinstance(val, (float, np.floating)) and float(val) in (0.0, 1.0):
            out.append(bool(float(val)))
            continue
        raise SignalValidationError(
            f"[fail-closed] {who}:{col} 只接受 bool 或 0/1,收到 {val!r}"
            f"({type(val).__name__})。字串 'False' 用 astype(bool) 會變成 True,"
            "那會讓不完整快照冒充完整,持股因為「沒看到」被賣掉")
    return pd.Series(out, index=series.index, dtype=bool)


def _strict_int(series: pd.Series, *, who: str, col: str) -> pd.Series:
    """轉成整數,但**拒絕**小數 —— `int(1.9)` 是 1,名次會被無聲改掉。"""
    num = pd.to_numeric(series, errors="coerce")
    if num.isna().any():
        raise SignalValidationError(
            f"[fail-closed] {who}:{col} 有無法解析成數字的值")
    if not np.isfinite(num.to_numpy(dtype=float)).all():
        raise SignalValidationError(f"[fail-closed] {who}:{col} 有 inf")
    frac = np.abs(num.to_numpy(dtype=float) - np.round(num.to_numpy(dtype=float)))
    bad = np.where(frac > 1e-9)[0]
    if bad.size:
        sample = [float(num.iloc[int(i)]) for i in bad[:5]]
        raise SignalValidationError(
            f"[fail-closed] {who}:{col} 必須是整數,收到小數 {sample};"
            "int(1.9)=1 會把名次無聲改掉,而輸出裡完全看不出來")
    return num.round().astype("int64")


@dataclass
class ValidationResult:
    frame: pd.DataFrame
    n_rows: int
    n_days: int
    warnings: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    formal_evidence_eligible: bool = True
    evidence_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_rows": int(self.n_rows), "n_days": int(self.n_days),
            "warnings": list(self.warnings), "checks": dict(self.checks),
            "formal_evidence_eligible": bool(self.formal_evidence_eligible),
            "evidence_note": self.evidence_note,
        }


def validate_signal_frame(frame: pd.DataFrame, *, who: str,
                          as_of_max: Optional[pd.Timestamp] = None,
                          require_provenance: bool = True) -> ValidationResult:
    """檢查並正規化一份 SignalFrame。回傳 `ValidationResult`。"""
    if not isinstance(frame, pd.DataFrame):
        raise SignalValidationError(f"[fail-closed] {who}:SignalFrame 必須是 DataFrame")
    if frame.empty:
        raise SignalValidationError(f"[fail-closed] {who}:SignalFrame 為空")

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise SignalValidationError(
            f"[fail-closed] {who}:SignalFrame 缺必要欄位 {missing}")

    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    if out["date"].isna().any():
        raise SignalValidationError(f"[fail-closed] {who}:date 有無法解析的值")
    out["stock_id"] = out["stock_id"].astype(str)

    checks: Dict[str, bool] = {}
    warnings: List[str] = []

    dup = int(out.duplicated(["date", "stock_id"]).sum())
    if dup:
        raise SignalValidationError(
            f"[fail-closed] {who}:有 {dup} 筆重複的 (date, stock_id);"
            "同一天同一檔兩個 rank,選股結果會取決於列順序")
    checks["unique_key"] = True

    out["eligible"] = _strict_bool(out["eligible"], who=who, col="eligible")
    out["snapshot_complete"] = _strict_bool(
        out["snapshot_complete"], who=who, col="snapshot_complete")
    checks["boolean_columns_strict"] = True

    if not out["eligible"].all():
        raise SignalValidationError(
            f"[fail-closed] {who}:SignalFrame 只能包含 eligible=True 的列;"
            "不可選的股票不該出現在當日排名母體裡(規格 §3.1)")
    checks["eligible_only"] = True

    # 分數:NaN 與 ±inf 都不行。inf 特別危險 —— 它不會讓排序報錯,而是無聲地
    # 把那一檔永久釘在第一名(或最後一名),看起來像一個很強的訊號。
    score = pd.to_numeric(out["raw_score"], errors="coerce")
    if score.isna().any():
        raise SignalValidationError(
            f"[fail-closed] {who}:raw_score 有 NaN 或無法解析的值;"
            "缺分數不得混進排名")
    if not np.isfinite(score.to_numpy(dtype=float)).all():
        raise SignalValidationError(
            f"[fail-closed] {who}:raw_score 有 ±inf;inf 不會讓排序報錯,"
            "只會無聲地把那一檔永久釘在名次頭尾,看起來像很強的訊號")
    out["raw_score"] = score.astype(float)
    if "alpha_score" in out.columns:
        alpha = pd.to_numeric(out["alpha_score"], errors="coerce")
        if alpha.isna().any() or not np.isfinite(
                alpha.to_numpy(dtype=float)).all():
            raise SignalValidationError(
                f"[fail-closed] {who}:alpha_score 有 NaN 或 ±inf")
        out["alpha_score"] = alpha.astype(float)
    checks["scores_present"] = True
    checks["scores_finite"] = True

    # 名次與母體數必須是真整數(見 `_strict_int`:int(1.9)=1 會無聲改名次)。
    out["rank"] = _strict_int(out["rank"], who=who, col="rank")
    out["ranking_universe_count"] = _strict_int(
        out["ranking_universe_count"], who=who, col="ranking_universe_count")
    over = out[out["rank"] > out["ranking_universe_count"]]
    if len(over):
        raise SignalValidationError(
            f"[fail-closed] {who}:有 {len(over)} 列 rank > "
            "ranking_universe_count;名次不可能大於母體")
    if int((out["rank"] < 1).sum()):
        raise SignalValidationError(f"[fail-closed] {who}:rank 必須 >= 1")
    checks["rank_is_integer"] = True

    # rank 必須是每日 1..N 的連續整數,且與 ranking_universe_count 一致。
    for day, grp in out.groupby("date"):
        ranks = sorted(int(r) for r in grp["rank"])
        if ranks != list(range(1, len(grp) + 1)):
            raise SignalValidationError(
                f"[fail-closed] {who}:{str(day)[:10]} 的 rank 不是 1..{len(grp)} "
                "的連續整數,entry/exit buffer 的名次語意會壞掉")
        counts = set(int(c) for c in grp["ranking_universe_count"])
        if counts != {len(grp)}:
            raise SignalValidationError(
                f"[fail-closed] {who}:{str(day)[:10]} 的 ranking_universe_count="
                f"{sorted(counts)} 與當日實際母體 {len(grp)} 不符;"
                "排名母體對不上就無法判斷 cs 排名是否只在 eligible 內做")
    checks["rank_dense_and_consistent"] = True

    incomplete = ~out["snapshot_complete"]
    if incomplete.any():
        # 不是錯誤:policy 會把不完整快照當 unknown 而不自動賣。但要標出來,
        # 因為它會讓 not_ranked 這條退出規則整段失效。
        warnings.append(
            f"{int(incomplete.sum())} 列 snapshot_complete=False;"
            "policy 將以 unknown 處理(不會因未出現而賣出)")
    checks["snapshot_completeness_declared"] = True

    if as_of_max is not None:
        limit = pd.Timestamp(as_of_max)
        beyond = int((out["date"] > limit).sum())
        if beyond:
            raise SignalValidationError(
                f"[fail-closed] {who}:有 {beyond} 列的日期晚於 as-of {limit.date()};"
                "訊號不得使用還沒發生的資料")
    checks["as_of_bounded"] = True

    for col in DERIVABLE_COLUMNS:
        if col in out.columns:
            continue
        warnings.append(f"缺選用欄位 {col},validator 補上預設值")
        if col == "alpha_score":
            out[col] = out["raw_score"]
        elif col == "rank_pct":
            out[col] = ((out["ranking_universe_count"] - out["rank"] + 1)
                        / out["ranking_universe_count"])
        elif col == "thesis_ok":
            out[col] = True
        elif col == "hard_exit":
            out[col] = False
        elif col == "reason_codes":
            out[col] = ""
        elif col == "eligibility_rule_id":
            out[col] = "unspecified"

    # provenance 欄位若**存在**就必須可用:空白或一份 frame 裡混兩個 strategy_id
    # 代表這份訊號不知道自己是誰產生的,manifest 記下來的規則指紋就對不到實際
    # 跑的東西。這比缺欄位更糟 —— 缺欄位看得出來,寫錯的看不出來。
    for col in PROVENANCE_COLUMNS:
        if col not in out.columns:
            continue
        vals = {str(v).strip() for v in out[col].tolist()}
        if any(v == "" or v.lower() in ("nan", "none") for v in vals):
            raise SignalValidationError(
                f"[fail-closed] {who}:{col} 有空白值;"
                "不知道是誰產生的訊號不得進入正式流程")
        if len(vals) > 1:
            raise SignalValidationError(
                f"[fail-closed] {who}:同一份 SignalFrame 出現多個 {col}="
                f"{sorted(vals)};一份 frame 只能來自一個策略版本,"
                "否則 manifest 的規則指紋對不到實際跑的規則")
    checks["provenance_consistent"] = True

    formal = True
    note = ""
    missing_prov = [c for c in PROVENANCE_COLUMNS if c not in out.columns]
    if missing_prov:
        if require_provenance:
            formal = False
            note = (f"SignalFrame 缺 provenance 欄位 {missing_prov}:"
                    "可供 debug,但不得產生正式證據(規格 §5.6)")
            warnings.append(note)
        for c in missing_prov:
            out[c] = "unknown"
    checks["provenance_present"] = not missing_prov

    out = out.sort_values(["date", "rank", "stock_id"],
                          kind="mergesort").reset_index(drop=True)
    return ValidationResult(
        frame=out, n_rows=len(out), n_days=int(out["date"].nunique()),
        warnings=warnings, checks=checks,
        formal_evidence_eligible=formal, evidence_note=note,
    )
