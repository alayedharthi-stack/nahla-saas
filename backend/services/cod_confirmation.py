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
      OPEN/CLOSED 24h → the same approved Meta image template
    Buttons stay deterministic: "تأكيد الطلب ✅" / "إلغاء الطلب ❌".

    StoreSync / Salla first observation of `under_review` MUST NOT send
    another confirmation request.

  Step 2 — handle_cod_reply(db, tenant_id, customer_phone, button_text)
    Triggered by the WhatsApp webhook when the customer taps a button
    or replies with the literal button text. Looks up the most-recent
    `pending_confirmation` Order on this tenant for this normalised
    phone, then either:
      • confirm  → creates a missing Salla order or updates an existing
                   Salla-origin order to `under_review` (Salla's slug for
                   "بإنتظار المراجعة"). Only after provider success is
                   the local status changed and the normal order-confirmation
                   template dispatched.
      • cancel   → updates an existing Salla order to `cancelled` first,
                   then mirrors the proven state locally.
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

_STORE_PENDING_CONFIRMATION_STATUSES = frozenset({
    "payment_pending",
    "pending_payment",
    "waiting_payment",
    "awaiting_payment",
    # Salla creates storefront COD orders as unpaid + in_progress. This state
    # is eligible only when the order also carries the server-side COD prompt
    # stamp checked below; ordinary in-progress orders remain ineligible.
    "in_progress",
})

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
    "إلغاء الطلب",
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
_COD_METHODS = frozenset({"cod", "cash_on_delivery", "cod_payment", "cash"})


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


def resolve_owned_cod_button_payload_from_context(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    button_text: str,
    context_wamid: Optional[str],
) -> Optional[str]:
    """Recover a COD action only from a correlated outbound COD prompt.

    Meta template quick replies can arrive as type=button with a template
    default payload instead of Nahla's deterministic payload when the outbound
    template component omitted the runtime payload. The visible Arabic title
    is never sufficient: ownership requires reply context.id to match a sent
    order_cod_pending automation execution, and the event's order must match
    tenant, customer, phone, external identity, and COD event evidence.

    The order need not still be pending here so a replay remains owned and is
    consumed before Brain; handle_cod_reply enforces pending state before any
    provider or local mutation.
    """
    context_id = str(context_wamid or "").strip()
    action = classify_cod_reply(button_text)
    if not context_id or action not in {"confirm", "cancel"}:
        return None

    from models import (  # noqa: PLC0415
        AutomationEvent,
        AutomationExecution,
        Order,
        SmartAutomation,
    )

    # Store-origin prompts are dispatched by the lifecycle ledger, not by a
    # SmartAutomation execution. The accepted provider WAMID is stamped on the
    # order after the send. Match that exact send as well as the tenant, phone,
    # pending state and COD evidence before treating a template button as ours.
    external_orders = (
        db.query(Order)
        .filter(
            Order.tenant_id == int(tenant_id),
            Order.status.in_(
                tuple({STATUS_PENDING_CUSTOMER, *_STORE_PENDING_CONFIRMATION_STATUSES})
            ),
            Order.extra_metadata["nahla_cod_confirmation_wamid"].as_string()
            == context_id,
        )
        .limit(2)
        .all()
    )
    external_matches = []
    for order in external_orders:
        meta = dict(getattr(order, "extra_metadata", None) or {})
        if (
            int(getattr(order, "tenant_id", 0) or 0) == int(tenant_id)
            and str(getattr(order, "status", "") or "").lower()
            in {STATUS_PENDING_CUSTOMER, *_STORE_PENDING_CONFIRMATION_STATUSES}
            and meta.get("nahla_cod_confirmation_origin") == "external_store"
            and meta.get("nahla_cod_confirmation_sent") is True
            and str(meta.get("nahla_cod_confirmation_wamid") or "").strip() == context_id
            and str(meta.get("payment_method") or "").strip().lower() in _COD_METHODS
            and str(getattr(order, "external_id", None) or "").strip()
            and _order_phone_matches(order, customer_phone)
        ):
            external_matches.append(order)
    if len(external_matches) == 1:
        return f"nahla_cod_{action}:{external_matches[0].id}"

    candidates = (
        db.query(AutomationExecution, AutomationEvent, SmartAutomation)
        .join(AutomationEvent, AutomationExecution.event_id == AutomationEvent.id)
        .join(SmartAutomation, AutomationExecution.automation_id == SmartAutomation.id)
        .filter(
            AutomationExecution.tenant_id == int(tenant_id),
            AutomationExecution.status == "sent",
            AutomationExecution.action_taken["wa_message_id"].as_string()
            == context_id,
            AutomationEvent.tenant_id == int(tenant_id),
            AutomationEvent.event_type == "order_cod_pending",
            SmartAutomation.tenant_id == int(tenant_id),
            SmartAutomation.automation_type == "cod_confirmation",
        )
        .order_by(AutomationExecution.id.desc())
        .limit(200)
        .all()
    )
    for execution, event, automation in candidates:
        event_payload = getattr(event, "payload", None) or {}
        if not isinstance(event_payload, dict):
            continue
        raw_order_id = (
            event_payload.get("order_internal_id")
            or event_payload.get("order_id")
        )
        try:
            order_id = int(raw_order_id)
        except (TypeError, ValueError):
            continue

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.tenant_id == int(tenant_id),
            )
            .first()
        )
        if order is None:
            continue
        rejection = _correlated_initial_cod_evidence_rejection(
            execution=execution,
            event=event,
            automation=automation,
            order=order,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            context_wamid=context_id,
        )
        if rejection is not None:
            _log_cod_evidence_rejection(
                rejection,
                tenant_id=tenant_id,
                order_id=order_id,
                event_id=getattr(event, "id", None),
                execution_id=getattr(execution, "id", None),
            )
            continue
        return f"nahla_cod_{action}:{order_id}"

    return None


