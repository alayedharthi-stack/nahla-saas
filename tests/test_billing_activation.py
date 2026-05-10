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

    def test_refuses_to_revive_admin_cancelled_sub(self, db, pending_sub):
        # Admin-cancelled sub: it WAS active before, so ``paid_at``
        # is set in metadata. Even if Moyasar still reports the
        # invoice as paid, we refuse to silently re-bill — the cancel
        # was an explicit admin / merchant decision (refund, downgrade,
        # etc.) and must not be undone by an automated reconcile.
        pending_sub.status = "cancelled"
        meta = dict(pending_sub.extra_metadata or {})
        meta["paid_at"] = datetime.now(timezone.utc).isoformat()
        pending_sub.extra_metadata = meta
        db.commit()

        activated, reason = activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        assert activated is False
        assert reason == "unexpected_status"
        db.refresh(pending_sub)
        assert pending_sub.status == "cancelled"

    def test_revives_auto_cancelled_sub_when_invoice_was_paid(self, db, pending_sub):
        # Tenant-33 case: merchant clicked Subscribe twice, our auto
        # sibling-cancel rule cancelled this sub when the second one
        # was created (so status="cancelled" but ``paid_at`` was never
        # set). Then the merchant paid this old sub's Moyasar invoice.
        # The reconcile MUST revive — otherwise the merchant pays and
        # stays on trial.
        pending_sub.status = "cancelled"
        # No paid_at — proves this was never activated.
        meta = dict(pending_sub.extra_metadata or {})
        meta.pop("paid_at", None)
        pending_sub.extra_metadata = meta
        db.commit()

        activated, reason = activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        assert activated is True
        assert reason == "activated"
        db.refresh(pending_sub)
        assert pending_sub.status == "active"
        assert (pending_sub.extra_metadata or {}).get("paid_at")

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


# ── Sibling cancellation invariant ──────────────────────────────────────


class TestSiblingCancellation:
    """Activation must leave the tenant with exactly one non-cancelled
    subscription. This is the invariant that protects the admin tenants
    table from rendering ``Growth + pending_payment`` for a tenant whose
    real state is ``Growth + active`` (the production bug that exposed
    this — a merchant who clicked Subscribe again after paying ended up
    with a fresh stale ``pending_payment`` row that ranked higher in the
    "latest sub" query than the activated one)."""

    def test_other_pending_subs_cancelled_on_activation(
        self, db, tenant, plan, pending_sub,
    ):
        # Simulate the merchant clicking "Subscribe" twice — _do_checkout
        # creates a second pending row alongside the first.
        sib_a = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id,
            status="pending_payment",
            started_at=datetime.now(timezone.utc),
            auto_renew=True, extra_metadata={"reason": "second_click"},
        )
        sib_b = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id,
            status="pending_payment",
            started_at=datetime.now(timezone.utc),
            auto_renew=True, extra_metadata={"reason": "third_click"},
        )
        db.add_all([sib_a, sib_b]); db.commit()

        activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        db.expire_all()  # drop session cache so we read committed state

        active = (
            db.query(BillingSubscription)
            .filter_by(tenant_id=tenant.id, status="active").all()
        )
        cancelled = (
            db.query(BillingSubscription)
            .filter_by(tenant_id=tenant.id, status="cancelled").all()
        )
        assert {s.id for s in active} == {pending_sub.id}
        assert {s.id for s in cancelled} == {sib_a.id, sib_b.id}

    def test_active_subs_in_other_tenants_untouched(
        self, db, tenant, plan, pending_sub,
    ):
        # Sanity: cancellation must be tenant-scoped. A pending sub in a
        # *different* tenant must NOT be cancelled when this one activates.
        from models import Tenant as TenantModel  # noqa: PLC0415
        other_tenant = TenantModel(name="Other tenant")
        db.add(other_tenant); db.commit(); db.refresh(other_tenant)

        other_pending = BillingSubscription(
            tenant_id=other_tenant.id, plan_id=plan.id,
            status="pending_payment",
            started_at=datetime.now(timezone.utc),
            auto_renew=True, extra_metadata={},
        )
        db.add(other_pending); db.commit(); db.refresh(other_pending)

        activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        db.refresh(other_pending)
        assert other_pending.status == "pending_payment"

    def test_cancelled_subs_not_revived(self, db, tenant, plan, pending_sub):
        # Already-cancelled rows must stay cancelled (we filter to
        # only ``pending_payment`` / ``payment_failed``).
        cancelled = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="cancelled",
            started_at=datetime.now(timezone.utc),
            auto_renew=False, extra_metadata={"old": True},
        )
        db.add(cancelled); db.commit(); db.refresh(cancelled)

        activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        db.refresh(cancelled)
        assert cancelled.status == "cancelled"


