"""
services/cod_confirmation.py
─────────────────────────────
Cash-on-Delivery confirmation flow.

The flow has two halves and they live in this module so the contract is
in one place rather than scattered between routers/ai_sales.py and the
WhatsApp webhook:

  Step 1 — checkout policy then optional send_cod_confirmation_template
    Triggered by POST /api/v1/ai-sales/create-order when the customer
    chose `cash_on_delivery`.
      • confirmation ENABLED → local `pending_confirmation`, send once,
        wait for confirm/cancel, then push to the store.
      • confirmation DISABLED → do not strand pending_confirmation; do
        not send; push immediately through the normal COD store path.
      • settings READ failure → fail closed (no send, no silent push).

    Customer-facing send is owned exclusively here. It uses the canonical
    order-updates `service_key=cod_confirmation` active APPROVED revision:
      OPEN 24h  → same revision as interactive/session buttons
      CLOSED 24h → same revision as Meta template
    Buttons stay deterministic: "تأكيد الطلب ✅" / "إلغاء الطلب ❌".

    StoreSync / Salla first observation of `under_review` MUST NOT send
    another confirmation request.

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
    Recognized button payloads (`nahla_cod_confirm` / `nahla_cod_cancel`)
    are always consumed by the webhook even when no pending order exists.
    Unrecognized buttons fall through to generic merchant routing.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified

from core.customer_display import (
    display_name_passthrough_or_fallback as _customer_display_passthrough,
)

logger = logging.getLogger("nahla.cod_confirmation")


# Status names used in this flow. Centralised so tests and callers don't
# drift apart.
STATUS_PENDING_CUSTOMER  = "pending_confirmation"
STATUS_PENDING_MERCHANT  = "under_review"
STATUS_CANCELLED         = "cancelled"
CANONICAL_SERVICE_KEY    = "cod_confirmation"

COD_INBOUND_CONSUMED = "consumed"
COD_INBOUND_PASSTHROUGH = "passthrough"

COD_CHECKOUT_WAIT_FOR_CUSTOMER = "wait_for_customer"
COD_CHECKOUT_PUSH_IMMEDIATE = "push_immediate"
COD_CHECKOUT_SETTINGS_UNAVAILABLE = "settings_unavailable"

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

_COD_BUTTON_TITLES: tuple[str, str] = (
    "تأكيد الطلب ✅",
    "إلغاء الطلب ❌",
)

_COD_CONFIRM_ID = "nahla_cod_confirm"
_COD_CANCEL_ID = "nahla_cod_cancel"


def parse_cod_button_payload(raw: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse deterministic COD button ids. Never treats an unverified id as truth."""
    text = str(raw or "").strip()
    if not text:
        return None, None
    lower = text.lower()
    for action, token in (("confirm", _COD_CONFIRM_ID), ("cancel", _COD_CANCEL_ID)):
        if lower == token:
            return action, None
        prefix = f"{token}:"
        if lower.startswith(prefix):
            rest = text[len(prefix):].strip()
            if rest.isdigit():
                return action, int(rest)
            return action, None
    return None, None


def is_owned_cod_button_payload(raw: Optional[str]) -> bool:
    """True when the WhatsApp button id/payload is a Nahla COD control."""
    action, _oid = parse_cod_button_payload(raw or "")
    return action is not None


async def intercept_cod_button_inbound(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
    button_payload: Optional[str],
) -> Tuple[str, Optional[str], Optional[Any]]:
    """
    Own interactive / template-button inbound for recognized COD payloads.

    Returns ``(consumed|passthrough, decision, order)``. ``consumed`` means
    the webhook must return without conversational routing, even when
    ``order`` is None (duplicate, stale, foreign id, or no pending order).

    A non-empty unrecognized payload is never stolen via visible title.
    """
    if not is_owned_cod_button_payload(button_payload):
        return COD_INBOUND_PASSTHROUGH, None, None
    decision, order = await handle_cod_reply(
        db,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        text=text,
        button_payload=button_payload,
    )
    return COD_INBOUND_CONSUMED, decision, order


class CodOrderingDisabled(Exception):
    """Merchant disabled COD as a payment method (not the notification)."""


