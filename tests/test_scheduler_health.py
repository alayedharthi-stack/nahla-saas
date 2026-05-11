"""
tests/test_scheduler_health.py
──────────────────────────────
Locks the F13 diagnostic endpoint:

  GET /admin/debug/scheduler-health

Covers ``get_campaign_dispatcher_state()`` (the pure computation
function) end-to-end. The endpoint itself is a thin wrapper around
this function plus os.environ reads — testing the state function
guarantees the endpoint's response shape.

Why this matters
────────────────
After F12 (campaign rescue), the only way to confirm from outside
Railway logs that:
  (a) the FastAPI lifespan completed,
  (b) the dispatcher loop is alive,
  (c) the rescue path is firing,
…is to read this state. If get_campaign_dispatcher_state() ever
silently degrades (e.g. timestamps stop being serialisable, alive
verdict regresses), the merchant loses their only post-deploy
diagnostic.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture(autouse=True)
def _reset_state():
    """The dispatcher state is module-level. Reset between tests so
    one test's mutations don't leak into the next."""
    from core import scheduler as sched

    saved = dict(sched._campaign_dispatcher_state)
    yield
    sched._campaign_dispatcher_state.clear()
    sched._campaign_dispatcher_state.update(saved)


class TestDispatcherStateSnapshot:
    def test_fresh_state_reports_not_started_and_not_alive(self):
        from core.scheduler import (
            _campaign_dispatcher_state,
            get_campaign_dispatcher_state,
        )

        _campaign_dispatcher_state.update({
            "started_at":               None,
            "last_tick_at":             None,
            "last_tick_ok":             None,
            "ticks_total":              0,
            "last_rescue_at":           None,
            "last_rescued_campaign_ids": [],
        })

        snap = get_campaign_dispatcher_state()
        assert snap["started"] is False
        assert snap["alive"] is False
        assert snap["last_tick_age_seconds"] is None
        assert snap["uptime_seconds"] is None

    def test_recent_tick_is_alive(self):
        from core.scheduler import (
            _campaign_dispatcher_state,
            get_campaign_dispatcher_state,
        )

        now = datetime.now(timezone.utc)
        _campaign_dispatcher_state.update({
            "started_at":   now - timedelta(minutes=5),
            "last_tick_at": now - timedelta(seconds=5),
            "last_tick_ok": True,
            "ticks_total":  10,
            "poll_seconds": 30,
        })

        snap = get_campaign_dispatcher_state()
        assert snap["started"] is True
        assert snap["alive"] is True, (
            "tick 5s ago with poll_seconds=30 must be considered alive "
            "(threshold is 3× poll period = 90s)"
        )
        assert snap["last_tick_age_seconds"] < 10
        # Timestamps are serialised to ISO strings for JSON.
        assert isinstance(snap["last_tick_at"], str)
        assert isinstance(snap["started_at"], str)
        assert snap["uptime_seconds"] is not None
        assert snap["uptime_seconds"] > 0

    def test_stale_tick_is_not_alive(self):
        """If no tick fired within 3× poll period the loop is
        considered dead. A real production case: uvicorn worker hung
        on a sync DB call and stopped servicing the asyncio event
        loop — the heartbeat goes stale."""
        from core.scheduler import (
            _campaign_dispatcher_state,
            get_campaign_dispatcher_state,
        )

        now = datetime.now(timezone.utc)
        _campaign_dispatcher_state.update({
            "started_at":   now - timedelta(hours=1),
            "last_tick_at": now - timedelta(minutes=5),  # 300s old
            "poll_seconds": 30,                          # 3× = 90s
        })

        snap = get_campaign_dispatcher_state()
        assert snap["started"] is True
        assert snap["alive"] is False
        assert snap["last_tick_age_seconds"] > 90

    def test_started_but_no_tick_yet_is_not_alive(self):
        """Transient state during boot: the loop entered, set
        started_at, but the first tick hasn't completed."""
        from core.scheduler import (
            _campaign_dispatcher_state,
            get_campaign_dispatcher_state,
        )

        _campaign_dispatcher_state.update({
            "started_at":   datetime.now(timezone.utc),
            "last_tick_at": None,
        })
        snap = get_campaign_dispatcher_state()
        assert snap["started"] is True
        assert snap["alive"] is False  # no tick yet → not alive

    def test_rescue_counters_surface_correctly(self):
        from core.scheduler import (
            _campaign_dispatcher_state,
            get_campaign_dispatcher_state,
        )

        rescued_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        _campaign_dispatcher_state.update({
            "started_at":               datetime.now(timezone.utc) - timedelta(minutes=2),
            "last_tick_at":             datetime.now(timezone.utc),
            "last_rescue_at":           rescued_at,
            "last_rescued_campaign_ids": [42, 99],
            "rescue_invocations_total":  3,
            "rescue_campaigns_total":    7,
            "poll_seconds":              30,
        })

        snap = get_campaign_dispatcher_state()
        assert snap["last_rescued_campaign_ids"] == [42, 99]
        assert snap["rescue_invocations_total"] == 3
        assert snap["rescue_campaigns_total"] == 7
        assert isinstance(snap["last_rescue_at"], str)
        assert snap["alive"] is True

    def test_snapshot_is_a_copy_not_a_reference(self):
        """Mutating the snapshot must not corrupt the live module
        state — endpoint consumers can mangle the dict freely."""
        from core.scheduler import (
            _campaign_dispatcher_state,
            get_campaign_dispatcher_state,
        )

        _campaign_dispatcher_state.update({
            "started_at":   datetime.now(timezone.utc),
            "last_tick_at": datetime.now(timezone.utc),
            "ticks_total":  5,
        })
        snap = get_campaign_dispatcher_state()
        snap["ticks_total"] = 9999
        snap["alive"] = "tampered"

        snap2 = get_campaign_dispatcher_state()
        assert snap2["ticks_total"] == 5
        assert snap2["alive"] is True


