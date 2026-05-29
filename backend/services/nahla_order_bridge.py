"""
services/nahla_order_bridge.py
──────────────────────────────
Phase 1 — confirmed WhatsApp bank-transfer receipt → internal paid Order.

Additive bridge only. Does NOT touch the conversation brain, payment
classifier, or store adapters. Called from ``order_flow.apply_state_patch``
when ``payment_receipt_received`` flips to True.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.order_bridge")

_NAHL_WA_EXT_PREFIX = "nahla-wa-"


def nahla_wa_external_id(tenant_id: int, conversation_id: int) -> str:
    return f"{_NAHL_WA_EXT_PREFIX}{tenant_id}-{conversation_id}"


def _parse_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        amt = float(value)
        return amt if amt > 0 else None
    text = str(value).replace("ر.س", "").replace("SAR", "").replace(",", "").strip()
    if not text:
        return None
    try:
        amt = float(text.split()[0])
        return amt if amt > 0 else None
    except (TypeError, ValueError):
        return None


def _resolve_order_amount(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    receipt_metadata: Dict[str, Any],
    line_items: List[Dict[str, Any]],
) -> Tuple[Optional[float], bool]:
    """
    Return ``(amount_sar, needs_amount_review)``.

    Priority:
      1. order_prep.total_price / order_prep.price
      2. current_product_focus.price
      3. receipt extraction (compute_receipt_fields)
      4. line-item unit price × qty
      5. unknown → None + needs_amount_review=True
    """
    for key in ("total_price", "price"):
        amt = _parse_amount(order_prep.get(key))
        if amt is not None:
            return amt, False

    focus = brain_state.get("current_product_focus") or {}
    if isinstance(focus, dict):
        amt = _parse_amount(focus.get("price"))
        if amt is not None:
            return amt, False

    try:
        from core.receipt_extraction import compute_receipt_fields  # noqa: PLC0415

        fields = compute_receipt_fields(metadata=receipt_metadata or {})
        for extracted in fields.amounts or ():
            raw_val = getattr(extracted, "value", None)
            amt = _parse_amount(raw_val)
            if amt is not None:
                return amt, False
    except Exception as exc:  # noqa: BLE001
        logger.debug("[NAHLA_ORDER_BRIDGE] receipt amount extraction failed: %s", exc)

    for item in line_items:
        unit = _parse_amount(item.get("unit_price") or item.get("price"))
        qty = item.get("quantity") or 1
        try:
            qty_n = max(int(qty), 1)
        except (TypeError, ValueError):
            qty_n = 1
        if unit is not None:
            return round(unit * qty_n, 2), False

    return None, True


def _build_line_items(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    focus = brain_state.get("current_product_focus") or {}
    if not isinstance(focus, dict):
        focus = {}
    product_name = (
        str(focus.get("title") or focus.get("name") or order_prep.get("selected_product") or "")
        .strip()
        or "منتج"
    )
    qty_raw = order_prep.get("quantity") or 1
    try:
        quantity = max(int(qty_raw), 1)
    except (TypeError, ValueError):
        quantity = 1
    unit_price = _parse_amount(focus.get("price") or order_prep.get("total_price") or order_prep.get("price"))
    item: Dict[str, Any] = {
        "product_name": product_name,
        "quantity":     quantity,
        "product_id":   focus.get("id") or order_prep.get("product_id"),
    }
    if unit_price is not None:
        item["unit_price"] = unit_price
    return [item]


def _format_total_sar(amount: Optional[float]) -> Optional[str]:
    if amount is None or amount <= 0:
        return None
    return f"{amount:.2f} ر.س"


def _allocate_nhl_number(db: Any, tenant_id: int) -> str:
    from sqlalchemy import func  # noqa: PLC0415
    from models import Order  # noqa: PLC0415

    existing_count = (
        db.query(func.count(Order.id))
        .filter(
            Order.tenant_id == tenant_id,
            Order.external_id.like(f"{_NAHL_WA_EXT_PREFIX}{tenant_id}-%"),
        )
        .scalar()
    ) or 0
    seq = int(existing_count) + 1
    return f"NHL-{tenant_id}-{seq:06d}"


def _customer_payload(conversation: Any, order_prep: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    customer = getattr(conversation, "customer", None)
    phone = None
    name = None
    if customer is not None:
        phone = getattr(customer, "phone", None) or getattr(customer, "mobile", None)
        name = getattr(customer, "name", None)
    meta = getattr(conversation, "extra_metadata", None) or {}
    if not phone and isinstance(meta, dict):
        phone = meta.get("phone")
    first = str(order_prep.get("customer_first_name") or "").strip()
    last = str(order_prep.get("customer_last_name") or "").strip()
    full_name = " ".join(p for p in (first, last) if p).strip() or (name or "")
    customer_info = {
        "name":  full_name or None,
        "phone": phone,
        "city":  order_prep.get("city"),
    }
    return full_name or None, customer_info


def upsert_nahla_paid_order(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> Optional[Any]:
    """
    Upsert a paid Nahla-native order for a confirmed transfer receipt.

    Idempotent on ``external_id = nahla-wa-{tenant_id}-{conversation_id}``.
    Never raises — failures are logged and swallowed so receipt ACK flow
    is never blocked.
    """
    if not bool(order_prep.get("payment_receipt_received")):
        return None

    conversation_id = getattr(conversation, "id", None)
    if not conversation_id:
        logger.info(
            "[NAHLA_ORDER_BRIDGE] skip — missing conversation_id tenant=%s",
            tenant_id,
        )
        return None

    try:
        from models import Order  # noqa: PLC0415

        external_id = nahla_wa_external_id(tenant_id, int(conversation_id))
        receipt_metadata = dict(order_prep.get("payment_receipt_metadata") or {})
        line_items = _build_line_items(order_prep=order_prep, brain_state=brain_state)
        amount, needs_review = _resolve_order_amount(
            order_prep=order_prep,
            brain_state=brain_state,
            receipt_metadata=receipt_metadata,
            line_items=line_items,
        )
        total_str = _format_total_sar(amount)
        customer_name, customer_info = _customer_payload(conversation, order_prep)

        confirmed_at = (
            str(order_prep.get("payment_receipt_at") or "").strip()
            or datetime.now(timezone.utc).isoformat()
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        existing = (
            db.query(Order)
            .filter_by(tenant_id=tenant_id, external_id=external_id)
            .first()
        )

        base_meta: Dict[str, Any] = {
            "source_kind":           "nahla_order",
            "source":                "ai_sales_agent",
            "created_via":           "nahla_order_bridge",
            "conversation_id":       conversation_id,
            "payment_confirmed_at":  confirmed_at,
            "payment_receipt_metadata": receipt_metadata,
            "needs_amount_review":   needs_review,
            "created_at":            confirmed_at,
        }

        if existing is not None:
            meta = dict(existing.extra_metadata or {})
            meta.update(base_meta)
            if total_str:
                existing.total = total_str
                meta["needs_amount_review"] = False
            elif meta.get("needs_amount_review") is None:
                meta["needs_amount_review"] = needs_review
            existing.status = "paid"
            existing.source = "whatsapp"
            existing.is_abandoned = False
            existing.line_items = line_items or existing.line_items
            if customer_name:
                existing.customer_name = customer_name
            if customer_info:
                existing.customer_info = {**(existing.customer_info or {}), **customer_info}
            existing.extra_metadata = meta
            db.add(existing)
            logger.info(
                "[NAHLA_ORDER_BRIDGE] updated tenant=%s conv=%s order_id=%s "
                "external_id=%s amount=%s needs_review=%s",
                tenant_id, conversation_id, existing.id, external_id,
                total_str or "unknown", meta.get("needs_amount_review"),
            )
            return existing

        order = Order(
            tenant_id             = tenant_id,
            external_id           = external_id,
            external_order_number = _allocate_nhl_number(db, tenant_id),
            status                = "paid",
            total                 = total_str,
            customer_name         = customer_name,
            customer_info         = customer_info,
            line_items            = line_items,
            checkout_url          = None,
            is_abandoned          = False,
            source                = "whatsapp",
            extra_metadata        = base_meta,
        )
        db.add(order)
        db.flush()
        logger.info(
            "[NAHLA_ORDER_BRIDGE] created tenant=%s conv=%s order_id=%s "
            "external_id=%s number=%s amount=%s needs_review=%s",
            tenant_id, conversation_id, order.id, external_id,
            order.external_order_number, total_str or "unknown", needs_review,
        )
        return order
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[NAHLA_ORDER_BRIDGE] upsert failed tenant=%s conv=%s: %s",
            tenant_id, conversation_id, exc,
        )
        return None