class CodCheckoutSettingsUnavailable(Exception):
    """Settings read failed; caller must not choose send vs immediate push."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CodCheckoutPlan:
    policy: str
    reason: Optional[str]
    local_status: Optional[str]
    push_to_store_now: bool
    send_confirmation: bool


def plan_cod_checkout(db, tenant_id: int) -> CodCheckoutPlan:
    """Decide wait-for-button vs immediate store push. Never treats DB failure as disabled."""
    from core.commerce_lifecycle.order_updates import (  # noqa: PLC0415
        REASON_SETTINGS_UNAVAILABLE,
        evaluate_order_update_delivery,
        load_order_update_settings_truth,
    )

    truth = load_order_update_settings_truth(db, int(tenant_id))
    if not truth.available:
        return CodCheckoutPlan(
            policy=COD_CHECKOUT_SETTINGS_UNAVAILABLE,
            reason=truth.reason or REASON_SETTINGS_UNAVAILABLE,
            local_status=None,
            push_to_store_now=False,
            send_confirmation=False,
        )
    allowed, reason = evaluate_order_update_delivery(
        db, int(tenant_id), CANONICAL_SERVICE_KEY
    )
    if allowed:
        return CodCheckoutPlan(
            policy=COD_CHECKOUT_WAIT_FOR_CUSTOMER,
            reason=None,
            local_status=STATUS_PENDING_CUSTOMER,
            push_to_store_now=False,
            send_confirmation=True,
        )
    return CodCheckoutPlan(
        policy=COD_CHECKOUT_PUSH_IMMEDIATE,
        reason=reason,
        local_status=STATUS_PENDING_MERCHANT,
        push_to_store_now=True,
        send_confirmation=False,
    )


def require_cod_checkout_plan(
    db,
    tenant_id: int,
    *,
    ordering_allowed: bool,
) -> CodCheckoutPlan:
    """COD payment-method gate, then confirmation-notification policy."""
    if not ordering_allowed:
        raise CodOrderingDisabled()
    plan = plan_cod_checkout(db, tenant_id)
    if plan.policy == COD_CHECKOUT_SETTINGS_UNAVAILABLE:
        raise CodCheckoutSettingsUnavailable(
            plan.reason or "order_update_settings_unavailable"
        )
    return plan


def disabled_confirmation_bypass_metadata(
    reason: Optional[str],
    *,
    external_id: Optional[str] = None,
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "cod_confirmation_bypassed": True,
        "cod_confirmation_bypass_reason": reason or "order_update_disabled",
    }
    if external_id:
        meta["cod_pushed_external_id"] = str(external_id)
    return meta


def classify_cod_reply(text: str) -> Optional[str]:
    """
    Map a customer reply to one of: 'confirm' | 'cancel' | None.
    Case-insensitive, whitespace-trimmed. Returns None when the message
    isn't a COD response — caller should then fall through to the normal
    AI reply path so we don't break unrelated conversations.
    """
    action, _oid = parse_cod_button_payload(text)
    if action is not None:
        return action
    if not text:
        return None
    norm = text.strip().lower()
    if norm in {t.lower() for t in _CONFIRM_TEXTS}:
        return "confirm"
    if norm in {t.lower() for t in _CANCEL_TEXTS}:
        return "cancel"
    return None


def _order_phone_matches(order: Any, customer_phone: str) -> bool:
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    normalized = normalize_phone(customer_phone) or customer_phone
    info = getattr(order, "customer_info", None) or {}
    if not isinstance(info, dict):
        return False
    for k in ("phone", "mobile"):
        v = info.get(k)
        if not v:
            continue
        if normalize_phone(str(v)) == normalized or str(v) == customer_phone:
            return True
    return False


def nahla_owns_cod_customer_confirmation(order: Any) -> bool:
    """True when Nahla checkout already requested or resolved COD confirm."""
    meta = getattr(order, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return False
    return bool(
        meta.get("nahla_cod_confirmation_sent")
        or meta.get("cod_confirmed_at")
        or meta.get("cod_cancelled_at")
        or meta.get("cod_pushed_external_id")
        or meta.get("cod_confirmation_bypassed")
    )


def _stamp_cod_confirmation_sent(order: Any, *, template: Any, send_method: str) -> None:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    meta["nahla_cod_confirmation_sent"] = True
    meta["nahla_cod_confirmation_sent_at"] = datetime.now(timezone.utc).isoformat()
    meta["nahla_cod_confirmation_service_key"] = CANONICAL_SERVICE_KEY
    meta["nahla_cod_confirmation_send_method"] = send_method
    if template is not None:
        meta["nahla_cod_confirmation_template_id"] = getattr(template, "id", None)
        meta["nahla_cod_confirmation_template_name"] = getattr(template, "name", None)
        meta["nahla_cod_confirmation_revision"] = getattr(template, "revision", None)
    order.extra_metadata = meta
    try:
        flag_modified(order, "extra_metadata")
    except Exception:  # noqa: silent-ok — SimpleNamespace orders in tests have no SA state
        pass


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
    Send the canonical ``cod_confirmation`` lifecycle revision.

    Failures log loudly but do not raise — the order itself is already durable.
    Returns a dict with `sent` (bool), `wa_message_id` (or None), and
    `error` (optional string) for the caller to log alongside the order.

    The hard-named legacy Meta template is no longer a send owner.
    """
    from core.commerce_lifecycle.canary_guard import (  # noqa: PLC0415
        MODE_LEGACY_LIFECYCLE,
        evaluate_and_audit,
    )
    from core.commerce_lifecycle.order_updates import (  # noqa: PLC0415
        evaluate_order_update_delivery,
        resolve_lifecycle_template_for_send,
    )
    from core.commerce_lifecycle.window import lifecycle_service_window_is_open  # noqa: PLC0415
    from core.automation_engine import (  # noqa: PLC0415
        send_lifecycle_whatsapp_session_body,
        send_lifecycle_whatsapp_template,
    )
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    canary = evaluate_and_audit(
        int(tenant_id),
        phone=customer_phone,
        sender_path="cod_confirmation",
        mode=MODE_LEGACY_LIFECYCLE,
        automation_type="cod_confirmation",
    )
    if not canary.allowed:
        logger.info(
            "[COD] tenant=%s order=%s: canary gate %s",
            tenant_id, getattr(order, "id", None), canary.reason,
        )
        return {"sent": False, "error": canary.reason, "canary_blocked": True}

    allowed, flag_reason = evaluate_order_update_delivery(
        db, int(tenant_id), CANONICAL_SERVICE_KEY
    )
    if not allowed:
        logger.info(
            "[COD] tenant=%s order=%s: delivery blocked %s",
            tenant_id, getattr(order, "id", None), flag_reason,
        )
        return {"sent": False, "error": flag_reason or "order_update_disabled"}

    template = resolve_lifecycle_template_for_send(
        db, int(tenant_id), CANONICAL_SERVICE_KEY
    )
    if template is None:
        logger.warning(
            "[COD] tenant=%s order=%s: no APPROVED cod_confirmation revision",
            tenant_id, getattr(order, "id", None),
        )
        return {"sent": False, "error": "no_approved_template"}

    to = normalize_phone(customer_phone) or customer_phone
    window_open, window_source = lifecycle_service_window_is_open(
        db, int(tenant_id), to
    )
    send_method = "session_message" if window_open else "approved_template"
    payload: Dict[str, Any] = {
        "order_number": str(
            getattr(order, "external_order_number", None)
            or getattr(order, "id", "")
            or ""
        ),
        "order_id": str(getattr(order, "id", "") or ""),
        "product_name": str(product_name or "طلبك"),
        "total": str(total_amount or ""),
        "amount": str(total_amount or ""),
        "payment_method": "cod",
        "customer_name": _customer_display_passthrough(customer_name),
    }
    last_mile_kwargs = dict(
        customer_name=_customer_display_passthrough(customer_name),
        service_key=CANONICAL_SERVICE_KEY,
        canary_mode=MODE_LEGACY_LIFECYCLE,
        canary_automation_type="cod_confirmation",
        canary_sender_path="cod_confirmation",
    )
    try:
        if send_method == "session_message":
            outcome, info = await send_lifecycle_whatsapp_session_body(
                db, int(tenant_id), to, template, payload, **last_mile_kwargs
            )
        else:
            outcome, info = await send_lifecycle_whatsapp_template(
                db, int(tenant_id), to, template, payload, **last_mile_kwargs
            )
    except Exception as exc:
        logger.error(
            "[COD] tenant=%s order=%s canonical send failed: %s",
            tenant_id, getattr(order, "id", None), exc,
        )
        return {
            "sent": False,
            "error": str(exc)[:200],
            "template_name": getattr(template, "name", None),
            "service_key": CANONICAL_SERVICE_KEY,
        }

    if outcome != "sent":
        logger.warning(
            "[COD] tenant=%s order=%s send outcome=%s error=%s",
            tenant_id,
            getattr(order, "id", None),
            outcome,
            (info or {}).get("error_code"),
        )
        return {
            "sent": False,
            "error": str((info or {}).get("error_code") or outcome),
            "template_name": getattr(template, "name", None),
            "service_key": CANONICAL_SERVICE_KEY,
            "send_method": send_method,
            "window_source": window_source,
            "canary_blocked": bool((info or {}).get("canary_blocked")),
        }

    _stamp_cod_confirmation_sent(order, template=template, send_method=send_method)
    try:
        from routers.conversations import record_outbound_message  # noqa: PLC0415
        record_outbound_message(
            db, tenant_id, to, f"[{getattr(template, 'name', CANONICAL_SERVICE_KEY)}]",
            event_type="cod_confirmation",
            customer_name=customer_name,
            extra={
                "template_name": getattr(template, "name", None),
                "service_key": CANONICAL_SERVICE_KEY,
                "order_id": getattr(order, "id", None),
                "send_method": send_method,
            },
        )
    except Exception:  # noqa: silent-ok — conversation log must not fail the COD send
        pass

    return {
        "sent": True,
        "wa_message_id": (info or {}).get("wa_message_id"),
        "error": None,
        "template_name": getattr(template, "name", None),
        "template_id": getattr(template, "id", None),
        "revision": getattr(template, "revision", None),
        "service_key": CANONICAL_SERVICE_KEY,
        "send_method": send_method,
        "window_source": window_source,
        "buttons": list(_COD_BUTTON_TITLES),
    }


