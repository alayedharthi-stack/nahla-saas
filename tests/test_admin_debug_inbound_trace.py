"""
tests/test_admin_debug_inbound_trace.py
───────────────────────────────────────
Locks the F17 diagnostic endpoint:

    GET /admin/debug/inbound-trace

This is the read-only inbound-AI trace tool support uses when a
merchant says "I connected 360dialog Coexistence but the AI is
silent". It walks the database (no side effects) and reports
which of seven pipeline stages the most recent inbound message
got stuck in.

Why test the endpoint directly
──────────────────────────────
The endpoint is the public contract: the dashboard renders one
row per stage, and the verdict_code drives the Arabic copy.
Asserting the response shape is more useful than asserting the
helpers in isolation, because the helpers (``has_billing_access``,
``is_internal_or_blocked``, ``is_inbound_before_ai_live_since``)
are already locked by their own suites. We mirror the calling
pattern of ``test_admin_debug_whatsapp_send.py``: invoke the
async handler directly with ``asyncio.run`` and a fake admin
payload — that keeps the test fast and avoids a TestClient.

What's covered:

* Tenant not found → 404.
* No inbound message yet → step_1 still reports webhook evidence
  from ``last_webhook_received_at``; step_2 fails with code
  ``no_inbound_message_found``.
* Happy path — webhook stamp + inbound + outbound (with wamid) →
  every stage green, ``verdict.code == "ok"``.
* Conversation paused → step_3 fails with ``ai_paused`` blocker.
* Billing access denied (no subscription) → step_4 fails with
  ``billing_access_denied`` blocker.
* Outbound without wamid → step_7 fails with ``missing_wamid``.
* Phone-targeted query resolves to the right wamid.
* wa_message_id-targeted query takes precedence over phone.
* Phone numbers are masked in the response (no leakage).

NOT covered (intentional):

* The provider HTTP call — this is a read-only diagnostic so the
  provider is never invoked.
* Auto-heal / pause / resume — same reason.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _run(coro):
    return asyncio.run(coro)


def _make_db():
    """In-memory SQLite with the JSONB→JSON downgrade pattern used
    by the rest of the suite. We patch the JSONB columns in place
    while Base.metadata.create_all() runs, then restore them so
    the rest of the test process behaves normally."""
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base

    engine = create_engine("sqlite:///:memory:")
    _saved = []
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


# ── Fixture helpers ────────────────────────────────────────────────


def _seed_tenant(db, *, tenant_id=33, is_platform_tenant=False, with_trial=False):
    """Seed a tenant. When ``with_trial=True`` we set
    ``trial_ends_at`` 14 days in the future so
    ``has_billing_access`` returns True without us having to seed a
    full BillingPlan + BillingSubscription chain."""
    from models import Tenant
    kwargs = dict(
        id=tenant_id,
        name=f"tenant-{tenant_id}",
        is_platform_tenant=is_platform_tenant,
    )
    if with_trial:
        kwargs["trial_started_at"] = datetime.now(timezone.utc)
        kwargs["trial_ends_at"]    = datetime.now(timezone.utc) + timedelta(days=14)
    t = Tenant(**kwargs)
    db.add(t); db.commit()
    return t


# Module-level sentinel so callers can pass `None` to mean "really
# unset the column" without conflicting with our default-args dance.
_UNSET = object()


def _seed_connection(
    db, *,
    tenant_id=33,
    provider="dialog360",
    connection_type="coexistence",
    status="connected",
    phone_number_id="100543193146977",
    access_token="d360_fake_token_ABCDEFGH",
    coexistence_secret="shared_secret_test",
    last_webhook_received_at=_UNSET,
):
    from models import WhatsAppConnection
    if last_webhook_received_at is _UNSET:
        last_webhook_received_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    # When the caller passes `None` explicitly we want the column to
    # be NULL — that's the "silent channel" scenario the diagnostic
    # is supposed to flag.
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        provider=provider,
        connection_type=connection_type,
        status=status,
        phone_number_id=phone_number_id,
        access_token=access_token,
        last_webhook_received_at=last_webhook_received_at,
        extra_metadata={
            "coexistence_internal_secret": coexistence_secret,
            "coexistence": {"last_event": {"field": "messages", "at": "2026-05-11T12:00:00Z"}},
        },
        webhook_verified=True,
    )
    db.add(conn); db.commit()
    return conn


def _grant_billing_access(db, *, tenant_id=33):
    """Bump the tenant's free-trial window so
    ``has_billing_access`` returns True. Avoids us having to seed a
    full BillingPlan + BillingSubscription chain just to clear
    stage 4 of the trace pipeline."""
    from models import Tenant
    t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if t is not None:
        t.trial_started_at = datetime.now(timezone.utc)
        t.trial_ends_at    = datetime.now(timezone.utc) + timedelta(days=14)
        db.commit()
    return t


def _seed_customer_and_convo(db, *, tenant_id=33, phone="+966537970430"):
    from models import Conversation, Customer
    c = Customer(tenant_id=tenant_id, normalized_phone=phone, phone=phone, name="Test")
    db.add(c); db.commit()
    convo = Conversation(
        tenant_id=tenant_id,
        customer_id=c.id,
        status="active",
    )
    db.add(convo); db.commit()
    return c, convo


def _seed_inbound(
    db,
    *,
    tenant_id=33,
    conversation_id,
    phone="+966537970430",
    wa_message_id="wamid.INBOUND_1",
    body="مرحبا",
    minutes_ago=2,
):
    from models import MessageEvent
    ev = MessageEvent(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        direction="inbound",
        body=body,
        event_type="whatsapp",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        extra_metadata={
            "phone": phone,
            "wa_message_id": wa_message_id,
            "message_origin": "live_webhook",
            "historical_import": False,
        },
    )
    db.add(ev); db.commit()
    return ev


def _seed_outbound(
    db,
    *,
    tenant_id=33,
    conversation_id,
    phone="+966537970430",
    wa_message_id="wamid.OUTBOUND_1",
    body="أهلًا بك! كيف يمكنني مساعدتك؟",
    seconds_after=4,
):
    """An outbound row whose extra_metadata carries a wamid =
    the wire call returned a wamid (i.e. provider accepted)."""
    from models import MessageEvent
    extra: dict = {"phone": phone, "source": "merchant_ai"}
    if wa_message_id is not None:
        extra["wa_message_id"] = wa_message_id
    ev = MessageEvent(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        direction="outbound",
        body=body,
        event_type="whatsapp",
        created_at=datetime.now(timezone.utc) + timedelta(seconds=seconds_after),
        extra_metadata=extra,
    )
    db.add(ev); db.commit()
    return ev


def _call_trace(*, db, tenant_id, phone=None, wa_message_id=None, admin_sub="admin@nahla"):
    """Invoke the handler directly, bypassing the require_admin
    Depends() by passing a fake admin payload."""
    from routers.admin_debug import admin_debug_inbound_trace
    return _run(admin_debug_inbound_trace(
        tenant_id=tenant_id,
        phone=phone,
        wa_message_id=wa_message_id,
        db=db,
        _admin={"sub": admin_sub, "role": "admin"},
    ))


# ── Tests ──────────────────────────────────────────────────────────


class TestTenantValidation:
    def test_unknown_tenant_returns_404(self):
        from fastapi import HTTPException
        db = _make_db()
        with pytest.raises(HTTPException) as exc:
            _call_trace(db=db, tenant_id=99999)
        assert exc.value.status_code == 404
        assert "not found" in str(exc.value.detail).lower()


class TestConnectionDiagnosis:
    def test_no_connection_row_surfaces_top_level_issue(self):
        db = _make_db()
        _seed_tenant(db)
        resp = _call_trace(db=db, tenant_id=33)
        assert resp["connection"]["found"] is False
        assert any("WhatsAppConnection" in s for s in resp["issues"])
        assert resp["ok"] is False

    def test_non_dialog360_connection_surfaces_provider_mismatch_issue(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, provider="meta", connection_type="direct")
        resp = _call_trace(db=db, tenant_id=33)
        assert resp["connection"]["provider"] == "meta"
        assert any("360dialog" in s and "ليس" in s for s in resp["issues"])

    def test_missing_access_token_surfaces_send_blocker_issue(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, access_token="")
        resp = _call_trace(db=db, tenant_id=33)
        assert resp["connection"]["access_token_present"] is False
        assert any("D360-API-KEY" in s for s in resp["issues"])

    def test_no_webhook_received_at_surfaces_silent_channel_issue(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, last_webhook_received_at=None)
        resp = _call_trace(db=db, tenant_id=33)
        # last_webhook_received_at=None ⇒ stage 1 not ok.
        assert resp["pipeline"]["step_1_webhook_received"]["ok"] is False
        assert "no_webhook_evidence" in resp["pipeline"]["step_1_webhook_received"]["blocked_by"]
        assert any("webhook" in s.lower() for s in resp["issues"])

    def test_access_token_tail_is_masked_not_full(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, access_token="d360_supersecret_TAIL")
        resp = _call_trace(db=db, tenant_id=33)
        tail = resp["connection"]["access_token_tail"]
        # Only the last 4 chars should leak.
        assert tail is None or len(tail) <= 8
        assert "supersecret" not in (tail or "")


class TestTargetResolution:
    """The endpoint targets a message in three modes — wamid >
    phone > latest. Lock the precedence."""

    def test_no_messages_at_all_step_2_blocks(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        resp = _call_trace(db=db, tenant_id=33)
        assert resp["inbound_message_found"] is False
        s2 = resp["pipeline"]["step_2_message_saved"]
        assert s2["ok"] is False
        assert "no_inbound_message_found" in s2["blocked_by"]
        # Webhook stage still ok thanks to last_webhook_received_at.
        assert resp["pipeline"]["step_1_webhook_received"]["ok"] is True

    def test_resolves_latest_when_no_filter(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db)
        _seed_inbound(db, conversation_id=convo.id, wa_message_id="wamid.OLD", minutes_ago=60)
        _seed_inbound(db, conversation_id=convo.id, wa_message_id="wamid.NEW", minutes_ago=1)
        resp = _call_trace(db=db, tenant_id=33)
        assert resp["pipeline"]["step_2_message_saved"]["details"]["wa_message_id"] == "wamid.NEW"

    def test_wa_message_id_filter_takes_precedence_over_phone(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db, phone="+966500000001")
        _seed_inbound(
            db, conversation_id=convo.id,
            phone="+966500000001", wa_message_id="wamid.WANTED", minutes_ago=10,
        )
        _seed_inbound(
            db, conversation_id=convo.id,
            phone="+966500000002", wa_message_id="wamid.OTHER", minutes_ago=1,
        )
        resp = _call_trace(
            db=db, tenant_id=33,
            phone="+966500000002",  # phone says "OTHER"
            wa_message_id="wamid.WANTED",  # wamid says "WANTED" — wins
        )
        assert resp["pipeline"]["step_2_message_saved"]["details"]["wa_message_id"] == "wamid.WANTED"

    def test_phone_filter_returns_latest_for_that_phone(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo_a = _seed_customer_and_convo(db, phone="+966500000001")
        _, convo_b = _seed_customer_and_convo(db, phone="+966500000002")
        _seed_inbound(
            db, conversation_id=convo_a.id,
            phone="+966500000001", wa_message_id="wamid.A1", minutes_ago=5,
        )
        _seed_inbound(
            db, conversation_id=convo_b.id,
            phone="+966500000002", wa_message_id="wamid.B1", minutes_ago=1,
        )
        resp = _call_trace(db=db, tenant_id=33, phone="+966500000001")
        assert resp["pipeline"]["step_2_message_saved"]["details"]["wa_message_id"] == "wamid.A1"


class TestPhoneMasking:
    """No phone number — input, inbound metadata, customer — should
    appear unredacted anywhere in the response payload."""

    def test_input_phone_is_masked(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db, phone="+966537970430")
        _seed_inbound(db, conversation_id=convo.id, phone="+966537970430")
        resp = _call_trace(db=db, tenant_id=33, phone="+966537970430")
        # input.phone_masked must NOT contain the middle digits.
        masked = resp["input"]["phone_masked"]
        assert masked is not None
        assert "537970" not in masked
        # The stored phone on the inbound block is also masked.
        assert "537970" not in (resp["pipeline"]["step_2_message_saved"]["details"]["phone"] or "")


class TestPipelineGates:
    """Each stage owns a different blocker code. These tests
    confirm the right code surfaces when the right thing breaks."""

    def test_happy_path_all_green(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db)
        inb = _seed_inbound(db, conversation_id=convo.id)
        _seed_outbound(db, conversation_id=convo.id, wa_message_id="wamid.OK")

        resp = _call_trace(db=db, tenant_id=33)
        p = resp["pipeline"]
        assert p["step_1_webhook_received"]["ok"] is True
        assert p["step_2_message_saved"]["ok"] is True
        assert p["step_3_conversation_state"]["ok"] is True
        assert p["step_4_ai_allowed"]["ok"] is True
        assert p["step_5_ai_generated"]["ok"] is True
        assert p["step_6_send_attempted"]["ok"] is True
        assert p["step_7_send_status"]["ok"] is True
        assert resp["verdict"]["code"] == "ok"
        assert resp["ok"] is True

    def test_ai_paused_blocks_at_stage_3(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db)
        convo.ai_paused = True
        convo.ai_paused_reason = "manual"
        db.commit()
        _seed_inbound(db, conversation_id=convo.id)

        resp = _call_trace(db=db, tenant_id=33)
        s3 = resp["pipeline"]["step_3_conversation_state"]
        assert s3["ok"] is False
        assert "ai_paused" in s3["blocked_by"]
        assert resp["verdict"]["failed_stage"] == "step_3_conversation_state"

    def test_billing_access_denied_blocks_at_stage_4(self):
        """A freshly-created Tenant gets an automatic 14-day trial
        from ``compute_trial_info`` (fallback to ``created_at``), so
        merely "not seeding a subscription" is NOT enough to deny
        billing. We have to push ``trial_started_at`` /
        ``trial_ends_at`` into the past explicitly."""
        from models import Tenant
        db = _make_db()
        _seed_tenant(db); _seed_connection(db)
        t = db.query(Tenant).filter(Tenant.id == 33).first()
        t.trial_started_at = datetime.now(timezone.utc) - timedelta(days=60)
        t.trial_ends_at    = datetime.now(timezone.utc) - timedelta(days=30)
        # Also kick created_at into the past so the
        # compute_trial_info fallback path can't bail it out.
        t.created_at = datetime.now(timezone.utc) - timedelta(days=120)
        db.commit()
        _, convo = _seed_customer_and_convo(db)
        _seed_inbound(db, conversation_id=convo.id)

        resp = _call_trace(db=db, tenant_id=33)
        s4 = resp["pipeline"]["step_4_ai_allowed"]
        assert s4["details"]["billing_access"] is False
        assert "billing_access_denied" in s4["blocked_by"]
        assert s4["ok"] is False
        # No outbound persisted, so step 5 also fails — but the
        # verdict should report the FIRST failing stage, which is
        # stage 4 here (stage 3 still ok because convo is clean).
        assert resp["verdict"]["failed_stage"] == "step_4_ai_allowed"

    def test_no_outbound_after_inbound_blocks_at_stage_5(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db)
        _seed_inbound(db, conversation_id=convo.id)
        # No outbound seeded.

        resp = _call_trace(db=db, tenant_id=33)
        s5 = resp["pipeline"]["step_5_ai_generated"]
        assert s5["ok"] is False
        assert "no_outbound_after_inbound" in s5["blocked_by"]
        assert resp["verdict"]["failed_stage"] == "step_5_ai_generated"

    def test_outbound_without_wamid_blocks_at_stage_7(self):
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db)
        _seed_inbound(db, conversation_id=convo.id)
        # Outbound row exists but did NOT get a wamid back (=
        # send failed at the provider).
        _seed_outbound(db, conversation_id=convo.id, wa_message_id=None)

        resp = _call_trace(db=db, tenant_id=33)
        p = resp["pipeline"]
        assert p["step_5_ai_generated"]["ok"] is True
        assert p["step_6_send_attempted"]["ok"] is True
        assert p["step_6_send_attempted"]["details"]["provider_message_id_recorded"] is False
        assert p["step_7_send_status"]["ok"] is False
        assert "missing_wamid" in p["step_7_send_status"]["blocked_by"]
        assert resp["verdict"]["failed_stage"] == "step_7_send_status"

    def test_platform_tenant_routing_blocks_at_stage_4(self):
        db = _make_db()
        _seed_tenant(db, is_platform_tenant=True)
        _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db)
        _seed_inbound(db, conversation_id=convo.id)

        resp = _call_trace(db=db, tenant_id=33)
        s4 = resp["pipeline"]["step_4_ai_allowed"]
        assert s4["details"]["is_platform_tenant"] is True
        assert "platform_tenant_routing" in s4["blocked_by"]


class TestNoSideEffects:
    """Read-only contract: the trace MUST NOT pause/resume any
    conversation, MUST NOT touch ai_paused flags, MUST NOT write
    any MessageEvent."""

    def test_running_trace_does_not_mutate_conversation_flags(self):
        from models import Conversation, MessageEvent
        db = _make_db()
        _seed_tenant(db); _seed_connection(db); _grant_billing_access(db)
        _, convo = _seed_customer_and_convo(db)
        _seed_inbound(db, conversation_id=convo.id)
        _seed_outbound(db, conversation_id=convo.id, wa_message_id="wamid.OK")

        before_paused = bool(convo.ai_paused)
        before_count = db.query(MessageEvent).count()

        _call_trace(db=db, tenant_id=33)
        # Re-fetch from DB to be sure we see committed state.
        fresh = db.query(Conversation).filter(Conversation.id == convo.id).first()
        assert bool(fresh.ai_paused) == before_paused
        assert db.query(MessageEvent).count() == before_count
