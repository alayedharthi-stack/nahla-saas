"""Webhook admission authenticates, deduplicates and keeps tenant ownership."""
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from backend.payments.models import PAYMENT_TABLES, MerchantPaymentProfile, PaymentBase
from backend.payments.webhook_events import ProviderEventConflict, admit_provider_event


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
    return engine


def test_authentication_and_exact_replay_are_safe():
    engine = _engine()
    args=dict(tenant_id=1,provider="moyasar",environment="test",provider_event_ref="e1",
              event_type="payment_paid",raw_payload=b'{"id":"e1"}',
              supplied_secret="secret",expected_secret="secret")
    first=admit_provider_event(engine,**args)
    second=admit_provider_event(engine,**args)
    assert not first.replay and second.replay and first.event_id == second.event_id
    engine.dispose()


def test_wrong_secret_and_cross_tenant_replay_fail_closed():
    engine = _engine()
    with pytest.raises(ProviderEventConflict, match="authentication"):
        admit_provider_event(engine,tenant_id=1,provider="moyasar",environment="test",
            provider_event_ref="e1",event_type="payment_paid",raw_payload=b"x",
            supplied_secret="wrong",expected_secret="secret")
    admit_provider_event(engine,tenant_id=1,provider="moyasar",environment="test",
        provider_event_ref="e1",event_type="payment_paid",raw_payload=b"x",
        supplied_secret="secret",expected_secret="secret")
    with pytest.raises(ProviderEventConflict, match="conflicts"):
        admit_provider_event(engine,tenant_id=2,provider="moyasar",environment="test",
            provider_event_ref="e1",event_type="payment_paid",raw_payload=b"x",
            supplied_secret="secret",expected_secret="secret")
    engine.dispose()
