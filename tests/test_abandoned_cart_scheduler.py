"""
tests/test_abandoned_cart_scheduler.py
──────────────────────────────────────
Pin the contract of the dedicated abandoned-cart reconciliation loop.

The loop's only responsibilities are:

  1. Discover every active Salla integration each tick.
  2. For each tenant, call ``StoreSyncService.sync_abandoned_carts()``
     with full per-tenant exception isolation.
  3. Record per-tenant + per-cycle health so operators can tell at a
     glance whether the loop is actually running.

These tests never touch the real DB or network — they monkeypatch the
two collaborators (``_list_active_salla_tenants`` and the
``StoreSyncService`` import-via-attribute) and assert on the state
registry instead. That keeps the suite fast and lets us prove
invariants like "a single failing tenant does not stop the loop".
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

# Make the `backend` package importable for direct test runs.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core import abandoned_cart_scheduler as ac  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clear the module-level registry between tests so prior runs
    can't bleed consecutive_failures or last_runs into a fresh case."""
    fresh = ac.SchedulerState()
    monkeypatch.setattr(ac, "STATE", fresh)
    yield


class _FakeSyncService:
    """Stand-in for :class:`StoreSyncService` — records the tenant id
    it was constructed with and returns a configurable result.

    The real service has many other methods; the scheduler only uses
    ``sync_abandoned_carts`` so that's the entire interface we need
    to fake here.
    """

    # Maps tenant_id → either a dict to return OR an Exception to raise.
    plan: dict = {}
    seen: list = []

    def __init__(self, db, tenant_id):  # noqa: D401
        self.tenant_id = tenant_id
        _FakeSyncService.seen.append(tenant_id)

    async def sync_abandoned_carts(self):  # noqa: D401
        out = self.plan.get(self.tenant_id, {"salla_count": 0})
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture(autouse=True)
def _patch_collaborators(monkeypatch):
    """Wire :class:`_FakeSyncService` and a fake DB session into the
    scheduler so the per-tenant tick has no real dependencies."""
    _FakeSyncService.plan = {}
    _FakeSyncService.seen = []

    class _FakeDB:
        def close(self):
            pass

    fake_module = type(sys)("services.store_sync")
    fake_module.StoreSyncService = _FakeSyncService

    fake_db_module = type(sys)("core.database")
    fake_db_module.SessionLocal = lambda: _FakeDB()

    monkeypatch.setitem(sys.modules, "services.store_sync", fake_module)
    monkeypatch.setitem(sys.modules, "core.database", fake_db_module)
    yield


# ── Per-tenant tick ─────────────────────────────────────────────────────────

class TestSyncOneTenant:
    """Each tick must record a TenantSyncStatus and never raise."""

    def test_successful_sync_records_ok_status(self):
        _FakeSyncService.plan[7] = {
            "salla_count": 3, "saved": 1, "updated": 2,
            "reconciled": 0, "skipped_no_id": 0,
        }
        asyncio.run(ac._sync_one_tenant(7))

        snap = ac.get_last_run_for_tenant(7)
        assert snap is not None
        assert snap["status"]   == "ok"
        assert snap["error"]    is None
        assert snap["salla_count"] == 3
        assert snap["saved"]    == 1
        assert snap["updated"]  == 2
        assert snap["consecutive_failures"] == 0
        # Always finishes (started_at and finished_at populated).
        assert snap["finished_at"] is not None
        assert snap["duration_ms"] is not None

    def test_failing_sync_records_error_and_increments_consec(self):
        _FakeSyncService.plan[42] = RuntimeError("boom")
        asyncio.run(ac._sync_one_tenant(42))
        s1 = ac.get_last_run_for_tenant(42)
        assert s1 is not None
        assert s1["status"] == "error"
        assert "boom" in s1["error"]
        assert s1["consecutive_failures"] == 1

        # Second failure → consec climbs.
        asyncio.run(ac._sync_one_tenant(42))
        s2 = ac.get_last_run_for_tenant(42)
        assert s2["consecutive_failures"] == 2

        # Then a success resets it to zero.
        _FakeSyncService.plan[42] = {"salla_count": 0}
        asyncio.run(ac._sync_one_tenant(42))
        s3 = ac.get_last_run_for_tenant(42)
        assert s3["status"] == "ok"
        assert s3["consecutive_failures"] == 0

    def test_one_tenant_failure_does_not_corrupt_others(self):
        # Two tenants in one cycle — first explodes, second succeeds.
        _FakeSyncService.plan[1] = ValueError("nope")
        _FakeSyncService.plan[2] = {"salla_count": 5, "saved": 5}

        async def _run_two():
            await ac._sync_one_tenant(1)
            await ac._sync_one_tenant(2)
        asyncio.run(_run_two())

        s1 = ac.get_last_run_for_tenant(1)
        s2 = ac.get_last_run_for_tenant(2)
        assert s1["status"] == "error"
        assert s2["status"] == "ok"
        assert s2["saved"]  == 5


