"""
tests/test_campaign_rescue_eligibility.py
─────────────────────────────────────────
Locks the F14 per-campaign rescue-eligibility verdict
(``core.scheduler.evaluate_rescue_eligibility``), which is exposed
through ``GET /campaigns/{id}/debug`` as the
``rescue_eligibility`` block.

The verdict logic MUST agree with the SQL filter in
``_find_stuck_immediate_campaigns`` — these tests are the contract
between the two. Whenever the SQL changes the verdict must change
in lockstep (and vice-versa).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.scheduler import evaluate_rescue_eligibility  # noqa: E402


def _camp(**kw):
    """Lightweight stand-in for a SQLAlchemy Campaign row — the
    helper only reads attributes, no DB needed."""
    defaults = dict(
        id=42,
        status="active",
        schedule_type="immediate",
        launched_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestRescueEligibilityHappyPath:
    def test_classic_stuck_campaign_is_rescuable(self):
        v = evaluate_rescue_eligibility(_camp(), send_logs_count=0)
        assert v["would_rescue"] is True
        assert v["blocked_by"] == []
        # Every condition reports pass=True.
        for k, cond in v["conditions"].items():
            assert cond["pass"] is True, (
                f"condition {k!r} failed unexpectedly: {cond!r}"
            )
        assert "ستلتقطها" in v["explanation_ar"]


class TestRescueEligibilityBlockers:
    def test_status_not_active_blocks(self):
        v = evaluate_rescue_eligibility(
            _camp(status="completed"), send_logs_count=0,
        )
        assert v["would_rescue"] is False
        assert v["conditions"]["status_is_active"]["pass"] is False
        assert v["conditions"]["status_is_active"]["value"] == "completed"
        assert any("status=" in b for b in v["blocked_by"])

    def test_schedule_type_not_immediate_blocks(self):
        v = evaluate_rescue_eligibility(
            _camp(schedule_type="scheduled"), send_logs_count=0,
        )
        assert v["would_rescue"] is False
        assert v["conditions"]["schedule_type_immediate"]["pass"] is False
        assert v["conditions"]["schedule_type_immediate"]["value"] == "scheduled"
        assert any("schedule_type=" in b for b in v["blocked_by"])

    def test_launched_at_null_blocks(self):
        v = evaluate_rescue_eligibility(
            _camp(launched_at=None), send_logs_count=0,
        )
        assert v["would_rescue"] is False
        assert v["conditions"]["launched_at_set"]["pass"] is False
        assert v["conditions"]["launched_at_set"]["value"] is None
        # When launched_at is None we should NOT also report
        # "within grace window" — only the launched_at_set
        # failure. Otherwise the merchant sees two confusing
        # blockers for one underlying issue.
        assert any("launched_at is null" in b for b in v["blocked_by"])
        # Grace window check fails AS WELL (because age is
        # unknown) but the human-facing message should reference
        # the launched_at issue not the grace window.

    def test_within_grace_window_blocks_with_age(self):
        """A campaign launched 10s ago is too fresh to rescue —
        its in-process asyncio task may still be running.
        Critical anti-double-dispatch safety."""
        now = datetime.now(timezone.utc)
        v = evaluate_rescue_eligibility(
            _camp(launched_at=now - timedelta(seconds=10)),
            send_logs_count=0,
            now=now,
        )
        assert v["would_rescue"] is False
        cond = v["conditions"]["past_grace_window"]
        assert cond["pass"] is False
        assert 9 <= cond["age_seconds"] <= 11
        # The message must include the exact age + threshold so
        # the merchant can decide "I'll wait 50s".
        msg = " ".join(v["blocked_by"])
        assert "grace window" in msg
        assert "10s" in msg or "10 " in msg

    def test_existing_send_logs_block(self):
        """The CRITICAL anti-double-send safety — even ONE
        send-log row means rescue must skip. The helper's verdict
        must agree with the SQL filter."""
        v = evaluate_rescue_eligibility(
            _camp(), send_logs_count=1,
        )
        assert v["would_rescue"] is False
        cond = v["conditions"]["no_send_logs"]
        assert cond["pass"] is False
        assert cond["value"] == 1
        assert any("send_logs already has 1" in b for b in v["blocked_by"])

    def test_multiple_blockers_all_reported(self):
        """The merchant should see EVERY blocker at once — don't
        short-circuit after the first one. Otherwise fixing one
        condition reveals the next and they hit the endpoint
        repeatedly."""
        v = evaluate_rescue_eligibility(
            _camp(
                status="completed",
                schedule_type="scheduled",
                launched_at=None,
            ),
            send_logs_count=5,
        )
        assert v["would_rescue"] is False
        # At least 3 distinct blockers in the list.
        assert len(v["blocked_by"]) >= 3


class TestRescueEligibilityShape:
    def test_threshold_block_is_present_and_consistent(self):
        v = evaluate_rescue_eligibility(_camp(), send_logs_count=0)
        thr = v["thresholds"]
        assert thr["stuck_after_seconds"] == 60
        assert thr["poll_seconds"] == 30

    def test_naive_datetime_treated_as_utc(self):
        """``launched_at`` may come back from SQLAlchemy as a naive
        datetime depending on the column definition. The helper
        must treat it as UTC, not raise."""
        now = datetime.now(timezone.utc)
        naive = (now - timedelta(seconds=120)).replace(tzinfo=None)
        v = evaluate_rescue_eligibility(
            _camp(launched_at=naive), send_logs_count=0,
        )
        # Should still classify as rescuable — past grace window.
        assert v["would_rescue"] is True

    def test_explanation_arabic_string_is_present_in_every_path(self):
        cases = [
            evaluate_rescue_eligibility(_camp(), send_logs_count=0),
            evaluate_rescue_eligibility(
                _camp(status="draft"), send_logs_count=0,
            ),
            evaluate_rescue_eligibility(
                _camp(launched_at=None), send_logs_count=0,
            ),
            evaluate_rescue_eligibility(
                _camp(), send_logs_count=4,
            ),
        ]
        for v in cases:
            assert isinstance(v["explanation_ar"], str)
            assert v["explanation_ar"].strip()
