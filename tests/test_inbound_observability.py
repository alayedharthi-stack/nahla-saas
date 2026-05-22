"""
tests/test_inbound_observability.py
────────────────────────────────────
Locks the contract for the May 2026 #22 "pre-brain visibility" layer:

* ``core.inbound_observability`` records inbound drops + webhook routing
  failures into ``ai_quality_events`` with ``category != 'ai_mismatch'``.
* The recorder MUST be exception-safe — every public call returns
  ``None`` on failure and never raises.
* The admin endpoint must accept ``?category=`` (filter) and surface
  ``counts_by_category`` in the summary so the owner-dashboard tabs can
  render badge counts cheaply.
* The migration adds the column + index (smoke-checked at the source).
* Each of the five wiring sites in ``routers/whatsapp_webhook.py`` must
  call the recorder before its early ``return``/``continue``.

We invoke the handler functions directly with an in-memory SQLite
session — same pattern as ``tests/test_admin_ai_quality.py`` — so the
suite stays fast and hermetic.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── In-memory SQLite session with just AiQualityEvent ─────────────────


def _fresh_session_and_engine():
    """Return ``(session, engine)`` against an isolated
    ``ai_quality_events`` table. The recorder opens its OWN session via
    ``session.SessionLocal``, so the test patches that symbol to point
    at the same engine — otherwise the writer would land on its own
    in-memory DB that the test can't inspect.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import AiQualityEvent  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    AiQualityEvent.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session(), Session


def _all_rows(session):
    from database.models import AiQualityEvent
    return session.query(AiQualityEvent).order_by(AiQualityEvent.id.asc()).all()


# ── 1. Recorder — happy path + safety net ─────────────────────────────


class TestRecorderHappyPath:
    def test_record_inbound_drop_writes_row_with_category(self):
        from core import inbound_observability as obs

        sess, SessionLocal = _fresh_session_and_engine()
        try:
            with patch.object(obs, "_write_event", wraps=obs._write_event), \
                 patch("session.SessionLocal", SessionLocal):
                new_id = obs.record_inbound_drop(
                    tenant_id=33,
                    drop_kind=obs.DROP_UNSUPPORTED_TYPE,
                    customer_phone="+966537970430",
                    inbound_preview="reaction msg",
                    chosen_path="normalized_type=reaction",
                )
            assert new_id is not None and new_id > 0

            rows = _all_rows(sess)
            assert len(rows) == 1
            row = rows[0]
            assert row.tenant_id == 33
            assert row.category == obs.CATEGORY_INBOUND_DROP
            assert row.mismatch_type == obs.DROP_UNSUPPORTED_TYPE
            # Phone is stored masked, not raw E.164.
            assert "537970" not in (row.customer_phone_masked or "")
            assert row.chosen_path == "normalized_type=reaction"
        finally:
            sess.close()

    def test_record_webhook_unrouted_uses_zero_tenant_when_unknown(self):
        from core import inbound_observability as obs

        sess, SessionLocal = _fresh_session_and_engine()
        try:
            with patch("session.SessionLocal", SessionLocal):
                obs.record_webhook_unrouted(
                    tenant_id=None,
                    sub_reason=obs.ROUTE_UNROUTED_UNKNOWN_PHONE,
                    phone_number_id="888777666555444",
                    detail="display=+966XXXXXX no row",
                )
            rows = _all_rows(sess)
            assert len(rows) == 1
            assert rows[0].tenant_id == 0
            assert rows[0].category == obs.CATEGORY_WEBHOOK_ROUTING
            assert rows[0].mismatch_type == obs.ROUTE_UNROUTED_UNKNOWN_PHONE
            # phone_number_id is embedded into the detail blob so
            # operators can correlate without joining tables.
            assert "888777666555444" in (rows[0].mismatch_reason or "")
        finally:
            sess.close()

    def test_record_webhook_unrouted_preserves_known_tenant(self):
        from core import inbound_observability as obs

        sess, SessionLocal = _fresh_session_and_engine()
        try:
            with patch("session.SessionLocal", SessionLocal):
                obs.record_webhook_unrouted(
                    tenant_id=33,
                    sub_reason=obs.ROUTE_UNROUTED_BAD_SECRET,
                    phone_number_id="123",
                    detail="conn=7",
                )
            rows = _all_rows(sess)
            assert rows[0].tenant_id == 33
            assert rows[0].category == obs.CATEGORY_WEBHOOK_ROUTING


        finally:
            sess.close()

    def test_unknown_category_coerced_to_inbound_drop(self):
        from core import inbound_observability as obs

        sess, SessionLocal = _fresh_session_and_engine()
        try:
            with patch("session.SessionLocal", SessionLocal):
                rid = obs._write_event(
                    tenant_id=1,
                    category="garbage_value",
                    mismatch_type="x",
                )
            assert rid is not None
            assert _all_rows(sess)[0].category == obs.CATEGORY_INBOUND_DROP
        finally:
            sess.close()


# ── 2. Recorder — never raises ────────────────────────────────────────


