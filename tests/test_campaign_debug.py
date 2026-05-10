"""tests/test_campaign_debug.py
─────────────────────────────────
Coverage for the campaign-diagnostic surface:

  * GET  /campaigns/{id}/debug            — full state snapshot
  * POST /campaigns/{id}/dispatch-now     — synchronous dispatcher kick
  * _classify_campaign_lifecycle helper   — granular merchant verb

We focus on the contract the UI relies on (lifecycle keys, hint text,
``ok``/``skipped`` shape on dispatch-now) rather than re-testing the
underlying dispatch_campaign which is exercised by
test_campaign_send_log.py + test_campaign_dispatcher.py.

The tests deliberately avoid TestClient because the rest of this repo
historically hits ``Session`` thread issues with SQLite-in-memory under
TestClient. We call the router handlers directly with ``asyncio.run``
(same pattern used in tests/test_manual_segments.py).
"""
from __future__ import annotations

import asyncio
import os
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
    Base, Campaign, CampaignSendLog, Customer, Tenant, WhatsAppTemplate,
)
from routers import campaigns as campaigns_router  # noqa: E402


def _make_db():
    """Fresh SQLite-backed engine that mirrors prod model definitions
    (JSONB is downgraded to JSON for SQLite)."""
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
    return Session(), engine


def _seed(db, *, status="active", audience_count=0, sent_count=0):
    t = Tenant(name="T", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    tpl = WhatsAppTemplate(
        tenant_id=t.id, name="tpl_promo", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )
    db.add(tpl); db.commit(); db.refresh(tpl)
    c = Campaign(
        tenant_id=t.id, name="C", campaign_type="broadcast",
        template_id=str(tpl.id), template_name=tpl.name,
        template_language="ar", template_category="MARKETING",
        audience_type="all", status=status,
        audience_count=audience_count, sent_count=sent_count,
        created_at=datetime.now(timezone.utc),
        launched_at=datetime.now(timezone.utc) if status == "active" else None,
    )
    db.add(c); db.commit(); db.refresh(c)
    return t, tpl, c


class _FakeReq:
    headers: dict = {}
    cookies: dict = {}
    state = type("S", (), {})()


def _call_debug(db, tenant_id, campaign_id):
    """Call the debug handler in-process with a stubbed tenant
    resolver so we don't depend on JWT/session middleware."""
    original = campaigns_router.resolve_tenant_id
    campaigns_router.resolve_tenant_id = (
        lambda request, db=None: tenant_id  # type: ignore
    )
    try:
        return asyncio.run(
            campaigns_router.debug_campaign(
                campaign_id=campaign_id, request=_FakeReq(), db=db,
            )
        )
    finally:
        campaigns_router.resolve_tenant_id = original


def _call_dispatch_now(db, tenant_id, campaign_id):
    original = campaigns_router.resolve_tenant_id
    campaigns_router.resolve_tenant_id = (
        lambda request, db=None: tenant_id  # type: ignore
    )
    try:
        return asyncio.run(
            campaigns_router.dispatch_campaign_now(
                campaign_id=campaign_id, request=_FakeReq(), db=db,
            )
        )
    finally:
        campaigns_router.resolve_tenant_id = original


# ── 1. _classify_campaign_lifecycle ─────────────────────────────────


class TestLifecycleClassifier:
    """The merchant-friendly verb that replaces the raw status pill.

    The classifier is pure (no DB), so we exercise every status × count
    combination the UI is allowed to encounter, and pin the keys the
    frontend LIFECYCLE_META map relies on.
    """

    def _verb(self, status, **counts):
        camp = Campaign(status=status, sent_count=counts.pop("sent", 0))
        return campaigns_router._classify_campaign_lifecycle(camp, counts)

    def test_active_with_zero_counts_is_pending_dispatch(self):
        # The exact case we're trying to surface: campaign is
        # ``active`` (immediate launch) but the asyncio task hasn't
        # snapshotted any recipient yet (or died silently).
        assert self._verb("active") == "pending_dispatch"

    def test_active_with_queued_rows_is_sending(self):
        assert self._verb("active", queued=4) == "sending"

    def test_completed_with_only_sends_is_sent(self):
        camp = Campaign(status="completed", sent_count=4)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {"sent": 4},
        ) == "sent"

    def test_completed_with_only_failures_is_failed_all(self):
        camp = Campaign(status="completed", sent_count=0)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {"failed": 4},
        ) == "failed_all"

    def test_completed_with_mixed_is_partial(self):
        camp = Campaign(status="completed", sent_count=2)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {"sent": 2, "failed": 1},
        ) == "partial"

    def test_completed_with_no_recipients_is_completed_empty(self):
        camp = Campaign(status="completed", sent_count=0)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {},
        ) == "completed_empty"

    def test_scheduled_is_waiting_scheduler(self):
        assert self._verb("scheduled") == "waiting_scheduler"

    def test_failed_status_passes_through(self):
        assert self._verb("failed") == "failed"

    def test_draft_passes_through(self):
        assert self._verb("draft") == "draft"


