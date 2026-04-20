"""
tests/test_cart_recovery_retry.py
─────────────────────────────────
Pin the contract of the temporary, feature-flagged manual retry button:

  POST /autopilot/abandoned-carts/{order_id}/retry

Why these tests matter
──────────────────────
The retry endpoint exists to unblock merchants while the new structured-
failure pipeline burns in. It must:

  * Be **invisible** when the env flag is off (403, not 404 — the
    dashboard distinguishes "feature disabled" from "wrong route").
  * Refuse to retry when the cart is already converted, has no recovery
    event linked, or has no failed step.
  * Be idempotent against double-clicks (same step within 60s collapses
    to the existing pending event).
  * NOT bypass the engine — the new event lands in the same queue as
    a sweeper-emitted follow-up so all the existing pre-send guards
    (already-purchased, opt-out, quiet hours) still protect the send.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import (  # noqa: E402
    AutomationEvent,
    AutomationExecution,
    Base,
    Customer,
    Order,
    SmartAutomation,
    Tenant,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


# ── Test rig: spin a real FastAPI app + sqlite in-memory DB so the
#    endpoint exercises the real router path (decorators, dependencies,
#    pydantic validation), not a hand-rolled call. ────────────────────────────
def _build_client(*, retry_flag: str | None):
    """Return (TestClient, db_session_factory). The flag arg controls
    whether AUTOPILOT_ENABLE_MANUAL_RETRY is set in the environment
    when the router is imported — we want to test both states.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    if retry_flag is None:
        os.environ.pop("AUTOPILOT_ENABLE_MANUAL_RETRY", None)
    else:
        os.environ["AUTOPILOT_ENABLE_MANUAL_RETRY"] = retry_flag

    # Force a fresh import of the router after the env is set, since
    # _manual_retry_enabled is a function read at request time — the
    # restart-vs-runtime distinction matters: we WANT the env to be
    # read on every call, not cached at import.
    if "routers.automations" in sys.modules:
        # Reload so the test for "flag off" doesn't see the previous
        # test's env value. The function reads os.getenv per call so
        # this is belt-and-braces.
        import importlib
        importlib.reload(sys.modules["routers.automations"])
    import routers.automations as automations_router  # noqa: E402

    # ``check_same_thread=False`` + ``StaticPool`` so the in-memory
    # DB is shared between the test main thread (seeding) and the
    # TestClient worker thread (request handling). Without this, the
    # request would see an empty schema and SQLite would raise
    # "SQLite objects created in a thread can only be used in that
    # same thread" on rollback.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Make sure tenant 1 always exists, even for tests that don't go
    # through ``_seed_failed_cart`` (e.g. the /autopilot/status checks).
    # Otherwise ``get_or_create_tenant`` raises 404.
    _bootstrap = Session()
    if not _bootstrap.query(Tenant).filter_by(id=1).first():
        _bootstrap.add(Tenant(id=1, name="retry test", is_active=True))
        _bootstrap.commit()
    _bootstrap.close()

    # Override get_db so the test rig writes to our in-memory DB.
    from core.database import get_db

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()

    # ``resolve_tenant_id`` (called directly inside the route, not as
    # a Depends) reads ``request.state.tenant_id``, which is normally
    # set by ``multi_tenant_middleware`` from the X-Tenant-ID header.
    # We don't want to import the real auth stack here — just stamp
    # the attribute on every request so the route resolves to tenant 1.
    @app.middleware("http")
    async def _stamp_tenant(request, call_next):
        request.state.tenant_id = 1
        return await call_next(request)

    app.include_router(automations_router.router)
    app.dependency_overrides[get_db] = _get_db

    client = TestClient(app)
    return client, Session


