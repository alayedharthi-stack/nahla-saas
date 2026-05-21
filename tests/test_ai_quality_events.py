"""
tests/test_ai_quality_events.py
───────────────────────────────
Locks the AI Quality Monitor persistence + aggregation contract
(May 2026 #12).

What the contract asserts
─────────────────────────
* ``mask_phone`` produces the documented privacy-safe shape.
* ``persist_alignment_mismatch``:
    - writes a single ``ai_quality_events`` row with masked phone,
      truncated previews, default ``resolved_status='open'``,
      ``alignment_passed=False``,
    - returns the new row id,
    - never raises into the caller — DB failures fall back to
      ``None`` while emitting an ``[AI_QUALITY]`` warning.
* ``aggregate_recent_mismatches`` returns a ``{type: count}`` dict
  scoped to ``since`` (and optional ``tenant_id``).
* ``check_threshold_and_alert`` emits ``[AI_QUALITY_ALERT]`` for each
  type that breaches its threshold and ``[AI_QUALITY_ALERT] all
  clear`` otherwise.
* The pipeline wires ``persist_alignment_mismatch`` into the
  alignment mismatch path.

The DB-touching tests use an in-memory SQLite engine so they run
on a clean Windows / Linux box without Postgres.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── In-memory engine fixture (function-scoped) ─────────────────────────


def _fresh_session():
    """Build a fresh SQLite session against the live ``Base.metadata``.

    We only need ``ai_quality_events`` for these tests. Tenants /
    conversations are referenced via ForeignKeys but SQLite ignores
    FK enforcement by default, so we don't need to seed those rows
    for the persistence path.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base, AiQualityEvent  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    # Only create the table we exercise — the full schema would drag
    # in JSONB / PG-specific types that SQLite cannot represent.
    AiQualityEvent.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


# ── 1. ``mask_phone`` ──────────────────────────────────────────────────


class TestMaskPhone:
    def test_e164(self):
        from core.ai_quality_events import mask_phone
        # Mirrors ``admin_debug._mask_phone``: keep first 4 chars
        # (``+966``) and last 3 (``430``), redact the middle.
        assert mask_phone("+966537970430") == "+966***430"

    def test_bare_digits(self):
        from core.ai_quality_events import mask_phone
        assert mask_phone("966537970430") == "9665***430"

    def test_short_input(self):
        from core.ai_quality_events import mask_phone
        assert mask_phone("12345") == "***"

    def test_empty_or_none(self):
        from core.ai_quality_events import mask_phone
        assert mask_phone("") == ""
        assert mask_phone(None) == ""

    def test_strips_whitespace(self):
        from core.ai_quality_events import mask_phone
        assert mask_phone("  +966537970430  ") == "+966***430"

    def test_middle_digits_are_redacted(self):
        """Privacy contract: the middle digits MUST never appear in
        the masked output."""
        from core.ai_quality_events import mask_phone
        masked = mask_phone("+966537970430")
        # "53797" lives in the middle of the original — verify
        # we redacted it. Last 3 ("430") and first 4 ("+966") are
        # intentionally preserved.
        for d in ("537970",):
            assert d not in masked


# ── 2. ``persist_alignment_mismatch`` happy path ──────────────────────