def resolve_verified_structured_cod_control(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    button_payload: str,
    button_text: str,
    context_wamid: Optional[str],
) -> Optional[str]:
    """Bind a provider button to one sent COD prompt before changing an order.

    This is for a structured WhatsApp button only. Ordinary conversational
    text must never acquire order-mutation authority from its wording.
    """
    action, order_id = parse_cod_button_payload(button_payload)
    context_id = str(context_wamid or "").strip()
    if action and order_id is not None:
        order = _load_bound_pending_cod_order(
            db, tenant_id=tenant_id, customer_phone=customer_phone,
            order_id=order_id, context_wamid=context_id,
        )
        if order is None:
            return None
        meta = dict(getattr(order, "extra_metadata", None) or {})
        stamped_wamid = str(meta.get("nahla_cod_confirmation_wamid") or "").strip()
        if stamped_wamid and context_id and stamped_wamid != context_id:
            return None
        if not stamped_wamid and not context_id:
            return None
        return f"nahla_cod_{action}:{order_id}"
    if not context_id:
        return None
    # Meta may return its own template payload. The title only chooses an
    # action after context.id proves which COD prompt was accepted for this
    # tenant and customer.
    return resolve_owned_cod_button_payload_from_context(
        db, tenant_id=tenant_id, customer_phone=customer_phone,
        button_text=button_text, context_wamid=context_id,
    )


async def apply_claimed_structured_cod_control(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
    button_payload: str,
    context_wamid: Optional[str],
) -> Tuple[Optional[str], Optional[Any]]:
    """Apply the verified store mutation without sending a second reply.

    The commerce runtime retains ownership of the natural customer response;
    the provider result, not that response, is the confirmation evidence.
    """
    control = resolve_verified_structured_cod_control(
        db, tenant_id=tenant_id, customer_phone=customer_phone,
        button_payload=button_payload, button_text=text,
        context_wamid=context_wamid,
    )
    if not control:
        return None, None
    return await handle_cod_reply(
        db, tenant_id=tenant_id, customer_phone=customer_phone,
        text=text, button_payload=control, context_wamid=context_wamid,
    )


