"""Tenant-scoped read model for the dormant merchant payments dashboard.

This module is deliberately route-free: it exposes no customer or merchant API
and performs no provider writes or money movement. It summarizes only records
already stored with provider evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .models import (
    MerchantPaymentAllocation,
    MerchantPaymentProfile,
    MerchantPaymentSettlement,
    MerchantPaymentTransaction,
)


@dataclass(frozen=True)
class PaymentSummary:
    onboarding_status: str
    currency: str
    confirmed_gross: Decimal
    provisional_platform_fees: Decimal
    provisional_merchant_share: Decimal
    provider_reported_settlements: Decimal
    confirmed_payment_count: int
    settlement_count: int


def merchant_payment_summary(
    engine: Engine, *, tenant_id: int, provider: str, environment: str, currency: str = "SAR"
) -> PaymentSummary:
    """Return one merchant's evidence-backed summary; never a stored-value balance."""
    if tenant_id <= 0 or not provider or environment not in ("test", "live"):
        raise ValueError("Invalid payment scope")
    if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
        raise ValueError("Invalid currency")

    profiles = MerchantPaymentProfile.__table__
    payments = MerchantPaymentTransaction.__table__
    allocations = MerchantPaymentAllocation.__table__
    settlements = MerchantPaymentSettlement.__table__

    with engine.connect() as connection:
        status = connection.execute(
            sa.select(profiles.c.onboarding_status).where(
                profiles.c.tenant_id == tenant_id,
                profiles.c.provider == provider,
                profiles.c.environment == environment,
            )
        ).scalar_one_or_none()
        if status is None:
            status = "not_started"

        payment_row = connection.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(payments.c.gross_amount), 0),
                sa.func.count(payments.c.id),
            ).where(
                payments.c.tenant_id == tenant_id,
                payments.c.provider == provider,
                payments.c.environment == environment,
                payments.c.currency == currency,
                payments.c.verification_state == "provider_confirmed",
            )
        ).one()

        allocation_row = connection.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(allocations.c.platform_fee_amount), 0),
                sa.func.coalesce(sa.func.sum(allocations.c.merchant_gross_share), 0),
            ).select_from(
                allocations.join(
                    payments,
                    sa.and_(
                        allocations.c.tenant_id == payments.c.tenant_id,
                        allocations.c.transaction_id == payments.c.id,
                    ),
                )
            ).where(
                allocations.c.tenant_id == tenant_id,
                payments.c.provider == provider,
                payments.c.environment == environment,
                payments.c.currency == currency,
                payments.c.verification_state == "provider_confirmed",
            )
        ).one()

        settlement_row = connection.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(settlements.c.reported_amount), 0),
                sa.func.count(settlements.c.id),
            ).where(
                settlements.c.tenant_id == tenant_id,
                settlements.c.provider == provider,
                settlements.c.environment == environment,
                settlements.c.currency == currency,
            )
        ).one()

    return PaymentSummary(
        onboarding_status=status,
        currency=currency,
        confirmed_gross=Decimal(payment_row[0]),
        provisional_platform_fees=Decimal(allocation_row[0]),
        provisional_merchant_share=Decimal(allocation_row[1]),
        provider_reported_settlements=Decimal(settlement_row[0]),
        confirmed_payment_count=int(payment_row[1]),
        settlement_count=int(settlement_row[1]),
    )