# ── _latest_subscription_for_tenant priority ────────────────────────────


class TestLatestSubscriptionPriority:
    """The admin tenants table reads ``_latest_subscription_for_tenant``
    to render Plan + Status. Before the fix, this query was a naive
    ``ORDER BY started_at DESC`` that surfaced any newly-created
    ``pending_payment`` row over an existing ``active`` one. These tests
    pin the priority order: active > trialing > pending_payment > other."""

    def _query(self, db, tenant_id):
        # Import lazily so this test file doesn't pull the FastAPI app
        # at module load time.
        from routers.admin import _latest_subscription_for_tenant  # noqa: PLC0415
        return _latest_subscription_for_tenant(db, tenant_id)

    def test_active_beats_newer_pending(self, db, tenant, plan):
        from datetime import timedelta as _td  # noqa: PLC0415
        now = datetime.now(timezone.utc)
        active = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="active",
            started_at=now - _td(days=2), auto_renew=True, extra_metadata={},
        )
        # Newer pending — would win under the naive query.
        pending = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=now, auto_renew=True, extra_metadata={},
        )
        db.add_all([active, pending]); db.commit()

        latest = self._query(db, tenant.id)
        assert latest is not None
        assert latest.id == active.id
        assert latest.status == "active"

    def test_trialing_beats_pending(self, db, tenant, plan):
        now = datetime.now(timezone.utc)
        trialing = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="trialing",
            started_at=now, auto_renew=True, extra_metadata={},
        )
        pending = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=now, auto_renew=True, extra_metadata={},
        )
        db.add_all([trialing, pending]); db.commit()

        latest = self._query(db, tenant.id)
        assert latest is not None
        assert latest.status == "trialing"

    def test_pending_returned_when_no_active(self, db, tenant, plan):
        pending = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=datetime.now(timezone.utc),
            auto_renew=True, extra_metadata={},
        )
        db.add(pending); db.commit()

        latest = self._query(db, tenant.id)
        assert latest is not None
        assert latest.status == "pending_payment"

    def test_returns_cancelled_only_as_last_resort(self, db, tenant, plan):
        cancelled = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="cancelled",
            started_at=datetime.now(timezone.utc),
            auto_renew=False, extra_metadata={},
        )
        db.add(cancelled); db.commit()

        latest = self._query(db, tenant.id)
        assert latest is not None
        assert latest.status == "cancelled"

    def test_returns_none_for_tenant_without_subs(self, db, tenant):
        latest = self._query(db, tenant.id)
        assert latest is None


# ── End-to-end: paid invoice → admin tenant summary shows active ────────


class TestEndToEndActivationVisible:
    """Pin the full contract: when reconcile activates a sub, the admin
    tenants table must render ``status=active``, not ``pending_payment``."""

    def test_activation_flips_admin_summary_to_active(
        self, db, tenant, plan, pending_sub,
    ):
        # Simulate a stale newer pending row created by a retry click.
        retry = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=datetime.now(timezone.utc),
            auto_renew=True, extra_metadata={"retry": True},
        )
        db.add(retry); db.commit()

        # Activate the original sub (the one Moyasar actually charged).
        activated, reason = activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        assert activated is True
        assert reason == "activated"
        db.expire_all()

        from routers.admin import _latest_subscription_for_tenant  # noqa: PLC0415
        latest = _latest_subscription_for_tenant(db, tenant.id)
        assert latest is not None
        assert latest.id == pending_sub.id
        assert latest.status == "active"

        # And the retry row is now cancelled.
        db.refresh(retry)
        assert retry.status == "cancelled"