async def intercept_cod_button_inbound(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
    button_payload: Optional[str],
    context_wamid: Optional[str] = None,
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
        context_wamid=context_wamid,
    )
    return COD_INBOUND_CONSUMED, decision, order


async def consume_owned_cod_button_inbound(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
    button_payload: Optional[str],
    context_wamid: Optional[str] = None,
    followup_send=None,
) -> str:
    """
    Fail-closed owner for recognized COD button payloads.

    Unrecognized payloads return passthrough and do not run the handler.
    Recognized payloads always return consumed and never raise, including when
    pending-order lookup, DB work, handle_cod_reply, or follow-up send fails.
    """
    if not is_owned_cod_button_payload(button_payload):
        return COD_INBOUND_PASSTHROUGH
    try:
        _disposition, decision, order = await intercept_cod_button_inbound(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            text=text,
            button_payload=button_payload,
            context_wamid=context_wamid,
        )
        if order is not None and followup_send is not None:
            try:
                await followup_send(decision, order)
            except Exception:
                logger.exception(
                    "[COD] owned-button followup failed tenant=%s",
                    tenant_id,
                )
    except Exception:
        logger.exception(
            "[COD] owned-button handler failed tenant=%s",
            tenant_id,
        )
    return COD_INBOUND_CONSUMED


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
    """Decide wait-for-button vs immediate store push from one settings snapshot."""
    from core.commerce_lifecycle.order_updates import (  # noqa: PLC0415
        REASON_SETTINGS_UNAVAILABLE,
        evaluate_order_update_delivery_from_truth,
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
    allowed, reason = evaluate_order_update_delivery_from_truth(
        truth, CANONICAL_SERVICE_KEY
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


def _normalized_phone_matches(left: Any, right: Any) -> bool:
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    left_normalized = normalize_phone(str(left or ""))
    right_normalized = normalize_phone(str(right or ""))
    return bool(left_normalized and left_normalized == right_normalized)


def _log_cod_evidence_rejection(
    reason: str,
    *,
    tenant_id: int,
    order_id: Optional[int] = None,
    event_id: Optional[int] = None,
    execution_id: Optional[int] = None,
) -> None:
    """Record the rejected guard without payload, phone, or full WAMID."""
    logger.info(
        "[COD_EVIDENCE] rejected reason=%s tenant=%s order=%s event=%s execution=%s",
        reason,
        tenant_id,
        order_id,
        event_id,
        execution_id,
    )


def _correlated_initial_cod_evidence_rejection(
    *,
    execution: Any,
    event: Any,
    automation: Any,
    order: Any,
    tenant_id: int,
    customer_phone: str,
    context_wamid: str,
) -> Optional[str]:
    """Return the first failed guard for a sent, order-bound COD prompt."""
    context_id = str(context_wamid or "").strip()
    if not context_id:
        return "context_wamid_missing"
    if int(getattr(order, "tenant_id", 0) or 0) != int(tenant_id):
        return "order_tenant_mismatch"
    if int(getattr(event, "tenant_id", 0) or 0) != int(tenant_id):
        return "event_tenant_mismatch"
    if int(getattr(execution, "tenant_id", 0) or 0) != int(tenant_id):
        return "execution_tenant_mismatch"
    if int(getattr(automation, "tenant_id", 0) or 0) != int(tenant_id):
        return "automation_tenant_mismatch"
    if str(getattr(event, "event_type", "") or "") != "order_cod_pending":
        return "event_type_mismatch"
    if getattr(event, "processed", False) is not True:
        return "event_not_processed"
    if str(getattr(execution, "status", "") or "") != "sent":
        return "execution_not_sent"
    if (
        str(getattr(automation, "automation_type", "") or "")
        != "cod_confirmation"
    ):
        return "automation_type_mismatch"

    automation_id = getattr(automation, "id", None)
    if getattr(execution, "event_id", None) != getattr(event, "id", None):
        return "execution_event_mismatch"
    if getattr(execution, "automation_id", None) != automation_id:
        return "execution_automation_mismatch"
    if getattr(event, "automation_id", None) != automation_id:
        return "event_automation_mismatch"

    action_taken = getattr(execution, "action_taken", None) or {}
    if not isinstance(action_taken, dict):
        return "action_taken_not_object"
    if str(action_taken.get("wa_message_id") or "").strip() != context_id:
        return "context_wamid_mismatch"
    if not _normalized_phone_matches(action_taken.get("to"), customer_phone):
        return "execution_phone_mismatch"
    if not _order_phone_matches(order, customer_phone):
        return "order_phone_mismatch"

    payload = getattr(event, "payload", None) or {}
    if not isinstance(payload, dict):
        return "event_payload_not_object"
    try:
        event_order_id = int(
            payload.get("order_internal_id") or payload.get("order_id")
        )
    except (TypeError, ValueError):
        return "event_order_id_missing"
    if event_order_id != int(getattr(order, "id", 0) or 0):
        return "event_order_id_mismatch"
    if str(payload.get("external_id") or "").strip() != str(
        getattr(order, "external_id", "") or ""
    ).strip():
        return "event_external_order_mismatch"
    event_order_number = str(
        payload.get("external_order_number") or payload.get("order_number") or ""
    ).strip()
    local_order_number = str(
        getattr(order, "external_order_number", "") or ""
    ).strip()
    if event_order_number and event_order_number != local_order_number:
        return "event_order_number_mismatch"
    if str(payload.get("message_type") or "").strip() != "initial_confirmation":
        return "event_message_type_mismatch"
    if (
        str(payload.get("payment_method") or "").strip().lower()
        not in _COD_METHODS
    ):
        return "event_payment_method_not_cod"

    event_customer_id = getattr(event, "customer_id", None)
    execution_customer_id = getattr(execution, "customer_id", None)
    if event_customer_id is None or execution_customer_id is None:
        return "customer_id_missing"
    if int(event_customer_id) != int(execution_customer_id):
        return "execution_customer_mismatch"
    order_customer_id = getattr(order, "customer_id", None)
    if (
        order_customer_id is not None
        and int(order_customer_id) != int(event_customer_id)
    ):
        return "order_customer_mismatch"
    return None


def _has_correlated_initial_cod_send(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    order: Any,
    context_wamid: Optional[str],
) -> bool:
    """Prove a stale row from the exact sent COD event and reply context."""
    context_id = str(context_wamid or "").strip()
    if not context_id:
        _log_cod_evidence_rejection(
            "context_wamid_missing",
            tenant_id=tenant_id,
            order_id=getattr(order, "id", None),
        )
        return False

    from models import (  # noqa: PLC0415
        AutomationEvent,
        AutomationExecution,
        SmartAutomation,
    )

    candidates = (
        db.query(AutomationExecution, AutomationEvent, SmartAutomation)
        .join(AutomationEvent, AutomationExecution.event_id == AutomationEvent.id)
        .join(SmartAutomation, AutomationExecution.automation_id == SmartAutomation.id)
        .filter(
            AutomationExecution.tenant_id == int(tenant_id),
            AutomationExecution.status == "sent",
            AutomationExecution.action_taken["wa_message_id"].as_string()
            == context_id,
            AutomationEvent.tenant_id == int(tenant_id),
            AutomationEvent.event_type == "order_cod_pending",
            SmartAutomation.tenant_id == int(tenant_id),
            SmartAutomation.automation_type == "cod_confirmation",
        )
        .order_by(AutomationExecution.id.desc())
        .limit(200)
        .all()
    )
    last_reason = "sent_cod_execution_not_found"
    for execution, event, automation in candidates:
        reason = _correlated_initial_cod_evidence_rejection(
            execution=execution,
            event=event,
            automation=automation,
            order=order,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            context_wamid=context_id,
        )
        if reason is None:
            logger.info(
                "[COD_EVIDENCE] correlated fallback accepted "
                "tenant=%s order=%s event=%s execution=%s",
                tenant_id,
                getattr(order, "id", None),
                getattr(event, "id", None),
                getattr(execution, "id", None),
            )
            return True
        last_reason = reason
    _log_cod_evidence_rejection(
        last_reason,
        tenant_id=tenant_id,
        order_id=getattr(order, "id", None),
    )
    return False


def stamp_initial_cod_automation_send_success(
    db,
    *,
    tenant_id: int,
    event: Any,
    automation: Any,
    action_info: Dict[str, Any],
    execution_id: int,
) -> bool:
    """Durably bind a successful initial Automation send to its Order row."""
    payload = getattr(event, "payload", None) or {}
    wamid = str((action_info or {}).get("wa_message_id") or "").strip()
    if (
        str(getattr(event, "event_type", "") or "") != "order_cod_pending"
        or str(getattr(automation, "automation_type", "") or "")
        != "cod_confirmation"
        or not isinstance(payload, dict)
        or str(payload.get("message_type") or "") != "initial_confirmation"
        or str(payload.get("payment_method") or "").strip().lower()
        not in _COD_METHODS
        or not wamid
    ):
        return False
    try:
        order_id = int(payload.get("order_internal_id") or payload.get("order_id"))
    except (TypeError, ValueError):
        return False

    from models import Order  # noqa: PLC0415

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.tenant_id == int(tenant_id))
        .first()
    )
    if order is None:
        return False
    if str(payload.get("external_id") or "").strip() != str(
        getattr(order, "external_id", "") or ""
    ).strip():
        return False
    order_customer_id = getattr(order, "customer_id", None)
    event_customer_id = getattr(event, "customer_id", None)
    if (
        order_customer_id is not None
        and event_customer_id is not None
        and int(order_customer_id) != int(event_customer_id)
    ):
        return False
    if not _order_phone_matches(order, str((action_info or {}).get("to") or "")):
        return False

    meta = dict(getattr(order, "extra_metadata", None) or {})
    meta["payment_method"] = "cod"
    meta["is_cod"] = True
    meta["nahla_cod_confirmation_sent"] = True
    meta["nahla_cod_confirmation_sent_at"] = datetime.now(timezone.utc).isoformat()
    meta["nahla_cod_confirmation_wamid"] = wamid
    meta["nahla_cod_confirmation_event_id"] = getattr(event, "id", None)
    meta["nahla_cod_confirmation_execution_id"] = int(execution_id)
    order.extra_metadata = meta
    flag_modified(order, "extra_metadata")
    return True


