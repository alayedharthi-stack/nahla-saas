"""Constitution-compliant compose for track_order_not_found (NL-V001).

Normal path: LLM wording from structured lookup facts only.
Emergency fallback: short deterministic template after genuine compose failure.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

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
)


def extract_track_order_not_found_facts(
    ctx: BrainContext,
    result: Any,
) -> Dict[str, Any]:
    slots = dict(getattr(getattr(ctx, "intent", None), "slots", None) or {})
    order_reference = (
        str(slots.get("order_id") or slots.get("order_number") or "").strip()
        or str((getattr(result, "data", None) or {}).get("order_reference") or "").strip()
    )
    return {
        "order_reference": order_reference,
        "lookup_result": "not_found",
        "order_verified": False,
    }


def build_track_order_not_found_owner_brief(facts: Mapping[str, Any]) -> Dict[str, Any]:
    ref = str(facts.get("order_reference") or "").strip() or "unknown"
    return {
        "owner": "tracking/order_not_found",
        "customer_goal": "track_order",
        "reply_goal": (
            "honestly_report_that_order_lookup_returned_not_found; "
            f"order_reference={ref}; ask_customer_to_confirm_or_resend_reference_if_helpful"
        ),
        "forbidden_objectives": (
            "invent_shipment_status",
            "invent_carrier",
            "invent_payment_status",
            "claim_order_exists",
            "checkout",
            "product_upsell",
        ),
        "required_evidence": (),
        "tone_guidance": "natural concise non-template",
        "compose_mode": "persona",
        "structured_facts": dict(facts),
    }


def format_track_order_not_found_facts_overlay(facts: Mapping[str, Any]) -> str:
    ref = str(facts.get("order_reference") or "").strip() or "unknown"
    return "\n".join(
        [
            "[TRACK_ORDER_NOT_FOUND_FACTS]",
            f"order_reference: {ref}",
            "lookup_result: not_found",
            "order_verified: false",
            "Compose naturally in Arabic using only these facts.",
            "Never invent shipment, carrier, payment status, or order existence.",
        ]
    )


def build_track_order_not_found_compose_decision(facts: Mapping[str, Any]) -> Decision:
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "track_order_not_found",
            "owner_brief": build_track_order_not_found_owner_brief(facts),
        },
        reason="track_order_not_found — constitution LLM compose",
    )


def record_llm_compose_metadata(result: Any, *, llm_candidate: str) -> None:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return
    data["compose_source"] = "llm"
    data["response_mode"] = "llm"
    data["llm_candidate_present"] = bool((llm_candidate or "").strip())
    data["final_text_transformed"] = False
    data["final_transform_reasons"] = []
    data["fallback_reason"] = ""
    data["fallback_action_type"] = ""


def record_fallback_metadata(result: Any, *, reason: str) -> None:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return
    data["compose_source"] = "fallback_deterministic"
    data["response_mode"] = "template"
    data["llm_candidate_present"] = True
    data["final_text_transformed"] = False
    data["final_transform_reasons"] = []
    data["fallback_reason"] = str(reason or "compose_failed")
    data["fallback_action_type"] = "track_order_not_found"


def is_usable_llm_reply(text: Optional[str]) -> bool:
    reply = str(text or "").strip()
    if not reply:
        return False
    if reply == T.order_status_not_found():
        return False
    return True


def claims_invented_order_facts(text: str) -> bool:
    lowered = str(text or "")
    return any(marker in lowered for marker in FORBIDDEN_INVENTION_MARKERS)