def _seed_failed_cart(Session, *, status: str = "failed", converted: bool = False):
    """Seed a tenant + customer + automation + abandoned order + a
    cart_abandoned event whose latest execution is failed (or whatever
    ``status`` argument requests).

    Returns ``(order_id, root_event_id)`` — primitives, not ORM
    instances, so the test body can close the seeding session without
    triggering DetachedInstanceError on attribute access.
    """
    db = Session()
    # Tenant 1 is bootstrapped by ``_build_client`` so the /autopilot/*
    # routes resolve cleanly. Don't re-insert it here.

    cust = Customer(tenant_id=1, name="عميل", phone="+966500111222")
    db.add(cust)
    db.commit()
    db.refresh(cust)

    auto = SmartAutomation(
        tenant_id=1,
        automation_type="abandoned_cart",
        name="Cart recovery",
        enabled=True,
        engine="advanced",
        config={"steps": [{"enabled": True, "delay_minutes": 30}]},
        trigger_event="cart_abandoned",
    )
    db.add(auto)
    db.commit()
    db.refresh(auto)

    payload = {
        "step_idx": 1,
        "cart_id": "abc",
        "phone": "+966500111222",
    }
    if converted:
        payload["recovery_converted_at"] = datetime.now(timezone.utc).isoformat()
        payload["recovery_cancel_reason"] = "customer_purchased"

    ev = AutomationEvent(
        tenant_id=1,
        event_type="cart_abandoned",
        customer_id=cust.id,
        payload=payload,
        processed=True,
        automation_id=auto.id,
        created_at=datetime.utcnow() - timedelta(minutes=10),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)

    order = Order(
        tenant_id=1,
        external_id="cart-abc",
        status="abandoned",
        customer_name="عميل",
        total=100.0,
        is_abandoned=True,
        extra_metadata={"recovery_event_id": ev.id, "abandoned_at": "2026-04-20T10:00:00+00:00"},
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    if status:
        ex = AutomationExecution(
            tenant_id=1,
            automation_id=auto.id,
            event_id=ev.id,
            customer_id=cust.id,
            status=status,
            error_message="template_not_approved" if status == "failed" else None,
            action_taken={
                "error":       "template_not_approved",
                "error_code":  "template_not_approved",
                "error_label": "القالب غير معتمد من Meta",
                "template":    "abandoned_cart_recovery_ar",
                "to":          "+966500111222",
            } if status == "failed" else {},
        )
        db.add(ex)
        db.commit()

    order_id = order.id
    event_id = ev.id
    db.close()
    return order_id, event_id


# ── Feature flag: OFF ────────────────────────────────────────────────────────
def test_retry_returns_403_when_feature_flag_disabled():
    client, Session = _build_client(retry_flag=None)
    order_id, _ = _seed_failed_cart(Session)

    resp = client.post(f"/autopilot/abandoned-carts/{order_id}/retry")
    assert resp.status_code == 403
    body = resp.json()
    detail = body.get("detail") or {}
    assert detail.get("error") == "manual_retry_disabled"
    assert "AUTOPILOT_ENABLE_MANUAL_RETRY" in detail.get("message", "")


def test_retry_returns_403_when_flag_explicitly_false():
    client, Session = _build_client(retry_flag="false")
    order_id, _ = _seed_failed_cart(Session)

    resp = client.post(f"/autopilot/abandoned-carts/{order_id}/retry")
    assert resp.status_code == 403


# ── Feature flag: ON ─────────────────────────────────────────────────────────
def test_retry_enqueues_new_event_for_failed_cart():
    """Happy path: feature on + cart has a failed execution → the
    endpoint creates a brand-new unprocessed AutomationEvent linked to
    the original root, with manual_retry=True and the same step_idx."""
    client, Session = _build_client(retry_flag="true")
    order_id, root_id = _seed_failed_cart(Session)

    resp = client.post(f"/autopilot/abandoned-carts/{order_id}/retry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["deduplicated"] is False
    assert body["step_idx"] == 1
    assert body["retry_event_id"] != root_id

    # Verify the new event row exists and carries the manual-retry markers.
    db = Session()
    new_event = db.query(AutomationEvent).filter(
        AutomationEvent.id == body["retry_event_id"],
    ).first()
    assert new_event is not None
    assert new_event.processed is False
    payload = new_event.payload or {}
    assert payload.get("manual_retry") is True
    assert int(payload.get("parent_event_id")) == root_id
    assert int(payload.get("step_idx")) == 1
    assert payload.get("retry_of") == root_id
    db.close()


def test_retry_idempotent_within_60s_window():
    """Two clicks within the 60s cooldown collapse to a single retry."""
    client, Session = _build_client(retry_flag="true")
    order_id, _ = _seed_failed_cart(Session)

    first = client.post(f"/autopilot/abandoned-carts/{order_id}/retry").json()
    second = client.post(f"/autopilot/abandoned-carts/{order_id}/retry").json()

    assert first["ok"] is True
    assert first["deduplicated"] is False
    assert second["ok"] is True
    assert second["deduplicated"] is True
    assert second["retry_event_id"] == first["retry_event_id"]


def test_retry_refuses_converted_cart():
    """If the customer already purchased we MUST NOT re-nag — even
    when the operator clicks retry."""
    client, Session = _build_client(retry_flag="true")
    order_id, _ = _seed_failed_cart(Session, converted=True, status="failed")

    resp = client.post(f"/autopilot/abandoned-carts/{order_id}/retry")
    assert resp.status_code == 409
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "already_converted"


def test_retry_refuses_when_no_failed_step():
    """Cart with only a 'sent' execution is a happy path — no retry."""
    client, Session = _build_client(retry_flag="true")
    order_id, _ = _seed_failed_cart(Session, status="sent")

    resp = client.post(f"/autopilot/abandoned-carts/{order_id}/retry")
    assert resp.status_code == 409
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "no_failed_step"


def test_retry_returns_404_for_other_tenants_orders():
    client, Session = _build_client(retry_flag="true")
    order_id, _ = _seed_failed_cart(Session)

    # Order id that doesn't exist for tenant 1.
    resp = client.post(f"/autopilot/abandoned-carts/{order_id + 9999}/retry")
    assert resp.status_code == 404


def test_retry_refuses_when_no_recovery_event_linked():
    """Cart with no recovery_event_id in metadata → 409 with the
    'no_recovery_event' code."""
    client, Session = _build_client(retry_flag="true")
    db = Session()
    # Tenant 1 already bootstrapped by _build_client.
    order = Order(
        tenant_id=1,
        external_id="cart-no-event",
        status="abandoned",
        customer_name="عميل",
        total=100.0,
        is_abandoned=True,
        extra_metadata={},  # no recovery_event_id
    )
    db.add(order)
    db.commit()
    order_id = order.id
    db.close()

    resp = client.post(f"/autopilot/abandoned-carts/{order_id}/retry")
    assert resp.status_code == 409
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "no_recovery_event"


# ── Status endpoint: surfaces the flag for the frontend ─────────────────────
def test_autopilot_status_exposes_manual_retry_flag_on():
    client, _ = _build_client(retry_flag="true")
    resp = client.get("/autopilot/status")
    assert resp.status_code == 200
    assert resp.json()["manual_retry_enabled"] is True


def test_autopilot_status_exposes_manual_retry_flag_off_by_default():
    client, _ = _build_client(retry_flag=None)
    resp = client.get("/autopilot/status")
    assert resp.status_code == 200
    assert resp.json()["manual_retry_enabled"] is False
