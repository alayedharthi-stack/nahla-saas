"""
services/cod_confirmation.py
─────────────────────────────
Cash-on-Delivery confirmation flow.

The flow has two halves and they live in this module so the contract is
in one place rather than scattered between routers/ai_sales.py and the
WhatsApp webhook:

  Step 1 — send_cod_confirmation_template(db, tenant_id, order)
    Triggered by POST /api/v1/ai-sales/create-order when the customer
    chose `cash_on_delivery`. Sends the APPROVED Meta template
    `cod_order_confirmation_ar` (named slots: customer_name, product_name,
    order_amount) with QUICK_REPLY buttons "تأكيد الطلب ✅" / "إلغاء
    الطلب ❌". The local order is stored in status `pending_confirmation`
    and is NOT yet pushed to the merchant store.

  Step 2 — handle_cod_reply(db, tenant_id, customer_phone, button_text)
    Triggered by the WhatsApp webhook when the customer taps a button
    or replies with the literal button text. Looks up the most-recent
    `pending_confirmation` Order on this tenant for this normalised
    phone, then either:
      • confirm  → pushes the order to the store via
                   store_integration.order_service.create_order, sets the
                   local status to `under_review` (Salla's slug for
                   "بإنتظار المراجعة"), and saves the returned external
                   order id.
      • cancel   → sets the local status to `cancelled`.
    No action taken if the customer has no pending COD order, so a
    button-text false positive is harmless.

The state names `pending_confirmation` and `under_review` are deliberate
and match what Salla's Orders API returns for `payment_method=cod` orders
(`under_review` = "بإنتظار المراجعة", the slug exposed in
backend/services/store_sync.py::_extract_status_string). If a future
adapter has different status slugs, document them here and map at the
call site — do not silently rename `under_review`.

Every transition is logged through observability.event_logger so the
"COD funnel" can be inspected per tenant from the dashboard.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.cod_confirmation")


# Status names used in this flow. Centralised so tests and callers don't
# drift apart.
STATUS_PENDING_CUSTOMER  = "pending_confirmation"
STATUS_PENDING_MERCHANT  = "under_review"
STATUS_CANCELLED         = "cancelled"

# Customer reply matchers. We accept the full button text plus a small
# whitelist of free-text equivalents Saudi customers commonly type when
# the QUICK_REPLY UI is not shown (e.g. on plain SMS gateways that don't
# render WhatsApp buttons).
_CONFIRM_TEXTS: tuple[str, ...] = (
    "تأكيد الطلب ✅",
    "تأكيد الطلب",
    "تأكيد",
    "اكد",
    "أكد",
    "أؤكد",
    "موافق",
    "نعم",
    "yes",
    "confirm",
    "ok",
)
_CANCEL_TEXTS: tuple[str, ...] = (
    "إلغاء الطلب ❌",
    "الغاء الطلب",
    "إلغاء",
    "الغاء",
    "لا",
    "no",
    "cancel",
)


def classify_cod_reply(text: str) -> Optional[str]:
    """
    Map a customer reply to one of: 'confirm' | 'cancel' | None.
    Case-insensitive, whitespace-trimmed. Returns None when the message
    isn't a COD response — caller should then fall through to the normal
    AI reply path so we don't break unrelated conversations.
    """
    if not text:
        return None
    norm = text.strip().lower()
    if norm in {t.lower() for t in _CONFIRM_TEXTS}:
        return "confirm"
    if norm in {t.lower() for t in _CANCEL_TEXTS}:
        return "cancel"
    return None


async def send_cod_confirmation_template(
    db,
    *,
    tenant_id: int,
    order: Any,
    customer_phone: str,
    customer_name: str,
    product_name: str,
    total_amount: str,
) -> Dict[str, Any]:
    """
    Send the cod_order_confirmation_ar template. Best-effort: failures
    log loudly but do not raise — the order itself is already durable.
    Returns a dict with `sent` (bool), `wa_message_id` (or None), and
    `error` (optional string) for the caller to log alongside the order.
    """
    from models import WhatsAppConnection, WhatsAppTemplate  # noqa: PLC0415
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415
    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

    template_name = "cod_order_confirmation_ar"

    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.tenant_id == tenant_id,
            WhatsAppConnection.status    == "connected",
        )
        .first()
    )
    if not wa_conn:
        logger.warning(
            "[COD] tenant=%s order=%s: no connected WhatsApp — skipping template send",
            tenant_id, getattr(order, "id", None),
        )
        return {"sent": False, "error": "no_whatsapp_connection"}

    template = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == tenant_id,
            WhatsAppTemplate.name      == template_name,
            WhatsAppTemplate.status    == "APPROVED",
        )
        .first()
    )
    if not template:
        logger.warning(
            "[COD] tenant=%s order=%s: template %s not APPROVED — skipping send",
            tenant_id, getattr(order, "id", None), template_name,
        )
        return {"sent": False, "error": "template_not_approved"}

    to = normalize_phone(customer_phone) or customer_phone
    body_params = [
        {"type": "text", "text": str(customer_name or "عميلنا الكريم")},
        {"type": "text", "text": str(product_name or "طلبك")},
        {"type": "text", "text": str(total_amount or "—")},
    ]
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to":                to,
        "type":              "template",
        "template": {
            "name":       template_name,
            "language":   {"code": template.language or "ar"},
            "components": [{"type": "body", "parameters": body_params}],
        },
    }

    try:
        response, _ = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="send_template",
            phone_id=wa_conn.phone_number_id,
            payload=payload,
        )
        wa_msg_id = (response or {}).get("messages", [{}])[0].get("id")
        return {"sent": True, "wa_message_id": wa_msg_id, "error": None}
    except Exception as exc:
        logger.error(
            "[COD] tenant=%s order=%s template send failed: %s",
            tenant_id, getattr(order, "id", None), exc,
        )
        return {"sent": False, "error": str(exc)[:200]}


def find_pending_cod_order(
    db, *, tenant_id: int, customer_phone: str
) -> Optional[Any]:
    """
    Return the most-recent Order in status pending_confirmation for this
    tenant + normalised phone, or None. Used by the webhook to bind a
    QUICK_REPLY tap to the right order without trusting any client-side id.
    """
    from models import Order  # noqa: PLC0415
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    normalized = normalize_phone(customer_phone) or customer_phone
    candidates = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
            Order.status    == STATUS_PENDING_CUSTOMER,
        )
        .order_by(Order.id.desc())
        .limit(50)
        .all()
    )
    for o in candidates:
        info = o.customer_info or {}
        for k in ("phone", "mobile"):
            v = info.get(k)
            if not v:
                continue
            if normalize_phone(str(v)) == normalized or str(v) == customer_phone:
                return o
    return None


async def handle_cod_reply(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
) -> Tuple[Optional[str], Optional[Any]]:
    """
    Process a customer's COD reply. Returns (decision, order) where
    decision is 'confirm' | 'cancel' | None and order is the affected
    Order row (or None when there was no pending order to match).

    On 'confirm':
      • status moves pending_confirmation → under_review
      • order is pushed to the store adapter (best-effort; failure is
        logged but the local transition still lands so the merchant can
        act on it from the dashboard)
      • external_id is updated when the store returns one
    On 'cancel':
      • status moves pending_confirmation → cancelled
    """
    decision = classify_cod_reply(text)
    if decision is None:
        return None, None

    order = find_pending_cod_order(
        db, tenant_id=tenant_id, customer_phone=customer_phone,
    )
    if order is None:
        return decision, None

    from observability.event_logger import log_event  # noqa: PLC0415

    if decision == "cancel":
        order.status = STATUS_CANCELLED
        meta = dict(order.extra_metadata or {})
        meta["cod_cancelled_at"] = datetime.now(timezone.utc).isoformat()
        order.extra_metadata = meta
        flag_modified(order, "extra_metadata")
        log_event(
            db, tenant_id, category="order", event_type="order.cod.cancelled",
            summary=f"COD order #{order.id} cancelled by customer",
            severity="info",
            payload={"order_id": order.id, "reply_text": text[:120]},
            reference_id=str(order.id),
        )
        db.commit()
        return decision, order

    # decision == "confirm"
    order.status = STATUS_PENDING_MERCHANT
    meta = dict(order.extra_metadata or {})
    meta["cod_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    order.extra_metadata = meta
    flag_modified(order, "extra_metadata")
    log_event(
        db, tenant_id, category="order", event_type="order.cod.confirmed",
        summary=f"COD order #{order.id} confirmed by customer — pushing to store",
        severity="info",
        payload={"order_id": order.id, "reply_text": text[:120]},
        reference_id=str(order.id),
    )

    # Push to the store adapter. Best-effort. The order is already in
    # under_review locally so the merchant sees it even if the push fails.
    pushed_external_id = await _push_cod_to_store(db, tenant_id, order)
    if pushed_external_id:
        order.external_id = pushed_external_id
        meta["cod_pushed_external_id"] = pushed_external_id
        order.extra_metadata = meta
        flag_modified(order, "extra_metadata")
        log_event(
            db, tenant_id, category="order", event_type="order.cod.pushed_to_store",
            summary=f"COD order #{order.id} pushed to store as {pushed_external_id}",
            severity="info",
            payload={"order_id": order.id, "external_id": pushed_external_id},
            reference_id=str(order.id),
        )

    db.commit()
    return decision, order


async def _push_cod_to_store(db, tenant_id: int, order: Any) -> Optional[str]:
    """
    Push a now-confirmed COD order to the merchant's store adapter.
    Returns the external order id on success, None on any failure.
    """
    info  = order.customer_info or {}
    items = order.line_items or []

    try:
        from store_integration.models import (  # noqa: PLC0415
            OrderInput as StoreOrderInput,
            OrderItemInput as StoreOrderItem,
        )
        from store_integration.order_service import create_order as store_create  # noqa: PLC0415
    except Exception as exc:
        logger.error("[COD] store_integration import failed: %s", exc)
        return None

    store_items: list = []
    for it in items:
        pid = it.get("product_id") or it.get("id") or 0
        store_items.append(StoreOrderItem(
            product_id = str(pid),
            variant_id = str(it["variant_id"]) if it.get("variant_id") else None,
            quantity   = int(it.get("quantity") or 1),
        ))
    if not store_items:
        return None

    order_input = StoreOrderInput(
        customer_name   = info.get("name") or "",
        customer_phone  = info.get("phone") or info.get("mobile") or "",
        building_number = info.get("building_number") or "",
        street          = info.get("street") or "",
        district        = info.get("district") or "",
        postal_code     = info.get("postal_code") or "",
        city            = info.get("city") or "",
        address         = info.get("address") or "",
        payment_method  = "cod",
        items           = store_items,
        notes           = (order.extra_metadata or {}).get("notes") or "",
    )
    try:
        store_order = await store_create(tenant_id, order_input)
    except Exception as exc:
        logger.error("[COD] store create_order failed tenant=%s: %s", tenant_id, exc)
        return None
    if store_order is None:
        return None
    return getattr(store_order, "id", None)
