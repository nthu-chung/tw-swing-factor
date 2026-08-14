# -*- coding: utf-8 -*-
"""統一的 IS/OS 時間切割，作為 evaluation 套件的唯一切割實作。

研究腳本以前各自用索引、日曆日近似或事件分位數切割，造成 70/30 的定義、
embargo 長度與 off-by-one 行為不一致。這個模組把切割規則集中在單一位置：

- ``ratio``：先排除 embargo，再按交易日切割，例如 0.70 = IS:OS 為 7:3。
- ``weeks``：從資料尾端往回保留固定 OS 週數，再留交易日 embargo，往前取固定
  IS 週數；更早資料不會偷偷混入 IS。

所有輸入都先轉成排序後的唯一交易日。IS、embargo、OS 彼此不重疊；若資料不足，
直接 raise，避免把空窗或只有幾天的 OS 當成有效驗證。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Optional

import pandas as pd

import config


@dataclass(frozen=True)
class EvaluationSplit:
    """一組不可變的時間切割及其可稽核 metadata。"""

    mode: str
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    os_start: pd.Timestamp
    os_end: pd.Timestamp
    n_total: int
    n_is: int
    n_embargo: int
    n_os: int
    is_ratio: Optional[float] = None
    is_weeks: Optional[int] = None
    os_weeks: Optional[int] = None

    @property
    def is_window(self) -> tuple[str, str]:
        return str(self.is_start.date()), str(self.is_end.date())

    @property
    def os_window(self) -> tuple[str, str]:
        return str(self.os_start.date()), str(self.os_end.date())

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in ("is_start", "is_end", "os_start", "os_end"):
            out[key] = str(out[key].date())
        return out


def _trading_dates(values: Iterable) -> pd.DatetimeIndex:
    dates = pd.to_datetime(pd.Index(list(values)), errors="coerce")
    dates = pd.DatetimeIndex(dates).dropna().sort_values().unique()
    if len(dates) < 3:
        raise ValueError(f"IS/OS 切割至少需要 3 個有效交易日，目前只有 {len(dates)} 個")
    return dates


def build_evaluation_split(
    dates: Iterable,
    *,
    mode: Optional[str] = None,
    is_ratio: Optional[float] = None,
    is_weeks: Optional[int] = None,
    os_weeks: Optional[int] = None,
    embargo_days: Optional[int] = None,
    minimum_embargo_days: int = 0,
) -> EvaluationSplit:
    """用統一規則建立 IS/OS 切割。

    ``embargo_days`` 以輸入序列中的交易日計數。帶未來標籤的研究應傳入
    ``minimum_embargo_days=標籤視窗``；若使用者把全域 embargo 設得更短，這裡會
    fail-closed，而不是允許標籤跨過邊界。
    """

    dts = _trading_dates(dates)
    split_mode = (mode or config.EVAL_SPLIT_MODE).strip().lower()
    gap = config.EMBARGO_DAYS if embargo_days is None else int(embargo_days)
    min_gap = int(minimum_embargo_days)
    if gap < 0 or min_gap < 0:
        raise ValueError("embargo_days 不可為負數")
    if gap < min_gap:
        raise ValueError(
            f"embargo={gap} 交易日小於未來標籤視窗 {min_gap} 日，會造成 IS/OS 洩漏"
        )

    n = len(dts)
    if split_mode == "ratio":
        ratio = config.IS_OS_SPLIT if is_ratio is None else float(is_ratio)
        if not 0 < ratio < 1:
            raise ValueError(f"IS ratio 必須介於 0 與 1 之間，目前為 {ratio}")
        # 7:3 是 IS:OS 的比例；embargo 是額外排除區，不能偷吃 OS 的 30%。
        usable = n - gap
        n_is = int(usable * ratio)
        os_idx = n_is + gap
        if n_is < 1 or os_idx >= n:
            raise ValueError(
                f"資料不足以切 IS:OS={ratio:.2f}:{1-ratio:.2f} + embargo={gap} 日："
                f"總交易日 {n}"
            )
        is_start_idx = 0
        is_end_idx = n_is - 1
        ratio_meta, is_weeks_meta, os_weeks_meta = ratio, None, None
    elif split_mode == "weeks":
        iw = config.IS_WEEKS if is_weeks is None else int(is_weeks)
        ow = config.OS_WEEKS if os_weeks is None else int(os_weeks)
        if iw <= 0 or ow <= 0:
            raise ValueError(f"固定週數必須為正整數，目前 IS={iw}、OS={ow}")

        os_threshold = dts[-1] - pd.Timedelta(weeks=ow)
        os_idx = int(dts.searchsorted(os_threshold, side="left"))
        is_end_idx = os_idx - gap - 1
        if is_end_idx < 0 or os_idx >= n:
            raise ValueError(
                f"資料不足以切 IS={iw}週、OS={ow}週、embargo={gap} 日：總交易日 {n}"
            )
        is_threshold = dts[is_end_idx] - pd.Timedelta(weeks=iw)
        is_start_idx = int(dts.searchsorted(is_threshold, side="left"))
        if is_start_idx > is_end_idx:
            raise ValueError("固定週數切割後 IS 為空")
        ratio_meta, is_weeks_meta, os_weeks_meta = None, iw, ow
    else:
        raise ValueError(f"未知 EVAL_SPLIT_MODE={split_mode!r}；只接受 'ratio' 或 'weeks'")

    n_is = is_end_idx - is_start_idx + 1
    n_os = n - os_idx
    actual_gap = os_idx - is_end_idx - 1
    if n_is < 2 or n_os < 2:
        raise ValueError(f"IS/OS 各至少需要 2 個交易日，目前 IS={n_is}、OS={n_os}")

    return EvaluationSplit(
        mode=split_mode,
        is_start=pd.Timestamp(dts[is_start_idx]),
        is_end=pd.Timestamp(dts[is_end_idx]),
        os_start=pd.Timestamp(dts[os_idx]),
        os_end=pd.Timestamp(dts[-1]),
        n_total=n,
        n_is=n_is,
        n_embargo=actual_gap,
        n_os=n_os,
        is_ratio=ratio_meta,
        is_weeks=is_weeks_meta,
        os_weeks=os_weeks_meta,
    )