class TestPersistAlignmentMismatch:
    def test_writes_row_with_expected_fields(self):
        from core.ai_quality_events import persist_alignment_mismatch
        from database.models import AiQualityEvent

        session = _fresh_session()
        try:
            new_id = persist_alignment_mismatch(
                session,
                tenant_id=42,
                conversation_id=17,
                customer_phone="+966537970430",
                inbound_text="هو ممتاز لمشاكل البطن؟",
                reply_text="ما تقصر أبداً ويّاك",
                mismatch_type="question_to_social",
                mismatch_reason="question_signal_in_inbound paired with purely-social reply",
                detected_intent="social",
                social_category="general_courtesy",
                action_taken="ACTION_SOCIAL_REPLY",
                chosen_path="social_template",
                fallback_used=False,
                order_status="discovery",
                awaiting_payment_receipt=False,
                model_used="claude-opus-4-6",
                turn=4,
                alignment_passed=False,
                regen_fired=False,
            )
            assert new_id is not None and new_id > 0

            row = session.query(AiQualityEvent).filter_by(id=new_id).one()
            # Privacy contract — masked phone, truncated previews.
            assert row.customer_phone_masked == "+966***430"
            assert "537970" not in (row.customer_phone_masked or "")
            assert row.inbound_preview == "هو ممتاز لمشاكل البطن؟"
            assert row.reply_preview == "ما تقصر أبداً ويّاك"
            # Classification fields.
            assert row.mismatch_type == "question_to_social"
            assert row.detected_intent == "social"
            assert row.social_category == "general_courtesy"
            assert row.action_taken == "ACTION_SOCIAL_REPLY"
            assert row.fallback_used is False
            assert row.model_used == "claude-opus-4-6"
            # Defaults — every new row starts unresolved + un-passed.
            assert row.resolved_status == "open"
            assert row.alignment_passed is False
            assert row.regen_fired is False
            assert row.tenant_id == 42
            assert row.conversation_id == 17
            assert row.turn == 4
            # ``created_at`` is server-side default but our explicit
            # write timestamp survives.
            assert isinstance(row.created_at, datetime)
        finally:
            session.close()

    def test_long_previews_are_truncated(self):
        from core.ai_quality_events import persist_alignment_mismatch, PREVIEW_MAX_CHARS
        from database.models import AiQualityEvent

        long_inbound = "س" * 600
        long_reply = "ر" * 800
        session = _fresh_session()
        try:
            new_id = persist_alignment_mismatch(
                session,
                tenant_id=1, conversation_id=None,
                customer_phone="+966500000001",
                inbound_text=long_inbound,
                reply_text=long_reply,
                mismatch_type="closing_to_reopen",
                mismatch_reason="closing inbound paired with reopen reply",
            )
            row = session.query(AiQualityEvent).filter_by(id=new_id).one()
            assert len(row.inbound_preview or "") <= PREVIEW_MAX_CHARS
            assert len(row.reply_preview or "") <= PREVIEW_MAX_CHARS
            # Trailing ellipsis confirms truncation actually happened.
            assert row.inbound_preview.endswith("…")
            assert row.reply_preview.endswith("…")
        finally:
            session.close()

    def test_db_failure_returns_none_and_does_not_raise(self, caplog):
        from core.ai_quality_events import persist_alignment_mismatch
        bad_db = MagicMock()
        bad_db.add.side_effect = RuntimeError("simulated outage")

        with caplog.at_level(logging.WARNING, logger="nahla.ai_quality_events"):
            result = persist_alignment_mismatch(
                bad_db,
                tenant_id=1, conversation_id=None,
                customer_phone="+966500000001",
                inbound_text="x", reply_text="y",
                mismatch_type="question_to_social",
                mismatch_reason="r",
            )
        assert result is None
        warns = [r for r in caplog.records if "[AI_QUALITY]" in r.getMessage()]
        assert warns, "DB failure must emit an [AI_QUALITY] warning"

    def test_phone_is_always_masked(self):
        """Even if the caller passes a valid full phone, the row stores
        the masked form. Defense in depth — if a future caller
        forgets to mask, the model layer still does."""
        from core.ai_quality_events import persist_alignment_mismatch
        from database.models import AiQualityEvent

        session = _fresh_session()
        try:
            new_id = persist_alignment_mismatch(
                session,
                tenant_id=1, conversation_id=None,
                customer_phone="+966537970430",
                inbound_text="x", reply_text="y",
                mismatch_type="religious_to_oos",
                mismatch_reason="r",
            )
            row = session.query(AiQualityEvent).filter_by(id=new_id).one()
            assert "537970" not in (row.customer_phone_masked or "")
            assert row.customer_phone_masked.startswith("+966")
            assert row.customer_phone_masked.endswith("430")
        finally:
            session.close()


# ── 3. ``aggregate_recent_mismatches`` ─────────────────────────────────