# ── Cycle integration ───────────────────────────────────────────────────────

class TestTickCycle:
    """A full ``_tick`` call must visit every active tenant and stamp
    cycle-level health on the SchedulerState."""

    def test_tick_visits_all_active_tenants(self, monkeypatch):
        monkeypatch.setattr(
            ac, "_list_active_salla_tenants", lambda: [10, 11, 12],
        )
        for tid in (10, 11, 12):
            _FakeSyncService.plan[tid] = {"salla_count": tid}

        asyncio.run(ac._tick())

        assert sorted(_FakeSyncService.seen) == [10, 11, 12]
        snap = ac.get_state_snapshot()
        assert snap["cycles_completed"]      == 1
        assert snap["tenants_in_last_cycle"] == 3
        assert snap["last_cycle_ok"]         is True
        assert snap["last_cycle_at"]         is not None

    def test_tick_continues_after_per_tenant_failure(self, monkeypatch):
        monkeypatch.setattr(
            ac, "_list_active_salla_tenants", lambda: [100, 101],
        )
        _FakeSyncService.plan[100] = RuntimeError("kaboom")
        _FakeSyncService.plan[101] = {"salla_count": 9, "saved": 9}

        asyncio.run(ac._tick())

        # Both tenants were attempted (kaboom didn't abort the cycle).
        assert _FakeSyncService.seen == [100, 101]
        snap = ac.get_state_snapshot()
        assert snap["cycles_completed"] == 1
        assert snap["last_cycle_ok"]    is True   # cycle itself succeeded
        per = snap["last_runs"]
        assert per["100"]["status"] == "error"
        assert per["101"]["status"] == "ok"

    def test_tick_records_zero_tenants_when_none_active(self, monkeypatch):
        monkeypatch.setattr(ac, "_list_active_salla_tenants", lambda: [])
        asyncio.run(ac._tick())
        snap = ac.get_state_snapshot()
        assert snap["cycles_completed"]      == 1
        assert snap["tenants_in_last_cycle"] == 0
        assert snap["last_runs"]             == {}

    def test_tenant_lookup_failure_does_not_kill_state(self, monkeypatch):
        def _boom():
            raise RuntimeError("db_unreachable")
        monkeypatch.setattr(ac, "_list_active_salla_tenants", _boom)

        asyncio.run(ac._tick())

        snap = ac.get_state_snapshot()
        assert snap["cycle_errors"]   >= 1
        assert snap["last_cycle_ok"]  is False
        # No per-tenant runs when the lookup itself failed.
        assert snap["last_runs"] == {}


# ── Snapshot serialisation ──────────────────────────────────────────────────

class TestStateSnapshot:
    """``get_state_snapshot`` must be a dict (JSON-serialisable) so the
    debug endpoint can return it verbatim without a custom encoder."""

    def test_snapshot_keys(self):
        snap = ac.get_state_snapshot()
        for k in ("started_at", "interval_seconds", "last_cycle_at",
                  "last_cycle_ok", "next_cycle_at", "cycles_completed",
                  "cycle_errors", "tenants_in_last_cycle", "last_runs"):
            assert k in snap

    def test_per_tenant_keys_after_one_run(self, monkeypatch):
        _FakeSyncService.plan[5] = {"salla_count": 1, "saved": 1}
        asyncio.run(ac._sync_one_tenant(5))
        snap = ac.get_state_snapshot()
        # Per-tenant keys are stringified for JSON friendliness.
        assert "5" in snap["last_runs"]
        for k in ("tenant_id", "started_at", "finished_at", "duration_ms",
                  "status", "error", "salla_count", "saved", "updated",
                  "consecutive_failures"):
            assert k in snap["last_runs"]["5"]
