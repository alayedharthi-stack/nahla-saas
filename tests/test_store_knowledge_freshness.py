"""
tests/test_store_knowledge_freshness.py
───────────────────────────────────────
Locks down the tz-defensive fix in `StoreKnowledgeLoader.is_fresh()`.

Background
──────────
`build_ai_context()` calls `loader.is_fresh()` near the end. That call
used to do:

    age = (datetime.now(timezone.utc) - snap.last_full_sync_at).total_seconds()

PostgreSQL returns `last_full_sync_at` as an offset-NAIVE datetime, but
`datetime.now(timezone.utc)` is offset-AWARE — Python raises:

    TypeError: can't subtract offset-naive and offset-aware datetimes

The exception bubbled up to `_handle_merchant_message`, was logged as
"[Merchant] Error generating reply" and produced ZERO reply for every
inbound message. Customers saw nothing.

Contract:
  * `is_fresh()` returns True/False without raising, regardless of
    whether `last_full_sync_at` is naive or aware.
  * Naive timestamps from the DB are interpreted as UTC (matching the
    convention used by every writer in this codebase).
  * Snapshots with no `last_full_sync_at` return False (cannot prove
    freshness).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from core.store_knowledge import StoreKnowledgeLoader  # noqa: E402


class _FakeSnap:
    def __init__(self, last_full_sync_at):
        self.last_full_sync_at = last_full_sync_at


def _make_loader(snap_value) -> StoreKnowledgeLoader:
    """Build a loader whose `.snapshot()` returns whatever we want
    without touching the DB."""
    loader = StoreKnowledgeLoader.__new__(StoreKnowledgeLoader)
    loader._cached_snapshot = snap_value  # type: ignore[attr-defined]
    loader.snapshot = lambda: snap_value  # type: ignore[method-assign]
    return loader


class TestIsFresh:
    def test_returns_false_when_snapshot_missing(self) -> None:
        assert _make_loader(None).is_fresh() is False

    def test_returns_false_when_last_sync_missing(self) -> None:
        assert _make_loader(_FakeSnap(None)).is_fresh() is False

    def test_naive_recent_timestamp_is_fresh(self) -> None:
        """Reproduces the production crash — a naive datetime coming
        from PostgreSQL must NOT raise and must be interpreted as UTC."""
        recent_naive = datetime.utcnow() - timedelta(minutes=10)
        loader = _make_loader(_FakeSnap(recent_naive))
        # Must not raise — this is the regression we are guarding.
        assert loader.is_fresh(max_age_hours=6) is True

    def test_naive_old_timestamp_is_stale(self) -> None:
        old_naive = datetime.utcnow() - timedelta(hours=24)
        loader = _make_loader(_FakeSnap(old_naive))
        assert loader.is_fresh(max_age_hours=6) is False

    def test_aware_recent_timestamp_is_fresh(self) -> None:
        recent_aware = datetime.now(timezone.utc) - timedelta(minutes=10)
        loader = _make_loader(_FakeSnap(recent_aware))
        assert loader.is_fresh(max_age_hours=6) is True

    def test_aware_old_timestamp_is_stale(self) -> None:
        old_aware = datetime.now(timezone.utc) - timedelta(hours=24)
        loader = _make_loader(_FakeSnap(old_aware))
        assert loader.is_fresh(max_age_hours=6) is False