# ── 2. /campaigns/{id}/debug ────────────────────────────────────────


class TestDebugEndpoint:
    def test_returns_full_snapshot_for_active_campaign(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        result = _call_debug(db, t.id, c.id)

        # Top-level shape — the UI relies on these keys.
        assert set(result.keys()) >= {
            "campaign", "recipients", "sample_failed", "sample_sent",
            "template", "wa_connection", "scheduler", "hints", "errors",
        }
        assert result["campaign"]["id"] == c.id
        assert result["campaign"]["lifecycle"] == "pending_dispatch"
        assert result["campaign"]["audience_count"] == 4

        # No recipients yet → all counters zero, no failed/sent samples.
        assert result["recipients"]["total"] == 0
        assert result["sample_failed"] == []
        assert result["sample_sent"] == []

        # Template is approved.
        assert result["template"]["approved"] is True

        # Scheduler section always present.
        assert result["scheduler"]["campaign_dispatcher_enabled"] in (True, False)
        # Hints include the "asyncio task died" diagnostic because
        # audience_count > 0 but no recipients were ever snapshotted.
        assert any("asyncio" in h or "Railway" in h for h in result["hints"])

    def test_surfaces_kill_switch_hint_for_non_immediate(self):
        # Set the kill switch and ensure the hint fires for a
        # scheduled campaign (the kill switch only affects scheduled
        # / delayed campaigns — immediate ones go through
        # asyncio.create_task and bypass the scheduler).
        os.environ["NAHLA_DISABLE_SCHEDULERS"] = "1"
        try:
            db, _ = _make_db()
            t, tpl, c = _seed(db, status="scheduled", audience_count=0)
            c.schedule_type = "scheduled"
            db.commit()
            result = _call_debug(db, t.id, c.id)
        finally:
            os.environ.pop("NAHLA_DISABLE_SCHEDULERS", None)
        assert result["scheduler"]["kill_switch_set"] is True
        assert any("NAHLA_DISABLE_SCHEDULERS" in h for h in result["hints"])

    def test_unapproved_template_surfaces_hint(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        tpl.status = "PENDING"
        db.commit()
        result = _call_debug(db, t.id, c.id)
        assert result["template"]["approved"] is False
        assert any("APPROVED" in h for h in result["hints"])

    def test_missing_template_does_not_crash(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        # Reassign a non-existent template id and verify the
        # endpoint still returns a structured response.
        c.template_id = "999999"
        db.commit()
        result = _call_debug(db, t.id, c.id)
        # Template lookup returns None — but the response must still
        # be a fully-formed dict.
        assert result["template"] is None
        # And the helpful hint surfaces.
        assert any("APPROVED" in h for h in result["hints"])

    def test_recipient_counters_aggregated_correctly(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        for status, phone in [
            ("sent", "+966500000001"),
            ("sent", "+966500000002"),
            ("failed", "+966500000003"),
            ("queued", "+966500000004"),
        ]:
            db.add(CampaignSendLog(
                tenant_id=t.id, campaign_id=c.id,
                customer_phone_e164=phone, status=status,
                error_message="boom" if status == "failed" else None,
                sent_at=datetime.now(timezone.utc) if status == "sent" else None,
            ))
        db.commit()

        result = _call_debug(db, t.id, c.id)
        r = result["recipients"]
        assert r["total"] == 4
        assert r["sent"] == 2
        assert r["failed"] == 1
        assert r["queued"] == 1
        # Sample arrays surface the most-recent rows.
        assert len(result["sample_sent"]) == 2
        assert len(result["sample_failed"]) == 1
        assert result["sample_failed"][0]["error_message"] == "boom"
        # Phone is masked.
        assert result["sample_sent"][0]["phone"].endswith("0001") or \
               result["sample_sent"][0]["phone"].endswith("0002")
        assert "•" in result["sample_sent"][0]["phone"]


# ── 3. /campaigns/{id}/dispatch-now ─────────────────────────────────


class TestDispatchNow:
    """The dispatch-now endpoint is fire-and-forget: it must return
    immediately (the dispatcher has 1.5s+ pauses between sends and
    would blow past our 25s frontend HTTP timeout for any real
    audience). These tests pin the contract."""

    def test_skipped_for_completed_campaign(self):
        # Completed campaign with sent_count>0 must not be re-kicked
        # — we return a clear "skipped:completed" so the merchant
        # doesn't think the manual button silently no-op'd.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4, sent_count=4)
        result = _call_dispatch_now(db, t.id, c.id)
        assert result["skipped"] is True
        assert result["reason"] == "completed"
        assert result["ok"] is True

    def test_returns_immediately_with_kicked_flag(self, monkeypatch):
        # The endpoint must spawn the dispatcher in the background
        # via asyncio.create_task and return ``kicked: true`` BEFORE
        # the dispatcher has done any work. We assert this by
        # patching create_task to record the call.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=2)

        spawned: list = []
        import asyncio as _aio
        original_create_task = _aio.create_task

        def _capture(coro, *args, **kwargs):
            spawned.append(coro)
            # Schedule the coroutine on the running loop so it does
            # not raise "coroutine was never awaited" warnings; the
            # test event loop closes before the dispatcher runs.
            return original_create_task(coro, *args, **kwargs)

        monkeypatch.setattr(_aio, "create_task", _capture)

        # Stub the actual dispatch so the background coroutine is
        # cheap (we're testing the endpoint, not the dispatcher).
        async def _fake(_db, cid):
            return {"campaign_id": cid, "status": "completed", "sent": 2,
                    "failed": 0, "queued": 0, "errors": []}
        import services.campaign_dispatcher as cd
        monkeypatch.setattr(cd, "dispatch_campaign", _fake)

        result = _call_dispatch_now(db, t.id, c.id)
        assert result["ok"] is True
        assert result["kicked"] is True
        # Status pre-flipped to 'active' so the next list refresh
        # immediately shows "جاري الإرسال".
        assert result["status"] == "active"
        # A background coroutine was actually scheduled.
        assert len(spawned) == 1

    def test_pre_flips_status_to_active(self, monkeypatch):
        # A campaign that was stuck in 'draft' should flip to
        # 'active' synchronously so the next /campaigns refresh
        # surfaces the new lifecycle pill ("جاري الإرسال") within
        # the polling window — without waiting for the dispatcher
        # to do it ≈3s later.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="draft", audience_count=2)
        c.launched_at = None
        db.commit()

        async def _noop(_db, cid):
            return {"campaign_id": cid, "status": "completed", "sent": 0,
                    "failed": 0, "queued": 0, "errors": []}
        import services.campaign_dispatcher as cd
        monkeypatch.setattr(cd, "dispatch_campaign", _noop)

        result = _call_dispatch_now(db, t.id, c.id)
        assert result["ok"] is True
        db.refresh(c)
        assert c.status == "active"
        assert c.launched_at is not None


# ── 4. _campaign_to_dict carries lifecycle for the listing endpoint ──


class TestCampaignToDictLifecycle:
    def test_active_with_zero_recipients_renders_pending_dispatch(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        out = campaigns_router._campaign_to_dict(c)
        assert out["lifecycle"] == "pending_dispatch"
        assert out["status"] == "active"
        assert out["last_error"] is None

    def test_failed_carries_last_error_first_dispatch_error(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="failed", audience_count=4)
        c.template_variables = {"_dispatch_errors": "lookup timeout|secondary issue"}
        db.commit()
        out = campaigns_router._campaign_to_dict(c)
        assert out["lifecycle"] == "failed"
        assert out["last_error"] == "lookup timeout"
        assert out["dispatch_errors"] == ["lookup timeout", "secondary issue"]
