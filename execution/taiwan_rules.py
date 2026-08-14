# -*- coding: utf-8 -*-
"""臺灣上市／上櫃普通股的版本化交易規則。

目前只涵蓋本 repo 的 long-only 普通股。ETF、權證、ETN、債券有不同升降單位與
稅率，傳入非普通股時應由上層拒絕，而不是沿用股票規則。

官方依據（2026-08-14 核對）：
- TWSE 營業細則第 62 條：普通股升降單位。
- TWSE 營業細則第 63 條：開盤競價基準 ±10%，不足一個 tick 時以一個 tick 計。
- TWSE／TPEx 交易制度：整股 1,000 股、零股 1 股、新上市櫃普通股首五日例外。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from enum import Enum
from typing import Optional, Union


Number = Union[int, float, str, Decimal]
MIN_STOCK_PRICE = Decimal("0.01")


class PriceDirection(str, Enum):
    DOWN = "down"
    UP = "up"
    NEAREST = "nearest"


@dataclass(frozen=True)
class PriceLimits:
    reference: Decimal
    lower: Optional[Decimal]
    upper: Optional[Decimal]
    unlimited: bool


@dataclass(frozen=True)
class TaiwanMarketRules:
    """目前適用普通股的規則版本；較舊歷史應另增版本，不可靜默套用。"""

    version: str = "tw-stock-2015-06-01"
    effective_from: date = date(2015, 6, 1)
    daily_limit_rate: Decimal = Decimal("0.10")
    regular_lot_shares: int = 1000
    odd_lot_shares: int = 1
    new_listing_unlimited_days: int = 5


CURRENT_RULES = TaiwanMarketRules()


def _decimal(value: Number) -> Decimal:
    out = value if isinstance(value, Decimal) else Decimal(str(value))
    if not out.is_finite():
        raise ValueError(f"價格必須是有限數值，目前為 {value!r}")
    return out


def stock_tick_size(price: Number) -> Decimal:
    """回傳普通股在指定價格帶的合法升降單位。"""
    p = _decimal(price)
    if p < MIN_STOCK_PRICE:
        raise ValueError("普通股價格不得低於 0.01 元")
    if p < Decimal("10"):
        return Decimal("0.01")
    if p < Decimal("50"):
        return Decimal("0.05")
    if p < Decimal("100"):
        return Decimal("0.10")
    if p < Decimal("500"):
        return Decimal("0.50")
    if p < Decimal("1000"):
        return Decimal("1")
    return Decimal("5")


def snap_stock_price(price: Number, direction: PriceDirection | str) -> Decimal:
    """把價格調整成普通股合法價位。

    漲停價使用 DOWN，跌停價使用 UP，確保兩者都不超出法定百分比範圍。
    """
    p = max(_decimal(price), MIN_STOCK_PRICE)
    tick = stock_tick_size(p)
    mode = PriceDirection(direction)
    rounding = {
        PriceDirection.DOWN: ROUND_FLOOR,
        PriceDirection.UP: ROUND_CEILING,
        PriceDirection.NEAREST: ROUND_HALF_UP,
    }[mode]
    units = (p / tick).to_integral_value(rounding=rounding)
    return max(units * tick, MIN_STOCK_PRICE)


def _next_legal_price(price: Decimal) -> Decimal:
    candidate = price + stock_tick_size(price)
    return snap_stock_price(candidate, PriceDirection.UP)


def _previous_legal_price(price: Decimal) -> Decimal:
    candidate = max(price - stock_tick_size(price), MIN_STOCK_PRICE)
    return snap_stock_price(candidate, PriceDirection.DOWN)


def stock_price_limits(
    reference_price: Number,
    *,
    unlimited: bool = False,
    rules: TaiwanMarketRules = CURRENT_RULES,
) -> PriceLimits:
    """依開盤競價基準計算普通股當日合法漲跌停價。

    `unlimited=True` 用於符合資格的新上市櫃普通股首五個交易日。上櫃轉上市、上市
    轉上櫃等例外必須由 PIT lifecycle 資料先判定，這裡不猜證券身分。
    """
    ref = _decimal(reference_price)
    if ref < MIN_STOCK_PRICE:
        raise ValueError("開盤競價基準不得低於 0.01 元")
    if unlimited:
        return PriceLimits(ref, None, None, True)

    upper_raw = ref * (Decimal("1") + rules.daily_limit_rate)
    lower_raw = ref * (Decimal("1") - rules.daily_limit_rate)
    upper = snap_stock_price(upper_raw, PriceDirection.DOWN)
    lower = snap_stock_price(lower_raw, PriceDirection.UP)

    # 第 63 條：升降幅度未滿最小升降單位時，仍按一個最小升降單位計算。
    if upper <= ref:
        upper = _next_legal_price(ref)
    if lower >= ref:
        lower = _previous_legal_price(ref)
    return PriceLimits(ref, lower, upper, False)


def is_new_listing_unlimited(
    trading_day_number: Optional[int],
    *,
    transferred_listing: bool = False,
    rules: TaiwanMarketRules = CURRENT_RULES,
) -> bool:
    """判斷是否落在合格初次上市櫃普通股的首五個交易日。"""
    if trading_day_number is None or transferred_listing:
        return False
    if trading_day_number < 1:
        raise ValueError("trading_day_number 必須從 1 開始")
    return trading_day_number <= rules.new_listing_unlimited_days