def nahla_owns_cod_customer_confirmation(order: Any) -> bool:
    """True when Nahla checkout already requested or resolved COD confirm."""
    from core.internal_e2e_safety import is_internal_e2e_order  # noqa: PLC0415
    if is_internal_e2e_order(order):
        return False
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
    from core.automation_engine import (  # noqa: PLC0415
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
    send_method = "approved_template"
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
        "buttons": list(_COD_BUTTON_TITLES),
    }


def find_pending_cod_orders(
    db, *, tenant_id: int, customer_phone: str
) -> list:
    """Return local or store-origin COD orders awaiting customer confirmation."""
    from models import Order  # noqa: PLC0415

    pending_statuses = tuple(
        {STATUS_PENDING_CUSTOMER, *_STORE_PENDING_CONFIRMATION_STATUSES}
    )
    candidates = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
            Order.status.in_(pending_statuses),
        )
        .order_by(Order.id.desc())
        .limit(50)
        .all()
    )
    matches = []
    for candidate in candidates:
        from core.internal_e2e_safety import is_internal_e2e_order  # noqa: PLC0415
        if is_internal_e2e_order(candidate):
            continue
        meta = dict(getattr(candidate, "extra_metadata", None) or {})
        payment_method = str(meta.get("payment_method") or "").strip().lower()
        is_cod = payment_method in {"cod", "cash_on_delivery", "cod_payment", "cash"}
        if not is_cod or not meta.get("nahla_cod_confirmation_sent"):
            continue
        if _order_phone_matches(candidate, customer_phone):
            matches.append(candidate)
    return matches


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
    context_wamid: Optional[str] = None,
) -> Optional[Any]:
    """Server-side bind: tenant + pending COD status + phone must all match."""
    from models import Order  # noqa: PLC0415

    order = (
        db.query(Order)
        .filter(
            Order.id == int(order_id),
            Order.tenant_id == int(tenant_id),
            Order.status.in_(
                tuple({STATUS_PENDING_CUSTOMER, *_STORE_PENDING_CONFIRMATION_STATUSES})
            ),
        )
        .first()
    )
    if order is None:
        _log_cod_evidence_rejection(
            "order_not_found_or_status_not_pending",
            tenant_id=tenant_id,
            order_id=order_id,
        )
        return None
    from core.internal_e2e_safety import is_internal_e2e_order  # noqa: PLC0415
    if is_internal_e2e_order(order):
        _log_cod_evidence_rejection(
            "internal_e2e_order_forbidden", tenant_id=tenant_id, order_id=order_id
        )
        return None
    if not _order_phone_matches(order, customer_phone):
        _log_cod_evidence_rejection(
            "order_phone_mismatch",
            tenant_id=tenant_id,
            order_id=order_id,
        )
        return None
    meta = dict(getattr(order, "extra_metadata", None) or {})
    payment_method = str(meta.get("payment_method") or "").strip().lower()
    if payment_method in _COD_METHODS and meta.get("nahla_cod_confirmation_sent"):
        return order

    # Legacy/already-sent rows may have been downgraded by a poller snapshot or
    # predate the durable send stamp.  The fallback is deliberately stronger
    # than either metadata flag: exact event, automation, customer, order,
    # provider-send WAMID, and inbound reply-context correlation are required.
    if _has_correlated_initial_cod_send(
        db,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        order=order,
        context_wamid=context_wamid,
    ):
        return order
    return None


