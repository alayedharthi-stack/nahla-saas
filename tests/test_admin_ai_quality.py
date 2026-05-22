"""
tests/test_admin_ai_quality.py
──────────────────────────────
Locks the contract for the ``/admin/ai-quality`` admin surface
(May 2026 #12):

* ``GET   /admin/ai-quality/events``         — paginated browse.
* ``GET   /admin/ai-quality/summary``        — counts + top
  conversations + latest 50 ring.
* ``PATCH /admin/ai-quality/events/{id}``    — operator triage.

We invoke the handler functions directly with an in-memory SQLite
session — no TestClient, no auth roundtrip, mirroring the pattern in
``tests/test_admin_debug_inbound_trace.py``. The ``require_admin``
dependency is bypassed by passing a fake claims dict explicitly.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── In-memory SQLite session with just AiQualityEvent ─────────────────


def _fresh_session():
    """Build a session against the isolated ``ai_quality_events``
    table. Foreign keys point at tables we don't materialize, but
    SQLite ignores FK enforcement by default so the inserts succeed.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base, AiQualityEvent  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    AiQualityEvent.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


# ── Seed helpers ──────────────────────────────────────────────────────


def _seed_event(
    session,
    *,
    tenant_id: int = 1,
    mismatch_type: str = "question_to_social",
    resolved_status: str = "open",
    conversation_id=None,
    customer_phone: str = "+966537970430",
    inbound: str = "هو ممتاز للجهاز الهضمي؟",
    reply: str = "ما تقصر أبداً وياك",
    detected_intent: str = "social",
    social_category: str = "general_courtesy",
    action_taken: str = "ACTION_SOCIAL_REPLY",
    chosen_path: str = "social_template",
    fallback_used: bool = False,
    order_status: str = "discovery",
    awaiting_payment_receipt: bool = False,
    model_used: str = "claude-opus-4-6",
    turn: int = 1,
    created_at: datetime | None = None,
):
    from core.ai_quality_events import persist_alignment_mismatch
    from database.models import AiQualityEvent

    new_id = persist_alignment_mismatch(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        inbound_text=inbound,
        reply_text=reply,
        mismatch_type=mismatch_type,
        mismatch_reason="seed",
        detected_intent=detected_intent,
        social_category=social_category,
        action_taken=action_taken,
        chosen_path=chosen_path,
        fallback_used=fallback_used,
        order_status=order_status,
        awaiting_payment_receipt=awaiting_payment_receipt,
        model_used=model_used,
        turn=turn,
    )
    row = session.query(AiQualityEvent).filter_by(id=new_id).one()
    if created_at is not None:
        row.created_at = created_at
    if resolved_status != "open":
        row.resolved_status = resolved_status
    session.flush()
    session.commit()
    return row


# ── 1. ``GET /admin/ai-quality/events`` filters & pagination ──────────


