"""
tests/test_ai_quality_scheduler.py
──────────────────────────────────
Locks the contract for the periodic AI Quality Monitor aggregation
job (May 2026 #12 / Commit C).

The job lives in ``backend/core/scheduler.py`` as
``run_ai_quality_scheduler`` and its synchronous tick
``_run_ai_quality_threshold_check``. It opens its own ``SessionLocal``,
calls ``check_threshold_and_alert``, and emits one
``[AI_QUALITY_ALERT]`` warning per breached mismatch type.

What's covered
──────────────
* Tick honours the ``AI_QUALITY_THRESHOLDS`` env override.
* Tick uses ``AI_QUALITY_LOOKBACK_HOURS`` (via
  ``check_threshold_and_alert``) for the aggregation window.
* Tick emits exactly one warning per breached type.
* Below-threshold tick stays quiet (info-only ``all clear``).
* Tick is exception-safe — DB outage returns a soft error dict, not
  a raise.
* The cadence helper resolves to a sane default + respects the env
  var override (with a 5 min floor).
* ``main.py`` registers the scheduler beside the other
  ``_start(...)`` calls (source-level guard).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── In-memory SQLite session helpers (same pattern as Commit A/B) ─────


def _fresh_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base, AiQualityEvent  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    AiQualityEvent.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _seed(session, mtype: str, n: int, *, when: datetime | None = None):
    from core.ai_quality_events import persist_alignment_mismatch
    from database.models import AiQualityEvent

    for _ in range(n):
        persist_alignment_mismatch(
            session,
            tenant_id=1, conversation_id=None,
            customer_phone="+966500000001",
            inbound_text="x", reply_text="y",
            mismatch_type=mtype,
            mismatch_reason="r",
        )
    if when is not None:
        rows = (
            session.query(AiQualityEvent)
                   .filter(AiQualityEvent.mismatch_type == mtype)
                   .all()
        )
        for r in rows[-n:]:
            r.created_at = when
        session.flush()


# ── 1. Cadence helpers ────────────────────────────────────────────────


class TestCadenceHelpers:
    def test_default_interval_is_six_hours(self, monkeypatch):
        from core.scheduler import _ai_quality_interval_seconds
        monkeypatch.delenv("AI_QUALITY_CHECK_INTERVAL_SECONDS", raising=False)
        assert _ai_quality_interval_seconds() == 6 * 3600

    def test_env_override_accepted(self, monkeypatch):
        from core.scheduler import _ai_quality_interval_seconds
        monkeypatch.setenv("AI_QUALITY_CHECK_INTERVAL_SECONDS", "43200")
        assert _ai_quality_interval_seconds() == 43200

    def test_floor_protects_against_misconfig(self, monkeypatch):
        from core.scheduler import _ai_quality_interval_seconds
        # 30 seconds would be a DDoS — we must clamp to 300.
        monkeypatch.setenv("AI_QUALITY_CHECK_INTERVAL_SECONDS", "30")
        assert _ai_quality_interval_seconds() == 300

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        from core.scheduler import _ai_quality_interval_seconds
        monkeypatch.setenv("AI_QUALITY_CHECK_INTERVAL_SECONDS", "definitely-not-a-number")
        assert _ai_quality_interval_seconds() == 6 * 3600


# ── 2. ``_run_ai_quality_threshold_check`` tick ───────────────────────


class TestThresholdTick:
    def _patch_session(self, session):
        """Make ``SessionLocal()`` (called inside the tick) return our
        in-memory SQLite session. We patch the import path that
        ``_run_ai_quality_threshold_check`` resolves at call time."""
        return patch("core.database.SessionLocal", return_value=session)

    def test_below_threshold_emits_all_clear(self, caplog, monkeypatch):
        from core.scheduler import _run_ai_quality_threshold_check

        session = _fresh_session()
        _seed(session, "question_to_social", 1)

        # Use the env-driven thresholds so the check sees a high bar.
        monkeypatch.setenv(
            "AI_QUALITY_THRESHOLDS",
            "question_to_social:50,delivery_to_receipt:50,"
            "closing_to_reopen:50,religious_to_oos:50",
        )

        with self._patch_session(session):
            with caplog.at_level(logging.INFO, logger="nahla.ai_quality_events"):
                result = _run_ai_quality_threshold_check()

        assert result["ok"] is True
        assert result["breaches"] == []
        msgs = [r.getMessage() for r in caplog.records
                if "[AI_QUALITY_ALERT]" in r.getMessage()]
        assert any("all clear" in m for m in msgs)
        session.close()

    def test_above_threshold_emits_warning_per_type(self, caplog, monkeypatch):
        from core.scheduler import _run_ai_quality_threshold_check

        session = _fresh_session()
        _seed(session, "delivery_to_receipt", 6)
        _seed(session, "closing_to_reopen", 9)
        _seed(session, "question_to_social", 2)

        monkeypatch.setenv(
            "AI_QUALITY_THRESHOLDS",
            "question_to_social:50,delivery_to_receipt:5,"
            "closing_to_reopen:8,religious_to_oos:50",
        )

        with self._patch_session(session):
            with caplog.at_level(logging.WARNING, logger="nahla.ai_quality_events"):
                result = _run_ai_quality_threshold_check()

        assert result["ok"] is True
        breach_types = {b["mismatch_type"] for b in result["breaches"]}
        assert breach_types == {"delivery_to_receipt", "closing_to_reopen"}

        warnings = [r.getMessage() for r in caplog.records
                    if r.levelname == "WARNING"
                    and "[AI_QUALITY_ALERT]" in r.getMessage()]
        # One warning per breached type.
        assert len(warnings) == 2
        assert any("delivery_to_receipt" in m and "count=6" in m for m in warnings)
        assert any("closing_to_reopen"   in m and "count=9" in m for m in warnings)
        session.close()

    def test_lookback_window_excludes_old_rows(self, caplog, monkeypatch):
        """Events older than the lookback window must NOT count toward
        the threshold even if they exist in the table."""
        from core.scheduler import _run_ai_quality_threshold_check

        session = _fresh_session()
        now = datetime.now(timezone.utc)
        # 10 inside the 6h window — would breach a threshold of 5.
        _seed(session, "delivery_to_receipt", 10,
              when=now - timedelta(hours=1))
        # 50 outside the window — must be ignored.
        _seed(session, "religious_to_oos", 50,
              when=now - timedelta(days=10))

        monkeypatch.setenv("AI_QUALITY_LOOKBACK_HOURS", "6")
        monkeypatch.setenv(
            "AI_QUALITY_THRESHOLDS",
            "question_to_social:50,delivery_to_receipt:5,"
            "closing_to_reopen:50,religious_to_oos:5",
        )

        with self._patch_session(session):
            with caplog.at_level(logging.WARNING, logger="nahla.ai_quality_events"):
                result = _run_ai_quality_threshold_check()

        breach_types = {b["mismatch_type"] for b in result["breaches"]}
        assert "delivery_to_receipt" in breach_types
        assert "religious_to_oos"    not in breach_types
        session.close()

    def test_db_failure_returns_soft_error(self, caplog, monkeypatch):
        """A bad DB connection must NOT raise out of the tick — the
        scheduler loop should keep ticking."""
        from core.scheduler import _run_ai_quality_threshold_check

        class _Boom:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("simulated DB outage")

        with patch("core.database.SessionLocal", new=_Boom()):
            with caplog.at_level(logging.WARNING):
                result = _run_ai_quality_threshold_check()

        assert result["ok"] is False
        assert "error" in result
        # No exception bubbled up.

    def test_session_is_closed_after_tick(self, monkeypatch):
        """The tick must close the session it opens — long-running
        leaks would exhaust the pool."""
        from core.scheduler import _run_ai_quality_threshold_check

        session = _fresh_session()
        original_close = session.close
        close_calls = {"n": 0}

        def _tracking_close():
            close_calls["n"] += 1
            return original_close()
        session.close = _tracking_close  # type: ignore[method-assign]

        with patch("core.database.SessionLocal", return_value=session):
            _run_ai_quality_threshold_check()

        assert close_calls["n"] == 1


# ── 3. Scheduler registration smoke check ─────────────────────────────


class TestSchedulerRegistration:
    def test_scheduler_factory_exists(self):
        """``run_ai_quality_scheduler`` is the public symbol the
        ``main.py`` lifespan registers — it must stay importable."""
        from core.scheduler import run_ai_quality_scheduler
        import asyncio
        assert asyncio.iscoroutinefunction(run_ai_quality_scheduler)

    def test_main_registers_ai_quality_scheduler(self):
        src = (REPO_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        assert "run_ai_quality_scheduler" in src, (
            "main.py must import run_ai_quality_scheduler"
        )
        assert "_start(\"ai_quality_monitor\"" in src, (
            "main.py must register the scheduler via _start(...)"
        )
