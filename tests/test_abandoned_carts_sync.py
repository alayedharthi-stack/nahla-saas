"""
tests/test_abandoned_carts_sync.py
──────────────────────────────────
End-to-end regression tests for the abandoned-cart pipeline that was the
root cause of the "Salla shows 2 carts, Nahla shows 0" merchant-facing
bug.

Until this fix Nahla had NO code path that fetched abandoned carts from
Salla:
  • SallaAdapter only had get_orders() — Salla's /orders endpoint never
    returns abandoned carts (those live behind /admin/v2/carts).
  • StoreSyncService had no sync_abandoned_carts() method.
  • The webhook dispatcher had no handler for the abandoned.cart event.
  • The dashboard query (Order.is_abandoned == True) was therefore always
    empty on prod regardless of how many carts the merchant abandoned in
    Salla.

These tests pin every stage of the new pipeline:

  1. ``test_sync_persists_abandoned_carts_so_dashboard_shows_them``
     Salla returns 2 carts → adapter → sync → DB → dashboard query
     mirrors what the SmartAutomations page reads. The dashboard MUST
     surface both rows.

  2. ``test_zero_result_sync_does_not_wipe_existing_carts``
     Silent-fail guard: if Salla returns zero carts but DB already had
     rows, KEEP them. A transient empty-page response must never wipe
     the dashboard.

  3. ``test_sync_reconciles_resumed_carts``
     A cart that no longer appears in Salla's response (customer either
     resumed it or Salla aged it out) is flipped back to
     ``is_abandoned=False`` so the dashboard count tracks Salla's
     live state.

  4. ``test_cart_id_namespace_does_not_collide_with_orders``
     Salla cart and order id-spaces are independent integers. A real
     order with id=12345 must NOT overwrite the cart with the same
     numeric id (cart row is stored as ``cart-12345``).

  5. ``test_webhook_handler_upserts_cart_into_orders_table``
     Real-time ``abandoned.cart`` Salla webhook lands the cart in the
     same table the dashboard reads from.

  6. ``test_dashboard_filter_does_not_hide_valid_carts``
     The /autopilot/queues query is the single source of truth for the
     dashboard. It must surface every is_abandoned=True row for the
     current tenant — no extra hidden filters.

  7. ``test_normaliser_uses_cart_prefixed_external_id``
     Pure-function check on _normalise_abandoned_cart so the namespace
     contract can never silently regress.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Order, Tenant  # noqa: E402
from services.store_sync import (  # noqa: E402
    StoreSyncService,
    _normalise_abandoned_cart,
)


# SQLite test DB needs JSONB → JSON remap (same trick the existing suite uses).
@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Cart Sync Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


class _StubAdapter:
    """Minimal adapter implementing the get_abandoned_carts contract.

    Returns a configurable list of raw cart dicts without touching the
    network. Raises if ``raise_with`` is set so we can test the silent-fail
    guard separately.
    """
    platform = "salla"

    def __init__(self, carts: List[Dict[str, Any]] | None = None, raise_with: Exception | None = None):
        self._carts = carts or []
        self._raise = raise_with

    async def get_abandoned_carts(self) -> List[Dict[str, Any]]:
        if self._raise is not None:
            raise self._raise
        return list(self._carts)


def _attach_adapter(svc: StoreSyncService, adapter: Any) -> None:
    """Bypass the lazy-loader so unit tests don't need the full Salla stack."""
    svc._adapter = adapter


def _sample_cart(cart_id: str, total: str = "248.00", phone: str = "+966555111222") -> Dict[str, Any]:
    return {
        "id":           cart_id,
        "total":        {"amount": total, "currency": "SAR"},
        "customer":     {"name": f"عميل {cart_id}", "mobile": phone},
        "items":        [{"product_id": "p-1", "name": "حذاء أحمر", "quantity": 1}],
        "checkout_url": f"https://store.example/cart/{cart_id}/resume",
        "created_at":   "2026-04-19 10:00:00",
    }


def _query_dashboard_carts(db, tenant_id: int) -> List[Order]:
    """Mirrors the exact filter used by /autopilot/queues."""
    return (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.is_abandoned == True)  # noqa: E712
        .order_by(Order.id.desc())
        .all()
    )


# ── 1. Happy path: Salla → DB → dashboard ────────────────────────────────────

