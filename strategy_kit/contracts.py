# -*- coding: utf-8 -*-
"""Python-first 策略的型別契約(`DataRequirements` / `SignalContext` / Protocol)。

為什麼要 typed 契約而不是「大家自己記得傳對」:研究規格 §5.3 列的每一項
(必要欄位、warmup、價格口徑、產業 PIT、最小橫斷面、資料時效)都對應一個
「缺了就會安靜產生假訊號」的失敗模式 —— 缺欄位回 NaN、warmup 不足讓前 N 天的
因子是半成品、價格口徑不符讓除息缺口變成假跌。這些都不會 crash,只會讓分數變差
一點點,而分數差一點點正好是研究者最不會懷疑的東西。

所以這裡的規則是:**缺資訊一律 fail-closed,不得用 0、空值或 all-False 靜默替代**。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class DataRequirements:
    """策略對輸入 panel 的要求(研究規格 §5.3)。"""

    required_columns: Tuple[str, ...]
    optional_columns: Tuple[str, ...] = ()
    warmup_bars: int = 0
    price_adjustment_requirement: str = "adjusted_total_return_compatible"
    requires_industry: bool = False
    industry_pit_required: bool = False
    minimum_cross_section: int = 1
    maximum_data_lag_days: Optional[int] = None
    external_dataset_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.required_columns:
            raise ValueError("required_columns 不得為空:策略必須說得出它吃什麼")
        if int(self.warmup_bars) < 0:
            raise ValueError("warmup_bars 不得為負")
        if int(self.minimum_cross_section) < 1:
            raise ValueError("minimum_cross_section 至少為 1")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DataRequirements":
        """接受策略以 dict 宣告(reference strategy 就是這樣),轉成 typed。"""
        def _tuple(key: str) -> Tuple[str, ...]:
            return tuple(str(x) for x in (raw.get(key) or ()))
        return cls(
            required_columns=_tuple("required_columns"),
            optional_columns=_tuple("optional_columns"),
            warmup_bars=int(raw.get("warmup_bars", 0) or 0),
            price_adjustment_requirement=str(
                raw.get("price_adjustment_requirement")
                or "adjusted_total_return_compatible"),
            requires_industry=bool(raw.get("requires_industry", False)),
            industry_pit_required=bool(raw.get("industry_pit_required", False)),
            minimum_cross_section=int(raw.get("minimum_cross_section", 1) or 1),
            maximum_data_lag_days=(
                None if raw.get("maximum_data_lag") in (None, "")
                else int(raw["maximum_data_lag"])),
            external_dataset_ids=_tuple("external_dataset_ids"),
        )

    def validate_panel(self, panel: pd.DataFrame, *, who: str) -> None:
        """對輸入 panel 做 fail-closed 檢查。

        刻意在**呼叫策略之前**做:策略拿到缺欄位的 panel 多半會回 NaN 或半成品
        分數,而不是報錯 —— 那種失敗在下游看起來就只是「這個策略比較弱」。
        """
        if not isinstance(panel, pd.DataFrame) or panel.empty:
            raise ValueError(f"[fail-closed] {who}:panel 為空,無法產生訊號")
        missing = [c for c in self.required_columns if c not in panel.columns]
        if missing:
            raise ValueError(
                f"[fail-closed] {who}:panel 缺必要欄位 {missing};"
                "不得用 0/空值替代(那會讓因子變成半成品而不是報錯)")
        n_days = panel["date"].nunique() if "date" in panel.columns else 0
        if n_days < int(self.warmup_bars):
            raise ValueError(
                f"[fail-closed] {who}:panel 只有 {n_days} 個交易日,"
                f"少於 warmup_bars={self.warmup_bars};前段因子會是半成品")
        if "stock_id" in panel.columns:
            widest = int(panel.groupby("date")["stock_id"].nunique().max())
            if widest < int(self.minimum_cross_section):
                raise ValueError(
                    f"[fail-closed] {who}:最寬的一天只有 {widest} 檔,"
                    f"少於 minimum_cross_section={self.minimum_cross_section}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SignalContext:
    """一次 `make_signals()` 呼叫的執行脈絡(研究規格 §5.4)。

    `mode` 決定這份訊號可以被用來做什麼:只有 `validation` / `forward` 產出的
    結果才可能升級證據等級;`discovery` 是探索,`live` 是人工流程。
    """

    as_of: pd.Timestamp
    start_date: Optional[pd.Timestamp] = None
    end_date: Optional[pd.Timestamp] = None
    universe_provider_id: str = "unknown"
    eligibility_rule_id: str = "unknown"
    phase: int = 0
    rng_seed: int = 0
    campaign_id: str = ""
    candidate_id: str = ""
    mode: str = "discovery"

    _MODES = ("discovery", "validation", "forward", "live")

    def __post_init__(self) -> None:
        if self.mode not in self._MODES:
            raise ValueError(
                f"[fail-closed] 未知的 SignalContext.mode={self.mode!r};"
                f"只接受 {self._MODES}")
        if self.start_date is not None and self.end_date is not None:
            if pd.Timestamp(self.start_date) > pd.Timestamp(self.end_date):
                raise ValueError("SignalContext 的 start_date 晚於 end_date")

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        for key in ("as_of", "start_date", "end_date"):
            val = out.get(key)
            out[key] = None if val is None else str(pd.Timestamp(val).date())
        return out


@runtime_checkable
class CrossSectionalStrategy(Protocol):
    """repo 內註冊的 Python 策略最小介面(研究規格 §5.2)。

    `make_signals()` 只負責產生訊號:不管理 cash/positions、不呼叫回測、
    不寫檔、不打網路、不改全域 `config`。評分由 evaluator 對完整
    `SignalFrame → PortfolioPolicy → Event Engine` 的結果產生,避免策略自己
    挑一個對自己有利的評分方式。
    """

    name: str
    version: str

    def data_requirements(self) -> Mapping[str, Any]: ...
    def default_parameters(self) -> Mapping[str, Any]: ...
    def parameter_space(self) -> Mapping[str, Any]: ...
    def make_signals(self, panel: pd.DataFrame,
                     params: Optional[Mapping[str, Any]] = None,
                     context: Any = None) -> pd.DataFrame: ...
