"""Dormant provider boundaries for Nahlah AI merchant payments.

Three provider surfaces are kept apart because the provider sells them apart:

* **Platform API** — registering merchants and reading their onboarding/KYB
  status. Documentation and sandbox are only available after a signed
  agreement, so no request or response schema is assumed here.
* **Payments** — merchant-scoped reads of payments, settlements and settlement
  lines. ``observe_payment`` and ``observe_settlements`` consume these reads.
* **Payout** — a separate product funded from a bank account. It is *not* the
  settlement rail and Nahlah AI holds no wallet or stored value.

Every ``Dormant*`` implementation fails closed with ``ProviderContractPending``.
No network client exists in this package. Writes to any provider (registering
a merchant, creating a payment, instructing a payout) need a separate review
and are intentionally absent from these read-only protocols.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Protocol, Sequence


class ProviderContractPending(RuntimeError):
    """The provider surface is not contracted; nothing may be called yet."""


@dataclass(frozen=True)
class ProviderCapabilities:
    """What the provider has told Nahlah AI it offers; design input, not a contract."""

    provider: str
    as_of: date
    marketplace_split_supported: bool
    platform_api_offered: bool
    platform_api_contracted: bool
    payout_offered: bool
    payout_contracted: bool
    wallet_supported: bool
    separate_account_per_merchant: bool
    platform_fee_terms_agreed: bool
    settlement_terms_agreed: bool

    @property
    def any_surface_contracted(self) -> bool:
        return self.platform_api_contracted or self.payout_contracted


# Moyasar's written update dated 2026-10-03. Marketplace split is not
# available; the offered alternative is a Platform API with one independent
# account per merchant. Documentation, sandbox, fees and settlement terms are
# all pending a signed agreement. Nothing here is a contractual commitment.
MOYASAR_CAPABILITIES_2026_10_03 = ProviderCapabilities(
    provider="moyasar",
    as_of=date(2026, 10, 3),
    marketplace_split_supported=False,
    platform_api_offered=True,
    platform_api_contracted=False,
    payout_offered=True,
    payout_contracted=False,
    wallet_supported=False,
    separate_account_per_merchant=True,
    platform_fee_terms_agreed=False,
    settlement_terms_agreed=False,
)


# ── Platform API (merchant onboarding) ──────────────────────────────────────


@dataclass(frozen=True)
class ProviderMerchantRegistration:
    """Provider's view of one merchant registration, status word verbatim."""

    registration_reference: str
    status: str
    merchant_reference: Optional[str]
    observed_at: datetime


class PlatformOnboardingProvider(Protocol):
    """Read-only Platform API surface. Registration *submission* is a write."""

    name: str

    async def fetch_registration(self, *, registration_reference: str) -> ProviderMerchantRegistration:
        ...


# ── Payments (merchant-scoped reads) ────────────────────────────────────────


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


@dataclass(frozen=True)
class ProviderSettlementLine:
    """One line of a provider settlement; ``payment_reference`` ties it to a payment."""

    settlement_reference: str
    line_reference: str
    line_type: str
    amount: Decimal
    currency: str
    payment_reference: Optional[str]


class PaymentProvider(Protocol):
    """Only trusted, merchant-scoped reads; writes need a separate review."""

    name: str

    async def fetch_payment(self, *, merchant_reference: str, payment_reference: str) -> ProviderPayment:
        ...

    async def list_settlements(self, *, merchant_reference: str) -> Sequence[ProviderSettlement]:
        ...


class SettlementLinesProvider(Protocol):
    """Settlement-line reads; the only provider evidence that a payment settled."""

    name: str

    async def list_settlement_lines(
        self, *, merchant_reference: str, settlement_reference: str
    ) -> Sequence[ProviderSettlementLine]:
        ...


# ── Payout (separate product, never the settlement rail) ────────────────────


@dataclass(frozen=True)
class ProviderPayout:
    reference: str
    status: str
    amount: Decimal
    currency: str


class PayoutProvider(Protocol):
    """Read-only payout surface. Nahlah AI never instructs a payout from here."""

    name: str

    async def fetch_payout(self, *, payout_reference: str) -> ProviderPayout:
        ...


# ── Fail-closed dormant implementations ─────────────────────────────────────


def _pending(surface: str, provider: str) -> ProviderContractPending:
    return ProviderContractPending(
        f"{provider} {surface} is not contracted for Nahlah AI; documentation, sandbox "
        "and credentials are pending a signed agreement"
    )


class DormantPlatformOnboardingProvider:
    name = "moyasar"

    async def fetch_registration(self, *, registration_reference: str) -> ProviderMerchantRegistration:
        raise _pending("Platform API", self.name)


class DormantPaymentProvider:
    name = "moyasar"

    async def fetch_payment(self, *, merchant_reference: str, payment_reference: str) -> ProviderPayment:
        raise _pending("Payments API", self.name)

    async def list_settlements(self, *, merchant_reference: str) -> Sequence[ProviderSettlement]:
        raise _pending("Settlements API", self.name)

    async def list_settlement_lines(
        self, *, merchant_reference: str, settlement_reference: str
    ) -> Sequence[ProviderSettlementLine]:
        raise _pending("Settlement lines API", self.name)


class DormantPayoutProvider:
    name = "moyasar"

    async def fetch_payout(self, *, payout_reference: str) -> ProviderPayout:
        raise _pending("Payout API", self.name)
