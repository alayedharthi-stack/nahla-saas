"""
services/salla_realtime_observability.py
Diagnostics for Salla near-real-time commerce sync.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.coupon_log_privacy import hash_identifier
from services.salla_realtime_events import (
    SALLA_ABANDONED_CART_CREATE_EVENTS,
    SALLA_ABANDONED_CART_PURCHASED_EVENTS,
    SALLA_ABANDONED_CART_STATUS_EVENTS,
    SALLA_ABANDONED_CART_UPDATE_EVENTS,
    SALLA_CUSTOMER_LOGIN_EVENTS,
    SALLA_CUSTOMER_WEBHOOK_EVENTS,
    SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST,
    SALLA_ORDER_WEBHOOK_EVENTS,
    SALLA_PRODUCT_DELETE_WEBHOOK_EVENTS,
    SALLA_PRODUCT_UPSERT_WEBHOOK_EVENTS,
    SALLA_SPECIAL_OFFER_WEBHOOK_EVENTS,
    event_registry_contract,
)

_DOMAIN_EVENTS: Dict[str, frozenset[str]] = {
    "orders": SALLA_ORDER_WEBHOOK_EVENTS,
    "customers": SALLA_CUSTOMER_WEBHOOK_EVENTS | SALLA_CUSTOMER_LOGIN_EVENTS,
    "products": SALLA_PRODUCT_UPSERT_WEBHOOK_EVENTS | SALLA_PRODUCT_DELETE_WEBHOOK_EVENTS,
    "abandoned_carts": (
        SALLA_ABANDONED_CART_CREATE_EVENTS
        | SALLA_ABANDONED_CART_UPDATE_EVENTS
        | SALLA_ABANDONED_CART_STATUS_EVENTS
        | SALLA_ABANDONED_CART_PURCHASED_EVENTS
    ),
    "special_offers": SALLA_SPECIAL_OFFER_WEBHOOK_EVENTS,
}

_FALLBACK_INTERVALS_SECONDS = {
    "orders_poller": 60,
    "abandoned_carts_poller": 300,
    "customers_products_reconciler": 900,
}


def _hash_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _dt_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _lag_seconds(received: Any, processed: Any) -> Optional[float]:
    if not received or not processed:
        return None
    if not isinstance(received, datetime):
        return None
    if not isinstance(processed, datetime):
        return None
    r = received if received.tzinfo else received.replace(tzinfo=timezone.utc)
    p = processed if processed.tzinfo else processed.replace(tzinfo=timezone.utc)
    return max(0.0, (p - r).total_seconds())


def _domain_stats(db: Session, tenant_id: int, event_types: Iterable[str]) -> Dict[str, Any]:
    from models import WebhookEvent

    types = tuple(event_types)
    base = db.query(WebhookEvent).filter(
        WebhookEvent.tenant_id == tenant_id,
        WebhookEvent.provider.in_(("salla", "salla_oauth")),
        WebhookEvent.event_type.in_(types),
    )
    last_received = base.with_entities(func.max(WebhookEvent.received_at)).scalar()
    last_processed = (
        base.filter(WebhookEvent.status == "processed")
        .with_entities(func.max(WebhookEvent.processed_at))
        .scalar()
    )
    dead_letters = (
        base.filter(WebhookEvent.status == "dead_letter")
        .with_entities(func.count(WebhookEvent.id))
        .scalar()
    ) or 0
    return {
        "webhook_required": sorted(types),
        "webhook_active": bool(last_received),
        "last_received": _dt_iso(last_received),
        "last_processed": _dt_iso(last_processed),
        "lag_seconds": _lag_seconds(last_received, last_processed),
        "dead_letters": int(dead_letters),
    }


def _sanitize_reconciler_tenant(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {
        "tenant_hash": raw.get("tenant_hash"),
        "integration_id": raw.get("integration_id"),
        "store_hash": raw.get("store_hash"),
        "scanned_at": raw.get("scanned_at"),
        "result": raw.get("result"),
        "error_code": raw.get("error_code"),
        "stats": raw.get("stats"),
    }


def build_realtime_commerce_diag(db: Session, tenant_id: int) -> Dict[str, Any]:
    from models import Integration
    from services.salla_commerce_reconciler import get_reconciler_state
    from services.salla_orders_poller import get_poller_state

    intg = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .order_by(Integration.id.asc())
        .first()
    )
    cfg = (intg.config or {}) if intg is not None else {}
    store_id = cfg.get("store_id") or cfg.get("merchant_id") or getattr(intg, "external_store_id", None)
    integration = {
        "present": intg is not None,
        "enabled": bool(getattr(intg, "enabled", False)) if intg else False,
        "token_present": bool(cfg.get("api_key")),
        "needs_reauth": bool(cfg.get("needs_reauth")),
        "store_hash": hash_identifier(store_id) if store_id else None,
        "phone_hash": _hash_phone(cfg.get("whatsapp_number") or cfg.get("phone")),
    }

    domains = {name: _domain_stats(db, tenant_id, events) for name, events in _DOMAIN_EVENTS.items()}
    reconciler = get_reconciler_state()
    orders_poller = get_poller_state()
    tenant_reconciler = reconciler.get("tenants", {}).get(tenant_id) or reconciler.get("tenants", {}).get(str(tenant_id))
    tenant_orders = orders_poller.get("tenants", {}).get(tenant_id) or orders_poller.get("tenants", {}).get(str(tenant_id))

    return {
        "tenant_id": tenant_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "integration": integration,
        "domains": domains,
        "event_registry": event_registry_contract(),
        "activation_checklist_total": len(SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST),
        "fallback_intervals_seconds": dict(_FALLBACK_INTERVALS_SECONDS),
        "capabilities": {
            "orders": {"inbound_webhook": True, "fallback_poller_seconds": 60},
            "abandoned_carts": {"inbound_webhook": True, "fallback_poller_seconds": 300},
            "customers": {"inbound_webhook": True, "fallback_reconciler_seconds": 900},
            "products": {"inbound_webhook": True, "fallback_reconciler_seconds": 900},
            "special_offers": {"inbound_webhook": True, "fallback_poller": False},
            "outbound_customer_product_offer_sync": False,
        },
        "reconciler": {
            "interval_seconds": reconciler.get("config", {}).get("poll_interval_seconds"),
            "last_tick_at": reconciler.get("last_tick_at"),
            "last_tick_scanned": reconciler.get("last_tick_scanned"),
            "last_tick_errors": reconciler.get("last_tick_errors"),
            "tenant": _sanitize_reconciler_tenant(tenant_reconciler),
        },
        "orders_poller": {
            "interval_seconds": orders_poller.get("config", {}).get("poll_interval_seconds"),
            "last_tick_at": orders_poller.get("last_tick_at"),
            "tenant": tenant_orders,
        },
    }