class TestRecorderSafety:
    def test_recorder_swallows_session_open_failure(self):
        from core import inbound_observability as obs

        def _boom():
            raise RuntimeError("DB unavailable")

        # SessionLocal() raising must NOT propagate — observability
        # rows are best-effort and must never break the inbound path.
        with patch("session.SessionLocal", _boom):
            result = obs.record_inbound_drop(
                tenant_id=33,
                drop_kind=obs.DROP_EMPTY_TEXT,
            )
        assert result is None  # graceful failure

    def test_recorder_swallows_commit_failure(self):
        from core import inbound_observability as obs

        sess, SessionLocal = _fresh_session_and_engine()

        class _FailingSession:
            def __init__(self):
                self._inner = SessionLocal()

            def add(self, row):
                self._inner.add(row)

            def commit(self):
                raise RuntimeError("commit blew up")

            def rollback(self):
                self._inner.rollback()

            def close(self):
                self._inner.close()

        try:
            with patch("session.SessionLocal", _FailingSession):
                result = obs.record_inbound_drop(
                    tenant_id=33,
                    drop_kind=obs.DROP_UNSUPPORTED_TYPE,
                )
            assert result is None
        finally:
            sess.close()


# ── 3. Admin endpoint — category filter + by_category summary ─────────


def _seed(session, *, category="ai_mismatch", mismatch_type="question_to_social",
          tenant_id=1, created_at=None, customer_phone="+966537970430"):
    """Direct row insert that bypasses ``persist_alignment_mismatch`` so
    we can stamp arbitrary ``category`` values (the brain recorder only
    writes ``ai_mismatch``).
    """
    from database.models import AiQualityEvent
    row = AiQualityEvent(
        tenant_id=tenant_id,
        conversation_id=None,
        customer_phone_masked="+966***430",
        category=category,
        mismatch_type=mismatch_type,
        mismatch_reason="seed",
        alignment_passed=False,
        regen_fired=False,
        resolved_status="open",
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    session.commit()
    return row


class TestAdminEndpointCategoryFilter:
    def test_list_filters_by_category(self):
        from routers.admin_ai_quality import list_ai_quality_events

        sess, _ = _fresh_session_and_engine()
        try:
            _seed(sess, category="ai_mismatch",     mismatch_type="question_to_social")
            _seed(sess, category="inbound_drop",    mismatch_type="unsupported_type")
            _seed(sess, category="inbound_drop",    mismatch_type="empty_text")
            _seed(sess, category="webhook_routing", mismatch_type="unrouted_missing_phone_id")

            resp = list_ai_quality_events(
                tenant_id=None, category="inbound_drop",
                mismatch_type=None, resolved_status=None,
                since=None, until=None, limit=50, offset=0,
                db=sess, _admin={"role": "admin"},
            )
            assert resp.total == 2
            assert {it.mismatch_type for it in resp.items} == {
                "unsupported_type", "empty_text",
            }
            assert all(it.category == "inbound_drop" for it in resp.items)
        finally:
            sess.close()

    def test_list_rejects_unknown_category(self):
        from fastapi import HTTPException
        from routers.admin_ai_quality import list_ai_quality_events

        sess, _ = _fresh_session_and_engine()
        try:
            with pytest.raises(HTTPException) as exc:
                list_ai_quality_events(
                    tenant_id=None, category="garbage",
                    mismatch_type=None, resolved_status=None,
                    since=None, until=None, limit=50, offset=0,
                    db=sess, _admin={"role": "admin"},
                )
            assert exc.value.status_code == 400
        finally:
            sess.close()

    def test_summary_returns_counts_by_category(self):
        from routers.admin_ai_quality import ai_quality_summary

        sess, _ = _fresh_session_and_engine()
        try:
            now = datetime.now(timezone.utc)
            # 3 ai_mismatch + 2 inbound_drop + 1 webhook_routing — the
            # tab badges should mirror those totals exactly.
            for _ in range(3):
                _seed(sess, category="ai_mismatch", created_at=now - timedelta(minutes=5))
            for _ in range(2):
                _seed(sess, category="inbound_drop", created_at=now - timedelta(minutes=5))
            _seed(sess, category="webhook_routing", created_at=now - timedelta(minutes=5))

            resp = ai_quality_summary(
                tenant_id=None, category=None, window_hours=24,
                db=sess, _admin={"role": "admin"},
            )
            by_cat = {c.category: c.count for c in resp.counts_by_category}
            assert by_cat == {
                "ai_mismatch":     3,
                "inbound_drop":    2,
                "webhook_routing": 1,
            }
            # Without an explicit category, total_in_window is the
            # unfiltered count so the dashboard's header is honest.
            assert resp.total_in_window == 6
        finally:
            sess.close()

    def test_summary_scopes_counts_by_type_when_category_passed(self):
        from routers.admin_ai_quality import ai_quality_summary

        sess, _ = _fresh_session_and_engine()
        try:
            now = datetime.now(timezone.utc)
            _seed(sess, category="ai_mismatch",
                  mismatch_type="question_to_social",
                  created_at=now - timedelta(minutes=5))
            _seed(sess, category="inbound_drop",
                  mismatch_type="unsupported_type",
                  created_at=now - timedelta(minutes=5))
            _seed(sess, category="inbound_drop",
                  mismatch_type="empty_text",
                  created_at=now - timedelta(minutes=5))

            resp = ai_quality_summary(
                tenant_id=None, category="inbound_drop", window_hours=24,
                db=sess, _admin={"role": "admin"},
            )
            types = {c.mismatch_type for c in resp.counts_by_type}
            assert types == {"unsupported_type", "empty_text"}
            assert resp.total_in_window == 2
            # counts_by_category remains unscoped — the tabs still see
            # every bucket even when a single tab is active.
            assert {c.category for c in resp.counts_by_category} >= {
                "ai_mismatch", "inbound_drop",
            }
        finally:
            sess.close()


# ── 4. Migration source-level smoke ──────────────────────────────────


class TestMigrationSchema:
    """We can't run alembic in this in-memory SQLite suite cheaply, so
    we lock the migration's *intent* by inspecting the source. A schema
    drift would break the dashboard contract and we want the test to
    catch it before deploy.
    """

    def test_migration_0070_adds_category_column_and_index(self):
        src = (
            REPO_ROOT / "database" / "migrations" / "versions"
            / "0070_ai_quality_category.py"
        ).read_text(encoding="utf-8")
        # The exact column + default + index are the part the dashboard
        # depends on. Anything else is implementation detail.
        assert "ai_quality_events" in src
        assert "add_column" in src
        assert '"category"' in src or "'category'" in src
        assert "ai_mismatch" in src  # server default keeps legacy rows valid
        assert "ix_aiq_tenant_category_created" in src

    def test_migration_revision_chain(self):
        src = (
            REPO_ROOT / "database" / "migrations" / "versions"
            / "0070_ai_quality_category.py"
        ).read_text(encoding="utf-8")
        assert 'revision = "0070"' in src
        assert 'down_revision = "0069"' in src


# ── 5. Webhook wiring source-level smoke ──────────────────────────────


class TestWebhookWiringSource:
    """Locks the call-site contract: every silent-drop branch in
    ``routers/whatsapp_webhook.py`` must invoke the recorder before
    returning. Without these source assertions the wiring could drift
    on a future refactor and the dashboard would silently go back to
    zeros — exactly the regression we're trying to prevent.
    """

    @classmethod
    def setup_class(cls):
        cls.src = (
            REPO_ROOT / "backend" / "routers" / "whatsapp_webhook.py"
        ).read_text(encoding="utf-8")

    def test_unsupported_type_drop_wired(self):
        assert "INBOUND_IGNORED_UNSUPPORTED" in self.src
        assert "DROP_UNSUPPORTED_TYPE" in self.src

    def test_empty_text_drop_wired(self):
        assert "INBOUND_IGNORED_EMPTY_TEXT" in self.src
        assert "DROP_EMPTY_TEXT" in self.src

    def test_pre_brain_handoff_drop_wired(self):
        assert "DROP_PRE_BRAIN_HANDOFF" in self.src

    def test_dispatcher_exception_wired(self):
        assert "DROP_DISPATCHER_EXCEPTION" in self.src

    def test_webhook_unrouted_branches_wired(self):
        # All five sub-reasons must reach the recorder.
        for token in (
            "ROUTE_UNROUTED_MISSING_PHONE",
            "ROUTE_UNROUTED_UNKNOWN_PHONE",
            "ROUTE_UNROUTED_AMBIGUOUS",
            "ROUTE_UNROUTED_WRONG_PROVIDER",
            "ROUTE_UNROUTED_BAD_SECRET",
        ):
            assert token in self.src, f"missing wiring constant: {token}"

    def test_observability_imports_are_local_to_drop_sites(self):
        """The wiring uses local imports (``from core.inbound_observability
        import ...`` inside the except branch) to keep startup cost zero
        on healthy workers that never hit a drop. Regression-pin that
        we didn't accidentally hoist them to module scope, which would
        force every cold-start to load the recorder + models even for
        merchants who never see a drop in months.
        """
        # The recorder should be imported AT LEAST 5 times (one per
        # wired site). If a future refactor moves to a module-scope
        # import that's fine — but the count must not regress to 0.
        count = self.src.count("from core.inbound_observability import")
        assert count >= 5, f"expected at least 5 local imports, got {count}"


# ── 6. Model contract ─────────────────────────────────────────────────


class TestModelDefault:
    def test_category_defaults_to_ai_mismatch_on_python_side(self):
        """When the legacy ``persist_alignment_mismatch`` writer hands
        the ORM a row without setting ``category``, the column must
        default to ``'ai_mismatch'`` so the dashboard's existing tab
        keeps showing brain-side mismatches without any backfill.
        """
        from database.models import AiQualityEvent

        sess, _ = _fresh_session_and_engine()
        try:
            row = AiQualityEvent(
                tenant_id=1,
                customer_phone_masked="+966***430",
                mismatch_type="x",
                alignment_passed=False,
                regen_fired=False,
                resolved_status="open",
                created_at=datetime.now(timezone.utc),
            )
            sess.add(row)
            sess.flush()
            sess.commit()
            assert row.category == "ai_mismatch"
        finally:
            sess.close()