class TestSchedulerHealthEndpoint:
    """Smoke-test the endpoint wiring + response shape. Auth is
    mocked because TestClient + JWT issuance is heavyweight for a
    diagnostic-only endpoint."""

    def test_endpoint_returns_full_shape(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import scheduler as sched

        # Pre-populate dispatcher state for the assertion.
        now = datetime.now(timezone.utc)
        sched._campaign_dispatcher_state.update({
            "started_at":               now - timedelta(minutes=2),
            "last_tick_at":             now - timedelta(seconds=3),
            "last_tick_ok":             True,
            "ticks_total":              4,
            "ticks_failed":             0,
            "last_rescue_at":           now - timedelta(seconds=30),
            "last_rescued_campaign_ids": [123],
            "rescue_invocations_total":  1,
            "rescue_campaigns_total":    1,
            "poll_seconds":              30,
            "stuck_threshold_seconds":   60,
        })

        # Stub require_admin → returns a fake admin dict.
        from core import auth as auth_mod

        def _fake_require_admin():
            return {"user_id": 1, "role": "admin"}

        monkeypatch.setattr(auth_mod, "require_admin", _fake_require_admin)

        # Re-import the router AFTER patching so the dependency
        # binding picks up our stub.
        import importlib
        from routers import admin_debug
        importlib.reload(admin_debug)

        app = FastAPI()
        app.include_router(admin_debug.router)
        client = TestClient(app)

        resp = client.get("/admin/debug/scheduler-health")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Top-level shape contract.
        assert "ts" in body
        assert "deployment" in body
        assert "kill_switches" in body
        assert "campaign_dispatcher" in body
        assert "issues" in body
        assert "hints"  in body
        assert "ok"     in body

        cd = body["campaign_dispatcher"]
        assert cd["started"] is True
        assert cd["alive"] is True
        assert cd["last_rescued_campaign_ids"] == [123]
        assert cd["rescue_invocations_total"] == 1
        assert cd["poll_seconds"] == 30
        assert cd["stuck_threshold_seconds"] == 60

        assert body["ok"] is True
        assert body["issues"] == []

    def test_endpoint_surfaces_kill_switch_as_issue(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import scheduler as sched

        # Pretend the loop is healthy but the kill switch is set —
        # the endpoint must report the kill switch as a blocking
        # issue and flip ok=False.
        sched._campaign_dispatcher_state.update({
            "started_at":   datetime.now(timezone.utc) - timedelta(minutes=2),
            "last_tick_at": datetime.now(timezone.utc),
            "poll_seconds": 30,
        })

        monkeypatch.setenv("NAHLA_DISABLE_SCHEDULERS", "1")

        from core import auth as auth_mod

        def _fake_require_admin():
            return {"user_id": 1, "role": "admin"}

        monkeypatch.setattr(auth_mod, "require_admin", _fake_require_admin)

        import importlib
        from routers import admin_debug
        importlib.reload(admin_debug)

        app = FastAPI()
        app.include_router(admin_debug.router)
        client = TestClient(app)

        resp = client.get("/admin/debug/scheduler-health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kill_switches"]["NAHLA_DISABLE_SCHEDULERS_active"] is True
        assert body["ok"] is False
        assert any(
            "NAHLA_DISABLE_SCHEDULERS" in i for i in body["issues"]
        )
