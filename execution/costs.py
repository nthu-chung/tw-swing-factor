# -*- coding: utf-8 -*-
"""券商成本與 long-only 現股訂單股數計算。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
from typing import Union


Number = Union[int, float, str, Decimal]


class OrderSizeMode(str, Enum):
    RESEARCH_FRACTIONAL = "research_fractional"
    REGULAR_LOT = "regular_lot"
    ODD_LOT_PROXY = "odd_lot_proxy"


@dataclass(frozen=True)
class TaiwanStockCostModel:
    """普通股成本模型；最低手續費是券商設定，不是交易所統一規則。"""

    commission_rate: Decimal = Decimal("0.001425")
    minimum_commission: Decimal = Decimal("0")
    sell_tax_rate: Decimal = Decimal("0.003")

    def __post_init__(self) -> None:
        for name in ("commission_rate", "minimum_commission", "sell_tax_rate"):
            value = Decimal(str(getattr(self, name)))
            if value < 0:
                raise ValueError(f"{name} 不得為負")
            object.__setattr__(self, name, value)

    def commission(self, gross_value: Number) -> Decimal:
        gross = Decimal(str(gross_value))
        if gross < 0:
            raise ValueError("成交金額不得為負")
        if gross == 0:
            return Decimal("0")
        return max(gross * self.commission_rate, self.minimum_commission)

    def buy_cash_required(self, shares: Number, price: Number) -> Decimal:
        gross = Decimal(str(shares)) * Decimal(str(price))
        return gross + self.commission(gross)

    def sell_proceeds(self, shares: Number, price: Number) -> Decimal:
        gross = Decimal(str(shares)) * Decimal(str(price))
        return gross - self.commission(gross) - gross * self.sell_tax_rate


def size_long_order(
    cash_budget: Number,
    price: Number,
    *,
    mode: OrderSizeMode | str,
    costs: TaiwanStockCostModel,
    regular_lot_shares: int = 1000,
) -> tuple[float, float]:
    """回傳 `(股數, 含買進手續費成本)`，絕不超出 cash_budget。"""
    budget = Decimal(str(cash_budget))
    px = Decimal(str(price))
    selected_mode = OrderSizeMode(mode)
    if budget <= 0 or px <= 0:
        return 0.0, 0.0

    if selected_mode == OrderSizeMode.RESEARCH_FRACTIONAL:
        # 先用比例費率求解，再處理可能較高的券商最低手續費。
        shares = budget / (px * (Decimal("1") + costs.commission_rate))
        gross = shares * px
        commission = costs.commission(gross)
        if gross + commission > budget:
            shares = max((budget - commission) / px, Decimal("0"))
        total = costs.buy_cash_required(shares, px)
        return float(shares), float(total)

    unit = regular_lot_shares if selected_mode == OrderSizeMode.REGULAR_LOT else 1
    units = int((budget / (px * unit)).to_integral_value(rounding=ROUND_FLOOR))
    while units > 0:
        shares = Decimal(units * unit)
        total = costs.buy_cash_required(shares, px)
        if total <= budget:
            return float(shares), float(total)
        units -= 1
    return 0.0, 0.0
