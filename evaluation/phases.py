# -*- coding: utf-8 -*-
"""統一的再平衡相位掃描(evaluation 套件的唯一實作)。

為什麼要有這一層
----------------
「每 N 日再平衡」沒有規定從哪一天起算 —— N 個起始偏移都是同一條規則的合法
實作,各自有自己的績效(rebalance timing luck)。實測同一組訊號換相位,Sharpe
可以從 -0.09 擺到 +1.09(AGENTS.md 陷阱 2);S19 的相位標準差 0.509 已經跟訊號
效果本身同量級。只報一條路徑等於挑路徑,所以正式 IS/OS/forward 一律跑滿所有
等價相位,並用**中位數與最小值**決策,不是最大值。

原本的 bug(這個模組要修的就是它)
----------------------------------
同一條規則有兩份手寫實作,連「相位數怎麼決定」都不一樣:

  - `backtest.run_full`:`for phase in range(rebalance_every)`,而 CLI 預設
    `rebalance_every=5`;
  - `strategies.s19_chip_momentum.evaluate`:`for ph in range(PORT_REBALANCE_DAYS)`
    = 20 個相位。

`forward_test` 又自己寫了第三份聚合(中位/最小/最差 MaxDD/single_phase_debug),
其中 `single_phase_debug` 是用 `len(df) == 1` 反推的 —— 那是**結果**不是**意圖**:
20 相位掃描只有一個相位產出結果時會被誤標成 debug,而再平衡天數真的是 1 的正式
全相位掃描也會被誤標。旗標必須來自呼叫端「我只要單相位」的明確要求。

介面
----
    sweep = sweep_phases(run_phase, n_phases=20)      # run_phase(phase) -> row|None
    sweep.rows            # 每相位一列的 DataFrame
    sweep.stats()         # 中位數 / 最小值 / 最差 MaxDD / single_phase_debug

`run_phase` 回傳 `None` 代表該相位沒有結果(例如訊號在該區間內沒出現),不會
中斷掃描,但會被記進 `phases_without_result`,呼叫端看得到「掃了幾個、成了幾個」。

刻意不做的事:這裡不算 metrics(Sharpe/MaxDD 由引擎的 summary 提供),也不決定
相位怎麼位移執行路徑(那是引擎的 `rebalance_phase`)。這一層只負責「掃滿」與
「聚合」,這樣新策略要跑相位時不必再抄一份迴圈。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

import pandas as pd

# 每相位一列的欄位慣例。Sharpe 欄名兩處本來就一致,MaxDD 則有兩個歷史名字
# (引擎 summary 用 `max_drawdown`,S19 的相位表用 `max_dd`),所以讀取端允許
# 兩者,但找不到任何一個時 fail-closed —— 不可以因為欄名打錯就靜默少報最差 MaxDD。
SHARPE_COLUMN = "sharpe"
DRAWDOWN_COLUMNS: Tuple[str, ...] = ("max_drawdown", "max_dd")
TRADES_COLUMN = "n_trades"


def phase_indices(n_phases: int, *,
                  single_phase_debug: bool = False) -> Tuple[int, ...]:
    """要跑哪些相位。

    `single_phase_debug=True` 只跑 phase 0,**僅供 debug**:單相位的績效是
    「一條路徑」,不是策略的分布。正式路徑不得傳這個旗標。
    """
    n = int(n_phases)
    if n < 1:
        raise ValueError(f"相位數必須 >= 1,目前為 {n_phases!r}")
    if single_phase_debug:
        return (0,)
    return tuple(range(n))


def _resolve_drawdown_column(df: pd.DataFrame,
                             drawdown_col: Optional[str]) -> str:
    if drawdown_col is not None:
        if drawdown_col not in df.columns:
            raise KeyError(f"相位表沒有 MaxDD 欄 {drawdown_col!r}")
        return drawdown_col
    for name in DRAWDOWN_COLUMNS:
        if name in df.columns:
            return name
    raise KeyError(
        f"[fail-closed] 相位表找不到 MaxDD 欄(找過 {list(DRAWDOWN_COLUMNS)}):"
        "缺最差 MaxDD 的相位摘要不得當成完整結果"
    )


def phase_stats(rows: pd.DataFrame, *,
                single_phase_debug: bool,
                n_phases_full: Optional[int] = None,
                n_phases_requested: Optional[int] = None,
                sharpe_col: str = SHARPE_COLUMN,
                drawdown_col: Optional[str] = None) -> Dict[str, Any]:
    """相位分布的統一聚合:每相位結果 → 中位數 / 最小值 / 最差 MaxDD。

    「最差 MaxDD」= **所有相位裡最糟的那一個 MaxDD**(不是中位數、不是平均)。
    本 repo 的 MaxDD 是帶號的負值(`((equity - peak) / peak).min()`),所以最糟
    = 最小值。如果傳進來的是正值,代表慣例被改過,這裡直接 raise —— 靜默取 min
    會變成回報**最好**的那個相位,方向剛好相反。

    `single_phase_debug` 必須由呼叫端傳入(掃描時的意圖),不從列數反推。
    """
    stats: Dict[str, Any] = {
        "n_phases_full": (None if n_phases_full is None else int(n_phases_full)),
        "n_phases_requested": (None if n_phases_requested is None
                               else int(n_phases_requested)),
        "single_phase_debug": bool(single_phase_debug),
    }
    if rows is None or len(rows) == 0 or sharpe_col not in rows.columns:
        stats["n_phases"] = 0
        return stats

    dd_col = _resolve_drawdown_column(rows, drawdown_col)
    sharpe = pd.to_numeric(rows[sharpe_col], errors="coerce")
    valid = rows[sharpe.notna()]
    sharpe = sharpe.dropna()
    stats["n_phases"] = int(len(valid))
    if valid.empty:
        return stats

    dd = pd.to_numeric(valid[dd_col], errors="coerce").dropna()
    if (dd > 0).any():
        raise ValueError(
            f"[fail-closed] {dd_col} 出現正值 {sorted(dd[dd > 0].tolist())[:3]}:"
            "本 repo 的 MaxDD 慣例是負值,取 min 才是最差;慣例不符時拒絕聚合"
        )
    stats.update({
        "sharpe_median": float(sharpe.median()),
        "sharpe_min": float(sharpe.min()),
        "sharpe_max": float(sharpe.max()),
        # 最差 = 最負;不是 median()、不是 mean()。
        "worst_max_drawdown": (float(dd.min()) if len(dd) else float("nan")),
    })
    if TRADES_COLUMN in valid.columns:
        trades = pd.to_numeric(valid[TRADES_COLUMN], errors="coerce")
        stats["n_trades_median"] = float(trades.median())
        stats["n_trades_total"] = float(trades.sum())
    return stats


@dataclass(frozen=True)
class PhaseSweep:
    """一次相位掃描的結果(每相位一列 + 掃描本身的 metadata)。"""

    rows: pd.DataFrame
    n_phases_full: int
    phases_run: Tuple[int, ...] = ()
    phases_without_result: Tuple[int, ...] = ()
    single_phase_debug: bool = False
    _stats_kwargs: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return int(len(self.rows))

    @property
    def empty(self) -> bool:
        return len(self.rows) == 0

    @property
    def full_sweep(self) -> bool:
        """是否掃滿所有等價相位(正式證據的必要條件)。"""
        return (not self.single_phase_debug
                and len(self.phases_run) == int(self.n_phases_full))

    def stats(self, **kwargs: Any) -> Dict[str, Any]:
        merged = {**self._stats_kwargs, **kwargs}
        return phase_stats(
            self.rows,
            single_phase_debug=self.single_phase_debug,
            n_phases_full=self.n_phases_full,
            n_phases_requested=len(self.phases_run),
            **merged,
        )


def sweep_phases(run_phase: Callable[[int], Optional[Mapping[str, Any]]], *,
                 n_phases: int,
                 single_phase_debug: bool = False,
                 stats_kwargs: Optional[Mapping[str, Any]] = None) -> PhaseSweep:
    """跑滿所有等價再平衡相位;每相位一列。

    這是 repo 裡**唯一**的相位掃描實作(正式 IS、OS、forward 共用),
    `tests/test_phase_sweep.py` 用 AST 掃描禁止再長出第四份手寫迴圈。

    參數:
      run_phase:`run_phase(phase) -> row dict | None`。回傳 None = 該相位沒有
                結果,掃描繼續(不是失敗),但會記進 `phases_without_result`。
      n_phases:等價相位數(= 再平衡週期天數)。
      single_phase_debug:只跑 phase 0,**僅供 debug**;結果會一路標到 summary。
    """
    indices = phase_indices(n_phases, single_phase_debug=single_phase_debug)
    rows: list = []
    missing: list = []
    for phase in indices:
        row = run_phase(phase)
        if row is None:
            missing.append(phase)
            continue
        if not isinstance(row, Mapping):
            raise TypeError(
                f"run_phase(phase={phase}) 必須回傳 dict 或 None,得到 {type(row)}"
            )
        row = dict(row)
        if "phase" in row and int(row["phase"]) != int(phase):
            raise ValueError(
                f"run_phase 回報的 phase={row['phase']} 與掃描中的 {phase} 不符"
            )
        row.setdefault("phase", phase)
        rows.append(row)
    return PhaseSweep(
        rows=pd.DataFrame(rows),
        n_phases_full=int(n_phases),
        phases_run=indices,
        phases_without_result=tuple(missing),
        single_phase_debug=bool(single_phase_debug),
        _stats_kwargs=dict(stats_kwargs or {}),
    )


def combine(sweeps: Sequence[PhaseSweep]) -> pd.DataFrame:
    """把多段(例如 IS 與 OS)的相位表接成一張表。"""
    frames = [s.rows for s in sweeps if len(s.rows)]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


__all__ = [
    "PhaseSweep", "combine", "phase_indices", "phase_stats", "sweep_phases",
    "SHARPE_COLUMN", "DRAWDOWN_COLUMNS",
]
