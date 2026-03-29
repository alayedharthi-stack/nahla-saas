"""
Zid webhook handlers.

Identical structure to the Salla handlers; only the payload field names
differ (Zid uses store_id/merchant_id instead of merchant, and
products/items instead of items, etc.).
"""

import sys
import os
from typing import Any, Dict

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Customer, Order, Product
from database.session import SessionLocal
from integrations.shared.base_webhook import log_webhook_event, resolve_tenant_or_skip

PROVIDER = "zid"


def _not_found() -> Dict[str, Any]:
    return {"processed": False, "reason": "unknown_store"}


def _store_id(payload: Dict[str, Any]) -> str:
    return str(payload.get("store_id", payload.get("merchant_id", "")))


# ── product ──────────────────────────────────────────────────────────────────

def handle_product_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    event     = payload.get("event", "")
    data      = payload.get("data", payload.get("product", {}))
    store_id  = _store_id(payload)

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
        raw_price     = data.get("price", data.get("sale_price", ""))
        p.price       = str(raw_price.get("amount", raw_price) if isinstance(raw_price, dict) else raw_price)
        images        = data.get("images", [])
        p.metadata    = {
            "status":    data.get("status", data.get("is_active")),
            "thumbnail": images[0].get("url") if images else None,
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
    data     = payload.get("data", payload.get("order", {}))
    store_id = _store_id(payload)

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

        status_map = {"order.created": "pending", "order.completed": "completed", "order.cancelled": "cancelled"}
        o.status        = status_map.get(event, str(data.get("status", o.status or "pending")))
        o.total         = str(data.get("total", data.get("total_amount", o.total or "")))
        customer        = data.get("customer", data.get("consumer", {}))
        o.customer_info = {"name": customer.get("name"), "phone": customer.get("mobile", customer.get("phone")),
                           "email": customer.get("email")}
        o.line_items    = [
            {"product_id": str(li.get("product_id", li.get("id", ""))), "name": li.get("name"),
             "quantity": li.get("quantity"), "price": str(li.get("price", li.get("unit_price", "")))}
            for li in data.get("products", data.get("items", []))
        ]
        o.metadata = {"source": PROVIDER, "event": event}
        db.commit()
        log_webhook_event(tenant_id, "order", external_id, event.split(".")[-1], PROVIDER)
        return {"processed": True, "action": event, "external_id": external_id}
    finally:
        db.close()


# ── customer ─────────────────────────────────────────────────────────────────

def handle_customer_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    event    = payload.get("event", "")
    data     = payload.get("data", payload.get("customer", {}))
    store_id = _store_id(payload)

    tenant_id = resolve_tenant_or_skip(PROVIDER, store_id)
    if not tenant_id:
        return _not_found()

    external_id = str(data.get("id", ""))
    db = SessionLocal()
    try:
        c = db.query(Customer).filter(
            Customer.tenant_id == tenant_id,
            Customer.metadata["external_id"].astext == external_id,
        ).first() or Customer(tenant_id=tenant_id)
        db.add(c)

        c.name     = data.get("name",   c.name)
        c.email    = data.get("email",  c.email)
        c.phone    = data.get("mobile", data.get("phone", c.phone))
        c.metadata = {"external_id": external_id, "source": PROVIDER,
                      "city": data.get("city"), "country": data.get("country", "SA")}
        db.commit()
        action = "updated" if "updated" in event else "created"
        log_webhook_event(tenant_id, "customer", external_id, action, PROVIDER)
        return {"processed": True, "action": action, "external_id": external_id}
    finally:
        db.close()
