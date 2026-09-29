"""Explicit Decimal fee quotes; these are estimates until provider evidence."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")
RATE_UNIT = Decimal("0.0001")


@dataclass(frozen=True)
class AllocationQuote:
    gross_amount: Decimal
    platform_fee_amount: Decimal
    merchant_gross_share: Decimal
    fee_rate_percent: Decimal


def quote_allocation(gross_amount: Decimal, rate_percent: Decimal) -> AllocationQuote:
    """Quote one merchant's gross share and a configured platform percentage.

    Provider processing fees, tax, refunds and net settlements are deliberately
    absent: they require provider evidence and the executed fee agreement.
    """
    if not isinstance(gross_amount, Decimal) or not isinstance(rate_percent, Decimal):
        raise TypeError("Amounts and percentages must be Decimal")
    if (
        not gross_amount.is_finite()
        or not rate_percent.is_finite()
        or gross_amount <= 0
        or gross_amount != gross_amount.quantize(CENT)
        or rate_percent < 0
        or rate_percent > 100
        or rate_percent != rate_percent.quantize(RATE_UNIT)
    ):
        raise ValueError("Invalid amount or percentage precision")

    platform_fee = (gross_amount * rate_percent / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    return AllocationQuote(gross_amount, platform_fee, gross_amount - platform_fee, rate_percent)
