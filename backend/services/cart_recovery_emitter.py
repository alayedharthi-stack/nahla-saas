"""
services/cart_recovery_emitter.py
─────────────────────────────────
Idempotent ``cart_abandoned`` AutomationEvent emitter for platform-sourced
abandoned carts (Salla / Zid / Shopify).

Background
──────────
Before this module existed, ``cart_abandoned`` AutomationEvents were only
emitted from the storefront tracking snippet (``routers/tracking.py``).
Carts that landed via the platform webhook (``abandoned.cart``) or the
periodic reconciliation sweep (``StoreSyncService.sync_abandoned_carts``)
were persisted as ``Order`` rows but never produced an event — so the
abandoned-cart automation never fired for those carts and the customer
never received a recovery WhatsApp.

This module closes the gap. It is called from both:

  • ``StoreSyncService.handle_abandoned_cart_webhook`` — near real-time.
  • ``StoreSyncService.sync_abandoned_carts``           — safety net.

Idempotency
───────────
We persist the emitted event id on ``Order.extra_metadata.recovery_event_id``
the moment the event is created. Subsequent calls for the same cart short-
circuit on that marker — no JSON predicates, no time-window heuristic, no
double-emit even under concurrent webhook + sweeper runs. If the marker
points at a deleted event (rare — operator manually purged the queue),
we re-emit with a fresh id.

Customer resolution
───────────────────
The recovery flow can only contact a customer that has a phone number.
We look up an existing Customer row by (tenant_id, normalized_phone). If
none exists, we upsert a lead customer (same primitive ``tracking.py``
uses) so the cart still feeds the recovery funnel even when the
shopper is brand new to the merchant. Carts whose payload carries no
phone (rare draft carts) skip event emission — the dashboard still
shows them, but the automation has no addressee.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from services.salla_datetime import salla_datetime_to_utc_iso
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.cart_recovery_emitter")

def _parse_abandoned_at_naive_utc(value):
    """Parse Salla abandonment timestamp to naive UTC for AutomationEvent.created_at."""
    from services.salla_datetime import salla_datetime_to_naive_utc

    return salla_datetime_to_naive_utc(value)




def _extract_phone(normalised: Dict[str, Any]) -> Optional[str]:
    info = normalised.get("customer_info") or {}
    if not isinstance(info, dict):
        return None
    raw = info.get("mobile") or info.get("phone") or ""
    raw = str(raw or "").strip()
    return raw or None


def _existing_event_id(db: Any, *, tenant_id: int, marker: Any) -> Optional[int]:
    """Return the event id from the order marker iff that event still
    exists. Avoids re-emitting on every webhook tick. Returns None when
    the marker is absent or the row was purged."""
    if not marker:
        return None
    try:
        from models import AutomationEvent  # noqa: PLC0415
        ev = (
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.tenant_id == tenant_id,
                AutomationEvent.id == int(marker),
            )
            .first()
        )
        return ev.id if ev else None
    except Exception:
        return None


def _resolve_customer_id(
    db: Any, *, tenant_id: int, phone: str, name: Optional[str], commit: bool = True,
) -> Optional[int]:
    try:
        from services.customer_intelligence import (  # noqa: PLC0415
            CustomerIntelligenceService,
            normalize_phone,
        )
    except Exception as exc:
        logger.error(
            "[CartRecoveryEmitter] cart_recovery.customer_intelligence_import_failed "
            "tenant=%s error_class=%s",
            tenant_id, type(exc).__name__,
        )
        return None

    normalized = normalize_phone(phone) or phone
    try:
        service = CustomerIntelligenceService(db, tenant_id)
        existing = service.find_customer_by_phone(normalized)
        if existing is not None:
            return existing.id
        lead = service.upsert_lead_customer(
            phone=normalized,
            name=name or normalized,
            source="abandoned_cart_emitter",
            extra_metadata={"origin_event": "cart_abandoned"},
            commit=commit,
        )
        return lead.id if lead else None
    except Exception as exc:
        logger.error(
            "[CartRecoveryEmitter] cart_recovery.customer_resolve_failed "
            "tenant=%s error_class=%s",
            tenant_id, type(exc).__name__,
        )
        return None


def emit_cart_abandoned_if_new(
    db: Any,
    *,
    tenant_id: int,
    cart_row: Any,
    normalised: Dict[str, Any],
    source: str = "store_sync",
    commit: bool = False,
) -> Optional[int]:
    """
    Idempotently emit a ``cart_abandoned`` AutomationEvent for the given
    persisted cart row. Returns the event id (existing or newly created),
    or ``None`` when emission was skipped (no phone, missing cart id,
    customer-resolution failure).

    Args:
      cart_row:    the persisted ``Order`` row (already committed). We
                   read its ``extra_metadata`` for the idempotency marker
                   and write the new event id back when we emit.
      normalised:  the dict returned by
                   ``store_sync._normalise_abandoned_cart`` for this cart.
      source:      audit string written into the event payload, used
                   downstream to distinguish webhook vs sync vs snippet.
    """
    if cart_row is None:
        return None

    cart_id = getattr(cart_row, "id", None)
    if cart_id is not None:
        try:
            from models import Order  # noqa: PLC0415

            cart_row = (
                db.query(Order)
                .filter(Order.tenant_id == tenant_id, Order.id == cart_id)
                .with_for_update()
                .populate_existing()
                .one()
            )
        except Exception:
            logger.exception(
                "[CartRecoveryEmitter] tenant=%s could not lock cart row id=%s",
                tenant_id,
                cart_id,
            )
            return None

    if not getattr(cart_row, "is_abandoned", True):
        return None

    cart_external = normalised.get("external_id") or getattr(cart_row, "external_id", "")
    raw_cart_id = normalised.get("raw_cart_id") or (
        cart_external.replace("cart-", "", 1) if str(cart_external).startswith("cart-") else cart_external
    )
    if not raw_cart_id:
        logger.debug(
            "[CartRecoveryEmitter] tenant=%s no usable cart id — skipping emit",
            tenant_id,
        )
        return None

    meta = dict(getattr(cart_row, "extra_metadata", None) or {})
    if meta.get("recovered_at") or str(meta.get("cart_status") or "").strip().lower() == "purchased":
        return None
    existing_id = _existing_event_id(
        db, tenant_id=tenant_id, marker=meta.get("recovery_event_id"),
    )
    if existing_id is not None:
        logger.debug(
            "[CartRecoveryEmitter] tenant=%s cart=%s already has event=%s — skip",
            tenant_id, cart_external, existing_id,
        )
        return existing_id

    phone = _extract_phone(normalised)
    if not phone:
        logger.info(
            "[CartRecoveryEmitter] tenant=%s cart=%s has no customer phone — "
            "skipping cart_abandoned emit (recovery flow has no addressee)",
            tenant_id, cart_external,
        )
        return None

    # Keep the cart FOR UPDATE transaction open. A mid-emit commit would
    # release the row lock before the event exists, letting a purchase
    # cancel miss the event and leave recovery sendable.
    customer_id = _resolve_customer_id(
        db, tenant_id=tenant_id, phone=phone,
        name=normalised.get("customer_name") or None,
        commit=False,
    )
    if customer_id is None:
        logger.warning(
            "[CartRecoveryEmitter] tenant=%s cart=%s could not resolve "
            "customer unresolved — skipping cart_abandoned emit",
            tenant_id, cart_external,
        )
        return None

    try:
        from core.automation_engine import emit_automation_event  # noqa: PLC0415
        from core.automation_triggers import AutomationTrigger  # noqa: PLC0415
    except Exception:
        logger.exception(
            "[CartRecoveryEmitter] automation_engine import failed tenant=%s",
            tenant_id,
        )
        return None

    allowed_sources = {
        "provider_explicit",
        "provider_webhook_event",
        "first_webhook_observation",
        "first_poller_observation",
    }
    anchor_iso = str(meta.get("first_provider_abandoned_observed_at") or "").strip()
    anchor_source = str(meta.get("abandonment_anchor_source") or "").strip()
    if not anchor_iso or anchor_source not in allowed_sources:
        candidate_iso = str(normalised.get("observation_candidate_iso") or "").strip()
        candidate_source = str(normalised.get("observation_candidate_source") or "").strip()
        if not candidate_iso or candidate_source not in allowed_sources:
            explicit = normalised.get("abandoned_at") or meta.get("abandoned_at")
            candidate_iso = salla_datetime_to_utc_iso(explicit) if explicit else ""
            candidate_source = "provider_explicit" if candidate_iso else ""
        if not candidate_iso or candidate_source not in allowed_sources:
            logger.warning(
                "[CartRecoveryEmitter] tenant=%s cart=%s missing first abandoned observation — skipping emit",
                tenant_id,
                cart_external,
            )
            return None
        meta["first_provider_abandoned_observed_at"] = candidate_iso
        meta["abandonment_anchor_source"] = candidate_source
        anchor_iso = candidate_iso
        anchor_source = candidate_source

    payload: Dict[str, Any] = {
        "source":               source,
        "cart_id":              raw_cart_id,
        "cart_external_id":     cart_external,
        "checkout_url":         normalised.get("checkout_url") or "",
        "cart_total":           normalised.get("total"),
        "items":                normalised.get("line_items") or [],
        "phone":                phone,
        "customer_name":        normalised.get("customer_name") or "",
        "abandoned_at":         anchor_iso,
        "abandonment_anchor_source": anchor_source,
    }

    try:
        event_created_at = _parse_abandoned_at_naive_utc(anchor_iso)
        if event_created_at is None:
            logger.warning(
                "[CartRecoveryEmitter] tenant=%s cart=%s invalid first-observation anchor — skipping emit",
                tenant_id,
                cart_external,
            )
            return None
        event = emit_automation_event(
            db,
            tenant_id=tenant_id,
            event_type=AutomationTrigger.CART_ABANDONED.value,
            customer_id=customer_id,
            payload=payload,
            commit=False,
            created_at=event_created_at,
        )
    except Exception:
        logger.exception(
            "[CartRecoveryEmitter] emit_automation_event failed tenant=%s "
            "cart=%s — recovery flow will not start for this cart",
            tenant_id, cart_external,
        )
        return None

    new_event_id = getattr(event, "id", None)
    if new_event_id is None:
        # Some test stubs return None — the marker is the only durable
        # idempotency mechanism, so skip writing it rather than poisoning it.
        logger.warning(
            "[CartRecoveryEmitter] tenant=%s cart=%s emitted event but no id "
            "available — idempotency marker NOT written",
            tenant_id, cart_external,
        )
        return None

    meta["recovery_event_id"] = new_event_id
    meta["recovery_emitted_at"] = datetime.now(timezone.utc).isoformat()
    try:
        cart_row.extra_metadata = meta
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        flag_modified(cart_row, "extra_metadata")
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        logger.exception(
            "[CartRecoveryEmitter] tenant=%s cart=%s — failed to persist "
            "recovery_event_id marker",
            tenant_id, cart_external,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None

    logger.info(
        "[CartRecoveryEmitter] tenant=%s cart=%s emitted cart_abandoned "
        "event=%s customer=%s source=%s checkout_url=%s",
        tenant_id, cart_external, new_event_id, customer_id, source,
        bool(payload["checkout_url"]),
    )
    return new_event_id
