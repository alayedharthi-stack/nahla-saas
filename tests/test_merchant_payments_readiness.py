"""Nahlah AI payments readiness: activation, onboarding, webhook ledger, settlements.

Generic commerce scenario: two unrelated merchants (a shoe store, tenant 1,
and a perfume store, tenant 2) on the same provider environment. Every test
asserts state, evidence and tenant boundaries, never prose.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from backend.payments import activation, onboarding, webhook_ledger
from backend.payments.models import (
    ALL_PAYMENT_TABLES,
    PAYMENT_READINESS_TABLES,
    PAYMENT_TABLES,
    MerchantPaymentActivation,
    MerchantPaymentFeePolicy,
    MerchantPaymentOnboardingEvent,
    MerchantPaymentProfile,
    MerchantPaymentSettlement,
    MerchantPaymentSettlementLine,
    MerchantPaymentTransaction,
    MerchantPaymentWebhookDelivery,
    PaymentBase,
)
from backend.payments.observations import observe_payment
from backend.payments.provider import (
    MOYASAR_CAPABILITIES_2026_10_03,
    DormantPaymentProvider,
    DormantPayoutProvider,
    DormantPlatformOnboardingProvider,
    ProviderContractPending,
    ProviderSettlement,
    ProviderSettlementLine,
)
from backend.payments.reconciliation import reconcile_merchant
from backend.payments.secret_refs import InvalidSecretReference, validate_secret_ref
from backend.payments.settlements import (
    SettlementObservationConflict,
    observe_settlement_lines,
    observe_settlements,
)
from database.models import Base as StartupBase

NOW = datetime.now(timezone.utc)
SCOPE1 = dict(tenant_id=1, provider="moyasar", environment="test")
SCOPE2 = dict(tenant_id=2, provider="moyasar", environment="test")
# Default is in-memory SQLite. Set PAYMENTS_TEST_DATABASE_URL to a disposable
# PostgreSQL database to prove the same DDL and logic there; the fixture
# creates and drops only the payments tables plus the tenants key mirror.
DATABASE_URL = os.environ.get("PAYMENTS_TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")
ALL_TABLES = [PaymentBase.metadata.tables["tenants"], *ALL_PAYMENT_TABLES]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fresh_engine():
    engine = sa.create_engine(DATABASE_URL)
    if engine.dialect.name == "sqlite":
        @sa.event.listens_for(engine, "connect")
        def enforce_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    else:
        with engine.begin() as connection:
            PaymentBase.metadata.drop_all(connection, tables=ALL_TABLES)
    return engine


@pytest.fixture
def engine():
    engine = _fresh_engine()
    with engine.begin() as connection:
        PaymentBase.metadata.create_all(connection, tables=ALL_TABLES)
        connection.execute(sa.text("INSERT INTO tenants (id) VALUES (1), (2), (3)"))
        connection.execute(sa.insert(MerchantPaymentProfile), [
            dict(**SCOPE1, onboarding_status="approved", provider_merchant_ref="acct-shoes",
                 approval_evidence_ref="kyb-shoes-1", approved_at=NOW),
            dict(**SCOPE2, onboarding_status="approved", provider_merchant_ref="acct-perfume",
                 approval_evidence_ref="kyb-perfume-1", approved_at=NOW),
        ])
        connection.execute(sa.insert(MerchantPaymentProfile).values(
            tenant_id=3, provider="moyasar", environment="test"))
        connection.execute(sa.insert(MerchantPaymentFeePolicy), [
            dict(**SCOPE1, rate_percent=Decimal("1.5000"), effective_at=NOW - timedelta(days=1)),
            dict(**SCOPE2, rate_percent=Decimal("2.0000"), effective_at=NOW - timedelta(days=1)),
        ])
    yield engine
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            PaymentBase.metadata.drop_all(connection, tables=ALL_TABLES)
    engine.dispose()


def _confirmed_payment(engine, scope, ref, gross):
    with engine.begin() as connection:
        return connection.execute(sa.insert(MerchantPaymentTransaction).values(
            **scope, provider_payment_ref=ref, currency="SAR", gross_amount=Decimal(gross),
            verification_state="provider_confirmed", provider_status="paid", provider_observed_at=NOW,
        )).inserted_primary_key[0]


# ── schema and migration ────────────────────────────────────────────────────


def test_readiness_tables_stay_out_of_startup_and_out_of_0115():
    assert all(table.name not in StartupBase.metadata.tables for table in ALL_PAYMENT_TABLES)
    assert len(PAYMENT_TABLES) == 6 and len(PAYMENT_READINESS_TABLES) == 4
    assert not set(PAYMENT_TABLES) & set(PAYMENT_READINESS_TABLES)
    assert all("tenant_id" in table.c for table in PAYMENT_READINESS_TABLES)
    assert isinstance(MerchantPaymentSettlementLine.amount.type, sa.Numeric)
    for column in (MerchantPaymentActivation.credential_ref, MerchantPaymentActivation.webhook_secret_ref):
        assert isinstance(column.type, sa.String) and column.type.length == 120


def test_migration_0116_requires_0115_then_creates_only_readiness_tables(monkeypatch):
    path = (Path(__file__).resolve().parents[1] / "database/migrations/versions"
            / "0116_merchant_payments_readiness.py")
    spec = importlib.util.spec_from_file_location("merchant_payments_readiness_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0115"

    engine = _fresh_engine()
    with engine.begin() as connection:
        PaymentBase.metadata.tables["tenants"].create(connection)
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
        with pytest.raises(RuntimeError, match="0115"):
            module.upgrade()
        PaymentBase.metadata.create_all(connection, tables=list(PAYMENT_TABLES))
        module.upgrade()
        names = set(sa.inspect(connection).get_table_names())
        assert {table.name for table in ALL_PAYMENT_TABLES}.issubset(names)
        with pytest.raises(RuntimeError, match="already exists"):
            module.upgrade()
        with pytest.raises(RuntimeError, match="must not be dropped"):
            module.downgrade()
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            PaymentBase.metadata.drop_all(connection, tables=ALL_TABLES)
    engine.dispose()


# ── provider boundaries ─────────────────────────────────────────────────────


def test_dormant_providers_fail_closed_and_write_nothing(engine):
    caps = MOYASAR_CAPABILITIES_2026_10_03
    assert caps.marketplace_split_supported is False and caps.wallet_supported is False
    assert caps.platform_api_contracted is False and caps.payout_contracted is False
    assert caps.any_surface_contracted is False
    with pytest.raises(ProviderContractPending):
        _run(DormantPlatformOnboardingProvider().fetch_registration(registration_reference="r"))
    with pytest.raises(ProviderContractPending):
        _run(DormantPayoutProvider().fetch_payout(payout_reference="p"))
    with pytest.raises(ProviderContractPending):
        _run(observe_payment(engine, tenant_id=1, environment="test", payment_reference="pay-1",
                             provider=DormantPaymentProvider()))
    with pytest.raises(ProviderContractPending):
        _run(observe_settlements(engine, tenant_id=1, environment="test", provider=DormantPaymentProvider()))
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(
            MerchantPaymentTransaction.__table__)).scalar_one() == 0
        assert connection.execute(sa.select(sa.func.count()).select_from(
            MerchantPaymentSettlement.__table__)).scalar_one() == 0


# ── onboarding ──────────────────────────────────────────────────────────────


def test_onboarding_transitions_are_closed_and_audited(engine):
    scope = dict(tenant_id=3, provider="moyasar", environment="test")
    with pytest.raises(onboarding.OnboardingTransitionError, match="not allowed"):
        onboarding.transition_onboarding(engine, **scope, to_status="approved", source="operator",
                                         evidence_ref="e", provider_merchant_ref="acct-gifts")
    first = onboarding.transition_onboarding(
        engine, **scope, to_status="pending", source="operator", provider_registration_ref="reg-gifts-1",
    )
    assert (first.from_status, first.to_status, first.replay) == ("not_started", "pending", False)
    with pytest.raises(onboarding.OnboardingTransitionError, match="evidence"):
        onboarding.transition_onboarding(engine, **scope, to_status="approved", source="provider_webhook")
    approved = onboarding.transition_onboarding(
        engine, **scope, to_status="approved", source="provider_webhook",
        evidence_ref="kyb-gifts-ok", provider_merchant_ref="acct-gifts", provider_status="active",
    )
    assert approved.to_status == "approved" and approved.event_id is not None
    replay = onboarding.transition_onboarding(
        engine, **scope, to_status="approved", source="provider_webhook",
        evidence_ref="kyb-gifts-ok", provider_merchant_ref="acct-gifts",
    )
    assert replay.replay and replay.event_id is None
    with pytest.raises(onboarding.OnboardingTransitionError, match="conflicts"):
        onboarding.transition_onboarding(engine, **scope, to_status="approved", source="provider_webhook",
                                         evidence_ref="x", provider_merchant_ref="acct-other")
    with engine.connect() as connection:
        profile = connection.execute(sa.select(MerchantPaymentProfile.__table__).where(
            MerchantPaymentProfile.__table__.c.tenant_id == 3)).mappings().one()
        assert profile["onboarding_status"] == "approved"
        assert profile["provider_merchant_ref"] == "acct-gifts"
        assert profile["approval_evidence_ref"] == "kyb-gifts-ok" and profile["approved_at"] is not None
        events = connection.execute(sa.select(MerchantPaymentOnboardingEvent.__table__).where(
            MerchantPaymentOnboardingEvent.__table__.c.tenant_id == 3
        ).order_by(MerchantPaymentOnboardingEvent.__table__.c.id)).mappings().all()
        assert [(e["from_status"], e["to_status"], e["source"]) for e in events] == [
            ("not_started", "pending", "operator"), ("pending", "approved", "provider_webhook"),
        ]
        assert events[1]["provider_status"] == "active"
    # Suspension and re-approval stay inside the table; approval from suspended needs evidence again.
    onboarding.transition_onboarding(engine, **scope, to_status="suspended", source="provider_webhook")
    with pytest.raises(onboarding.OnboardingTransitionError):
        onboarding.transition_onboarding(engine, **scope, to_status="pending", source="operator")
    assert onboarding.transition_onboarding(
        engine, **scope, to_status="approved", source="operator", evidence_ref="kyb-gifts-2",
        provider_merchant_ref="acct-gifts",
    ).to_status == "approved"


def test_provider_reference_resolves_to_exactly_one_tenant(engine):
    assert onboarding.tenant_for_provider_reference(
        engine, provider="moyasar", environment="test", reference="acct-shoes") == 1
    assert onboarding.tenant_for_provider_reference(
        engine, provider="moyasar", environment="test", reference="acct-nobody") is None
    assert onboarding.tenant_for_provider_reference(
        engine, provider="moyasar", environment="live", reference="acct-shoes") is None
    onboarding.transition_onboarding(
        engine, tenant_id=3, provider="moyasar", environment="test", to_status="pending",
        source="operator", provider_registration_ref="reg-shared",
    )
    assert onboarding.tenant_for_provider_reference(
        engine, provider="moyasar", environment="test", reference="reg-shared") == 3
    with engine.begin() as connection:
        connection.execute(sa.insert(MerchantPaymentOnboardingEvent).values(
            **SCOPE2, from_status="not_started", to_status="pending", source="operator",
            provider_registration_ref="reg-shared", recorded_at=NOW,
        ))
    with pytest.raises(onboarding.OnboardingTransitionError, match="more than one tenant"):
        onboarding.tenant_for_provider_reference(
            engine, provider="moyasar", environment="test", reference="reg-shared")
    assert onboarding.ensure_profile(engine, tenant_id=3, provider="moyasar", environment="live") > 0
    assert onboarding.ensure_profile(engine, **SCOPE1) == onboarding.ensure_profile(engine, **SCOPE1)


# ── activation ──────────────────────────────────────────────────────────────


def test_secret_references_never_accept_raw_keys():
    assert validate_secret_ref("MOYASAR_TEST_SECRET_KEY_T1", field="credential_ref") == "MOYASAR_TEST_SECRET_KEY_T1"
    assert validate_secret_ref("vault:payments/moyasar/t1.webhook", field="webhook_secret_ref")
    for bad in ("sk_test_EXAMPLE_NOT_A_KEY", "pk_live_x", "whsec_abc", "",
                "Bearer abc", "A" * 121, "x" * 40, "has space", "1starts_with_digit"):
        with pytest.raises(InvalidSecretReference):
            validate_secret_ref(bad, field="credential_ref")


def test_activation_is_dormant_until_every_blocker_clears_and_stays_tenant_scoped(engine):
    for scope in (SCOPE1, SCOPE2):
        assert activation.payment_acceptance_enabled(engine, **scope) is False
    readiness = activation.activation_readiness(engine, **SCOPE1)
    assert readiness.activation_state == "dormant" and not readiness.ready_to_enable
    assert readiness.blockers == ("credential_ref_missing", "webhook_secret_ref_missing")
    with pytest.raises(activation.ActivationError, match="Not ready"):
        activation.enable_merchant_payments(engine, **SCOPE1, evidence_ref="owner-approval-1")
    with pytest.raises(InvalidSecretReference):
        activation.register_secret_refs(engine, **SCOPE1, credential_ref="sk_test_EXAMPLE_REF")
    activation.register_secret_refs(engine, **SCOPE1, credential_ref="MOYASAR_T1_TEST_KEY")
    assert activation.activation_readiness(engine, **SCOPE1).blockers == ("webhook_secret_ref_missing",)
    activation.register_secret_refs(engine, **SCOPE1, webhook_secret_ref="MOYASAR_T1_TEST_WEBHOOK")
    assert activation.activation_readiness(engine, **SCOPE1).ready_to_enable
    assert activation.payment_acceptance_enabled(engine, **SCOPE1) is False  # ready is not enabled
    with pytest.raises(activation.ActivationError, match="evidence"):
        activation.enable_merchant_payments(engine, **SCOPE1, evidence_ref="  ")
    enabled = activation.enable_merchant_payments(engine, **SCOPE1, evidence_ref="owner-approval-1")
    assert enabled.enabled and activation.payment_acceptance_enabled(engine, **SCOPE1) is True
    assert activation.payment_acceptance_enabled(engine, **SCOPE2) is False
    assert activation.payment_acceptance_enabled(engine, tenant_id=1, provider="moyasar", environment="live") is False
    with pytest.raises(activation.ActivationError, match="Disable"):
        activation.register_secret_refs(engine, **SCOPE1, credential_ref="MOYASAR_T1_TEST_KEY_V2")
    with engine.connect() as connection:
        row = connection.execute(sa.select(MerchantPaymentActivation.__table__)).mappings().one()
        assert row["enabled_evidence_ref"] == "owner-approval-1" and row["enabled_at"] is not None
        assert "sk_" not in json.dumps({k: str(v) for k, v in row.items()})
    disabled = activation.disable_merchant_payments(engine, **SCOPE1, reason="provider suspended account")
    assert disabled.activation_state == "disabled" and not disabled.enabled
    assert activation.payment_acceptance_enabled(engine, **SCOPE1) is False
    # Provider approval alone never enables: suspend the profile and the gate closes.
    activation.enable_merchant_payments(engine, **SCOPE1, evidence_ref="owner-approval-2")
    onboarding.transition_onboarding(engine, **SCOPE1, to_status="suspended", source="provider_webhook")
    assert activation.payment_acceptance_enabled(engine, **SCOPE1) is False
    with pytest.raises(activation.ActivationError, match="No payment profile"):
        activation.disable_merchant_payments(engine, tenant_id=3, provider="moyasar", environment="live", reason="x")


def test_activation_evidence_is_database_enforced(engine):
    with engine.connect() as connection:
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentActivation).values(
                **SCOPE1, activation_state="enabled", credential_ref="K", webhook_secret_ref="W",
                created_at=NOW, updated_at=NOW,
            ))  # enabled without enabled_at / evidence
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentActivation).values(
                tenant_id=1, provider="moyasar", environment="live", created_at=NOW, updated_at=NOW,
            ))  # no profile for this mode
        with connection.begin():
            connection.execute(sa.insert(MerchantPaymentActivation).values(**SCOPE1, created_at=NOW, updated_at=NOW))
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentActivation).values(**SCOPE1, created_at=NOW, updated_at=NOW))


# ── webhook ledger ──────────────────────────────────────────────────────────


def _secret_from_body(raw, _headers):
    return json.loads(raw).get("secret_token")


def test_webhook_deliveries_are_durable_deduplicated_redacted_and_authenticated(engine):
    body = json.dumps({"id": "evt-1", "type": "payment_paid", "secret_token": "s3cret",
                       "data": {"source": {"number": "4111111111111111", "name": "A"}, "amount": 10000}}).encode()
    auth = webhook_ledger.SharedSecretAuthenticator(expected_secret="s3cret", extract=_secret_from_body)
    first = webhook_ledger.record_delivery(engine, provider="moyasar", environment="test",
                                           raw_payload=body, headers={}, authenticator=auth)
    second = webhook_ledger.record_delivery(engine, provider="moyasar", environment="test",
                                            raw_payload=body, headers={}, authenticator=auth)
    assert not first.replay and second.replay and first.delivery_id == second.delivery_id
    assert second.attempts == 2 and first.authentication_state == "verified"
    other_env = webhook_ledger.record_delivery(engine, provider="moyasar", environment="live",
                                               raw_payload=body, headers={}, authenticator=auth)
    assert other_env.delivery_id != first.delivery_id  # test and live never collide
    with engine.connect() as connection:
        row = connection.execute(sa.select(MerchantPaymentWebhookDelivery.__table__).where(
            MerchantPaymentWebhookDelivery.__table__.c.id == first.delivery_id)).mappings().one()
    stored = json.loads(row["redacted_payload"])
    assert stored["secret_token"] == "[redacted]" and stored["data"]["source"]["number"] == "[redacted]"
    assert stored["data"]["source"]["name"] == "A" and stored["data"]["amount"] == 10000
    assert "4111111111111111" not in row["redacted_payload"] and "s3cret" not in row["redacted_payload"]
    assert row["payload_sha256"] == hashlib.sha256(body).hexdigest() and row["tenant_id"] is None

    wrong = webhook_ledger.record_delivery(
        engine, provider="moyasar", environment="test", raw_payload=b'{"id":"evt-2","secret_token":"nope"}',
        headers={}, authenticator=auth,
    )
    assert wrong.authentication_state == "failed"
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="authenticated"):
        webhook_ledger.admit_delivery(engine, delivery_id=wrong.delivery_id, tenant_id=1,
                                      provider_event_ref="evt-2", event_type="payment_paid", event_category="payment")
    unverified = webhook_ledger.record_delivery(
        engine, provider="moyasar", environment="test", raw_payload=b'{"id":"evt-3"}', headers={}, authenticator=None,
    )
    assert unverified.authentication_state == "unverified"
    # A later redelivery that authenticates upgrades the row; a failing one never downgrades it.
    upgraded = webhook_ledger.record_delivery(
        engine, provider="moyasar", environment="test", raw_payload=b'{"id":"evt-3"}', headers={},
        authenticator=webhook_ledger.SharedSecretAuthenticator("k", lambda *_: "k"),
    )
    assert upgraded.replay and upgraded.authentication_state == "verified"
    downgrade_attempt = webhook_ledger.record_delivery(
        engine, provider="moyasar", environment="test", raw_payload=b'{"id":"evt-3"}', headers={},
        authenticator=webhook_ledger.SharedSecretAuthenticator("k", lambda *_: "wrong"),
    )
    assert downgrade_attempt.authentication_state == "verified"

    hmac_body = b'{"id":"evt-4"}'
    signature = hmac.new(b"hs", hmac_body, hashlib.sha256).hexdigest()
    signed = webhook_ledger.record_delivery(
        engine, provider="moyasar", environment="test", raw_payload=hmac_body,
        headers={"X-Signature": signature.upper()},
        authenticator=webhook_ledger.HmacSha256Authenticator(secret="hs", header_name="X-Signature"),
    )
    assert signed.authentication_state == "verified"
    with pytest.raises(webhook_ledger.WebhookLedgerError):
        webhook_ledger.record_delivery(engine, provider="moyasar", environment="test", raw_payload=b"",
                                       headers={}, authenticator=None)


def test_webhook_admission_keeps_tenant_ownership_and_terminal_outcomes(engine):
    auth = webhook_ledger.SharedSecretAuthenticator("k", lambda *_: "k")
    delivery = webhook_ledger.record_delivery(engine, provider="moyasar", environment="test",
                                              raw_payload=b'{"id":"evt-1","v":1}', headers={}, authenticator=auth)
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="admitted"):
        webhook_ledger.complete_delivery(engine, delivery_id=delivery.delivery_id, outcome="processed")
    webhook_ledger.admit_delivery(engine, delivery_id=delivery.delivery_id, tenant_id=1,
                                  provider_event_ref="evt-1", event_type="payment_paid", event_category="payment")
    webhook_ledger.admit_delivery(engine, delivery_id=delivery.delivery_id, tenant_id=1,
                                  provider_event_ref="evt-1", event_type="payment_paid", event_category="payment")
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="different tenant"):
        webhook_ledger.admit_delivery(engine, delivery_id=delivery.delivery_id, tenant_id=2,
                                      provider_event_ref="evt-1", event_type="payment_paid", event_category="payment")
    # A second delivery carrying the same provider event ref cannot be attributed to another tenant.
    redelivery = webhook_ledger.record_delivery(engine, provider="moyasar", environment="test",
                                                raw_payload=b'{"id":"evt-1","v":2}', headers={}, authenticator=auth)
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="another tenant"):
        webhook_ledger.admit_delivery(engine, delivery_id=redelivery.delivery_id, tenant_id=2,
                                      provider_event_ref="evt-1", event_type="payment_paid", event_category="payment")
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="reason"):
        webhook_ledger.complete_delivery(engine, delivery_id=redelivery.delivery_id, outcome="ignored")
    webhook_ledger.complete_delivery(engine, delivery_id=redelivery.delivery_id, outcome="ignored",
                                     reason="duplicate of admitted delivery")
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="terminal"):
        webhook_ledger.admit_delivery(engine, delivery_id=redelivery.delivery_id, tenant_id=1,
                                      provider_event_ref="evt-1", event_type="payment_paid", event_category="payment")
    webhook_ledger.complete_delivery(engine, delivery_id=delivery.delivery_id, outcome="processed")
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="terminal"):
        webhook_ledger.complete_delivery(engine, delivery_id=delivery.delivery_id, outcome="failed", reason="x")
    with pytest.raises(webhook_ledger.WebhookLedgerError, match="category"):
        webhook_ledger.admit_delivery(engine, delivery_id=delivery.delivery_id, tenant_id=1,
                                      provider_event_ref="evt-9", event_type="t", event_category="wallet")

    onboarding_delivery = webhook_ledger.record_delivery(engine, provider="moyasar", environment="test",
                                                         raw_payload=b'{"id":"reg-9"}', headers={}, authenticator=auth)
    webhook_ledger.admit_delivery(engine, delivery_id=onboarding_delivery.delivery_id, tenant_id=2,
                                  provider_event_ref="reg-9", event_type="registration_updated",
                                  event_category="onboarding")
    mine = webhook_ledger.tenant_deliveries(engine, tenant_id=1)
    theirs = webhook_ledger.tenant_deliveries(engine, tenant_id=2)
    assert [row["provider_event_ref"] for row in mine] == ["evt-1"]
    assert [row["provider_event_ref"] for row in theirs] == ["reg-9"]
    assert mine[0]["processing_state"] == "processed" and mine[0]["processed_at"] is not None
    with engine.connect() as connection:
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentWebhookDelivery).values(
                provider="moyasar", environment="test", delivery_key="d" * 64, payload_sha256="p" * 64,
                payload_size=3, authentication_state="unverified", processing_state="admitted",
                tenant_id=1, provider_event_ref="x", event_type="y", received_at=NOW, last_received_at=NOW,
            ))  # the database also refuses admission without authentication


# ── settlements and reconciliation ──────────────────────────────────────────


class FakeSettlementsProvider:
    name = "moyasar"

    def __init__(self, settlements=(), lines=()):
        self.settlements = list(settlements)
        self.lines = list(lines)
        self.seen_merchants = []

    async def fetch_payment(self, *, merchant_reference, payment_reference):  # pragma: no cover
        raise AssertionError("not used")

    async def list_settlements(self, *, merchant_reference):
        self.seen_merchants.append(merchant_reference)
        return list(self.settlements)

    async def list_settlement_lines(self, *, merchant_reference, settlement_reference):
        self.seen_merchants.append(merchant_reference)
        return [line for line in self.lines if line.settlement_reference == settlement_reference]


def _settlement(ref, amount, status="transferred", recipient="bank-shoes"):
    return ProviderSettlement(ref, status, Decimal(amount), "SAR", recipient)


def _line(settlement_ref, line_ref, line_type, amount, payment_ref=None):
    return ProviderSettlementLine(settlement_ref, line_ref, line_type, Decimal(amount), "SAR", payment_ref)


def test_settlement_observation_is_idempotent_and_refuses_conflicts(engine):
    provider = FakeSettlementsProvider([_settlement("stl-1", "98.50", status="pending")])
    first = _run(observe_settlements(engine, tenant_id=1, environment="test", provider=provider))
    assert first.inserted == 1 and provider.seen_merchants == ["acct-shoes"]
    again = _run(observe_settlements(engine, tenant_id=1, environment="test", provider=provider))
    assert again.inserted == 0 and again.status_updated == 0 and again.settlement_ids == first.settlement_ids
    provider.settlements = [_settlement("stl-1", "98.50", status="transferred")]
    progressed = _run(observe_settlements(engine, tenant_id=1, environment="test", provider=provider))
    assert progressed.status_updated == 1
    provider.settlements = [_settlement("stl-1", "99.00")]
    with pytest.raises(SettlementObservationConflict, match="amount"):
        _run(observe_settlements(engine, tenant_id=1, environment="test", provider=provider))
    # The perfume store's provider account cannot claim the shoe store's settlement reference.
    other = FakeSettlementsProvider([_settlement("stl-1", "98.50", recipient="bank-perfume")])
    with pytest.raises(SettlementObservationConflict):
        _run(observe_settlements(engine, tenant_id=2, environment="test", provider=other))
    with pytest.raises(SettlementObservationConflict, match="not provider-approved"):
        _run(observe_settlements(engine, tenant_id=3, environment="test", provider=provider))
    bad = FakeSettlementsProvider([ProviderSettlement("stl-2", "transferred", Decimal("1.005"), "SAR", "r")])
    with pytest.raises(SettlementObservationConflict, match="Invalid"):
        _run(observe_settlements(engine, tenant_id=1, environment="test", provider=bad))
    with engine.connect() as connection:
        rows = connection.execute(sa.select(MerchantPaymentSettlement.__table__)).mappings().all()
    assert len(rows) == 1 and rows[0]["tenant_id"] == 1 and rows[0]["provider_status"] == "transferred"


def test_settlement_lines_are_the_only_settlement_evidence_and_stay_tenant_bound(engine):
    shoes_a = _confirmed_payment(engine, SCOPE1, "pay-a", "100.00")
    _confirmed_payment(engine, SCOPE1, "pay-b", "50.00")
    perfume = _confirmed_payment(engine, SCOPE2, "pay-p", "300.00")
    assert perfume > shoes_a
    for scope, pid, gross, fee, rate in ((SCOPE1, shoes_a, "100.00", "1.50", "1.5000"),):
        with engine.begin() as connection:
            connection.execute(sa.insert(PaymentBase.metadata.tables["merchant_payment_allocations"]).values(
                tenant_id=scope["tenant_id"], transaction_id=pid, gross_amount=Decimal(gross),
                platform_fee_amount=Decimal(fee), merchant_gross_share=Decimal(gross) - Decimal(fee),
                fee_rate_percent=Decimal(rate),
            ))

    before = reconcile_merchant(engine, **SCOPE1)
    assert before.confirmed_gross == Decimal("150.00") and before.settled_gross == Decimal("0.00")
    assert before.awaiting_settlement_gross == Decimal("150.00") and before.evidence_state == "partial"
    assert {p.settlement_state for p in before.payments} == {"awaiting_provider_settlement"}

    provider = FakeSettlementsProvider(
        [_settlement("stl-1", "97.00")],
        [_line("stl-1", "ln-1", "payment", "100.00", "pay-a"), _line("stl-1", "ln-2", "fee", "-3.00")],
    )
    _run(observe_settlements(engine, tenant_id=1, environment="test", provider=provider))
    with pytest.raises(SettlementObservationConflict, match="not recorded for this tenant"):
        _run(observe_settlement_lines(engine, tenant_id=2, environment="test",
                                      settlement_reference="stl-1", provider=provider))
    lines = _run(observe_settlement_lines(engine, tenant_id=1, environment="test",
                                          settlement_reference="stl-1", provider=provider))
    assert (lines.inserted, lines.replayed, lines.linked_to_payments) == (2, 0, 1)
    replay = _run(observe_settlement_lines(engine, tenant_id=1, environment="test",
                                           settlement_reference="stl-1", provider=provider))
    assert (replay.inserted, replay.replayed, replay.linked_to_payments) == (0, 2, 0)

    after = reconcile_merchant(engine, **SCOPE1)
    assert after.settled_gross == Decimal("100.00") and after.settled_payment_count == 1
    assert after.awaiting_settlement_gross == Decimal("50.00")
    assert after.provider_reported_settlement_total == Decimal("97.00")
    assert after.settlement_line_total == Decimal("97.00") and after.unmatched_line_count == 0
    assert after.provisional_platform_fees == Decimal("1.50") and after.provisional_merchant_share == Decimal("98.50")
    assert after.evidence_state == "partial"
    states = {p.provider_payment_ref: (p.settlement_state, p.settlement_reference) for p in after.payments}
    assert states == {"pay-a": ("provider_settled", "stl-1"), "pay-b": ("awaiting_provider_settlement", None)}
    assert not hasattr(after, "balance") and not hasattr(after, "available_balance")

    # The perfume store sees none of the shoe store's evidence.
    other = reconcile_merchant(engine, **SCOPE2)
    assert other.confirmed_gross == Decimal("300.00") and other.settled_gross == Decimal("0.00")
    assert other.settlement_count == 0 and other.provider_reported_settlement_total == Decimal("0.00")
    assert other.provisional_platform_fees == Decimal("0.00")

    # A shoe-store settlement line naming the perfume store's payment is refused and writes nothing.
    provider.settlements.append(_settlement("stl-2", "300.00"))
    provider.lines.append(_line("stl-2", "ln-3", "payment", "300.00", "pay-p"))
    _run(observe_settlements(engine, tenant_id=1, environment="test", provider=provider))
    with pytest.raises(SettlementObservationConflict, match="another tenant"):
        _run(observe_settlement_lines(engine, tenant_id=1, environment="test",
                                      settlement_reference="stl-2", provider=provider))
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(
            MerchantPaymentSettlementLine.__table__).where(
            MerchantPaymentSettlementLine.__table__.c.provider_settlement_ref == "stl-2")).scalar_one() == 0

    # A payment line the platform never observed stays unmatched and marks nothing settled.
    provider.lines = [l for l in provider.lines if l.settlement_reference != "stl-2"]
    provider.lines.append(_line("stl-2", "ln-4", "payment", "300.00", "pay-unknown"))
    unmatched = _run(observe_settlement_lines(engine, tenant_id=1, environment="test",
                                              settlement_reference="stl-2", provider=provider))
    assert unmatched.linked_to_payments == 0
    report = reconcile_merchant(engine, **SCOPE1)
    assert report.unmatched_line_count == 1 and report.unmatched_line_total == Decimal("300.00")
    assert report.settled_gross == Decimal("100.00") and report.evidence_state == "partial"

    # Completing the remaining payment with a provider line yields 'complete' only when nothing is unmatched.
    provider.lines = [l for l in provider.lines if l.line_reference != "ln-4"]
    provider.lines.append(_line("stl-2", "ln-5", "payment", "50.00", "pay-b"))
    _run(observe_settlement_lines(engine, tenant_id=1, environment="test",
                                  settlement_reference="stl-2", provider=provider))
    complete = reconcile_merchant(engine, **SCOPE1)
    assert complete.settled_gross == Decimal("150.00") and complete.awaiting_settlement_gross == Decimal("0.00")
    # ln-4 remains stored (evidence is never deleted) so the report stays partial, not complete.
    assert complete.unmatched_line_count == 1 and complete.evidence_state == "partial"


def test_reconciliation_flags_inconsistent_evidence_and_empty_scope(engine):
    empty = reconcile_merchant(engine, tenant_id=3, provider="moyasar", environment="test")
    assert empty.evidence_state == "none" and empty.confirmed_gross == Decimal("0.00")
    pid = _confirmed_payment(engine, SCOPE1, "pay-x", "80.00")
    with engine.begin() as connection:
        connection.execute(sa.insert(MerchantPaymentSettlement).values(
            **SCOPE1, provider_settlement_ref="stl-x", recipient_ref="r", currency="SAR",
            reported_amount=Decimal("80.00"), provider_status="transferred", provider_observed_at=NOW,
        ))
        connection.execute(sa.insert(MerchantPaymentSettlementLine).values(
            **SCOPE1, provider_settlement_ref="stl-x", provider_line_ref="l1", line_type="payment",
            transaction_id=pid, provider_payment_ref="pay-x", currency="SAR", amount=Decimal("70.00"),
            provider_observed_at=NOW,
        ))
    report = reconcile_merchant(engine, **SCOPE1)
    assert report.evidence_state == "inconsistent"  # line amount disagrees with the confirmed gross
    with engine.connect() as connection:
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentSettlementLine).values(
                tenant_id=2, provider="moyasar", environment="test", provider_settlement_ref="stl-x",
                provider_line_ref="l2", line_type="payment", transaction_id=pid, provider_payment_ref="pay-x",
                currency="SAR", amount=Decimal("80.00"), provider_observed_at=NOW,
            ))  # composite FK: another tenant cannot reference this payment
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(sa.insert(MerchantPaymentSettlementLine).values(
                **SCOPE1, provider_settlement_ref="stl-missing", provider_line_ref="l3", line_type="fee",
                currency="SAR", amount=Decimal("-1.00"), provider_observed_at=NOW,
            ))  # a line needs a recorded settlement
    with pytest.raises(ValueError):
        reconcile_merchant(engine, **SCOPE1, currency="sar")
