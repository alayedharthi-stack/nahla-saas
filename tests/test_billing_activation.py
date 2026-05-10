"""tests/test_billing_activation.py
────────────────────────────────────
Lock-in tests for ``services.billing_activation``.

These tests cover the three reconciliation paths that activate a Moyasar
subscription:

  1. The shared activation helper (``activate_subscription_from_moyasar_invoice``)
     — pure DB logic, idempotency, and the four refusal reasons.
  2. The webhook normaliser (``normalize_moyasar_event``) — must turn both
     invoice-shape and payment-shape webhook bodies into the same payload.
  3. The result-page reconcile (``reconcile_subscription_from_moyasar``)
     — the polling-page path that queries Moyasar live when the
     subscription is still pending. We monkey-patch the Moyasar client so
     no network is required.

Background — production bug this protects against:
    Moyasar's ``callback_url`` on invoices is the **browser redirect URL**
    after the customer pays — not a server-to-server webhook. So no
    webhook ever arrived for hosted-page invoices, and the subscription
    stayed in ``pending_payment`` forever. The fix moved the activation
    into a shared helper that the polling page can call live. These
    tests pin that contract so a future refactor cannot reintroduce the
    silent-stuck-payment regression.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    Base,
    BillingPayment,
    BillingPlan,
    BillingSubscription,
    Tenant,
)
from services.billing_activation import (  # noqa: E402
    activate_subscription_from_moyasar_invoice,
    extract_payment_id_from_invoice,
    normalize_moyasar_event,
    reconcile_subscription_from_moyasar,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_db():
    """In-memory SQLite that mirrors the production schema. JSONB columns
    are replaced with JSON for SQLite compatibility, then restored after
    create_all so the rest of the suite is unaffected."""
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture
def db():
    s = _make_db()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def tenant(db):
    t = Tenant(name="Tenant 33", subscription_status="trialing")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@pytest.fixture
def plan(db):
    p = BillingPlan(
        tenant_id=None,
        slug="growth",
        name="Growth",
        description="Growth plan",
        currency="SAR",
        price_sar=849,
        billing_cycle="monthly",
        features=[],
        limits={},
        extra_metadata={"name_ar": "النمو"},
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture
def pending_sub(db, tenant, plan):
    s = BillingSubscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status="pending_payment",
        started_at=datetime.now(timezone.utc),
        auto_renew=True,
        extra_metadata={
            "gateway":           "moyasar",
            "moyasar_invoice_id": "inv_b3f61bf1",
            "price_charged_sar":  849,
        },
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _paid_invoice(invoice_id: str = "inv_b3f61bf1", payment_id: str = "pay_f6a32bd2"):
    """Realistic Moyasar invoice payload with a single paid payment."""
    return {
        "id":       invoice_id,
        "status":   "paid",
        "amount":   84900,
        "currency": "SAR",
        "metadata": {
            "subscription_id": "11",
            "tenant_id":       "33",
            "plan_slug":       "growth",
        },
        "payments": [
            {"id": payment_id, "status": "paid", "amount": 84900},
        ],
    }


# ── normalize_moyasar_event ─────────────────────────────────────────────


class TestNormalizeMoyasarEvent:
    """The shape detector is the single point that protects us from
    silently dropping webhook deliveries the way the original handler
    did. These tests pin both shapes."""

    def test_payment_shape_returned_as_payment(self):
        event = {"id": "pay_x", "status": "paid", "metadata": {"a": 1}}
        payload, shape = normalize_moyasar_event(event)
        assert shape == "payment"
        assert payload is event

    def test_invoice_shape_unwraps_data(self):
        event = {
            "id":   "evt_x",
            "type": "invoice.paid",
            "data": {"id": "inv_x", "status": "paid", "metadata": {"k": "v"}},
        }
        payload, shape = normalize_moyasar_event(event)
        assert shape == "invoice"
        assert payload == event["data"]

    def test_empty_event_returns_payment_shape(self):
        payload, shape = normalize_moyasar_event({})
        assert shape == "payment"
        assert payload == {}

    def test_data_without_status_or_metadata_treated_as_payment(self):
        # Some webhook test pings include a ``data`` wrapper but with
        # unrelated keys; we should not misclassify them.
        event = {"id": "x", "status": "paid", "data": {"unrelated": True}}
        payload, shape = normalize_moyasar_event(event)
        assert shape == "payment"
        assert payload is event


# ── extract_payment_id_from_invoice ─────────────────────────────────────


class TestExtractPaymentId:
    def test_picks_first_paid_payment(self):
        inv = _paid_invoice(payment_id="pay_real")
        assert extract_payment_id_from_invoice(inv) == "pay_real"

    def test_skips_unpaid_payments(self):
        inv = {
            "id": "inv_x",
            "payments": [
                {"id": "pay_failed", "status": "failed"},
                {"id": "pay_ok",     "status": "paid"},
            ],
        }
        assert extract_payment_id_from_invoice(inv) == "pay_ok"

    def test_falls_back_to_invoice_id_when_no_payments(self):
        inv = {"id": "inv_only", "status": "paid"}
        assert extract_payment_id_from_invoice(inv) == "inv_only"


# ── activate_subscription_from_moyasar_invoice ──────────────────────────


class TestActivateSubscription:
    def test_happy_path_activates_and_records_payment(self, db, pending_sub):
        activated, reason = activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        assert activated is True
        assert reason == "activated"

        db.refresh(pending_sub)
        assert pending_sub.status == "active"
        assert pending_sub.extra_metadata["moyasar_payment_id"] == "pay_f6a32bd2"
        assert pending_sub.extra_metadata["moyasar_invoice_id"] == "inv_b3f61bf1"
        assert "paid_at" in pending_sub.extra_metadata
        assert pending_sub.extra_metadata["activation_source"] == "reconcile"

        payment = db.query(BillingPayment).filter_by(subscription_id=pending_sub.id).one()
        assert payment.amount_sar == 849
        assert payment.gateway == "moyasar"
        assert payment.transaction_reference == "pay_f6a32bd2"
        assert payment.status == "paid"

    def test_idempotent_when_already_active(self, db, pending_sub):
        # First call activates.
        activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        # Second call must be a no-op.
        activated, reason = activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        assert activated is False
        assert reason == "already_active"

        # And we still only have ONE BillingPayment row.
        count = db.query(BillingPayment).filter_by(subscription_id=pending_sub.id).count()
        assert count == 1

    def test_refuses_unpaid_invoice(self, db, pending_sub):
        unpaid = {**_paid_invoice(), "status": "pending"}
        activated, reason = activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=unpaid,
        )
        assert activated is False
        assert reason == "invoice_not_paid"
        db.refresh(pending_sub)
        assert pending_sub.status == "pending_payment"  # untouched

    def test_refuses_to_revive_cancelled_sub(self, db, pending_sub):
        pending_sub.status = "cancelled"
        db.commit()
        activated, reason = activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        assert activated is False
        assert reason == "unexpected_status"
        db.refresh(pending_sub)
        # Must NOT have flipped to active — that would silently re-bill
        # a merchant who cancelled.
        assert pending_sub.status == "cancelled"

    def test_records_activation_source(self, db, pending_sub):
        activate_subscription_from_moyasar_invoice(
            db, pending_sub,
            invoice_data=_paid_invoice(),
            source="webhook_invoice",
        )
        db.refresh(pending_sub)
        assert pending_sub.extra_metadata["activation_source"] == "webhook_invoice"

    def test_preserves_existing_metadata(self, db, pending_sub):
        # Add some unrelated keys that activation must not nuke.
        meta = dict(pending_sub.extra_metadata or {})
        meta["custom_flag"] = "keep_me"
        meta["launch_discount"] = True
        pending_sub.extra_metadata = meta
        db.commit()

        activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        db.refresh(pending_sub)
        assert pending_sub.extra_metadata["custom_flag"] == "keep_me"
        assert pending_sub.extra_metadata["launch_discount"] is True

    def test_raises_on_malformed_invoice_data(self, db, pending_sub):
        with pytest.raises(ValueError):
            activate_subscription_from_moyasar_invoice(
                db, pending_sub, invoice_data="not a dict",  # type: ignore[arg-type]
            )

    def test_uses_metadata_price_when_invoice_amount_zero(self, db, pending_sub):
        # Some refund / launch-discount edge cases give amount=0; we
        # should fall back to the recorded price_charged_sar.
        zero_amount_invoice = {**_paid_invoice(), "amount": 0}
        activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=zero_amount_invoice,
        )
        payment = db.query(BillingPayment).filter_by(subscription_id=pending_sub.id).one()
        assert payment.amount_sar == 849  # from price_charged_sar in fixture

    def test_uses_explicit_payment_id_when_provided(self, db, pending_sub):
        # Webhook handler passes payment_id explicitly when it was
        # extracted from a payment-shape event; that override must win.
        activated, _ = activate_subscription_from_moyasar_invoice(
            db, pending_sub,
            invoice_data=_paid_invoice(payment_id="pay_from_invoice"),
            payment_id="pay_explicit",
        )
        assert activated is True
        payment = db.query(BillingPayment).filter_by(subscription_id=pending_sub.id).one()
        assert payment.transaction_reference == "pay_explicit"


# ── reconcile_subscription_from_moyasar (the polling-page path) ─────────


class _FakeMoyasarClient:
    """Stand-in for ``MoyasarClient`` used by the reconcile fixture."""

    def __init__(self, invoice_response):
        self._response = invoice_response
        self.calls = []

    async def get_invoice(self, invoice_id: str):
        self.calls.append(invoice_id)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _run(coro):
    """Run an async coroutine to completion in a private event loop.

    We use ``asyncio.run`` rather than ``@pytest.mark.asyncio`` because
    the repo doesn't depend on pytest-asyncio, and these tests don't
    need any of its sugar. Each call gets a fresh loop so there's no
    cross-test contamination.
    """
    return asyncio.run(coro)


class TestReconcileFromMoyasar:
    def test_no_invoice_id_skips_reconcile(self, db, tenant, plan):
        sub = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=datetime.now(timezone.utc), auto_renew=True,
            extra_metadata={},  # no invoice id
        )
        db.add(sub); db.commit(); db.refresh(sub)

        activated, reason = _run(reconcile_subscription_from_moyasar(db, sub))
        assert activated is False
        assert reason == "no_invoice_id"

    def test_already_active_short_circuits(self, db, pending_sub):
        pending_sub.status = "active"
        db.commit()
        activated, reason = _run(reconcile_subscription_from_moyasar(db, pending_sub))
        assert activated is False
        assert reason == "already_active"

    def test_moyasar_unconfigured_returns_clear_reason(
        self, db, pending_sub, monkeypatch,
    ):
        # Simulate "no Moyasar secret anywhere" — neither tenant nor platform.
        from services import billing_activation as ba
        monkeypatch.setattr(ba, "_moyasar_client", lambda *_a, **_k: None)

        activated, reason = _run(reconcile_subscription_from_moyasar(db, pending_sub))
        assert activated is False
        assert reason == "moyasar_unconfigured"
        db.refresh(pending_sub)
        assert pending_sub.status == "pending_payment"

    def test_live_paid_invoice_activates(self, db, pending_sub, monkeypatch):
        fake = _FakeMoyasarClient(_paid_invoice())
        from services import billing_activation as ba
        monkeypatch.setattr(ba, "_moyasar_client", lambda *_a, **_k: fake)

        activated, reason = _run(reconcile_subscription_from_moyasar(db, pending_sub))
        assert activated is True
        assert reason == "activated"
        assert fake.calls == ["inv_b3f61bf1"]

        db.refresh(pending_sub)
        assert pending_sub.status == "active"
        assert pending_sub.extra_metadata["activation_source"] == "result_page_poll"

    def test_live_pending_invoice_keeps_sub_pending(
        self, db, pending_sub, monkeypatch,
    ):
        # Customer hasn't paid yet — Moyasar reports invoice as pending.
        fake = _FakeMoyasarClient({**_paid_invoice(), "status": "pending"})
        from services import billing_activation as ba
        monkeypatch.setattr(ba, "_moyasar_client", lambda *_a, **_k: fake)

        activated, reason = _run(reconcile_subscription_from_moyasar(db, pending_sub))
        assert activated is False
        assert reason == "invoice_not_paid"
        db.refresh(pending_sub)
        assert pending_sub.status == "pending_payment"

    def test_moyasar_api_error_does_not_raise(
        self, db, pending_sub, monkeypatch,
    ):
        # Network blip / 5xx from Moyasar must be swallowed so the polling
        # page can retry on the next tick.
        fake = _FakeMoyasarClient(RuntimeError("connection reset"))
        from services import billing_activation as ba
        monkeypatch.setattr(ba, "_moyasar_client", lambda *_a, **_k: fake)

        activated, reason = _run(reconcile_subscription_from_moyasar(db, pending_sub))
        assert activated is False
        assert reason == "moyasar_api_error"
        db.refresh(pending_sub)
        assert pending_sub.status == "pending_payment"

    def test_reconcile_is_idempotent_across_polls(
        self, db, pending_sub, monkeypatch,
    ):
        # Two consecutive polls must result in exactly one BillingPayment.
        fake = _FakeMoyasarClient(_paid_invoice())
        from services import billing_activation as ba
        monkeypatch.setattr(ba, "_moyasar_client", lambda *_a, **_k: fake)

        _run(reconcile_subscription_from_moyasar(db, pending_sub))
        _run(reconcile_subscription_from_moyasar(db, pending_sub))

        count = db.query(BillingPayment).filter_by(subscription_id=pending_sub.id).count()
        assert count == 1
