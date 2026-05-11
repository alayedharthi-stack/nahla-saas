"""
tests/test_campaign_stuck_immediate_rescue.py
─────────────────────────────────────────────
Locks the F12 fix for the "campaign stuck at بانتظار بدء الإرسال"
production bug. Two layers:

A) Scheduler rescue path
    ──────────────────────
    The periodic dispatcher loop in ``core.scheduler`` historically
    only matched ``status IN ('scheduled','draft') AND schedule_type
    IN ('scheduled','delayed')``. Immediate campaigns
    (``schedule_type='immediate'``, ``status='active'``) rely
    entirely on an in-process ``asyncio.create_task`` fired from
    ``POST /campaigns``. If that task is dropped — uvicorn restart
    between ``db.commit()`` and ``create_task``, OOM, an uncaught
    exception that poisons the session so the failure-flip cannot
    write ``status='failed'`` — the campaign stays at
    ``status='active'`` with ZERO ``campaign_send_logs`` rows
    forever.

    F12 adds ``_find_stuck_immediate_campaigns`` which the loop
    calls every cycle to rescue such campaigns. Constraints:

      * status == 'active'
      * schedule_type == 'immediate'
      * launched_at <= now - threshold_seconds
      * NO campaign_send_logs row exists yet (the critical
        anti-double-send safety)

B) Session-recovery rollback in ``_dispatch_campaign_async``
    ──────────────────────────────────────────────────────────
    When ``dispatch_campaign`` raises mid-transaction, the session
    is left in a ``PendingRollback`` state. The except handler's
    next ``db.query(Campaign)`` then raises and gets silently
    swallowed — the campaign never gets ``status='failed'``. F12
    adds an explicit ``db.rollback()`` before the recovery query.

Both layers tested against an in-memory SQLite DB seeded with
real ORM rows (same pattern as test_campaign_send_log.py).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

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
    Campaign,
    CampaignSendLog,
    Tenant,
    WhatsAppTemplate,
)


# ── SQLite in-memory DB (mirrors test_campaign_send_log.py shim) ────────


def _make_db():
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
    return Session(), engine


def _seed_tenant(db, name: str = "T") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_template(db, tenant_id: int, name: str = "tpl") -> WhatsAppTemplate:
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=name,
        language="ar",
        category="MARKETING",
        status="APPROVED",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


def _seed_campaign(
    db,
    tenant_id: int,
    template: WhatsAppTemplate,
    *,
    status: str = "active",
    schedule_type: str = "immediate",
    launched_at: Optional[datetime] = None,
    name: str = "C",
) -> Campaign:
    """Build a Campaign row matching the exact shape the wizard /
    ``POST /campaigns`` route produces."""
    c = Campaign(
        tenant_id=tenant_id,
        name=name,
        campaign_type="broadcast",
        template_id=str(template.id),
        template_name=template.name,
        template_language="ar",
        template_category="MARKETING",
        audience_type="all",
        status=status,
        schedule_type=schedule_type,
        launched_at=launched_at,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_send_log_row(db, campaign: Campaign, *, phone: str = "+966500000001") -> CampaignSendLog:
    row = CampaignSendLog(
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        customer_phone_e164=phone,
        status="queued",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ──────────────────────────────────────────────────────────────────────
# A) _find_stuck_immediate_campaigns — filter correctness
# ──────────────────────────────────────────────────────────────────────


class TestFindStuckImmediate:
    """The rescue probe must be CONSERVATIVE: it must rescue
    genuinely-stuck campaigns but NEVER re-dispatch a campaign
    that's already in flight (which would double-send to
    customers). The four-condition filter encodes that
    invariant."""

    def test_rescues_active_immediate_with_no_logs_past_threshold(self):
        from core.scheduler import _find_stuck_immediate_campaigns

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        now = datetime.now(timezone.utc)
        # Launched 2 minutes ago, no send-log rows — classic stuck.
        camp = _seed_campaign(
            db, t.id, tpl,
            status="active", schedule_type="immediate",
            launched_at=now - timedelta(seconds=120),
        )
        stuck = _find_stuck_immediate_campaigns(db, now=now, threshold_seconds=60)
        assert [c.id for c in stuck] == [camp.id]

    def test_skips_campaign_within_grace_window(self):
        """A campaign launched 10s ago might still be in flight —
        the async task may not have called .commit() yet.  We must
        NOT rescue it; threshold protects us."""
        from core.scheduler import _find_stuck_immediate_campaigns

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        now = datetime.now(timezone.utc)
        _seed_campaign(
            db, t.id, tpl,
            status="active", schedule_type="immediate",
            launched_at=now - timedelta(seconds=10),  # within grace
        )
        stuck = _find_stuck_immediate_campaigns(db, now=now, threshold_seconds=60)
        assert stuck == []

    def test_skips_campaign_with_existing_send_log_rows(self):
        """The CRITICAL anti-double-send safety: a campaign with
        even a single ``campaign_send_logs`` row is in flight or
        completed. Rescuing it would re-snapshot — though the
        UNIQUE constraint blocks duplicates, the second
        ``dispatch_campaign`` would still iterate the same
        ``queued`` rows and could double-send to anyone whose
        ``sending`` row hasn't flipped to ``sent`` yet."""
        from core.scheduler import _find_stuck_immediate_campaigns

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        now = datetime.now(timezone.utc)
        camp = _seed_campaign(
            db, t.id, tpl,
            status="active", schedule_type="immediate",
            launched_at=now - timedelta(seconds=300),  # 5 min ago, way past grace
        )
        _seed_send_log_row(db, camp)  # ← at least one row exists
        stuck = _find_stuck_immediate_campaigns(db, now=now, threshold_seconds=60)
        assert stuck == [], (
            "A campaign with an existing send-log row MUST NOT be "
            "considered stuck — re-dispatching it would risk double-send"
        )

    def test_skips_non_immediate_campaigns(self):
        """Scheduled / delayed campaigns are handled by the
        original loop. The rescue probe must not duplicate that
        work — would be benign but wasteful."""
        from core.scheduler import _find_stuck_immediate_campaigns

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        now = datetime.now(timezone.utc)
        for sched_type in ("scheduled", "delayed"):
            _seed_campaign(
                db, t.id, tpl,
                status="active", schedule_type=sched_type,
                launched_at=now - timedelta(seconds=300),
            )
        stuck = _find_stuck_immediate_campaigns(db, now=now, threshold_seconds=60)
        assert stuck == []

    def test_skips_terminal_statuses(self):
        """``completed`` / ``failed`` / ``paused`` campaigns must
        never be rescued — they reached a terminal state on
        purpose."""
        from core.scheduler import _find_stuck_immediate_campaigns

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        now = datetime.now(timezone.utc)
        for status in ("completed", "failed", "paused", "draft"):
            _seed_campaign(
                db, t.id, tpl,
                status=status, schedule_type="immediate",
                launched_at=now - timedelta(seconds=300),
            )
        stuck = _find_stuck_immediate_campaigns(db, now=now, threshold_seconds=60)
        assert stuck == []

    def test_skips_active_immediate_without_launched_at(self):
        """If ``launched_at`` was never set (corrupted row,
        manual DB edit), the row's age is unknowable — refuse to
        rescue rather than guess."""
        from core.scheduler import _find_stuck_immediate_campaigns

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        now = datetime.now(timezone.utc)
        _seed_campaign(
            db, t.id, tpl,
            status="active", schedule_type="immediate",
            launched_at=None,
        )
        stuck = _find_stuck_immediate_campaigns(db, now=now, threshold_seconds=60)
        assert stuck == []

    def test_rescues_only_eligible_when_multiple_campaigns_present(self):
        """Mixed bag — only the genuinely stuck one comes back."""
        from core.scheduler import _find_stuck_immediate_campaigns

        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        now = datetime.now(timezone.utc)

        stuck_target = _seed_campaign(
            db, t.id, tpl, name="STUCK",
            status="active", schedule_type="immediate",
            launched_at=now - timedelta(seconds=300),
        )
        # Sibling: in flight (has rows) — must NOT be rescued.
        in_flight = _seed_campaign(
            db, t.id, tpl, name="IN_FLIGHT",
            status="active", schedule_type="immediate",
            launched_at=now - timedelta(seconds=300),
        )
        _seed_send_log_row(db, in_flight)
        # Sibling: completed — must NOT be rescued.
        _seed_campaign(
            db, t.id, tpl, name="DONE",
            status="completed", schedule_type="immediate",
            launched_at=now - timedelta(seconds=300),
        )
        # Sibling: within grace window — must NOT be rescued.
        _seed_campaign(
            db, t.id, tpl, name="FRESH",
            status="active", schedule_type="immediate",
            launched_at=now - timedelta(seconds=5),
        )

        stuck = _find_stuck_immediate_campaigns(db, now=now, threshold_seconds=60)
        assert [c.id for c in stuck] == [stuck_target.id]