# ── Entitlements: plan_id → slug resolution ─────────────────────────────


class TestEntitlementsResolvesPlanSlug:
    """Regression test for the second half of the 'merchant dashboard
    doesn't show plan' bug.

    Before the fix, ``get_entitlements`` did:

        raw_slug  = getattr(sub, "plan_id", "") or ""
        plan_slug = _resolve_plan_slug(raw_slug) or "starter"

    But ``sub.plan_id`` is the FK *integer* to ``billing_plans.id`` —
    not a slug. So ``_resolve_plan_slug(11)`` returned ``11``, then
    ``11 not in PLAN_DEFINITIONS`` collapsed it to ``"none"``. Result:
    every active Moyasar sub showed ``plan="none"`` on the merchant
    dashboard while the admin tenants table (which JOINs via plan_id
    → BillingPlan.slug) showed Growth + active correctly.

    These tests pin the contract that the entitlements resolver must
    do the same join.
    """

    def test_active_sub_resolves_to_growth_plan(self, db, tenant, plan, pending_sub):
        # Activate the sub via the production helper.
        activate_subscription_from_moyasar_invoice(
            db, pending_sub, invoice_data=_paid_invoice(),
        )
        db.expire_all()

        from core.plan_entitlements import get_entitlements  # noqa: PLC0415
        ent = get_entitlements(db, tenant.id)
        assert ent.plan_slug == "growth"
        assert ent.billing_status == "active"
        assert ent.is_active is True
        assert ent.is_blocked is False
        # And the to_dict() shape — what the frontend actually sees —
        # must use the key ``plan`` (not ``plan_slug``):
        as_dict = ent.to_dict()
        assert as_dict["plan"] == "growth"
        assert as_dict["billing_status"] == "active"
        assert as_dict["is_active"] is True

    def test_no_sub_resolves_to_none(self, db, tenant):
        from core.plan_entitlements import get_entitlements  # noqa: PLC0415
        ent = get_entitlements(db, tenant.id)
        assert ent.plan_slug == "none"
        assert ent.is_active is False

    def test_pending_sub_does_not_grant_features(self, db, tenant, plan, pending_sub):
        # While pending_payment, plan must NOT be granted — only after
        # activation. This guards against a future regression where
        # someone "helpfully" widens the active-status filter and
        # accidentally lets unpaid trials access paid features.
        from core.plan_entitlements import get_entitlements  # noqa: PLC0415
        ent = get_entitlements(db, tenant.id)
        assert ent.plan_slug == "none"
        assert ent.is_active is False

    def test_cancelled_sub_resolves_to_none(self, db, tenant, plan):
        cancelled = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="cancelled",
            started_at=datetime.now(timezone.utc),
            auto_renew=False, extra_metadata={},
        )
        db.add(cancelled); db.commit()

        # No active sub anywhere — entitlements should be "none".
        from core.plan_entitlements import get_entitlements  # noqa: PLC0415
        ent = get_entitlements(db, tenant.id)
        assert ent.plan_slug == "none"


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


# ── Lazy reconcile resilience ───────────────────────────────────────────
#
# These tests pin the contract that ``lazy_reconcile_tenant_pending_subs``
# — which runs from /billing/status, /billing/entitlements, and the
# debug endpoint on every dashboard load — never raises and always
# returns a well-formed ``(bool, list[dict])`` even when:
#   * the initial DB query fails,
#   * a per-sub Moyasar call raises,
#   * the session is already in a bad transaction state on entry.
#
# Production bug this prevents: tenant 33 saw a 500 on
# ``/billing/debug/current?force_reconcile=1`` because a single sub's
# reconcile threw and we hadn't covered that path at the lazy layer.


