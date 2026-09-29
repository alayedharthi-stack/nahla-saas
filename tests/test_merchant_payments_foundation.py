"""The dormant foundation has enforceable money and tenant boundaries."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from backend.payments.fees import quote_allocation
from backend.payments.models import (
    PAYMENT_TABLES,
    MerchantPaymentAllocation,
    MerchantPaymentFeePolicy,
    MerchantPaymentProfile,
    MerchantPaymentProviderEvent,
    MerchantPaymentSettlement,
    MerchantPaymentTransaction,
    PaymentBase,
)
from database.models import Base as StartupBase


@pytest.fixture
def database():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")

    @sa.event.listens_for(engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        PaymentBase.metadata.create_all(
            connection,
            tables=[PaymentBase.metadata.tables["tenants"], *PAYMENT_TABLES],
        )
        connection.execute(sa.text("INSERT INTO tenants (id) VALUES (1), (2)"))
        connection.execute(sa.insert(MerchantPaymentProfile), [
            {"tenant_id": 1, "provider": "moyasar", "environment": "test"},
            {"tenant_id": 2, "provider": "moyasar", "environment": "test"},
            {"tenant_id": 2, "provider": "moyasar", "environment": "live"},
        ])
    yield engine
    engine.dispose()


def test_does_not_materialize_at_application_startup():
    assert all(table.name not in StartupBase.metadata.tables for table in PAYMENT_TABLES)
    assert all("tenant_id" in table.c for table in PAYMENT_TABLES)
    assert all(isinstance(column.type, sa.Numeric) for column in (
        MerchantPaymentFeePolicy.rate_percent,
        MerchantPaymentTransaction.gross_amount,
        MerchantPaymentAllocation.platform_fee_amount,
        MerchantPaymentSettlement.reported_amount,
    ))


def test_quote_uses_decimal_and_snapshots_custom_rates():
    quote = quote_allocation(Decimal("100.00"), Decimal("1.5000"))
    assert quote.platform_fee_amount == Decimal("1.50")
    assert quote.merchant_gross_share == Decimal("98.50")
    assert quote_allocation(Decimal("0.30"), Decimal("1.0000")).platform_fee_amount == Decimal("0.00")
    assert quote_allocation(Decimal("1.00"), Decimal("0.5000")).platform_fee_amount == Decimal("0.01")
    assert quote_allocation(Decimal("100.00"), Decimal("0")).merchant_gross_share == Decimal("100.00")
    with pytest.raises(TypeError):
        quote_allocation(100.00, Decimal("1.5"))
    with pytest.raises(ValueError):
        quote_allocation(Decimal("1.001"), Decimal("1.5"))
    with pytest.raises(ValueError):
        quote_allocation(Decimal("NaN"), Decimal("1.5"))
    with pytest.raises(ValueError):
        quote_allocation(Decimal("10.00"), Decimal("100.0001"))


def test_provider_approval_requires_trusted_reference(database):
    with database.connect() as connection:
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentProfile).values(
                tenant_id=1, provider="moyasar", environment="test",
                onboarding_status="approved", provider_merchant_ref="merchant-1",
            ))
        with connection.begin():
            connection.execute(sa.insert(MerchantPaymentProfile).values(
                tenant_id=1, provider="second_provider", environment="test", onboarding_status="pending",
            ))


def test_tenant_isolation_and_global_provider_reference_dedup(database):
    now = datetime.now(timezone.utc)
    with database.begin() as connection:
        payment_id = connection.execute(sa.insert(MerchantPaymentTransaction).values(
            tenant_id=1, provider="moyasar", environment="test", provider_payment_ref="p-1",
            gross_amount=Decimal("100.00"), currency="SAR", provider_observed_at=now,
            verification_state="provider_confirmed",
        )).inserted_primary_key[0]

    def allocation(tenant_id):
        return sa.insert(MerchantPaymentAllocation).values(
            tenant_id=tenant_id, transaction_id=payment_id,
            gross_amount=Decimal("100.00"), platform_fee_amount=Decimal("1.50"),
            merchant_gross_share=Decimal("98.50"), fee_rate_percent=Decimal("1.5000"),
        )

    with database.connect() as connection:
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(allocation(2))  # Composite FK refuses another tenant's payment.
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentAllocation).values(
                tenant_id=1, transaction_id=payment_id,
                gross_amount=Decimal("99.00"), platform_fee_amount=Decimal("1.50"),
                merchant_gross_share=Decimal("97.50"), fee_rate_percent=Decimal("1.5000"),
            ))  # A valid internal split cannot quote the wrong payment amount.
        with connection.begin():
            connection.execute(allocation(1))
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(allocation(1))  # One allocation per payment.
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentTransaction).values(
                tenant_id=2, provider="moyasar", environment="test", provider_payment_ref="p-1",
                gross_amount=Decimal("100.00"), currency="SAR",
            ))
        with connection.begin():
            connection.execute(sa.insert(MerchantPaymentTransaction).values(
                tenant_id=2, provider="moyasar", environment="live", provider_payment_ref="p-1",
                gross_amount=Decimal("100.00"), currency="SAR",
            ))


def test_allocation_and_event_invariants_are_database_enforced(database):
    with database.begin() as connection:
        payment_id = connection.execute(sa.insert(MerchantPaymentTransaction).values(
            tenant_id=1, provider="moyasar", environment="test", gross_amount=Decimal("10.00"),
            currency="SAR",
        )).inserted_primary_key[0]
    with database.connect() as connection:
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentAllocation).values(
                tenant_id=1, transaction_id=payment_id, gross_amount=Decimal("10.00"),
                platform_fee_amount=Decimal("1.00"), merchant_gross_share=Decimal("8.00"),
                fee_rate_percent=Decimal("10.0000"),
            ))
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentProviderEvent).values(
                tenant_id=1, provider="moyasar", environment="test", provider_event_ref="evt-1",
                event_type="payment_paid", payload_sha256="bad", authenticated_at=datetime.now(timezone.utc),
            ))
        with connection.begin():
            connection.execute(sa.insert(MerchantPaymentProviderEvent).values(
                tenant_id=1, provider="moyasar", environment="test", provider_event_ref="evt-1",
                event_type="payment_paid", payload_sha256="a" * 64,
                authenticated_at=datetime.now(timezone.utc),
            ))
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentProviderEvent).values(
                tenant_id=2, provider="moyasar", environment="test", provider_event_ref="evt-1",
                event_type="payment_paid", payload_sha256="a" * 64,
                authenticated_at=datetime.now(timezone.utc),
            ))  # A replay cannot be reassigned to a different merchant.
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentSettlement).values(
                tenant_id=1, provider="moyasar", environment="live", provider_settlement_ref="s-1",
                currency="SAR", reported_amount=Decimal("10.00"), provider_status="transferred",
                provider_observed_at=datetime.now(timezone.utc),
            ))  # There is no approved tenant/profile association for this mode.


def test_migration_creates_only_payment_tables_and_refuses_unknown_schema(monkeypatch):
    migration_path = (Path(__file__).resolve().parents[1] / "database/migrations/versions"
                      / "0114_merchant_payments_foundation.py")
    spec = importlib.util.spec_from_file_location("merchant_payments_migration", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0112"  # Does not depend on dormant 0113.

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        PaymentBase.metadata.tables["tenants"].create(connection)
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
        module.upgrade()
        assert {table.name for table in PAYMENT_TABLES}.issubset(sa.inspect(connection).get_table_names())
        with pytest.raises(RuntimeError, match="already exists"):
            module.upgrade()
        with pytest.raises(RuntimeError, match="must not be dropped"):
            module.downgrade()
    engine.dispose()