def test_sync_persists_abandoned_carts_so_dashboard_shows_them():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    _attach_adapter(svc, _StubAdapter([
        _sample_cart("100", total="248.00"),
        _sample_cart("101", total="348.50"),
    ]))

    result = asyncio.run(svc.sync_abandoned_carts())

    assert result["fetched"] is True
    assert result["salla_count"] == 2
    assert result["saved"] == 2
    assert result["updated"] == 0
    assert result["reconciled"] == 0

    dashboard_rows = _query_dashboard_carts(db, tenant_id)
    assert len(dashboard_rows) == 2, "Dashboard query must surface both Salla carts"

    externals = sorted(r.external_id for r in dashboard_rows)
    assert externals == ["cart-100", "cart-101"]

    for row in dashboard_rows:
        assert row.is_abandoned is True
        assert row.status == "abandoned"
        assert row.source == "salla"
        assert row.checkout_url and row.checkout_url.startswith("https://store.example/cart/")
        assert row.customer_info.get("phone")  # phone normalised onto both keys


# ── 2. Silent-fail guard: zero result must not wipe existing carts ───────────

def test_zero_result_sync_does_not_wipe_existing_carts():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)

    _attach_adapter(svc, _StubAdapter([
        _sample_cart("200"),
        _sample_cart("201"),
    ]))
    asyncio.run(svc.sync_abandoned_carts())
    assert len(_query_dashboard_carts(db, tenant_id)) == 2

    _attach_adapter(svc, _StubAdapter([]))
    second = asyncio.run(svc.sync_abandoned_carts())

    assert second["salla_count"] == 0
    assert second["saved"] == 0
    assert second["reconciled"] == 0, "Guard must short-circuit BEFORE reconciliation"

    rows_after = _query_dashboard_carts(db, tenant_id)
    assert len(rows_after) == 2, (
        "A transient empty response from Salla must never wipe the dashboard "
        "— this protects merchants from token blips and Salla outages."
    )


def test_adapter_exception_keeps_existing_carts_visible():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)

    _attach_adapter(svc, _StubAdapter([_sample_cart("300")]))
    asyncio.run(svc.sync_abandoned_carts())
    assert len(_query_dashboard_carts(db, tenant_id)) == 1

    _attach_adapter(svc, _StubAdapter(raise_with=RuntimeError("salla_boom")))
    result = asyncio.run(svc.sync_abandoned_carts())
    assert result["fetched"] is False, "fetch error must be reported, not swallowed silently"

    assert len(_query_dashboard_carts(db, tenant_id)) == 1


# ── 3. Reconciliation: resumed carts disappear ───────────────────────────────

def test_sync_reconciles_resumed_carts():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)

    _attach_adapter(svc, _StubAdapter([
        _sample_cart("400"),
        _sample_cart("401"),
    ]))
    asyncio.run(svc.sync_abandoned_carts())
    assert {r.external_id for r in _query_dashboard_carts(db, tenant_id)} == {"cart-400", "cart-401"}

    _attach_adapter(svc, _StubAdapter([_sample_cart("400")]))
    result = asyncio.run(svc.sync_abandoned_carts())

    assert result["reconciled"] == 1
    rows = _query_dashboard_carts(db, tenant_id)
    assert len(rows) == 1
    assert rows[0].external_id == "cart-400"

    cleared = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.external_id == "cart-401")
        .first()
    )
    assert cleared is not None, "Reconciled rows must be RETAINED with is_abandoned=False, not deleted"
    assert cleared.is_abandoned is False
    meta = cleared.extra_metadata or {}
    assert meta.get("recovered_or_expired_at"), (
        "Reconciliation must stamp a timestamp so the recovery automation "
        "knows the cart left the abandoned state."
    )


# ── 4. ID-space namespacing ──────────────────────────────────────────────────

def test_cart_id_namespace_does_not_collide_with_orders():
    """A real order with id=12345 must NOT overwrite a cart with id=12345.

    Salla's cart and order id-spaces are completely independent integers.
    Without the ``cart-`` prefix, a later sync_orders run would clobber the
    cart row and the dashboard would lose it.
    """
    db, tenant_id = _make_db()

    db.add(Order(
        tenant_id=tenant_id,
        external_id="12345",
        external_order_number="12345",
        status="completed",
        total="500.00",
        is_abandoned=False,
        source="salla",
    ))
    db.commit()

    svc = StoreSyncService(db, tenant_id)
    _attach_adapter(svc, _StubAdapter([_sample_cart("12345")]))
    asyncio.run(svc.sync_abandoned_carts())

    order_row = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.external_id == "12345")
        .first()
    )
    assert order_row is not None
    assert order_row.is_abandoned is False, "Real order must NOT be flipped by cart sync"
    assert order_row.status == "completed"

    cart_row = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.external_id == "cart-12345")
        .first()
    )
    assert cart_row is not None
    assert cart_row.is_abandoned is True
    assert cart_row.status == "abandoned"