# ──────────────────────────────────────────────────────────────────────
# B) Session-recovery rollback in _dispatch_campaign_async
# ──────────────────────────────────────────────────────────────────────


class TestDispatchAsyncSessionRecovery:
    """If ``dispatch_campaign`` raises with a poisoned session,
    the except handler in ``_dispatch_campaign_async`` MUST
    rollback before querying so the failure-flip can run.
    Pre-F12 the flip silently failed and the campaign stayed
    ``active`` forever."""

    def _dispatch_async(self):
        # Imported here so the SessionLocal monkeypatch below works.
        from routers.campaigns import _dispatch_campaign_async
        return _dispatch_campaign_async

    def _make_shared_engine_factory(self):
        """Build an engine + a Session factory that hands out a NEW
        Session each time ``SessionLocal()`` is called, all bound to
        the same engine.  The production code's ``finally:
        db.close()`` only closes the session it owns; our assertion
        session is independent and stays alive."""
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
        return engine, Session

    def test_status_flipped_to_failed_when_dispatcher_raises_with_clean_session(
        self, monkeypatch
    ):
        """Baseline: even with a CLEAN session (no rollback
        needed) the except path must still flip status."""
        import asyncio
        from routers import campaigns as campaigns_mod

        engine, Session = self._make_shared_engine_factory()
        seed_db = Session()
        try:
            t = _seed_tenant(seed_db)
            tpl = _seed_template(seed_db, t.id)
            now = datetime.now(timezone.utc)
            camp = _seed_campaign(
                seed_db, t.id, tpl,
                status="active", schedule_type="immediate",
                launched_at=now,
            )
            campaign_id = camp.id
        finally:
            seed_db.close()

        # Each SessionLocal() call gives the production code its
        # own session bound to the same engine — closing it does
        # NOT poison our assertion session.
        monkeypatch.setattr(
            "core.database.SessionLocal",
            lambda: Session(),
        )

        async def _raising_dispatch(*_a, **_k):
            raise RuntimeError("simulated dispatch error")

        monkeypatch.setattr(
            "services.campaign_dispatcher.dispatch_campaign",
            _raising_dispatch,
        )

        asyncio.run(campaigns_mod._dispatch_campaign_async(campaign_id))

        # Fresh session for assertions.
        assert_db = Session()
        try:
            camp_after = (
                assert_db.query(Campaign)
                .filter(Campaign.id == campaign_id)
                .first()
            )
            assert camp_after is not None
            assert camp_after.status == "failed", (
                f"campaign status was '{camp_after.status}', should be "
                f"'failed' after dispatcher raised"
            )
            assert "_dispatch_errors" in (camp_after.template_variables or {})
            assert "simulated dispatch error" in (
                camp_after.template_variables.get("_dispatch_errors") or ""
            )
        finally:
            assert_db.close()

    def test_status_flipped_to_failed_even_with_poisoned_session(
        self, monkeypatch
    ):
        """Regression test for the actual production bug:
        ``dispatch_campaign`` raises AND the session is in a
        PendingRollback state. Pre-F12 the recovery
        ``db.query(Campaign)`` would itself raise and be
        silently swallowed → campaign frozen at 'active'.
        Post-F12 the explicit ``db.rollback()`` clears the
        flag and the flip succeeds."""
        import asyncio
        from routers import campaigns as campaigns_mod

        engine, Session = self._make_shared_engine_factory()
        seed_db = Session()
        try:
            t = _seed_tenant(seed_db)
            tpl = _seed_template(seed_db, t.id)
            camp = _seed_campaign(
                seed_db, t.id, tpl,
                status="active", schedule_type="immediate",
                launched_at=datetime.now(timezone.utc),
            )
            campaign_id = camp.id
            tenant_id = t.id
        finally:
            seed_db.close()

        # Capture the session handed to the production code so we
        # can count rollback() calls on it.
        captured = {"db": None, "rollback_calls": 0}

        def _instrumented_session_local():
            s = Session()
            real_rollback = s.rollback

            def _counting_rollback(*a, **k):
                captured["rollback_calls"] += 1
                return real_rollback(*a, **k)

            s.rollback = _counting_rollback
            captured["db"] = s
            return s

        monkeypatch.setattr(
            "core.database.SessionLocal",
            _instrumented_session_local,
        )

        async def _raising_then_poison(db, _cid, *_a, **_k):
            """Simulate the production failure mode: an exception
            is raised AFTER a flush poisoned the session.  We
            drive the session into a 'needs rollback' state by
            attempting an invalid insert, then raise."""
            try:
                bad = Campaign(
                    tenant_id=tenant_id,
                    name=None,  # NOT NULL violated
                    campaign_type="broadcast",
                    status="active",
                )
                db.add(bad)
                db.flush()
            except Exception:
                # Session is now in PendingRollback — any query
                # before a rollback will raise.
                pass
            raise RuntimeError("dispatch failed mid-transaction")

        monkeypatch.setattr(
            "services.campaign_dispatcher.dispatch_campaign",
            _raising_then_poison,
        )

        asyncio.run(campaigns_mod._dispatch_campaign_async(campaign_id))

        # Hard assertion: F12's rollback ran.
        assert captured["rollback_calls"] >= 1, (
            "F12 rollback was not invoked — the failure-flip will "
            "silently fail on real PendingRollback sessions"
        )

        # Fresh session for the status assertion.
        assert_db = Session()
        try:
            camp_after = (
                assert_db.query(Campaign)
                .filter(Campaign.id == campaign_id)
                .first()
            )
            assert camp_after is not None
            assert camp_after.status == "failed", (
                f"campaign status was '{camp_after.status}', should be "
                f"'failed' after poisoned-session recovery. Pre-F12 this "
                f"would have been 'active' (the exact production symptom)."
            )
        finally:
            assert_db.close()

    def test_already_completed_campaign_not_clobbered(self, monkeypatch):
        """If the dispatcher raised after marking the campaign
        completed/failed (rare but possible from a downstream
        notification handler), the except path MUST NOT
        overwrite that terminal status."""
        import asyncio
        from routers import campaigns as campaigns_mod

        engine, Session = self._make_shared_engine_factory()
        seed_db = Session()
        try:
            t = _seed_tenant(seed_db)
            tpl = _seed_template(seed_db, t.id)
            camp = _seed_campaign(
                seed_db, t.id, tpl,
                status="completed", schedule_type="immediate",
                launched_at=datetime.now(timezone.utc),
            )
            campaign_id = camp.id
        finally:
            seed_db.close()

        monkeypatch.setattr(
            "core.database.SessionLocal",
            lambda: Session(),
        )

        async def _raising(*_a, **_k):
            raise RuntimeError("post-completion noise")

        monkeypatch.setattr(
            "services.campaign_dispatcher.dispatch_campaign",
            _raising,
        )

        asyncio.run(campaigns_mod._dispatch_campaign_async(campaign_id))

        assert_db = Session()
        try:
            camp_after = (
                assert_db.query(Campaign)
                .filter(Campaign.id == campaign_id)
                .first()
            )
            assert camp_after.status == "completed"  # preserved
        finally:
            assert_db.close()