class TestAggregateRecentMismatches:
    def _seed(self, session, *, tenant_id: int, mtype: str, n: int, when: datetime):
        from core.ai_quality_events import persist_alignment_mismatch
        from database.models import AiQualityEvent

        for _ in range(n):
            persist_alignment_mismatch(
                session,
                tenant_id=tenant_id, conversation_id=None,
                customer_phone="+966500000001",
                inbound_text="x", reply_text="y",
                mismatch_type=mtype,
                mismatch_reason="r",
            )
        # Override created_at on the rows we just inserted (the fixed
        # default is "now", which would all fall in the same window).
        rows = (
            session.query(AiQualityEvent)
                   .filter(AiQualityEvent.tenant_id == tenant_id)
                   .filter(AiQualityEvent.mismatch_type == mtype)
                   .all()
        )
        for r in rows[-n:]:
            r.created_at = when
        session.flush()

    def test_counts_per_type(self):
        from core.ai_quality_events import aggregate_recent_mismatches
        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            self._seed(session, tenant_id=1, mtype="question_to_social",
                       n=3, when=now - timedelta(hours=1))
            self._seed(session, tenant_id=1, mtype="closing_to_reopen",
                       n=5, when=now - timedelta(hours=2))
            self._seed(session, tenant_id=1, mtype="religious_to_oos",
                       n=1, when=now - timedelta(hours=3))

            counts = aggregate_recent_mismatches(
                session, since=now - timedelta(hours=6),
            )
            assert counts == {
                "question_to_social": 3,
                "closing_to_reopen":  5,
                "religious_to_oos":   1,
            }
        finally:
            session.close()

    def test_since_filter_excludes_old_events(self):
        from core.ai_quality_events import aggregate_recent_mismatches
        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            self._seed(session, tenant_id=1, mtype="question_to_social",
                       n=2, when=now - timedelta(hours=1))
            self._seed(session, tenant_id=1, mtype="question_to_social",
                       n=4, when=now - timedelta(hours=20))  # older than window

            counts = aggregate_recent_mismatches(
                session, since=now - timedelta(hours=6),
            )
            assert counts == {"question_to_social": 2}
        finally:
            session.close()

    def test_tenant_scoping(self):
        from core.ai_quality_events import aggregate_recent_mismatches
        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            self._seed(session, tenant_id=1, mtype="question_to_social",
                       n=2, when=now - timedelta(hours=1))
            self._seed(session, tenant_id=2, mtype="question_to_social",
                       n=7, when=now - timedelta(hours=1))

            both = aggregate_recent_mismatches(
                session, since=now - timedelta(hours=6),
            )
            assert both["question_to_social"] == 9

            scoped = aggregate_recent_mismatches(
                session, since=now - timedelta(hours=6), tenant_id=1,
            )
            assert scoped == {"question_to_social": 2}
        finally:
            session.close()

    def test_no_rows_returns_empty_dict(self):
        from core.ai_quality_events import aggregate_recent_mismatches
        session = _fresh_session()
        try:
            now = datetime.now(timezone.utc)
            assert aggregate_recent_mismatches(
                session, since=now - timedelta(hours=1),
            ) == {}
        finally:
            session.close()


# ── 4. ``check_threshold_and_alert`` ───────────────────────────────────