class TestLazyReconcileResilience:
    def test_lazy_reconcile_returns_well_formed_when_no_pending(self, db, tenant):
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            lazy_reconcile_tenant_pending_subs,
        )
        _LAZY_RECONCILE_LAST.clear()
        activated, results = _run(
            lazy_reconcile_tenant_pending_subs(db, tenant.id, source="test"),
        )
        assert activated is False
        assert results == []

    def test_lazy_reconcile_per_sub_failure_does_not_crash_loop(
        self, db, tenant, plan, monkeypatch,
    ):
        # Two pending subs — first one raises on reconcile, second one
        # must still get processed. This is the exact production
        # scenario the debug endpoint hit.
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            lazy_reconcile_tenant_pending_subs,
        )
        from services import billing_activation as ba

        _LAZY_RECONCILE_LAST.clear()

        s1 = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=datetime.now(timezone.utc), auto_renew=True,
            extra_metadata={"moyasar_invoice_id": "inv_a"},
        )
        s2 = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=datetime.now(timezone.utc), auto_renew=True,
            extra_metadata={"moyasar_invoice_id": "inv_b"},
        )
        db.add_all([s1, s2]); db.commit(); db.refresh(s1); db.refresh(s2)

        seen: list[int] = []

        async def _flaky_reconcile(_db, sub, *, source):
            seen.append(sub.id)
            if sub.id == s1.id:
                raise RuntimeError("boom")
            return False, "invoice_not_paid"

        monkeypatch.setattr(
            ba, "reconcile_subscription_from_moyasar", _flaky_reconcile,
        )

        activated, results = _run(
            lazy_reconcile_tenant_pending_subs(db, tenant.id, source="test"),
        )
        # Must have visited BOTH subs.
        assert sorted(seen) == sorted([s1.id, s2.id])
        # Result list reflects the failure of the first AND the
        # successful no-op of the second.
        sids = {r["sub_id"]: r for r in results}
        assert "error" in sids[s1.id]
        assert sids[s1.id]["error_type"] == "RuntimeError"
        assert sids[s2.id].get("activated") is False
        assert sids[s2.id].get("reason") == "invoice_not_paid"
        assert activated is False  # neither flipped to active

    def test_lazy_reconcile_recovers_from_stale_failed_transaction(
        self, db, tenant, plan, monkeypatch,
    ):
        # If a previous request left the session in a "failed transaction"
        # state, the first query of lazy reconcile must rollback first
        # so it doesn't propagate the error and 500 the caller. We
        # simulate the state by raising on the first attempt and
        # checking the call site rolled back before retrying.
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            lazy_reconcile_tenant_pending_subs,
        )
        _LAZY_RECONCILE_LAST.clear()

        s = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=datetime.now(timezone.utc), auto_renew=True,
            extra_metadata={"moyasar_invoice_id": "inv_x"},
        )
        db.add(s); db.commit(); db.refresh(s)

        # Force the session into a "needs rollback" state.
        try:
            db.execute(__import__("sqlalchemy").text("SELECT 1 FROM nonexistent_table"))
        except Exception:
            pass

        # Lazy reconcile must NOT raise; it must rollback and run
        # cleanly. (We don't care here whether Moyasar is configured —
        # we only assert no exception leaks.)
        activated, results = _run(
            lazy_reconcile_tenant_pending_subs(db, tenant.id, source="test"),
        )
        assert isinstance(activated, bool)
        assert isinstance(results, list)

    def test_lazy_reconcile_revives_auto_cancelled_paid_sub(
        self, db, tenant, plan, monkeypatch,
    ):
        # The exact tenant-33 production scenario:
        #   1. Auto-cancelled sub 11 (status=cancelled, has invoice, no paid_at).
        #   2. Newer pending sub 12 (status=pending_payment, different invoice).
        #   3. Moyasar reports sub 11's invoice as PAID, sub 12's as pending.
        # Expected: lazy reconcile finds sub 11 via the cancelled-revivable
        # path, queries Moyasar, sees paid, and flips sub 11 to active.
        # Sub 12 stays pending (its invoice isn't paid).
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            lazy_reconcile_tenant_pending_subs,
        )
        from services import billing_activation as ba

        _LAZY_RECONCILE_LAST.clear()

        sub11 = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="cancelled",
            started_at=datetime.now(timezone.utc), auto_renew=True,
            extra_metadata={"moyasar_invoice_id": "inv_11"},  # NO paid_at
        )
        sub12 = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="pending_payment",
            started_at=datetime.now(timezone.utc), auto_renew=True,
            extra_metadata={"moyasar_invoice_id": "inv_12"},
        )
        db.add_all([sub11, sub12]); db.commit()
        db.refresh(sub11); db.refresh(sub12)

        # Mock Moyasar: inv_11 paid, inv_12 still pending.
        class _Fake:
            async def get_invoice(self, invoice_id):
                if invoice_id == "inv_11":
                    return {**_paid_invoice(invoice_id="inv_11", payment_id="pay_11")}
                return {**_paid_invoice(invoice_id="inv_12"), "status": "pending"}
        monkeypatch.setattr(ba, "_moyasar_client", lambda *_a, **_k: _Fake())

        activated, results = _run(
            lazy_reconcile_tenant_pending_subs(db, tenant.id, source="test"),
        )

        assert activated is True
        # Sub 11 must now be active.
        db.refresh(sub11)
        assert sub11.status == "active"
        # Sub 12 must be cancelled — the activation helper cancels
        # stale sibling pending subs so the merchant ends up with
        # exactly one non-cancelled subscription, never two.
        db.refresh(sub12)
        assert sub12.status == "cancelled"
        # The result list includes raw moyasar snapshots so the debug
        # endpoint can surface what Moyasar said. Note: sub12's snapshot
        # may be missing if it was iterated AFTER sub11's activation
        # (and thus already cancelled by the sibling rule); but sub11's
        # snapshot must be present.
        snaps_by_sub = {r["sub_id"]: r for r in results}
        assert "moyasar_snapshot" in snaps_by_sub[sub11.id]
        assert snaps_by_sub[sub11.id]["moyasar_snapshot"]["status"] == "paid"

    def test_lazy_reconcile_skips_admin_cancelled_subs(
        self, db, tenant, plan, monkeypatch,
    ):
        # Admin-cancelled sub (paid_at present) must NEVER be revived
        # by lazy reconcile, even if Moyasar still reports paid.
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            lazy_reconcile_tenant_pending_subs,
        )
        from services import billing_activation as ba

        _LAZY_RECONCILE_LAST.clear()

        admin_cancelled = BillingSubscription(
            tenant_id=tenant.id, plan_id=plan.id, status="cancelled",
            started_at=datetime.now(timezone.utc), auto_renew=True,
            extra_metadata={
                "moyasar_invoice_id": "inv_old",
                "paid_at": datetime.now(timezone.utc).isoformat(),  # was active
            },
        )
        db.add(admin_cancelled); db.commit(); db.refresh(admin_cancelled)

        called: list[str] = []

        class _Fake:
            async def get_invoice(self, invoice_id):
                called.append(invoice_id)
                return _paid_invoice(invoice_id=invoice_id)
        monkeypatch.setattr(ba, "_moyasar_client", lambda *_a, **_k: _Fake())

        activated, results = _run(
            lazy_reconcile_tenant_pending_subs(db, tenant.id, source="test"),
        )

        assert activated is False
        # Critically — Moyasar must NOT have been called for an
        # admin-cancelled sub. We never want to read the invoice,
        # let alone activate it.
        assert called == []
        db.refresh(admin_cancelled)
        assert admin_cancelled.status == "cancelled"
