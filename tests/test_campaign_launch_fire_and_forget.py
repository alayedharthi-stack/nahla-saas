"""
tests/test_campaign_launch_fire_and_forget.py
─────────────────────────────────────────────
Locks the contract that large-campaign launches are TRULY
fire-and-forget — the HTTP request that creates / kicks a campaign
must never wait for the dispatcher's synchronous prep phase
(audience materialisation, frequency cap, funnel commit) to finish.

Production bug this protects against
─────────────────────────────────────
``dispatch_campaign`` is declared ``async`` but its first ~30% is
*synchronous* DB work (``_snapshot_recipients`` + ``_apply_frequency_cap``
+ funnel ``db.commit()``) — there is no ``await`` until deep inside
``_dispatch_queued_rows``. When that task was scheduled with
``asyncio.create_task`` on the uvicorn event loop, the loop was the
same loop responsible for flushing the HTTP response back to nginx;
the synchronous prep monopolised the loop and for ~8000-customer
audiences the wizard hit its 25 s ``AbortSignal.timeout`` —
``signal timed out`` — even though the campaign row was already
created and the dispatch was running fine in the background.

The fix in ``routers/campaigns.py`` introduces
``_spawn_dispatch_in_background`` which runs the dispatcher on a
dedicated daemon thread with its own ``asyncio.run`` event loop, so
the request event loop is freed the instant we return.

These tests assert:
  1. ``_spawn_dispatch_in_background`` returns immediately even if
     the dispatcher blocks for far longer than the frontend's 25 s
     timeout.
  2. It does NOT use ``asyncio.create_task`` on the calling event
     loop — i.e. a running loop is never required and the helper is
     safe to call from synchronous endpoints.
  3. The spawned thread eventually executes ``_dispatch_campaign_async``
     with the right ``campaign_id`` (so the fire-and-forget contract
     also actually fires).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ── 1) Returns instantly even when dispatcher blocks for >25s ──────────


def test_spawn_returns_immediately_when_dispatcher_is_slow(monkeypatch):
    """The whole point: the wizard's POST must return in <100ms
    even when the dispatcher's synchronous prep phase would block
    the request thread for far longer than the 25 s frontend
    timeout. We simulate that by patching
    ``_dispatch_campaign_async`` with a coroutine that sleeps for
    30 s, and we time how long ``_spawn_dispatch_in_background``
    takes to return on the caller."""
    from routers import campaigns as campaigns_mod

    started_evt = threading.Event()
    can_finish = threading.Event()

    async def _slow_dispatch(cid: int):
        started_evt.set()
        # Block the inner thread loop until the test releases it —
        # in production this represents the 8000-row snapshot +
        # frequency cap that runs before the first ``await``.
        while not can_finish.is_set():
            time.sleep(0.01)

    monkeypatch.setattr(
        campaigns_mod, "_dispatch_campaign_async", _slow_dispatch
    )

    t0 = time.monotonic()
    campaigns_mod._spawn_dispatch_in_background(12345)
    elapsed = time.monotonic() - t0

    try:
        # The helper itself MUST be effectively instant — the
        # whole budget is busy thread + Thread.start(), tens of
        # ms at most. Anything close to a second indicates the
        # caller is being blocked by the dispatcher (regression).
        assert elapsed < 0.5, (
            f"_spawn_dispatch_in_background took {elapsed:.3f}s — "
            f"caller is being blocked, fire-and-forget is broken"
        )

        # And the background thread actually started running the
        # dispatcher (otherwise we'd have a silently-dropped task).
        assert started_evt.wait(timeout=3.0), (
            "background dispatch thread never started running "
            "_dispatch_campaign_async"
        )
    finally:
        # Let the daemon thread exit cleanly so pytest doesn't
        # leak workers between tests.
        can_finish.set()


# ── 2) Helper doesn't require a running event loop ────────────────────


def test_spawn_does_not_require_a_running_event_loop(monkeypatch):
    """``asyncio.create_task`` raises ``RuntimeError: no running
    event loop`` when called outside an async context. The new
    helper uses a thread + ``asyncio.run``, which works from any
    synchronous caller. This test calls the helper from plain
    synchronous test code (no event loop running) and asserts no
    exception escapes."""
    from routers import campaigns as campaigns_mod

    seen = threading.Event()

    async def _noop_dispatch(cid: int):
        seen.set()

    monkeypatch.setattr(
        campaigns_mod, "_dispatch_campaign_async", _noop_dispatch
    )

    # If the helper still relied on ``asyncio.create_task`` we'd
    # get RuntimeError here — there's no running loop in pytest.
    campaigns_mod._spawn_dispatch_in_background(999)

    assert seen.wait(timeout=3.0), (
        "_dispatch_campaign_async was never invoked by the background "
        "thread — fire-and-forget contract not honoured"
    )


# ── 3) Correct campaign_id is forwarded ───────────────────────────────


def test_spawn_forwards_campaign_id_to_dispatcher(monkeypatch):
    """Regression guard: if someone refactors the helper, the
    campaign_id must still reach _dispatch_campaign_async — a
    silent off-by-one here would mean the wizard "launched" a
    different (or no) campaign."""
    from routers import campaigns as campaigns_mod

    received: list[int] = []
    done = threading.Event()

    async def _capture_dispatch(cid: int):
        received.append(cid)
        done.set()

    monkeypatch.setattr(
        campaigns_mod, "_dispatch_campaign_async", _capture_dispatch
    )

    campaigns_mod._spawn_dispatch_in_background(42)

    assert done.wait(timeout=3.0), "dispatcher never ran"
    assert received == [42], (
        f"dispatcher received {received!r} but the wizard launched "
        f"campaign_id=42 — campaign_id was dropped or mutated"
    )


# ── 4) Exceptions on the inner thread don't crash the helper ──────────


def test_spawn_swallows_inner_thread_exceptions(monkeypatch, caplog):
    """The daemon thread runs on its own event loop with its own
    SessionLocal — if it raises, that exception has nowhere to
    propagate. The helper MUST log it (so support can debug) but
    MUST NOT raise back into the calling HTTP handler.
    Otherwise a broken dispatcher would surface as a 500 on the
    POST /campaigns even though the row was already created."""
    import logging

    from routers import campaigns as campaigns_mod

    crashed = threading.Event()

    async def _exploding_dispatch(cid: int):
        crashed.set()
        raise RuntimeError("simulated dispatcher crash")

    monkeypatch.setattr(
        campaigns_mod, "_dispatch_campaign_async", _exploding_dispatch
    )

    with caplog.at_level(logging.ERROR, logger="nahla-backend"):
        # No exception should escape — this is the whole contract.
        campaigns_mod._spawn_dispatch_in_background(7)
        assert crashed.wait(timeout=3.0)
        # Give the thread a beat to write its log line.
        time.sleep(0.2)

    assert any(
        "background dispatch thread crashed" in rec.message
        and "campaign=7" in rec.message
        for rec in caplog.records
    ), (
        "expected an error log for the inner-thread crash so support "
        "can correlate it with the campaign"
    )
