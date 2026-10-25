"""The dormant payment read model is truthful and tenant-isolated."""
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa

from backend.payments.models import (
    PAYMENT_TABLES, MerchantPaymentAllocation, MerchantPaymentProfile,
    MerchantPaymentSettlement, MerchantPaymentTransaction, PaymentBase,
)
from backend.payments.read_model import merchant_payment_summary


def _engine():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    @sa.event.listens_for(engine, "connect")
    def foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    with engine.begin() as c:
        PaymentBase.metadata.create_all(c, tables=[PaymentBase.metadata.tables["tenants"], *PAYMENT_TABLES])
        c.execute(sa.text("INSERT INTO tenants (id) VALUES (1), (2)"))
        now = datetime.now(timezone.utc)
        c.execute(sa.insert(MerchantPaymentProfile), [
            dict(tenant_id=1, provider="moyasar", environment="test", onboarding_status="approved",
                 provider_merchant_ref="m1", approval_evidence_ref="a1", approved_at=now),
            dict(tenant_id=2, provider="moyasar", environment="test", onboarding_status="approved",
                 provider_merchant_ref="m2", approval_evidence_ref="a2", approved_at=now),
        ])
        p1 = c.execute(sa.insert(MerchantPaymentTransaction).values(
            tenant_id=1, provider="moyasar", environment="test", provider_payment_ref="p1",
            currency="SAR", gross_amount=Decimal("100.00"), verification_state="provider_confirmed",
            provider_observed_at=now)).inserted_primary_key[0]
        p2 = c.execute(sa.insert(MerchantPaymentTransaction).values(
            tenant_id=2, provider="moyasar", environment="test", provider_payment_ref="p2",
            currency="SAR", gross_amount=Decimal("900.00"), verification_state="provider_confirmed",
            provider_observed_at=now)).inserted_primary_key[0]
        for tenant, pid, gross, fee in [(1,p1,"100.00","1.50"),(2,p2,"900.00","9.00")]:
            g, f = Decimal(gross), Decimal(fee)
            c.execute(sa.insert(MerchantPaymentAllocation).values(
                tenant_id=tenant, transaction_id=pid, gross_amount=g,
                platform_fee_amount=f, merchant_gross_share=g-f,
                fee_rate_percent=Decimal("1.5000") if tenant == 1 else Decimal("1.0000")))
        c.execute(sa.insert(MerchantPaymentSettlement).values(
            tenant_id=1, provider="moyasar", environment="test", provider_settlement_ref="s1",
            recipient_ref="m1", currency="SAR", reported_amount=Decimal("98.50"),
            provider_status="transferred", provider_observed_at=now))
    return engine


def test_summary_is_tenant_scoped_and_does_not_invent_wallet_balance():
    engine = _engine()
    one = merchant_payment_summary(engine, tenant_id=1, provider="moyasar", environment="test")
    two = merchant_payment_summary(engine, tenant_id=2, provider="moyasar", environment="test")
    assert one.confirmed_gross == Decimal("100.00")
    assert one.provisional_platform_fees == Decimal("1.50")
    assert one.provisional_merchant_share == Decimal("98.50")
    assert one.provider_reported_settlements == Decimal("98.50")
    assert one.confirmed_payment_count == one.settlement_count == 1
    assert two.confirmed_gross == Decimal("900.00")
    assert two.provider_reported_settlements == Decimal("0")
    assert two.settlement_count == 0
    engine.dispose()


def test_missing_profile_is_not_started_and_has_zero_evidence():
    engine = _engine()
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO tenants (id) VALUES (3)"))
    summary = merchant_payment_summary(engine, tenant_id=3, provider="moyasar", environment="test")
    assert summary.onboarding_status == "not_started"
    assert summary.confirmed_gross == Decimal("0")
    assert summary.provider_reported_settlements == Decimal("0")
    engine.dispose()
