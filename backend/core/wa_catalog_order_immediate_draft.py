"""
core/wa_catalog_order_immediate_draft.py
────────────────────────────────────────
Phase B — persist WhatsApp ``catalog_order`` inbound as a visible Nahla
Order draft immediately (before Brain completes). Operational only; no reply
wording changes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.wa_cart_line_items import ITEM_STATUS_CONFIRMED, ITEM_STATUS_NEEDS_REVIEW
from core.wa_native_catalog_order import (
    NativeCatalogOrderPayload,
    build_line_items_from_payload,
    parse_native_catalog_order,
)
from services.nahla_order_bridge import (
    is_open_wa_draft_order,
    nahla_wa_catalog_external_id,
    nahla_wa_external_id,
    sync_nahla_wa_order,
)

logger = logging.getLogger("nahla.wa_catalog_order_immediate_draft")

_CATALOG_SOURCE = "whatsapp_catalog_order"


def _immediate_draft_enabled() -> bool:
    from core.config import WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED  # noqa: PLC0415

    return WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED


def is_catalog_order_inbound(metadata: Optional[Dict[str, Any]]) -> bool:
    meta = dict(metadata or {})
    return meta.get("source_type") == "catalog_order" and bool(
        meta.get("product_items") or meta.get("order")
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _catalog_match_label(raw_status: str, *, has_product_id: bool) -> str:
    status = (raw_status or "").strip().lower()
    if status == ITEM_STATUS_CONFIRMED:
        return "matched"
    if status == ITEM_STATUS_NEEDS_REVIEW and has_product_id:
        return "needs_review"
    if not has_product_id:
        return "unmatched"
    return "needs_review"


def enrich_catalog_line_items_for_draft(
    line_items: List[Dict[str, Any]],
    *,
    currency: str,
) -> List[Dict[str, Any]]:
    """Dashboard-compatible line_items with catalog draft fields."""
    enriched: List[Dict[str, Any]] = []
    for raw in line_items or []:
        row = dict(raw or {})
        qty = int(row.get("quantity") or 1)
        unit = _as_float(row.get("unit_price") or row.get("price")) or 0.0
        name = (
            str(row.get("product_name") or row.get("title") or row.get("name") or "")
            .strip()
        )
        has_product_id = bool(row.get("product_id"))
        row["catalog_name"] = name
        row["name"] = name or row.get("product_retailer_id") or "منتج"
        row["quantity"] = qty
        row["unit_price"] = unit
        row["price"] = unit
        row["line_total"] = round(unit * qty, 2)
        row["currency"] = str(row.get("currency") or currency or "SAR")
        row["source"] = "whatsapp_catalog"
        row["catalog_match_status"] = _catalog_match_label(
            str(row.get("match_status") or ""),
            has_product_id=has_product_id,
        )
        if row.get("sku") is None and row.get("product_retailer_id"):
            row["sku"] = row.get("product_retailer_id")
        enriched.append(row)
    return enriched


def _resolve_catalog_totals(
    meta: Dict[str, Any],
    line_items: List[Dict[str, Any]],
) -> Tuple[Optional[float], str]:
    currency = str(meta.get("currency") or "SAR")
    total = _as_float(meta.get("total_price"))
    if total is None:
        running = 0.0
        found = False
        for row in line_items:
            lt = _as_float(row.get("line_total"))
            if lt is not None:
                running += lt
                found = True
        total = running if found else None
    return total, currency


def build_catalog_order_prep(
    *,
    meta: Dict[str, Any],
    line_items: List[Dict[str, Any]],
    payload: NativeCatalogOrderPayload,
) -> Dict[str, Any]:
    total, currency = _resolve_catalog_totals(meta, line_items)
    prep: Dict[str, Any] = {
        "line_items": list(line_items),
        "cart_items": list(line_items),
        "order_status": "awaiting_address",
        "catalog_checkout_total": total,
        "catalog_checkout_currency": currency,
        "total_price": total,
    }
    if payload.customer_note:
        prep["address_line"] = payload.customer_note
    first = next((li for li in line_items if li.get("product_id")), line_items[0] if line_items else None)
    if first and first.get("product_id"):
        prep["product_id"] = str(first.get("product_id"))
        prep["quantity"] = int(first.get("quantity") or 1)
    return prep


def build_catalog_brain_state(
    *,
    meta: Dict[str, Any],
    line_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    first = line_items[0] if line_items else {}
    return {
        "stage": "ordering",
        "cart_items": list(line_items),
        "current_product_focus": {
            "id": first.get("product_id") or first.get("product_retailer_id"),
            "external_id": first.get("product_retailer_id") or "",
            "title": first.get("product_name") or first.get("title") or "",
            "price": first.get("unit_price") or first.get("price"),
            "currency": first.get("currency") or meta.get("currency") or "SAR",
            "from_catalog_order": True,
            "from_native_catalog_order": True,
            "line_items_count": len(line_items),
        },
    }


def build_catalog_order_metadata(
    *,
    meta: Dict[str, Any],
    message_event_id: Optional[int],
    item_count: int,
    total_price: Optional[float],
    currency: str,
    source_message_key: Optional[str] = None,
) -> Dict[str, Any]:
    catalog_meta = {
        "source": _CATALOG_SOURCE,
        "source_type": "catalog_order",
        "catalog_item_count": item_count,
        "catalog_total_price": total_price,
        "catalog_currency": currency,
    }
    if message_event_id is not None:
        catalog_meta["source_message_id"] = str(message_event_id)
        catalog_meta["raw_payload_ref"] = {"message_event_id": int(message_event_id)}
    if source_message_key:
        catalog_meta["source_message_key"] = source_message_key
    if meta.get("catalog_id"):
        catalog_meta["catalog_id"] = meta.get("catalog_id")
    return {"catalog_order": catalog_meta, "order_source_label": "WhatsApp Catalog"}


def _load_existing_draft(db: Any, *, tenant_id: int, conversation_id: int) -> Any:
    try:
        from models import Order  # noqa: PLC0415

        external_id = nahla_wa_external_id(tenant_id, conversation_id)
        return (
            db.query(Order)
            .filter_by(tenant_id=tenant_id, external_id=external_id)
            .first()
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_ORDER_DRAFT] existing draft lookup failed tenant=%s conv=%s",
            tenant_id,
            conversation_id,
        )
        return None


def _idempotent_noop(
    existing: Any,
    *,
    message_event_id: Optional[int],
    line_items: List[Dict[str, Any]],
) -> bool:
    if existing is None:
        return False
    meta = dict(getattr(existing, "extra_metadata", None) or {})
    if meta.get("merchant_edit_locked"):
        return False
    catalog_meta = dict(meta.get("catalog_order") or {})
    prev_msg = catalog_meta.get("source_message_id")
    if message_event_id is None or prev_msg is None:
        return False
    if str(prev_msg) != str(message_event_id):
        return False
    from core.wa_cart_line_items import line_items_fingerprint  # noqa: PLC0415

    prev_fp = str((meta.get("last_sync_snapshot") or {}).get("line_items_fingerprint") or "")
    curr_fp = line_items_fingerprint(line_items)
    return bool(prev_fp and prev_fp == curr_fp)


def persist_catalog_order_immediate_draft(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    inbound_metadata: Dict[str, Any],
    customer: Any = None,
    phone: str = "",
    message_event_id: Optional[int] = None,
    source_message_key: Optional[str] = None,
) -> Optional[Any]:
    """
    Parse catalog_order inbound and upsert a visible WhatsApp draft Order.

    Idempotent on ``nahla-wa-{tenant}-{conversation_id}``; replays of the
    same ``message_event_id`` with unchanged line items are no-ops.
    """
    if not _immediate_draft_enabled():
        return None
    if not is_catalog_order_inbound(inbound_metadata):
        return None

    conversation_id = getattr(conversation, "id", None)
    if not conversation_id:
        return None

    meta = dict(inbound_metadata or {})
    try:
        payload = parse_native_catalog_order(
            {
                "catalog_id": meta.get("catalog_id"),
                "text": meta.get("customer_note"),
                "product_items": meta.get("product_items") or [],
            },
            metadata=meta,
        )
        if not payload.items:
            logger.info(
                "[CATALOG_ORDER_DRAFT] skip tenant=%s conv=%s reason=empty_items",
                tenant_id,
                conversation_id,
            )
            return None

        resolution = build_line_items_from_payload(db, tenant_id, payload)
        currency = str(meta.get("currency") or "SAR")
        line_items = enrich_catalog_line_items_for_draft(
            resolution.line_items,
            currency=currency,
        )
        if not line_items:
            return None

        existing = _load_existing_draft(
            db,
            tenant_id=tenant_id,
            conversation_id=int(conversation_id),
        )
        locked_meta = dict(getattr(existing, "extra_metadata", None) or {}) if existing else {}
        if locked_meta.get("merchant_edit_locked"):
            logger.info(
                "[CATALOG_ORDER_DRAFT] skip tenant=%s conv=%s reason=merchant_edit_locked",
                tenant_id,
                conversation_id,
            )
            divergence = dict(locked_meta.get("catalog_order_divergence") or {})
            divergence.update(
                {
                    "skipped": True,
                    "reason": "merchant_edit_locked",
                    "incoming_item_count": len(line_items),
                }
            )
            locked_meta["catalog_order_divergence"] = divergence
            existing.extra_metadata = locked_meta
            db.add(existing)
            return existing

        if _idempotent_noop(
            existing,
            message_event_id=message_event_id,
            line_items=line_items,
        ):
            logger.info(
                "[CATALOG_ORDER_DRAFT] noop tenant=%s conv=%s message_event=%s",
                tenant_id,
                conversation_id,
                message_event_id,
            )
            return existing

        external_id_override: Optional[str] = None
        if existing is not None and not is_open_wa_draft_order(existing):
            external_id_override = nahla_wa_catalog_external_id(
                tenant_id,
                int(conversation_id),
                message_event_id=message_event_id,
                source_message_key=source_message_key,
            )
            logger.info(
                "[CATALOG_ORDER_DRAFT] new draft external_id tenant=%s conv=%s "
                "prev_order=%s external_id=%s reason=closed_prior_order",
                tenant_id,
                conversation_id,
                getattr(existing, "id", None),
                external_id_override,
            )
            existing = None

        if customer is None:
            try:
                from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

                svc = CustomerIntelligenceService(db, tenant_id)
                customer = svc.upsert_lead_customer(
                    phone=phone,
                    source="whatsapp_inbound",
                    extra_metadata={"channel": "whatsapp"},
                    commit=False,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[CATALOG_ORDER_DRAFT] customer upsert failed tenant=%s conv=%s",
                    tenant_id,
                    conversation_id,
                )
                customer = None

        order_prep = build_catalog_order_prep(
            meta=meta,
            line_items=line_items,
            payload=payload,
        )
        brain_state = build_catalog_brain_state(meta=meta, line_items=line_items)
        total_price, currency = _resolve_catalog_totals(meta, line_items)
        extra_meta = build_catalog_order_metadata(
            meta=meta,
            message_event_id=message_event_id,
            item_count=len(line_items),
            total_price=total_price,
            currency=currency,
            source_message_key=source_message_key,
        )

        order = sync_nahla_wa_order(
            db,
            tenant_id=tenant_id,
            conversation=conversation,
            brain_state=brain_state,
            order_prep=order_prep,
            trigger="catalog_order_immediate",
            customer=customer,
            force_catalog_draft=True,
            extra_order_metadata=extra_meta,
            external_id_override=external_id_override,
        )
        if order is not None:
            logger.info(
                "[CATALOG_ORDER_DRAFT] persisted tenant=%s conv=%s order_id=%s "
                "items=%d total=%s currency=%s message_event=%s",
                tenant_id,
                conversation_id,
                getattr(order, "id", None),
                len(line_items),
                total_price,
                currency,
                message_event_id,
            )
        return order
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_ORDER_DRAFT] persist failed tenant=%s conv=%s",
            tenant_id,
            conversation_id,
        )
        return None


__all__ = [
    "build_catalog_brain_state",
    "build_catalog_order_metadata",
    "build_catalog_order_prep",
    "enrich_catalog_line_items_for_draft",
    "is_catalog_order_inbound",
    "persist_catalog_order_immediate_draft",
]
