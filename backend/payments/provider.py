"""Boundary for a future contract-enabled marketplace provider adapter.

The public Moyasar API does not establish this account's marketplace scopes,
sub-merchant identity mapping or fee/split contract. There is intentionally no
live adapter or automatic fallback to the existing tenant invoice integration.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ProviderPayment:
    reference: str
    status: str
    gross_amount: Decimal
    currency: str
    merchant_reference: str


@dataclass(frozen=True)
class ProviderSettlement:
    reference: str
    status: str
    amount: Decimal
    currency: str
    recipient_reference: str


class PaymentProvider(Protocol):
    """Only trusted, merchant-scoped reads; writes need a separate review."""

    name: str

    async def fetch_payment(self, *, merchant_reference: str, payment_reference: str) -> ProviderPayment:
        ...

    async def list_settlements(self, *, merchant_reference: str) -> Sequence[ProviderSettlement]:
        ...