def find_pending_cod_orders(
    db, *, tenant_id: int, customer_phone: str
) -> list:
    """Return pending_confirmation orders for this tenant + normalised phone."""
    from models import Order  # noqa: PLC0415

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
    return [o for o in candidates if _order_phone_matches(o, customer_phone)]


def find_pending_cod_order(
    db, *, tenant_id: int, customer_phone: str
) -> Optional[Any]:
    """
    Bind a reply only when exactly one pending COD order exists for this
    tenant + phone. Ambiguous multiples must not guess.
    """
    matches = find_pending_cod_orders(
        db, tenant_id=tenant_id, customer_phone=customer_phone
    )
    if len(matches) == 1:
        return matches[0]
    return None


def _load_bound_pending_cod_order(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    order_id: int,
) -> Optional[Any]:
    """Server-side bind: tenant + pending status + phone must all match."""
    from models import Order  # noqa: PLC0415

    order = (
        db.query(Order)
        .filter(
            Order.id == int(order_id),
            Order.tenant_id == int(tenant_id),
            Order.status == STATUS_PENDING_CUSTOMER,
        )
        .first()
    )
    if order is None:
        return None
    if not _order_phone_matches(order, customer_phone):
        return None
    return order


async def handle_cod_reply(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
    button_payload: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Any]]:
    """
    Process a customer's COD reply. Returns (decision, order) where
    decision is 'confirm' | 'cancel' | None and order is the affected
    Order row (or None when there was no pending order to match).

    Button ids ``nahla_cod_confirm`` / ``nahla_cod_cancel`` (optionally
    ``:order_id``) bind first. A client-supplied order id is never trusted
    unless the row is still ``pending_confirmation`` for this tenant and
    phone. Text fallback binds only when exactly one pending COD order
    exists for that customer.
    """
    payload_action, payload_oid = parse_cod_button_payload(button_payload or "")
    text_action, text_oid = parse_cod_button_payload(text)
    decision = payload_action or text_action or classify_cod_reply(text)
    if decision is None:
        return None, None

    bound_oid = payload_oid or text_oid
    if bound_oid is not None:
        order = _load_bound_pending_cod_order(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            order_id=bound_oid,
        )
    else:
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
