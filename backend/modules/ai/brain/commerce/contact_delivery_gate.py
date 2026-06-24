"""
Central gate for vCard / contact delivery.

Every outbound contact card must pass through :func:`evaluate_contact_delivery_gate`.
Operational rule: explicit contact intent or genuine escalation only.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from modules.ai.brain.commerce.store_url_resolver import is_online_store_inquiry

logger = logging.getLogger("nahla.brain.contact_delivery_gate")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_GENERAL_ORDER_STATUS_RE = re.compile(
    r"(?:"
    r"أ?رسل(?:ت|نا)?\s*(?:لكم|لنا|طلب|الطلب)"
    r"|(?:طلب|الطلب)\s*(?:من|عبر|في)\s*(?:مساند|سند|تطبيق|موقع|متجر)"
    r"|(?:أ?)?(?:sent|submitted)\s+(?:an?\s+)?order"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_WEAK_PRONOUN_CONTACT_RE = re.compile(
    r"(?:"
    r"^و?(?:ين|كم|ايش|وش)\s*رقم(?:ه|ها|هم)?$"
    r"|^رقم(?:ه|ها|هم)\s*وين$"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class ContactDeliveryDecision:
    allow: bool
    reason: str
    contact_type: str = ""

    def to_log_dict(self) -> dict:
        return {
            "allow": self.allow,
            "reason": self.reason,
            "contact_type": self.contact_type or "",
        }


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def _is_product_or_offer_inquiry(message: str, intent_name: str = "") -> bool:
    intent = str(intent_name or "").strip().lower()
    if intent in {
        "ask_product",
        "ask_price",
        "product_visual_request",
        "solution_seeking_commerce",
        "need_based_product_advice",
    }:
        return True
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_commerce_or_product_flow_message,
        )

        return is_commerce_or_product_flow_message(message or "")
    except Exception:
        return False


def _is_branch_location_only(message: str) -> bool:
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            has_explicit_contact_intent,
        )
        from modules.ai.brain.commerce.link_intent import (  # noqa: PLC0415
            LinkIntentType,
            resolve_link_intent,
        )

        if has_explicit_contact_intent(message or ""):
            return False
        return resolve_link_intent(message or "") == LinkIntentType.PHYSICAL_LOCATION
    except Exception:
        return False


def _has_explicit_contact_request(message: str) -> bool:
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            has_explicit_contact_intent,
            is_explicit_arrival_intent,
        )
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            classify_staff_contact_request,
        )

        if has_explicit_contact_intent(message or ""):
            return True
        if is_explicit_arrival_intent(message or ""):
            return True
        kind, _target, _reason = classify_staff_contact_request(message or "")
        return bool(kind)
    except Exception:
        return False


def evaluate_contact_delivery_gate(
    *,
    customer_message: str,
    intent_name: str = "",
    delivery_path: str = "",
    reply_mentions_staff: bool = False,
    escalation_reason: str = "",
    policy_deliver_contact: bool = False,
) -> ContactDeliveryDecision:
    """Decide whether a vCard/contact card may be sent this turn."""
    msg = str(customer_message or "")
    norm = _norm(msg)
    path = str(delivery_path or "").strip().lower()

    if is_online_store_inquiry(msg):
        return ContactDeliveryDecision(
            allow=False,
            reason="online_store_inquiry",
        )

    if _GENERAL_ORDER_STATUS_RE.search(norm):
        if not _has_explicit_contact_request(msg):
            return ContactDeliveryDecision(
                allow=False,
                reason="general_order_message",
            )

    if _is_product_or_offer_inquiry(msg, intent_name=intent_name):
        return ContactDeliveryDecision(
            allow=False,
            reason="product_inquiry",
        )

    if _is_branch_location_only(msg):
        return ContactDeliveryDecision(
            allow=False,
            reason="branch_location_only",
        )

    if _WEAK_PRONOUN_CONTACT_RE.search(norm) and not policy_deliver_contact:
        return ContactDeliveryDecision(
            allow=False,
            reason="weak_followup_no_explicit_contact",
        )

    if reply_mentions_staff and not _has_explicit_contact_request(msg):
        if not policy_deliver_contact and not escalation_reason:
            return ContactDeliveryDecision(
                allow=False,
                reason="staff_mention_without_explicit_request",
            )

    if policy_deliver_contact and _has_explicit_contact_request(msg):
        return ContactDeliveryDecision(
            allow=True,
            reason="explicit_contact_request",
            contact_type="staff_or_branch",
        )

    if _has_explicit_contact_request(msg):
        return ContactDeliveryDecision(
            allow=True,
            reason="explicit_contact_request",
            contact_type="staff_or_branch",
        )

    if escalation_reason:
        low = escalation_reason.strip().lower()
        if low in {
            "handoff",
            "talk_to_human",
            "employee_not_responding",
            "complaint_escalation",
            "genuine_escalation",
        }:
            return ContactDeliveryDecision(
                allow=True,
                reason="genuine_escalation",
                contact_type="escalation",
            )

    if path in {
        "handoff",
        "pre_brain_handoff",
        "staff_contact_policy",
        "staff_contact_recovery",
        "arrival_contact_delivery",
        "branch_trigger_router",
        "structured_admin_contact",
    } and policy_deliver_contact:
        if _has_explicit_contact_request(msg) or escalation_reason:
            return ContactDeliveryDecision(
                allow=True,
                reason=f"policy_path:{path}",
                contact_type="staff_or_branch",
            )

    # Marker / LLM [CALL:...] resolution — require explicit contact in message.
    if path in {"call_marker", "staff_contact_safety_net"}:
        if _has_explicit_contact_request(msg):
            return ContactDeliveryDecision(
                allow=True,
                reason="explicit_contact_request",
                contact_type="marker_resolved",
            )
        return ContactDeliveryDecision(
            allow=False,
            reason="marker_without_explicit_contact",
        )

    logger.info(
        "[CONTACT_DELIVERY_GATE] deny path=%s preview=%r",
        path or "-",
        msg[:80],
    )
    return ContactDeliveryDecision(
        allow=False,
        reason="no_explicit_contact_intent",
    )


__all__ = [
    "ContactDeliveryDecision",
    "evaluate_contact_delivery_gate",
]
