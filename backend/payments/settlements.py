"""Dormant, provider-verified settlement and settlement-line observation.

A settlement row is what the provider reported it transferred; a settlement
*line* of type ``payment`` that names one of this tenant's observed payments is
the only evidence that the payment was settled. Nothing here computes a
balance, instructs a payout or changes an order. The caller supplies an
authenticated tenant scope; no route imports this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .fees import CENT
from .models import (
    MerchantPaymentProfile,
    MerchantPaymentSettlement,
    MerchantPaymentSettlementLine,
    MerchantPaymentTransaction,
)
from .provider import (
    PaymentProvider,
    ProviderSettlement,
    ProviderSettlementLine,
    SettlementLinesProvider,
)

LINE_TYPES = frozenset({"payment", "fee", "refund", "chargeback", "adjustment", "other"})


class SettlementObservationConflict(ValueError):
    """Provider evidence disagrees with the existing financial record."""


@dataclass(frozen=True)
class SettlementObservation:
    settlement_ids: Sequence[int]
    inserted: int
    status_updated: int


@dataclass(frozen=True)
class SettlementLineObservation:
    inserted: int
    replayed: int
    linked_to_payments: int


def _valid_currency(currency: str) -> bool:
    return len(currency) == 3 and currency.isalpha() and currency == currency.upper()


def _valid_amount(amount, *, allow_negative: bool) -> bool:
    return (
        isinstance(amount, Decimal) and amount.is_finite() and amount == amount.quantize(CENT)
        and (amount != 0 if allow_negative else amount > 0)
    )


def _approved_merchant_ref(connection, *, tenant_id: int, provider: str, environment: str) -> str:
    profiles = MerchantPaymentProfile.__table__
    merchant_ref = connection.execute(
        sa.select(profiles.c.provider_merchant_ref).where(
            profiles.c.tenant_id == tenant_id,
            profiles.c.provider == provider,
            profiles.c.environment == environment,
            profiles.c.onboarding_status == "approved",
        )
    ).scalar_one_or_none()
    if not merchant_ref:
        raise SettlementObservationConflict("Merchant is not provider-approved")
    return merchant_ref


async def observe_settlements(
    engine: Engine, *, tenant_id: int, environment: str, provider: PaymentProvider
) -> SettlementObservation:
    """Record the provider's settlements for one approved merchant, idempotently.

    Amount, currency and recipient of an existing row may never change; a
    changed provider status is recorded as the newer provider observation.
    A settlement reference already stored for another tenant is a conflict.
    """
    if tenant_id <= 0 or environment not in ("test", "live"):
        raise ValueError("Invalid settlement scope")
    with engine.connect() as connection:
        merchant_ref = _approved_merchant_ref(
            connection, tenant_id=tenant_id, provider=provider.name, environment=environment
        )
    reported = list(await provider.list_settlements(merchant_reference=merchant_ref))
    for item in reported:
        if (
            not isinstance(item, ProviderSettlement) or not item.reference or not item.status
            or not _valid_amount(item.amount, allow_negative=False) or not _valid_currency(item.currency)
            or item.recipient_reference is None
        ):
            raise SettlementObservationConflict("Invalid provider settlement evidence")

    settlements = MerchantPaymentSettlement.__table__
    now = datetime.now(timezone.utc)
    ids, inserted, updated = [], 0, 0
    with engine.begin() as connection:
        current_ref = _approved_merchant_ref(
            connection, tenant_id=tenant_id, provider=provider.name, environment=environment
        )
        if current_ref != merchant_ref:
            raise SettlementObservationConflict("Merchant approval changed")
        for item in reported:
            existing = connection.execute(
                sa.select(settlements).where(
                    settlements.c.provider == provider.name,
                    settlements.c.environment == environment,
                    settlements.c.provider_settlement_ref == item.reference,
                )
            ).mappings().one_or_none()
            if existing is None:
                ids.append(connection.execute(
                    sa.insert(settlements).values(
                        tenant_id=tenant_id, provider=provider.name, environment=environment,
                        provider_settlement_ref=item.reference, recipient_ref=item.recipient_reference,
                        currency=item.currency, reported_amount=item.amount,
                        provider_status=item.status, provider_observed_at=now,
                    )
                ).inserted_primary_key[0])
                inserted += 1
                continue
            if (
                existing["tenant_id"] != tenant_id
                or Decimal(existing["reported_amount"]) != item.amount
                or existing["currency"] != item.currency
                or (existing["recipient_ref"] or "") != item.recipient_reference
            ):
                raise SettlementObservationConflict("Existing settlement scope, amount or recipient conflicts")
            if existing["provider_status"] != item.status:
                connection.execute(
                    sa.update(settlements).where(settlements.c.id == existing["id"]).values(
                        provider_status=item.status, provider_observed_at=now,
                    )
                )
                updated += 1
            ids.append(existing["id"])
    return SettlementObservation(tuple(ids), inserted, updated)


async def observe_settlement_lines(
    engine: Engine,
    *,
    tenant_id: int,
    environment: str,
    settlement_reference: str,
    provider: SettlementLinesProvider,
) -> SettlementLineObservation:
    """Record a settlement's lines and link ``payment`` lines to observed payments.

    The settlement must already be recorded for this tenant. A payment line is
    linked only when its payment reference names a payment this same tenant
    observed as provider-confirmed; a reference owned by another tenant is a
    conflict and nothing is written. Unlinked lines are stored for the
    reconciliation report but never mark any payment settled.
    """
    if tenant_id <= 0 or environment not in ("test", "live") or not settlement_reference:
        raise ValueError("Invalid settlement line scope")
    settlements = MerchantPaymentSettlement.__table__
    lines = MerchantPaymentSettlementLine.__table__
    payments = MerchantPaymentTransaction.__table__

    with engine.connect() as connection:
        merchant_ref = _approved_merchant_ref(
            connection, tenant_id=tenant_id, provider=provider.name, environment=environment
        )
        settlement = connection.execute(
            sa.select(settlements.c.tenant_id, settlements.c.currency).where(
                settlements.c.provider == provider.name,
                settlements.c.environment == environment,
                settlements.c.provider_settlement_ref == settlement_reference,
            )
        ).mappings().one_or_none()
    if settlement is None or settlement["tenant_id"] != tenant_id:
        raise SettlementObservationConflict("Settlement is not recorded for this tenant")

    reported = list(await provider.list_settlement_lines(
        merchant_reference=merchant_ref, settlement_reference=settlement_reference
    ))
    for line in reported:
        if (
            not isinstance(line, ProviderSettlementLine)
            or line.settlement_reference != settlement_reference
            or not line.line_reference or line.line_type not in LINE_TYPES
            or not _valid_amount(line.amount, allow_negative=True)
            or line.currency != settlement["currency"]
            or (line.line_type == "payment" and not line.payment_reference)
        ):
            raise SettlementObservationConflict("Invalid provider settlement line evidence")

    now = datetime.now(timezone.utc)
    inserted, replayed, linked = 0, 0, 0
    with engine.begin() as connection:
        for line in reported:
            transaction_id = None
            if line.payment_reference:
                owner = connection.execute(
                    sa.select(payments.c.id, payments.c.tenant_id, payments.c.verification_state).where(
                        payments.c.provider == provider.name,
                        payments.c.environment == environment,
                        payments.c.provider_payment_ref == line.payment_reference,
                    )
                ).mappings().one_or_none()
                if owner is not None:
                    if owner["tenant_id"] != tenant_id:
                        raise SettlementObservationConflict(
                            "Settlement line names a payment observed for another tenant"
                        )
                    if owner["verification_state"] == "provider_confirmed":
                        transaction_id = owner["id"]
            existing = connection.execute(
                sa.select(lines).where(
                    lines.c.provider == provider.name,
                    lines.c.environment == environment,
                    lines.c.provider_settlement_ref == settlement_reference,
                    lines.c.provider_line_ref == line.line_reference,
                )
            ).mappings().one_or_none()
            if existing is not None:
                if (
                    existing["tenant_id"] != tenant_id
                    or Decimal(existing["amount"]) != line.amount
                    or existing["line_type"] != line.line_type
                    or (existing["provider_payment_ref"] or None) != line.payment_reference
                ):
                    raise SettlementObservationConflict("Existing settlement line conflicts with provider evidence")
                if existing["transaction_id"] is None and transaction_id is not None:
                    # The payment was observed after the line: link it now, never relink.
                    connection.execute(
                        sa.update(lines).where(lines.c.id == existing["id"]).values(transaction_id=transaction_id)
                    )
                    linked += 1
                replayed += 1
                continue
            connection.execute(
                sa.insert(lines).values(
                    tenant_id=tenant_id, provider=provider.name, environment=environment,
                    provider_settlement_ref=settlement_reference, provider_line_ref=line.line_reference,
                    line_type=line.line_type, transaction_id=transaction_id,
                    provider_payment_ref=line.payment_reference, currency=line.currency,
                    amount=line.amount, provider_observed_at=now,
                )
            )
            inserted += 1
            if transaction_id is not None:
                linked += 1
    return SettlementLineObservation(inserted, replayed, linked)
