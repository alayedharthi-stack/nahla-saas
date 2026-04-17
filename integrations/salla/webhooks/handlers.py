"""
Salla webhook handlers.

DEPRECATED (ADR 0001, 2026-04-16)
─────────────────────────────────
This module is superseded by the durable webhook queue
(`backend/core/webhook_events.py`) and the async dispatcher
(`backend/core/webhook_dispatcher.py`). All live Salla traffic now lands in
the `webhook_events` table at `POST /webhook/salla` and is processed by
`_dispatch_salla(...)` in the dispatcher, which delegates to
`services/store_sync.StoreSyncService`.

Do NOT extend this file. It is retained only until the transition has
baked in production for at least one release. New Salla handling logic
belongs in `StoreSyncService` or the dispatcher, not here.

All HMAC verification and tenant resolution are delegated to the shared
layer.  This module focuses only on mapping Salla event payloads to the
Nahla data models.
"""

import sys
import os
import warnings
from typing import Any, Dict

warnings.warn(
    "integrations.salla.webhooks.handlers is deprecated; see docs/adr/0001-durable-webhook-queue-and-nh-coupons.md",
    DeprecationWarning,
    stacklevel=2,
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Customer, Order, Product
from database.session import SessionLocal
from integrations.shared.base_webhook import log_webhook_event, resolve_tenant_or_skip

PROVIDER = "salla"


# ── helpers ──────────────────────────────────────────────────────────────────

def _not_found() -> Dict[str, Any]:
    return {"processed": False, "reason": "unknown_store"}


# ── product ──────────────────────────────────────────────────────────────────

def handle_product_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    event     = payload.get("event", "")
    data      = payload.get("data", {})
    store_id  = str(payload.get("merchant", ""))

    tenant_id = resolve_tenant_or_skip(PROVIDER, store_id)
    if not tenant_id:
        return _not_found()

    external_id = str(data.get("id", ""))
    db = SessionLocal()
    try:
        if "deleted" in event:
            p = db.query(Product).filter(
                Product.tenant_id == tenant_id, Product.external_id == external_id
            ).first()
            if p:
                db.delete(p)
                db.commit()
            log_webhook_event(tenant_id, "product", external_id, "deleted", PROVIDER)
            return {"processed": True, "action": "deleted", "external_id": external_id}

        p = db.query(Product).filter(
            Product.tenant_id == tenant_id, Product.external_id == external_id
        ).first() or Product(tenant_id=tenant_id, external_id=external_id)
        db.add(p)

        p.title       = data.get("name", p.title or "")
        p.description = data.get("description", p.description)
        p.sku         = data.get("sku", p.sku)
        price         = data.get("price", {})
        p.price       = str(price.get("amount", price) if isinstance(price, dict) else price)
        thumb         = data.get("thumbnail", {})
        # `metadata` is a SQLAlchemy reserved name; use the mapped attribute.
        p.extra_metadata = {
            "status":    data.get("status"),
            "thumbnail": thumb.get("url") if isinstance(thumb, dict) else None,
            "quantity":  data.get("quantity"),
            "source":    PROVIDER,
        }
        db.commit()
        action = "updated" if "updated" in event else "created"
        log_webhook_event(tenant_id, "product", external_id, action, PROVIDER)
        return {"processed": True, "action": action, "external_id": external_id}
    finally:
        db.close()


# ── order ────────────────────────────────────────────────────────────────────

def handle_order_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    event    = payload.get("event", "")
    data     = payload.get("data", {})
    store_id = str(payload.get("merchant", ""))

    tenant_id = resolve_tenant_or_skip(PROVIDER, store_id)
    if not tenant_id:
        return _not_found()

    external_id = str(data.get("id", ""))
    db = SessionLocal()
    try:
        o = db.query(Order).filter(
            Order.tenant_id == tenant_id, Order.external_id == external_id
        ).first() or Order(tenant_id=tenant_id, external_id=external_id)
        db.add(o)

        # NOTE: Salla returns `data["status"]` as a dict
        # ({"id":..., "slug": "under_review", "name": "..."}). Storing
        # str(dict) used to corrupt every order. Extract slug defensively
        # even though this handler is deprecated — any straggler webhook
        # going through this path must not poison the DB.
        status_map = {"order.created": "pending", "order.completed": "completed", "order.cancelled": "cancelled"}
        raw_status = data.get("status")
        if isinstance(raw_status, dict):
            raw_status = (
                raw_status.get("slug")
                or raw_status.get("name")
                or raw_status.get("code")
                or "pending"
            )
        o.status        = status_map.get(event, str(raw_status or o.status or "pending"))
        amounts         = data.get("amounts", {})
        total           = amounts.get("total", {})
        o.total         = str(total.get("amount", total) if isinstance(total, dict) else total)
        customer        = data.get("customer", {})
        o.customer_info = {"name": customer.get("name"), "phone": customer.get("mobile"), "email": customer.get("email")}
        o.line_items    = [
            {"product_id": str(li.get("product_id")), "name": li.get("name"),
             "quantity": li.get("quantity"), "price": str(li.get("price", {}).get("amount", ""))}
            for li in data.get("items", [])
        ]
        o.is_abandoned  = (event == "order.created" and o.status == "pending")
        # `metadata` is reserved by SQLAlchemy's declarative Base; the
        # mapped column is `extra_metadata = Column('metadata', JSONB)`.
        # Setting `o.metadata` was a silent no-op (or worse: shadowed Base
        # metadata). Use the correct attribute.
        o.extra_metadata = {"source": PROVIDER, "event": event}
        db.commit()
        log_webhook_event(tenant_id, "order", external_id, event.split(".")[-1], PROVIDER)
        return {"processed": True, "action": event, "external_id": external_id}
    finally:
        db.close()


# ── customer ─────────────────────────────────────────────────────────────────

def handle_customer_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    event    = payload.get("event", "")
    data     = payload.get("data", {})
    store_id = str(payload.get("merchant", ""))

    tenant_id = resolve_tenant_or_skip(PROVIDER, store_id)
    if not tenant_id:
        return _not_found()

    external_id = str(data.get("id", ""))
    db = SessionLocal()
    try:
        c = db.query(Customer).filter(
            Customer.tenant_id == tenant_id,
            Customer.extra_metadata["external_id"].astext == external_id,
        ).first() or Customer(tenant_id=tenant_id)
        db.add(c)

        c.name     = data.get("name",   c.name)
        c.email    = data.get("email",  c.email)
        c.phone    = data.get("mobile", c.phone)
        # `metadata` is a SQLAlchemy reserved name; use the mapped attribute.
        c.extra_metadata = {"external_id": external_id, "source": PROVIDER,
                            "city": data.get("city"), "country": data.get("country", "SA")}
        db.commit()
        action = "updated" if "updated" in event else "created"
        log_webhook_event(tenant_id, "customer", external_id, action, PROVIDER)
        return {"processed": True, "action": action, "external_id": external_id}
    finally:
        db.close()
