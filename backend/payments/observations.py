"""Dormant, provider-verified payment observation and provisional fee quote.

The caller supplies an authenticated tenant ID. No route imports this module;
there is no checkout, webhook handler, payout or order state transition here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .fees import CENT, quote_allocation
from .models import (
    MerchantPaymentAllocation,
    MerchantPaymentFeePolicy,
    MerchantPaymentProfile,
    MerchantPaymentTransaction,
)
from .provider import PaymentProvider, ProviderPayment


class PaymentObservationConflict(ValueError):
    """Provider evidence disagrees with the existing financial record."""


def _validate_payment(payment: ProviderPayment, expected_ref: str, merchant_ref: str) -> None:
    if payment.reference != expected_ref or payment.merchant_reference != merchant_ref:
        raise PaymentObservationConflict("Payment reference or merchant identity mismatch")
    amount = payment.gross_amount
    if (
        not isinstance(amount, Decimal) or not amount.is_finite()
        or amount <= 0 or amount != amount.quantize(CENT)
        or len(payment.currency) != 3 or not payment.currency.isalpha()
        or payment.currency != payment.currency.upper()
        or not payment.status
    ):
        raise PaymentObservationConflict("Invalid provider payment evidence")


async def observe_payment(
    engine: Engine,
    *,
    tenant_id: int,
    environment: str,
    payment_reference: str,
    provider: PaymentProvider,
) -> int:
    """Store one provider observation and its provisional fee quote.

    A profile must already have independent provider approval evidence and an
    effective fee policy. A repeat returns the same row without changing its
    monetary snapshot. The provider call occurs outside the write transaction.
    The caller must never use the resulting quote as proof of settlement.
    """
    if tenant_id <= 0 or environment not in ("test", "live") or not payment_reference:
        raise ValueError("Invalid payment scope")
    scope = dict(tenant_id=tenant_id, provider=provider.name, environment=environment)
    profiles = MerchantPaymentProfile.__table__
    payments = MerchantPaymentTransaction.__table__
    fees = MerchantPaymentFeePolicy.__table__
    allocations = MerchantPaymentAllocation.__table__

    with engine.connect() as connection:
        profile = connection.execute(
            sa.select(profiles).where(
                profiles.c.tenant_id == tenant_id,
                profiles.c.provider == provider.name,
                profiles.c.environment == environment,
                profiles.c.onboarding_status == "approved",
            )
        ).mappings().one_or_none()
    if profile is None or not profile["provider_merchant_ref"]:
        raise PaymentObservationConflict("Merchant is not provider-approved")

    observed = await provider.fetch_payment(
        merchant_reference=profile["provider_merchant_ref"],
        payment_reference=payment_reference,
    )
    _validate_payment(observed, payment_reference, profile["provider_merchant_ref"])
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        # Recheck after the network call; approval could have been suspended.
        current = connection.execute(
            sa.select(profiles.c.provider_merchant_ref).where(
                profiles.c.tenant_id == tenant_id,
                profiles.c.provider == provider.name,
                profiles.c.environment == environment,
                profiles.c.onboarding_status == "approved",
            )
        ).scalar_one_or_none()
        if current != observed.merchant_reference:
            raise PaymentObservationConflict("Merchant approval changed")

        existing = connection.execute(
            sa.select(payments).where(
                payments.c.provider == provider.name,
                payments.c.environment == environment,
                payments.c.provider_payment_ref == payment_reference,
            )
        ).mappings().one_or_none()
        if existing is not None:
            if (
                existing["tenant_id"] != tenant_id
                or existing["gross_amount"] != observed.gross_amount
                or existing["currency"] != observed.currency
                or existing["verification_state"] != "provider_confirmed"
            ):
                raise PaymentObservationConflict("Existing payment scope, amount or evidence conflicts")
            allocation = connection.execute(
                sa.select(allocations.c.id).where(
                    allocations.c.tenant_id == tenant_id,
                    allocations.c.transaction_id == existing["id"],
                    allocations.c.gross_amount == observed.gross_amount,
                )
            ).scalar_one_or_none()
            if allocation is None:
                raise PaymentObservationConflict("Existing payment has no matching fee snapshot")
            return existing["id"]

        policy = connection.execute(
            sa.select(fees.c.rate_percent).where(
                fees.c.tenant_id == tenant_id,
                fees.c.provider == provider.name,
                fees.c.environment == environment,
                fees.c.effective_at <= now,
            ).order_by(fees.c.effective_at.desc()).limit(1)
        ).scalar_one_or_none()
        if policy is None:
            raise PaymentObservationConflict("No effective platform fee policy")
        quote = quote_allocation(observed.gross_amount, policy)
        payment_id = connection.execute(
            sa.insert(payments).values(
                **scope,
                provider_payment_ref=payment_reference,
                currency=observed.currency,
                gross_amount=observed.gross_amount,
                verification_state="provider_confirmed",
                provider_status=observed.status,
                provider_observed_at=now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            sa.insert(allocations).values(
                tenant_id=tenant_id,
                transaction_id=payment_id,
                gross_amount=quote.gross_amount,
                platform_fee_amount=quote.platform_fee_amount,
                merchant_gross_share=quote.merchant_gross_share,
                fee_rate_percent=quote.fee_rate_percent,
            )
        )
        return payment_id
