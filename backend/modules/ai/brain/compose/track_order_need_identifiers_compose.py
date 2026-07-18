"""Constitution-compliant compose for track_order_need_order_number (NL-V002).

Normal path: LLM wording from structured lookup facts only.
Emergency fallback: short deterministic template after genuine compose failure.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from core.outbound_sanitizer import contains_handoff_promise

from ..decision.actions import ACTION_LLM_REPLY
from ..types import BrainContext, Decision
from . import templates as T

FORBIDDEN_INVENTION_MARKERS = (
    "تم الشحن",
    "شركة الشحن",
    "تم الدفع",
    "وصل الإيصال",
    "الطلب موجود",
    "تم تأكيد الطلب",
    "جاري المعالجة",
    "تم التوصيل",
    "تم تحويلك",
    "بيتواصلون معك",
    "سيتواصلون معك",
)

FALSE_ESCALATION_MARKERS = (
    "تم تحويلك",
    "بيتواصلون معك",
    "سيتواصلون معك",
)


def extract_track_order_need_identifiers_facts(
    ctx: BrainContext,
    result: Any,
) -> Dict[str, Any]:
    data = dict(getattr(result, "data", None) or {})
    msg_key = str(data.get("message") or "").strip()
    lookup_blocked_reason = (
        "missing_order_identifier"
        if msg_key in {"", "need_order_number", "no_orders_found"}
        else "missing_order_identifier"
    )
    return {
        "tracking_intent_recognized": True,
        "lookup_started": False,
        "lookup_blocked_reason": lookup_blocked_reason,
        "order_verified": False,
        "requested_identifier_types": ["order_number"],
    }


def build_track_order_need_identifiers_owner_brief(
    facts: Mapping[str, Any],
) -> Dict[str, Any]:
    requested = list(facts.get("requested_identifier_types") or ["order_number"])
    return {
        "owner": "tracking/need_identifiers",
        "customer_goal": "track_order",
        "reply_goal": (
            "clarify_that_order_lookup_cannot_start_without_required_identifier; "
            f"requested_identifier_types={','.join(requested)}"
        ),
        "forbidden_objectives": (
            "invent_shipment_status",
            "invent_carrier",
            "invent_payment_status",
            "claim_order_exists",
            "claim_lookup_occurred",
            "checkout",
            "product_upsell",
            "ask_for_phone_unless_explicitly_required",
        ),
        "required_evidence": (),
        "tone_guidance": "natural concise non-template",
        "compose_mode": "persona",
        "structured_facts": dict(facts),
    }


def format_track_order_need_identifiers_facts_overlay(
    facts: Mapping[str, Any],
) -> str:
    requested = ", ".join(facts.get("requested_identifier_types") or ["order_number"])
    return "\n".join(
        [
            "[TRACK_ORDER_NEED_IDENTIFIERS_FACTS]",
            "tracking_intent_recognized: true",
            "lookup_started: false",
            f"lookup_blocked_reason: {facts.get('lookup_blocked_reason', 'missing_order_identifier')}",
            "order_verified: false",
            f"requested_identifier_types: {requested}",
            "Compose naturally in the customer's language using only these facts.",
            "Ask for the required identifier(s) without implying lookup or order status.",
            "Never invent shipment, carrier, payment status, or order existence.",
        ]
    )


def build_track_order_need_identifiers_compose_decision(
    facts: Mapping[str, Any],
) -> Decision:
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "track_order_need_identifiers",
            "owner_brief": build_track_order_need_identifiers_owner_brief(facts),
        },
        reason="track_order_need_order_number — constitution LLM compose",
    )


def record_llm_compose_metadata(result: Any, *, llm_candidate: str) -> None:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return
    data["track_order_need_identifiers_compose_active"] = True
    data["compose_source"] = "llm"
    data["response_mode"] = "llm"
    data["llm_candidate_present"] = bool((llm_candidate or "").strip())
    data["final_text_transformed"] = False
    data["final_transform_reasons"] = []
    data["final_customer_text_source"] = "llm"
    data["fallback_reason"] = ""
    data["fallback_action_type"] = ""


def record_fallback_metadata_on_data(
    data: Any,
    *,
    reason: str,
    transformed_by_guard: bool = False,
) -> None:
    if not isinstance(data, dict):
        return
    data["track_order_need_identifiers_compose_active"] = True
    data["compose_source"] = "fallback_deterministic"
    data["response_mode"] = "template"
    data["llm_candidate_present"] = True
    data["final_text_transformed"] = bool(transformed_by_guard)
    data["final_transform_reasons"] = (
        ["staff_escalation_truth_guard"] if transformed_by_guard else []
    )
    data["final_customer_text_source"] = "fallback_deterministic"
    data["fallback_reason"] = str(reason or "compose_failed")
    data["fallback_action_type"] = "track_order_need_identifiers"


def record_fallback_metadata(result: Any, *, reason: str) -> None:
    record_fallback_metadata_on_data(
        getattr(result, "data", None),
        reason=reason,
    )


def is_usable_llm_reply(text: Optional[str]) -> bool:
    reply = str(text or "").strip()
    if not reply:
        return False
    if reply == T.track_order_need_identifiers_emergency_fallback():
        return False
    if claims_invented_order_facts(reply):
        return False
    if claims_false_escalation(reply):
        return False
    return True


def unusable_llm_reply_reason(text: Optional[str]) -> str:
    reply = str(text or "").strip()
    if not reply:
        return "compose_failed_or_empty"
    if claims_false_escalation(reply):
        return "compose_false_escalation_claim"
    if claims_invented_order_facts(reply):
        return "compose_unsupported_operational_claim"
    return "compose_unusable"


def claims_invented_order_facts(text: str) -> bool:
    lowered = str(text or "")
    return any(marker in lowered for marker in FORBIDDEN_INVENTION_MARKERS)


def claims_false_escalation(text: str) -> bool:
    reply = str(text or "")
    return contains_handoff_promise(reply) or any(
        marker in reply for marker in FALSE_ESCALATION_MARKERS
    )