class TestListEvents:
    def test_returns_newest_first(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            r1 = _seed_event(session, mismatch_type="question_to_social",
                             created_at=now - timedelta(hours=3))
            r2 = _seed_event(session, mismatch_type="closing_to_reopen",
                             created_at=now - timedelta(hours=2))
            r3 = _seed_event(session, mismatch_type="religious_to_oos",
                             created_at=now - timedelta(hours=1))

            resp = list_ai_quality_events(
                tenant_id=None, category=None, mismatch_type=None, resolved_status=None,
                since=None, until=None, limit=50, offset=0,
                db=session, _admin={"role": "admin"},
            )
            assert resp.total == 3
            assert [it.id for it in resp.items] == [r3.id, r2.id, r1.id]
        finally:
            session.close()

    def test_filter_by_mismatch_type(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            _seed_event(session, mismatch_type="question_to_social",
                        created_at=now - timedelta(hours=1))
            _seed_event(session, mismatch_type="closing_to_reopen",
                        created_at=now - timedelta(hours=1))

            resp = list_ai_quality_events(
                tenant_id=None, category=None,
                mismatch_type="closing_to_reopen",
                resolved_status=None, since=None, until=None,
                limit=50, offset=0,
                db=session, _admin={"role": "admin"},
            )
            assert resp.total == 1
            assert resp.items[0].mismatch_type == "closing_to_reopen"
        finally:
            session.close()

    def test_filter_by_tenant(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            _seed_event(session, tenant_id=1)
            _seed_event(session, tenant_id=1)
            _seed_event(session, tenant_id=2)

            resp = list_ai_quality_events(
                tenant_id=2, category=None, mismatch_type=None, resolved_status=None,
                since=None, until=None, limit=50, offset=0,
                db=session, _admin={"role": "admin"},
            )
            assert resp.total == 1
            assert resp.items[0].tenant_id == 2
        finally:
            session.close()

    def test_filter_by_resolved_status(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            _seed_event(session, resolved_status="open")
            _seed_event(session, resolved_status="reviewed")
            _seed_event(session, resolved_status="ignored")

            resp = list_ai_quality_events(
                tenant_id=None, category=None, mismatch_type=None,
                resolved_status="reviewed",
                since=None, until=None, limit=50, offset=0,
                db=session, _admin={"role": "admin"},
            )
            assert resp.total == 1
            assert resp.items[0].resolved_status == "reviewed"
        finally:
            session.close()

    def test_invalid_resolved_status_returns_400(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            with pytest.raises(HTTPException) as exc:
                list_ai_quality_events(
                    tenant_id=None, category=None, mismatch_type=None,
                    resolved_status="bogus",
                    since=None, until=None, limit=50, offset=0,
                    db=session, _admin={"role": "admin"},
                )
            assert exc.value.status_code == 400
        finally:
            session.close()

    def test_invalid_since_returns_400(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            with pytest.raises(HTTPException) as exc:
                list_ai_quality_events(
                    tenant_id=None, category=None, mismatch_type=None, resolved_status=None,
                    since="not-a-date", until=None, limit=50, offset=0,
                    db=session, _admin={"role": "admin"},
                )
            assert exc.value.status_code == 400
        finally:
            session.close()

    def test_since_and_until_filter_window(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            _seed_event(session, created_at=now - timedelta(days=7))   # outside
            inside = _seed_event(session, created_at=now - timedelta(hours=2))
            _seed_event(session, created_at=now - timedelta(days=30))  # outside

            since_iso = (now - timedelta(days=1)).isoformat()
            until_iso = now.isoformat()
            resp = list_ai_quality_events(
                tenant_id=None, category=None, mismatch_type=None, resolved_status=None,
                since=since_iso, until=until_iso, limit=50, offset=0,
                db=session, _admin={"role": "admin"},
            )
            assert resp.total == 1
            assert resp.items[0].id == inside.id
        finally:
            session.close()

    def test_pagination_offset_limit(self):
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            ids = []
            for i in range(7):
                row = _seed_event(session, created_at=now - timedelta(minutes=i))
                ids.append(row.id)

            page1 = list_ai_quality_events(
                tenant_id=None, category=None, mismatch_type=None, resolved_status=None,
                since=None, until=None, limit=3, offset=0,
                db=session, _admin={"role": "admin"},
            )
            page2 = list_ai_quality_events(
                tenant_id=None, category=None, mismatch_type=None, resolved_status=None,
                since=None, until=None, limit=3, offset=3,
                db=session, _admin={"role": "admin"},
            )
            assert page1.total == page2.total == 7
            assert len(page1.items) == 3 and len(page2.items) == 3
            assert {it.id for it in page1.items}.isdisjoint(
                {it.id for it in page2.items}
            )
        finally:
            session.close()

    def test_response_carries_masked_phone_only(self):
        """Privacy regression — full digits must never appear in the
        list response body."""
        from routers.admin_ai_quality import list_ai_quality_events

        session = _fresh_session()
        try:
            _seed_event(session, customer_phone="+966537970430")
            resp = list_ai_quality_events(
                tenant_id=None, category=None, mismatch_type=None, resolved_status=None,
                since=None, until=None, limit=50, offset=0,
                db=session, _admin={"role": "admin"},
            )
            it = resp.items[0]
            assert "537970" not in (it.customer_phone_masked or "")
            assert it.customer_phone_masked.startswith("+966")
            assert it.customer_phone_masked.endswith("430")
        finally:
            session.close()


# ── 2. ``GET /admin/ai-quality/summary`` ───────────────────────────────


class TestSummary:
    def test_counts_by_type_and_total(self):
        from routers.admin_ai_quality import ai_quality_summary

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            for _ in range(3):
                _seed_event(session, mismatch_type="question_to_social",
                            created_at=now - timedelta(minutes=10))
            for _ in range(5):
                _seed_event(session, mismatch_type="closing_to_reopen",
                            created_at=now - timedelta(minutes=20))
            _seed_event(session, mismatch_type="religious_to_oos",
                        created_at=now - timedelta(minutes=30))
            # outside window:
            _seed_event(session, mismatch_type="question_to_social",
                        created_at=now - timedelta(days=10))

            resp = ai_quality_summary(
                tenant_id=None, category=None, window_hours=24,
                db=session, _admin={"role": "admin"},
            )
            counts = {c.mismatch_type: c.count for c in resp.counts_by_type}
            assert counts == {
                "question_to_social": 3,
                "closing_to_reopen":  5,
                "religious_to_oos":   1,
            }
            assert resp.total_in_window == 9
            # Counts come back sorted desc by count → first is the
            # noisiest type.
            assert resp.counts_by_type[0].mismatch_type == "closing_to_reopen"
        finally:
            session.close()

    def test_top_conversations_ordered_by_count(self):
        from routers.admin_ai_quality import ai_quality_summary

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            for _ in range(4):
                _seed_event(session, conversation_id=101,
                            created_at=now - timedelta(minutes=5))
            for _ in range(2):
                _seed_event(session, conversation_id=202,
                            created_at=now - timedelta(minutes=5))
            _seed_event(session, conversation_id=303,
                        created_at=now - timedelta(minutes=5))
            # No conversation_id rows — must be excluded from "top".
            _seed_event(session, conversation_id=None,
                        created_at=now - timedelta(minutes=5))

            resp = ai_quality_summary(
                tenant_id=None, category=None, window_hours=24,
                db=session, _admin={"role": "admin"},
            )
            ids = [tc.conversation_id for tc in resp.top_conversations]
            assert ids == [101, 202, 303]
            # First entry is the noisiest:
            assert resp.top_conversations[0].count == 4
        finally:
            session.close()

    def test_latest_events_capped_at_50(self):
        from routers.admin_ai_quality import ai_quality_summary

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            for i in range(120):
                _seed_event(session, created_at=now - timedelta(minutes=i))
            resp = ai_quality_summary(
                tenant_id=None, category=None, window_hours=24,
                db=session, _admin={"role": "admin"},
            )
            assert len(resp.latest_events) <= 50
            # Newest first.
            ts = [it.created_at for it in resp.latest_events]
            assert ts == sorted(ts, reverse=True)
        finally:
            session.close()

    def test_total_open_independent_of_window(self):
        from routers.admin_ai_quality import ai_quality_summary

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            # In-window open
            _seed_event(session, resolved_status="open",
                        created_at=now - timedelta(minutes=5))
            # Out-of-window open (must still count toward total_open)
            _seed_event(session, resolved_status="open",
                        created_at=now - timedelta(days=5))
            # Reviewed — must NOT count toward total_open
            _seed_event(session, resolved_status="reviewed",
                        created_at=now - timedelta(minutes=10))

            resp = ai_quality_summary(
                tenant_id=None, category=None, window_hours=1,
                db=session, _admin={"role": "admin"},
            )
            assert resp.total_open == 2
            assert resp.total_in_window == 2  # open in-window + reviewed
        finally:
            session.close()

    def test_tenant_scoping(self):
        from routers.admin_ai_quality import ai_quality_summary

        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            _seed_event(session, tenant_id=1,
                        created_at=now - timedelta(minutes=5))
            _seed_event(session, tenant_id=1,
                        created_at=now - timedelta(minutes=5))
            _seed_event(session, tenant_id=2,
                        created_at=now - timedelta(minutes=5))

            resp = ai_quality_summary(
                tenant_id=2, category=None, window_hours=24,
                db=session, _admin={"role": "admin"},
            )
            assert resp.total_in_window == 1
        finally:
            session.close()


# ── 3. ``PATCH /admin/ai-quality/events/{id}`` ─────────────────────────


class TestResolveEvent:
    def test_mark_reviewed_stamps_actor(self):
        from routers.admin_ai_quality import (
            AiQualityResolvePayload, resolve_ai_quality_event,
        )
        from database.models import AiQualityEvent

        session = _fresh_session()
        try:
            row = _seed_event(session)
            assert row.resolved_status == "open"

            resp = resolve_ai_quality_event(
                event_id=row.id,
                payload=AiQualityResolvePayload(
                    resolved_status="reviewed",
                    resolved_note="not a real bug — courtesy reply was fine",
                ),
                db=session,
                _admin={"role": "admin", "email": "ops@nahla.app"},
            )
            assert resp.resolved_status == "reviewed"
            assert resp.resolved_by == "ops@nahla.app"
            assert resp.resolved_at is not None
            assert resp.resolved_note == \
                "not a real bug — courtesy reply was fine"

            stored = session.query(AiQualityEvent).filter_by(id=row.id).one()
            assert stored.resolved_status == "reviewed"
        finally:
            session.close()

    def test_mark_ignored_and_fixed(self):
        from routers.admin_ai_quality import (
            AiQualityResolvePayload, resolve_ai_quality_event,
        )

        session = _fresh_session()
        try:
            r1 = _seed_event(session)
            r2 = _seed_event(session)
            resolve_ai_quality_event(
                event_id=r1.id,
                payload=AiQualityResolvePayload(resolved_status="ignored"),
                db=session, _admin={"role": "admin", "user_id": 7},
            )
            resolve_ai_quality_event(
                event_id=r2.id,
                payload=AiQualityResolvePayload(resolved_status="fixed"),
                db=session, _admin={"role": "owner", "user_id": 9},
            )
            from database.models import AiQualityEvent
            assert session.query(AiQualityEvent).filter_by(id=r1.id).one().resolved_status == "ignored"
            assert session.query(AiQualityEvent).filter_by(id=r2.id).one().resolved_status == "fixed"
        finally:
            session.close()

    def test_reopen_clears_actor(self):
        from routers.admin_ai_quality import (
            AiQualityResolvePayload, resolve_ai_quality_event,
        )

        session = _fresh_session()
        try:
            row = _seed_event(session)
            resolve_ai_quality_event(
                event_id=row.id,
                payload=AiQualityResolvePayload(resolved_status="reviewed"),
                db=session, _admin={"role": "admin", "email": "ops@nahla.app"},
            )
            resp = resolve_ai_quality_event(
                event_id=row.id,
                payload=AiQualityResolvePayload(resolved_status="open"),
                db=session, _admin={"role": "admin", "email": "ops@nahla.app"},
            )
            assert resp.resolved_status == "open"
            assert resp.resolved_by is None
            assert resp.resolved_at is None
        finally:
            session.close()

    def test_unknown_event_returns_404(self):
        from routers.admin_ai_quality import (
            AiQualityResolvePayload, resolve_ai_quality_event,
        )

        session = _fresh_session()
        try:
            with pytest.raises(HTTPException) as exc:
                resolve_ai_quality_event(
                    event_id=99999,
                    payload=AiQualityResolvePayload(resolved_status="reviewed"),
                    db=session, _admin={"role": "admin"},
                )
            assert exc.value.status_code == 404
        finally:
            session.close()

    def test_invalid_status_returns_400(self):
        from routers.admin_ai_quality import (
            AiQualityResolvePayload, resolve_ai_quality_event,
        )

        session = _fresh_session()
        try:
            row = _seed_event(session)
            with pytest.raises(HTTPException) as exc:
                resolve_ai_quality_event(
                    event_id=row.id,
                    payload=AiQualityResolvePayload(resolved_status="bogus"),
                    db=session, _admin={"role": "admin"},
                )
            assert exc.value.status_code == 400
        finally:
            session.close()


# ── 4. Router registration smoke check ────────────────────────────────


class TestRouterRegistered:
    def test_main_includes_admin_ai_quality_router(self):
        src = (REPO_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        assert "from routers.admin_ai_quality import router as _admin_ai_quality_router" in src
        assert "app.include_router(_admin_ai_quality_router)" in src

    def test_router_has_three_routes(self):
        from routers.admin_ai_quality import router
        paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}  # type: ignore[attr-defined]
        assert ("/admin/ai-quality/events", ("GET",)) in paths
        assert ("/admin/ai-quality/summary", ("GET",)) in paths
        assert ("/admin/ai-quality/events/{event_id}", ("PATCH",)) in paths
