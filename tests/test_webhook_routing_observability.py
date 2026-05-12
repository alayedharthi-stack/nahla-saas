"""
tests/test_webhook_routing_observability.py
───────────────────────────────────────────
Locks F19 — the read-only diagnostic for 360dialog webhook routing.

What this catches
─────────────────
The "customer sent a message but it doesn't appear in Nahla" failure
mode. Pre-F19 the routing path inside ``_handle_360dialog_body`` had
six silent drop points (missing phone_id, unknown phone_id,
ambiguous, wrong provider, bad secret, scope mismatch). All six
returned HTTP 200 to 360dialog and left ``last_webhook_received_at``
untouched — so neither the merchant nor support could distinguish
"webhook fired" from "webhook landed nowhere".

F19 wires every routing decision into a process-local ring buffer
and exposes it via ``GET /admin/debug/recent-webhook-events``. Test
strategy:

* Drive the helpers in ``core.wa_webhook_observability`` directly —
  ``record_event`` / ``get_recent_events`` / ``get_route_status_counts``
  / ``get_distinct_payload_phone_ids``.
* Drive the endpoint by calling the FastAPI handler in-process with
  a fake admin payload (same pattern as F17 / F18).
* Smoke-test the live integration with ``_handle_360dialog_body`` —
  the wiring inside ``whatsapp_webhook.py`` writes the same records
  the module-level helpers see. We pre-seed a WhatsAppConnection,
  fire one matched + one unmatched payload, and assert both land in
  the ring.

The actual provider POST is NOT exercised here — that's F18's
suite.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_ring():
    """Module-level state must be wiped between tests so ordering /
    count assertions remain deterministic."""
    from core.wa_webhook_observability import reset_for_tests
    reset_for_tests()
    yield
    reset_for_tests()


# ── core.wa_webhook_observability ──────────────────────────────────


class TestRecordAndRead:
    def test_empty_buffer_returns_empty_list(self):
        from core.wa_webhook_observability import get_recent_events
        assert get_recent_events() == []
        assert get_recent_events(tenant_id=33) == []

    def test_record_then_read_newest_first(self):
        from core.wa_webhook_observability import (
            get_recent_events, record_event, ROUTE_MATCHED,
        )
        for i in range(3):
            record_event(
                scope="channel", field="messages",
                phone_number_id_from_payload="100543193146977",
                display_phone_number="+966500000000",
                matched_tenant_id=33,
                matched_connection_id=42,
                matched_phone_number_id="100543193146977",
                route_status=ROUTE_MATCHED,
                messages_count=i,
            )
        out = get_recent_events()
        assert len(out) == 3
        # Newest first.
        assert out[0]["messages_count"] == 2
        assert out[-1]["messages_count"] == 0

    def test_filter_by_tenant_includes_unrouted_too(self):
        """Routed events for the target tenant AND unrouted events
        (which can't be assigned a tenant) must both appear, so the
        operator sees the failures alongside the successes."""
        from core.wa_webhook_observability import (
            get_recent_events, record_event,
            ROUTE_MATCHED, ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="100543193146977",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="100543193146977",
            route_status=ROUTE_MATCHED,
        )
        # Another tenant's routed event — should NOT appear.
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="200000000000000",
            display_phone_number=None,
            matched_tenant_id=99, matched_connection_id=99,
            matched_phone_number_id="200000000000000",
            route_status=ROUTE_MATCHED,
        )
        # Unrouted — no tenant id; should appear in EVERY filter.
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="999999999999999",
            display_phone_number=None,
            matched_tenant_id=None, matched_connection_id=None,
            matched_phone_number_id=None,
            route_status=ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        out = get_recent_events(tenant_id=33)
        tids = [e.get("matched_tenant_id") for e in out]
        assert 33 in tids
        assert None in tids  # unrouted included
        assert 99 not in tids

    def test_filter_by_phone_number_id_matches_payload_or_connection(self):
        from core.wa_webhook_observability import (
            get_recent_events, record_event, ROUTE_MATCHED,
        )
        # Payload phone_id differs from connection's stored phone_id —
        # this is the drift signal the F19 endpoint highlights.
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="1061057720431678",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="100543193146977",
            route_status=ROUTE_MATCHED,
        )
        # Query by the OLD phone id (connection's stored one).
        out_old = get_recent_events(phone_number_id="100543193146977")
        assert len(out_old) == 1
        # Query by the NEW phone id (payload's actual delivery).
        out_new = get_recent_events(phone_number_id="1061057720431678")
        assert len(out_new) == 1
        # Both queries return the SAME event.
        assert out_old[0]["ts"] == out_new[0]["ts"]

    def test_minutes_window_excludes_old_events(self):
        import time as _time
        from core.wa_webhook_observability import (
            _EVENTS, _LOCK, get_recent_events, record_event, ROUTE_MATCHED,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="X",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="X",
            route_status=ROUTE_MATCHED,
        )
        # Backdate the only entry by 2 hours. We hold the lock so the
        # mutation is well-defined even though there are no other
        # writers in a test.
        with _LOCK:
            for e in _EVENTS:
                e["ts"] = _time.time() - 2 * 3600
        out = get_recent_events(minutes=30)
        assert out == []
        out2 = get_recent_events(minutes=180)
        assert len(out2) == 1


class TestPhoneIdDrift:
    def test_distinct_phone_ids_counts_each_unique_value(self):
        from core.wa_webhook_observability import (
            get_distinct_payload_phone_ids, record_event, ROUTE_MATCHED,
        )
        for i in range(3):
            record_event(
                scope="channel", field="messages",
                phone_number_id_from_payload="100543193146977",
                display_phone_number=None,
                matched_tenant_id=33, matched_connection_id=42,
                matched_phone_number_id="100543193146977",
                route_status=ROUTE_MATCHED,
            )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="1061057720431678",
            display_phone_number=None,
            matched_tenant_id=None, matched_connection_id=None,
            matched_phone_number_id=None,
            route_status="unrouted_unknown_phone_id",
        )
        out = get_distinct_payload_phone_ids()
        assert out == {
            "100543193146977":  3,
            "1061057720431678": 1,
        }

    def test_phone_id_mismatch_flag_set_when_payload_neq_connection(self):
        from core.wa_webhook_observability import (
            get_recent_events, record_event, ROUTE_MATCHED,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="A",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="B",
            route_status=ROUTE_MATCHED,
        )
        e = get_recent_events()[0]
        assert e["phone_id_mismatch"] is True

    def test_phone_id_mismatch_flag_false_when_equal(self):
        from core.wa_webhook_observability import (
            get_recent_events, record_event, ROUTE_MATCHED,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="A",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="A",
            route_status=ROUTE_MATCHED,
        )
        e = get_recent_events()[0]
        assert e["phone_id_mismatch"] is False


class TestRouteStatusCounts:
    def test_aggregates_per_status(self):
        from core.wa_webhook_observability import (
            get_route_status_counts, record_event,
            ROUTE_MATCHED, ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
            ROUTE_UNROUTED_BAD_SECRET,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="X",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="X",
            route_status=ROUTE_MATCHED,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="Y",
            display_phone_number=None,
            matched_tenant_id=None, matched_connection_id=None,
            matched_phone_number_id=None,
            route_status=ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="X",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="X",
            route_status=ROUTE_UNROUTED_BAD_SECRET,
        )
        c = get_route_status_counts(tenant_id=33)
        assert c.get(ROUTE_MATCHED) == 1
        # Unrouted always shared in the tenant view.
        assert c.get(ROUTE_UNROUTED_UNKNOWN_PHONE_ID) == 1
        assert c.get(ROUTE_UNROUTED_BAD_SECRET) == 1


# ── GET /admin/debug/recent-webhook-events ─────────────────────────


def _call_endpoint(*, tenant_id=None, phone_number_id=None, minutes=30, limit=100):
    from routers.admin_debug import admin_debug_recent_webhook_events
    return _run(admin_debug_recent_webhook_events(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        minutes=minutes,
        limit=limit,
        _admin={"sub": "admin@nahla", "role": "admin"},
    ))


class TestRecentWebhookEventsEndpoint:
    def test_empty_buffer_returns_hint(self):
        resp = _call_endpoint()
        assert resp["events"] == []
        assert resp["events_returned"] == 0
        assert any("webhooks" in h or "process" in h for h in resp["hints"])
        # ok=True when no issues AND no matched events (idle state).
        assert resp["ok"] is True

    def test_phone_id_drift_surfaces_as_issue(self):
        from core.wa_webhook_observability import (
            record_event, ROUTE_MATCHED, ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        # Old phone_id (still on connection)
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="100543193146977",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="100543193146977",
            route_status=ROUTE_MATCHED,
        )
        # New phone_id (drifted) — dropped.
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="1061057720431678",
            display_phone_number=None,
            matched_tenant_id=None, matched_connection_id=None,
            matched_phone_number_id=None,
            route_status=ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        resp = _call_endpoint()
        assert resp["phone_id_drift_detected"] is True
        assert "100543193146977" in resp["distinct_payload_phone_ids"]
        assert "1061057720431678" in resp["distinct_payload_phone_ids"]
        # Specifically warns about the dual-phone-id situation.
        assert any("phone_number_id" in i for i in resp["issues"])
        assert resp["ok"] is False

    def test_unknown_phone_id_surfaces_as_issue(self):
        from core.wa_webhook_observability import (
            record_event, ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="999",
            display_phone_number=None,
            matched_tenant_id=None, matched_connection_id=None,
            matched_phone_number_id=None,
            route_status=ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        resp = _call_endpoint()
        # The dedicated counter for this status fires.
        assert resp["route_status_counts"].get("unrouted_unknown_phone_id") == 1
        assert any("phone_number_id" in i for i in resp["issues"])
        assert resp["ok"] is False

    def test_all_matched_with_no_issues_is_ok_true(self):
        from core.wa_webhook_observability import record_event, ROUTE_MATCHED
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="100543193146977",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="100543193146977",
            route_status=ROUTE_MATCHED,
        )
        resp = _call_endpoint()
        assert resp["ok"] is True
        assert resp["events_returned"] == 1
        assert resp["events"][0]["route_status"] == "matched"

    def test_bad_secret_surfaces_as_issue(self):
        from core.wa_webhook_observability import (
            record_event, ROUTE_UNROUTED_BAD_SECRET, SECRET_MISMATCH,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="100543193146977",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="100543193146977",
            route_status=ROUTE_UNROUTED_BAD_SECRET,
            secret_check=SECRET_MISMATCH,
        )
        resp = _call_endpoint()
        assert resp["route_status_counts"].get("unrouted_bad_secret") == 1
        assert any("secret" in i.lower() or "X-Nahla" in i for i in resp["issues"])

    def test_all_unrouted_surfaces_top_level_issue(self):
        from core.wa_webhook_observability import (
            record_event, ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        for _ in range(3):
            record_event(
                scope="channel", field="messages",
                phone_number_id_from_payload="999",
                display_phone_number=None,
                matched_tenant_id=None, matched_connection_id=None,
                matched_phone_number_id=None,
                route_status=ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
            )
        resp = _call_endpoint()
        # When NOTHING was routed, the response calls it out
        # explicitly so the operator doesn't have to count.
        assert any(
            "routing" in i.lower() or "inbound" in i.lower()
            or "فشل routing" in i for i in resp["issues"]
        )

    def test_tenant_filter_restricts_matched_but_keeps_unrouted(self):
        from core.wa_webhook_observability import (
            record_event, ROUTE_MATCHED, ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="A",
            display_phone_number=None,
            matched_tenant_id=33, matched_connection_id=42,
            matched_phone_number_id="A",
            route_status=ROUTE_MATCHED,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="B",
            display_phone_number=None,
            matched_tenant_id=99, matched_connection_id=99,
            matched_phone_number_id="B",
            route_status=ROUTE_MATCHED,
        )
        record_event(
            scope="channel", field="messages",
            phone_number_id_from_payload="C",
            display_phone_number=None,
            matched_tenant_id=None, matched_connection_id=None,
            matched_phone_number_id=None,
            route_status=ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
        )
        resp = _call_endpoint(tenant_id=33)
        tids = [e.get("matched_tenant_id") for e in resp["events"]]
        assert 33 in tids
        assert None in tids       # unrouted always included
        assert 99 not in tids

    def test_limit_clamps_events_returned(self):
        from core.wa_webhook_observability import record_event, ROUTE_MATCHED
        for i in range(10):
            record_event(
                scope="channel", field="messages",
                phone_number_id_from_payload="X",
                display_phone_number=None,
                matched_tenant_id=33, matched_connection_id=42,
                matched_phone_number_id="X",
                route_status=ROUTE_MATCHED,
                messages_count=i,
            )
        resp = _call_endpoint(limit=3)
        assert len(resp["events"]) == 3
        # Newest first.
        assert resp["events"][0]["messages_count"] == 9


# ── Live wiring: _handle_360dialog_body integration smoke ──────────


def _make_db_with_connection(
    *,
    tenant_id=33,
    phone_number_id="100543193146977",
    coexistence_secret="shared_secret_test",
):
    """In-memory SQLite that downgrades JSONB columns to JSON so we
    can spin up a real WhatsAppConnection row without Postgres."""
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base, Tenant, WhatsAppConnection

    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    db = Session()

    t = Tenant(id=tenant_id, name=f"tenant-{tenant_id}")
    db.add(t); db.commit()
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        provider="dialog360",
        connection_type="coexistence",
        status="connected",
        phone_number_id=phone_number_id,
        access_token="d360_fake",
        extra_metadata={"coexistence_internal_secret": coexistence_secret},
    )
    db.add(conn); db.commit()
    return Session, db


class TestLiveWiring:
    """Verify that the actual ``_handle_360dialog_body`` writes
    records via the same recorder our endpoint reads.

    We mock out the downstream message dispatcher (``_dispatch_message``)
    so the test doesn't have to stand up the full inbound pipeline —
    we only care about the routing-decision record."""

    def test_matched_payload_writes_matched_event(self):
        SessionLocalFactory, _seeded = _make_db_with_connection(
            phone_number_id="100543193146977",
            coexistence_secret="my_secret",
        )

        from core.wa_webhook_observability import get_recent_events

        with patch(
            "routers.whatsapp_webhook.SessionLocal",
            SessionLocalFactory,
        ), patch(
            "routers.whatsapp_webhook._dispatch_message",
            new=lambda *a, **kw: _async_noop(),
        ):
            from routers.whatsapp_webhook import _handle_360dialog_body

            body = {
                "entry": [{
                    "changes": [{
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": "100543193146977",
                                "display_phone_number": "+966500000000",
                            },
                            "messages": [{
                                "id": "wamid.INBOUND_1",
                                "from": "966537970430",
                                "type": "text",
                                "text": {"body": "مرحبا"},
                            }],
                        },
                    }],
                }],
            }
            _run(_handle_360dialog_body(
                body, {"x_nahla_coexistence_secret": "my_secret"}, scope="channel",
            ))

        events = get_recent_events(tenant_id=33)
        assert len(events) >= 1
        # The matched event for our tenant should be present.
        matched = [e for e in events if e["route_status"] == "matched"]
        assert len(matched) == 1
        e = matched[0]
        assert e["matched_tenant_id"] == 33
        assert e["phone_number_id_from_payload"] == "100543193146977"
        assert e["matched_phone_number_id"]      == "100543193146977"
        assert e["phone_id_mismatch"] is False
        assert e["messages_count"] == 1

    def test_unknown_phone_id_writes_unrouted_event(self):
        """The exact failure mode F19 was built for: webhook arrives
        with a phone_id that does NOT match any connection row."""
        SessionLocalFactory, _seeded = _make_db_with_connection(
            phone_number_id="100543193146977",
            coexistence_secret="my_secret",
        )

        from core.wa_webhook_observability import get_recent_events

        with patch(
            "routers.whatsapp_webhook.SessionLocal",
            SessionLocalFactory,
        ):
            from routers.whatsapp_webhook import _handle_360dialog_body

            body = {
                "entry": [{
                    "changes": [{
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": "1061057720431678",  # DIFFERENT
                                "display_phone_number": "+966500000000",
                            },
                            "messages": [{
                                "id": "wamid.UNROUTED_1",
                                "from": "966500000001",
                                "type": "text",
                                "text": {"body": "hi"},
                            }],
                        },
                    }],
                }],
            }
            _run(_handle_360dialog_body(body, {}, scope="channel"))

        events = get_recent_events()
        statuses = [e["route_status"] for e in events]
        assert "unrouted_unknown_phone_id" in statuses
        unrouted = [
            e for e in events
            if e["route_status"] == "unrouted_unknown_phone_id"
        ][0]
        assert unrouted["phone_number_id_from_payload"] == "1061057720431678"
        # An inbound message was on the dropped delivery.
        assert unrouted["messages_count"] == 1


async def _async_noop(*args, **kwargs):  # noqa: D401
    """Awaitable no-op for ``_dispatch_message`` patch."""
    return None