# ── 5. Webhook real-time path ────────────────────────────────────────────────

def test_webhook_handler_upserts_cart_into_orders_table():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    _attach_adapter(svc, _StubAdapter([]))

    payload = _sample_cart("500")
    asyncio.run(svc.handle_abandoned_cart_webhook(payload))

    rows = _query_dashboard_carts(db, tenant_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.external_id == "cart-500"
    assert row.is_abandoned is True
    assert row.status == "abandoned"
    assert (row.extra_metadata or {}).get("source_kind") == "abandoned_cart"


def test_webhook_handler_is_idempotent():
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    _attach_adapter(svc, _StubAdapter([]))

    payload = _sample_cart("600", total="100.00")
    asyncio.run(svc.handle_abandoned_cart_webhook(payload))
    asyncio.run(svc.handle_abandoned_cart_webhook(payload))

    rows = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.external_id == "cart-600")
        .all()
    )
    assert len(rows) == 1, "Duplicate webhooks for the same cart must update, not insert"

    payload["total"] = {"amount": "999.99", "currency": "SAR"}
    asyncio.run(svc.handle_abandoned_cart_webhook(payload))

    refreshed = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id, Order.external_id == "cart-600")
        .first()
    )
    assert refreshed.total == "999.99", "Webhook upsert must refresh mutable fields"


# ── 6. Dashboard filter parity ───────────────────────────────────────────────

def test_dashboard_filter_does_not_hide_valid_carts():
    """The dashboard query must surface every is_abandoned=True row.

    No extra implicit filter (status whitelist, customer-status, age window,
    etc.) is allowed to silently exclude valid carts. The merchant must
    see exactly what Salla showed plus what our webhook recorded.
    """
    db, tenant_id = _make_db()
    svc = StoreSyncService(db, tenant_id)
    _attach_adapter(svc, _StubAdapter([
        _sample_cart("700", phone="+966500000001"),
        {**_sample_cart("701", phone="+966500000002"), "customer": {"mobile": "+966500000002"}},
        {**_sample_cart("702"), "items": []},
        {"id": "703", "customer": {"mobile": "+966500000003"},
         "items": [{"product_id": "p-1", "name": "x"}]},
    ]))
    asyncio.run(svc.sync_abandoned_carts())

    rows = _query_dashboard_carts(db, tenant_id)
    externals = {r.external_id for r in rows}
    assert "cart-700" in externals
    assert "cart-701" in externals
    assert "cart-703" in externals


def test_tenant_isolation():
    db, t1 = _make_db()
    t2 = Tenant(name="Other Tenant", is_active=True)
    db.add(t2)
    db.commit()

    svc1 = StoreSyncService(db, t1)
    _attach_adapter(svc1, _StubAdapter([_sample_cart("800")]))
    asyncio.run(svc1.sync_abandoned_carts())

    svc2 = StoreSyncService(db, t2.id)
    _attach_adapter(svc2, _StubAdapter([_sample_cart("801")]))
    asyncio.run(svc2.sync_abandoned_carts())

    t1_rows = _query_dashboard_carts(db, t1)
    t2_rows = _query_dashboard_carts(db, t2.id)
    assert {r.external_id for r in t1_rows} == {"cart-800"}
    assert {r.external_id for r in t2_rows} == {"cart-801"}


# ── 7. Normaliser contract ───────────────────────────────────────────────────

def test_normaliser_uses_cart_prefixed_external_id():
    raw = _sample_cart("999", total="50.00")
    n = _normalise_abandoned_cart(raw)
    assert n["external_id"] == "cart-999"
    assert n["raw_cart_id"] == "999"
    assert n["status"] == "abandoned"
    assert n["is_abandoned"] is True
    assert n["source"] == "salla"
    assert n["total"] == "50.00"
    assert n["checkout_url"].endswith("/999/resume")


def test_normaliser_handles_missing_id_gracefully():
    n = _normalise_abandoned_cart({"customer": {"mobile": "+966555000000"}})
    assert n["external_id"] == "", "no id → empty external_id so sync layer can skip"


def test_normaliser_normalises_phone():
    n = _normalise_abandoned_cart(_sample_cart("1000", phone="0555111222"))
    info = n["customer_info"]
    assert info.get("mobile"), "phone must be normalised onto mobile"
    assert info.get("phone"), "phone must be mirrored onto phone key"
