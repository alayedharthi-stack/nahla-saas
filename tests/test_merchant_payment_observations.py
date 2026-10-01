"""Provider observations stay tenant-bound and cannot invent settlement."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from backend.payments.models import (
    PAYMENT_TABLES, MerchantPaymentAllocation, MerchantPaymentFeePolicy,
    MerchantPaymentProfile, MerchantPaymentTransaction, PaymentBase,
)
from backend.payments.observations import PaymentObservationConflict, observe_payment
from backend.payments.provider import ProviderPayment


class FakeProvider:
    name = "example"

    def __init__(self, payment: ProviderPayment, *, fail: bool = False):
        self.payment = payment
        self.fail = fail
        self.calls = []

    async def fetch_payment(self, *, merchant_reference, payment_reference):
        self.calls.append((merchant_reference, payment_reference))
        if self.fail:
            raise ConnectionError("provider unavailable")
        return self.payment


@pytest.fixture
def engine():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")

    @sa.event.listens_for(engine, "connect")
    def foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        PaymentBase.metadata.create_all(
            connection, tables=[PaymentBase.metadata.tables["tenants"], *PAYMENT_TABLES],
        )
        connection.execute(sa.text("INSERT INTO tenants (id) VALUES (1), (2)"))
        connection.execute(sa.insert(MerchantPaymentProfile), [
            dict(tenant_id=1, provider="example", environment="test",
                 provider_merchant_ref="merchant-1", onboarding_status="approved",
                 approval_evidence_ref="approval-1", approved_at=datetime.now(timezone.utc)),
            dict(tenant_id=2, provider="example", environment="test",
                 provider_merchant_ref="merchant-2", onboarding_status="approved",
                 approval_evidence_ref="approval-2", approved_at=datetime.now(timezone.utc)),
        ])
        connection.execute(sa.insert(MerchantPaymentFeePolicy), [
            dict(tenant_id=1, provider="example", environment="test",
                 rate_percent=Decimal("1.5000"),
                 effective_at=datetime.now(timezone.utc) - timedelta(days=1)),
            dict(tenant_id=2, provider="example", environment="test",
                 rate_percent=Decimal("0.5000"),
                 effective_at=datetime.now(timezone.utc) - timedelta(days=1)),
        ])
    yield engine
    engine.dispose()


def provider_payment(*, merchant="merchant-1", amount=Decimal("100.00"), ref="p-1"):
    return FakeProvider(ProviderPayment(
        reference=ref, merchant_reference=merchant, gross_amount=amount,
        currency="SAR", status="paid",
    ))


def test_verified_observation_and_replay_keep_one_fee_snapshot(engine):
    provider = provider_payment()
    first = asyncio.run(observe_payment(
        engine, tenant_id=1, environment="test", payment_reference="p-1", provider=provider,
    ))
    with engine.begin() as connection:
        connection.execute(sa.insert(MerchantPaymentFeePolicy).values(
            tenant_id=1, provider="example", environment="test",
            rate_percent=Decimal("1.0000"),
            effective_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
    second = asyncio.run(observe_payment(
        engine, tenant_id=1, environment="test", payment_reference="p-1", provider=provider,
    ))
    assert second == first
    with engine.connect() as connection:
        payment = connection.execute(sa.select(MerchantPaymentTransaction.__table__)).mappings().one()
        allocation = connection.execute(sa.select(MerchantPaymentAllocation.__table__)).mappings().one()
    assert payment["verification_state"] == "provider_confirmed"
    assert payment["tenant_id"] == allocation["tenant_id"] == 1
    assert allocation["platform_fee_amount"] == Decimal("1.50")
    assert allocation["merchant_gross_share"] == Decimal("98.50")
    assert allocation["fee_rate_percent"] == Decimal("1.5000")
    assert provider.calls == [("merchant-1", "p-1")] * 2


def test_provider_identity_amount_and_reference_conflicts_do_not_write(engine):
    for provider in (
        provider_payment(merchant="merchant-2"),
        provider_payment(ref="other"),
        provider_payment(amount=Decimal("1.001")),
    ):
        with pytest.raises(PaymentObservationConflict):
            asyncio.run(observe_payment(
                engine, tenant_id=1, environment="test", payment_reference="p-1", provider=provider,
            ))
    provider = provider_payment()
    asyncio.run(observe_payment(engine, tenant_id=1, environment="test", payment_reference="p-1", provider=provider))
    with pytest.raises(PaymentObservationConflict):
        asyncio.run(observe_payment(
            engine, tenant_id=2, environment="test", payment_reference="p-1",
            provider=provider_payment(merchant="merchant-2"),
        ))
    with pytest.raises(PaymentObservationConflict):
        asyncio.run(observe_payment(
            engine, tenant_id=1, environment="test", payment_reference="p-1",
            provider=provider_payment(amount=Decimal("101.00")),
        ))
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(MerchantPaymentTransaction)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(MerchantPaymentAllocation)) == 1


def test_missing_fee_suspended_merchant_and_provider_failure_do_not_write(engine):
    with engine.begin() as connection:
        connection.execute(sa.delete(MerchantPaymentFeePolicy).where(MerchantPaymentFeePolicy.tenant_id == 1))
    with pytest.raises(PaymentObservationConflict, match="fee policy"):
        asyncio.run(observe_payment(
            engine, tenant_id=1, environment="test", payment_reference="p-1", provider=provider_payment(),
        ))
    with engine.begin() as connection:
        connection.execute(sa.update(MerchantPaymentProfile).where(
            MerchantPaymentProfile.tenant_id == 1
        ).values(onboarding_status="suspended"))
    provider = provider_payment()
    with pytest.raises(PaymentObservationConflict, match="approved"):
        asyncio.run(observe_payment(
            engine, tenant_id=1, environment="test", payment_reference="p-1", provider=provider,
        ))
    assert provider.calls == []
    with engine.begin() as connection:
        connection.execute(sa.update(MerchantPaymentProfile).where(
            MerchantPaymentProfile.tenant_id == 1
        ).values(onboarding_status="approved"))
    with pytest.raises(ConnectionError):
        asyncio.run(observe_payment(
            engine, tenant_id=1, environment="test", payment_reference="p-1",
            provider=FakeProvider(provider.payment, fail=True),
        ))
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(MerchantPaymentTransaction)) == 0


def test_approval_revoked_during_provider_read_refuses_observation(engine):
    class RevokingProvider(FakeProvider):
        async def fetch_payment(self, *, merchant_reference, payment_reference):
            with engine.begin() as connection:
                connection.execute(sa.update(MerchantPaymentProfile).where(
                    MerchantPaymentProfile.tenant_id == 1
                ).values(onboarding_status="suspended"))
            return await super().fetch_payment(
                merchant_reference=merchant_reference, payment_reference=payment_reference,
            )

    provider = RevokingProvider(provider_payment().payment)
    with pytest.raises(PaymentObservationConflict, match="approval changed"):
        asyncio.run(observe_payment(
            engine, tenant_id=1, environment="test", payment_reference="p-1", provider=provider,
        ))
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(MerchantPaymentTransaction)) == 0


def test_existing_unverified_payment_cannot_be_claimed_as_observed(engine):
    with engine.begin() as connection:
        connection.execute(sa.insert(MerchantPaymentTransaction).values(
            tenant_id=1, provider="example", environment="test",
            provider_payment_ref="p-1", gross_amount=Decimal("100.00"), currency="SAR",
            verification_state="unverified",
        ))
    with pytest.raises(PaymentObservationConflict, match="evidence conflicts"):
        asyncio.run(observe_payment(
            engine, tenant_id=1, environment="test", payment_reference="p-1",
            provider=provider_payment(),
        ))
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(MerchantPaymentAllocation)) == 0