async def handle_cod_reply(
    db,
    *,
    tenant_id: int,
    customer_phone: str,
    text: str,
    button_payload: Optional[str] = None,
    context_wamid: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Any]]:
    """
    Process a customer's COD reply. Returns (decision, order) where
    decision is 'confirm' | 'cancel' | 'confirm_failed' | 'cancel_failed'
    | None and order is the affected
    Order row (or None when there was no pending order to match).

    Button ids ``nahla_cod_confirm`` / ``nahla_cod_cancel`` (optionally
    ``:order_id``) bind first. A client-supplied order id is never trusted
    unless the row is still in a local/store pending-confirmation state for
    this tenant and phone. Text fallback binds only when exactly one pending
    COD order exists for that customer.
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
            context_wamid=context_wamid,
        )
    else:
        order = find_pending_cod_order(
            db, tenant_id=tenant_id, customer_phone=customer_phone,
        )
    if order is None:
        return decision, None

    from observability.event_logger import log_event  # noqa: PLC0415

    previous_status = str(getattr(order, "status", None) or "").strip().lower()
    external_id = str(getattr(order, "external_id", None) or "").strip()

    if decision == "cancel":
        if external_id:
            from store_integration.order_service import update_order_status  # noqa: PLC0415

            cancelled_in_store = await update_order_status(
                int(tenant_id), external_id, STATUS_CANCELLED
            )
            if not cancelled_in_store:
                meta = dict(order.extra_metadata or {})
                meta["cod_cancel_store_update_failed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                order.extra_metadata = meta
                flag_modified(order, "extra_metadata")
                db.commit()
                return "cancel_failed", order
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

    # decision == "confirm".  Provider mutation is the evidence boundary:
    # never claim confirmation until the order exists in Salla under_review.
    meta = dict(order.extra_metadata or {})
    meta["cod_confirm_requested_at"] = datetime.now(timezone.utc).isoformat()
    meta["cod_previous_status"] = previous_status
    order.extra_metadata = meta
    flag_modified(order, "extra_metadata")

    if external_id:
        from store_integration.order_service import update_order_status  # noqa: PLC0415

        updated = await update_order_status(
            int(tenant_id), external_id, STATUS_PENDING_MERCHANT
        )
        pushed_external_id = external_id if updated else None
    else:
        pushed_external_id = await _push_cod_to_store(db, tenant_id, order)

    if not pushed_external_id:
        meta["cod_confirm_store_update_failed_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        order.extra_metadata = meta
        flag_modified(order, "extra_metadata")
        db.commit()
        return "confirm_failed", order

    order.status = STATUS_PENDING_MERCHANT
    order.external_id = pushed_external_id
    meta["cod_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    meta["cod_pushed_external_id"] = pushed_external_id
    meta.pop("cod_confirm_store_update_failed_at", None)
    order.extra_metadata = meta
    flag_modified(order, "extra_metadata")
    log_event(
        db, tenant_id, category="order", event_type="order.cod.confirmed",
        summary=f"COD order #{order.id} confirmed by customer in store",
        severity="info",
        payload={
            "order_id": order.id,
            "external_id": pushed_external_id,
            "previous_status": previous_status,
            "current_status": STATUS_PENDING_MERCHANT,
        },
        reference_id=str(order.id),
    )

    db.commit()
    return decision, order


async def send_order_confirmation_after_cod(
    db,
    *,
    tenant_id: int,
    order: Any,
) -> Dict[str, Any]:
    """Dispatch the canonical order confirmation after proven COD acceptance."""
    meta = dict(getattr(order, "extra_metadata", None) or {})
    external_id = str(
        getattr(order, "external_id", None)
        or meta.get("cod_pushed_external_id")
        or ""
    ).strip()
    if not external_id or not meta.get("cod_confirmed_at"):
        return {"sent": False, "error": "cod_store_confirmation_unproven"}

    from core.commerce_lifecycle.dispatch import (  # noqa: PLC0415
        dispatch_external_lifecycle_notification,
    )

    info = dict(getattr(order, "customer_info", None) or {})
    previous_status = str(
        meta.get("cod_previous_status") or STATUS_PENDING_CUSTOMER
    )
    result = await dispatch_external_lifecycle_notification(
        db,
        tenant_id=int(tenant_id),
        order=order,
        provider="salla",
        raw_previous_status=previous_status,
        raw_current_status=STATUS_PENDING_MERCHANT,
        normalized_order={
            "external_id": external_id,
            "external_order_number": str(
                getattr(order, "external_order_number", None)
                or external_id
            ),
            "status": STATUS_PENDING_MERCHANT,
            "payment_method": "cod",
            "customer_name": str(
                getattr(order, "customer_name", None)
                or info.get("name")
                or ""
            ),
            "customer_phone": str(info.get("phone") or info.get("mobile") or ""),
            "cod_customer_confirmed": True,
            "lifecycle_observation": "cod_customer_confirmation",
            "lifecycle_source_event": "order.cod.confirmed",
        },
        raw_payload={
            "event": "order.cod.confirmed",
            "external_id": external_id,
        },
    )
    return {
        "sent": bool(result.dispatched),
        "duplicate": bool(result.duplicate),
        "error": result.reason_code,
        "provider_message_id": result.provider_message_id,
        "ledger_id": result.ledger_id,
    }


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