class TestCheckThresholdAndAlert:
    def _seed(self, session, mtype: str, n: int):
        from core.ai_quality_events import persist_alignment_mismatch
        for _ in range(n):
            persist_alignment_mismatch(
                session,
                tenant_id=1, conversation_id=None,
                customer_phone="+966500000001",
                inbound_text="x", reply_text="y",
                mismatch_type=mtype,
                mismatch_reason="r",
            )

    def test_below_threshold_emits_all_clear(self, caplog):
        from core.ai_quality_events import check_threshold_and_alert
        session = _fresh_session()
        try:
            self._seed(session, "question_to_social", 2)
            with caplog.at_level(logging.INFO, logger="nahla.ai_quality_events"):
                breaches = check_threshold_and_alert(
                    session,
                    thresholds={
                        "question_to_social": 100,
                        "closing_to_reopen":  100,
                        "religious_to_oos":   100,
                        "delivery_to_receipt": 100,
                    },
                    lookback_hours=6,
                )
            assert breaches == []
            msgs = [r.getMessage() for r in caplog.records
                    if "[AI_QUALITY_ALERT]" in r.getMessage()]
            assert any("all clear" in m for m in msgs), \
                "below-threshold check must log [AI_QUALITY_ALERT] all clear"
        finally:
            session.close()

    def test_above_threshold_emits_warning(self, caplog):
        from core.ai_quality_events import check_threshold_and_alert
        session = _fresh_session()
        try:
            self._seed(session, "delivery_to_receipt", 6)
            self._seed(session, "question_to_social", 2)
            with caplog.at_level(logging.WARNING, logger="nahla.ai_quality_events"):
                breaches = check_threshold_and_alert(
                    session,
                    thresholds={
                        "delivery_to_receipt": 5,
                        "question_to_social":  10,
                        "closing_to_reopen":   100,
                        "religious_to_oos":    100,
                    },
                    lookback_hours=6,
                )
            assert breaches == [("delivery_to_receipt", 6, 5)]
            msgs = [r.getMessage() for r in caplog.records
                    if r.levelname == "WARNING" and "[AI_QUALITY_ALERT]" in r.getMessage()]
            assert msgs, "must emit a WARNING log for the breach"
            assert any("delivery_to_receipt" in m and "count=6" in m for m in msgs)
        finally:
            session.close()

    def test_env_thresholds_override_defaults(self, monkeypatch):
        from core.ai_quality_events import _resolve_thresholds, DEFAULT_ALERT_THRESHOLDS
        monkeypatch.setenv(
            "AI_QUALITY_THRESHOLDS",
            "question_to_social:99,closing_to_reopen:42",
        )
        thr = _resolve_thresholds()
        assert thr["question_to_social"] == 99
        assert thr["closing_to_reopen"] == 42
        # Untouched keys keep the defaults.
        assert thr["religious_to_oos"] == DEFAULT_ALERT_THRESHOLDS["religious_to_oos"]

    def test_env_lookback_override(self, monkeypatch):
        from core.ai_quality_events import _resolve_lookback_hours
        monkeypatch.setenv("AI_QUALITY_LOOKBACK_HOURS", "12")
        assert _resolve_lookback_hours() == 12
        monkeypatch.setenv("AI_QUALITY_LOOKBACK_HOURS", "")
        assert _resolve_lookback_hours() == 6  # default


# ── 5. Pipeline wire-up ────────────────────────────────────────────────


class TestPipelineWireUp:
    """Source-level guard: pipeline.py must import + invoke
    ``persist_alignment_mismatch`` inside the alignment-mismatch
    branch."""

    def test_pipeline_imports_persistence_helper(self):
        src = (
            REPO_ROOT / "backend" / "modules" / "ai" / "brain" / "pipeline.py"
        ).read_text(encoding="utf-8")
        assert "from core.ai_quality_events import" in src, (
            "pipeline.py must import persistence helper"
        )
        assert "persist_alignment_mismatch" in src, (
            "pipeline.py must invoke persist_alignment_mismatch"
        )

    def test_persistence_call_is_inside_mismatch_branch(self):
        src = (
            REPO_ROOT / "backend" / "modules" / "ai" / "brain" / "pipeline.py"
        ).read_text(encoding="utf-8")
        # The persistence call must come AFTER ``if not _align_result.passed:``
        # — this guarantees we never persist a passed-alignment row.
        guard_idx = src.find("if not _align_result.passed:")
        persist_idx = src.find("persist_alignment_mismatch(")
        assert guard_idx > 0 and persist_idx > guard_idx, (
            "persistence call must live INSIDE the mismatch branch"
        )

    def test_persistence_call_passes_conversation_id(self):
        """Regression guard — the conversation_id link is what powers
        the inbox deep-link in the dashboard."""
        src = (
            REPO_ROOT / "backend" / "modules" / "ai" / "brain" / "pipeline.py"
        ).read_text(encoding="utf-8")
        # The arg name appears in the call site; we don't assert the
        # exact value, just that the keyword is present in the call.
        idx = src.find("persist_alignment_mismatch(")
        assert idx > 0
        snippet = src[idx: idx + 1500]
        assert "conversation_id=" in snippet, (
            "persistence call must pass conversation_id"
        )
