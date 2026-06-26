"""
turn/owner_brief.py
───────────────────
Build OwnerBrief — goals and constraints for compose, not reply text.
"""
from __future__ import annotations

from typing import Any, Optional

from ..types import BrainContext
from .contract import (
    COMPOSE_MODE_HYBRID,
    COMPOSE_MODE_OPERATIONAL,
    COMPOSE_MODE_PERSONA,
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_HEALTH_ADVISORY,
    OWNER_ORDERING,
    OWNER_PAYMENT,
    OWNER_PERSONA_SOCIAL,
    OWNER_POST_PURCHASE,
    OWNER_STAFF_ESCALATION,
    OWNER_SUPPORT,
    OWNER_TRACKING,
    OwnerBrief,
    TurnUnderstanding,
)

_DISCOVERY_FORBIDDEN = (
    "product_discovery",
    "top_products",
    "catalog_browse",
)


def build_owner_brief(
    owner: str,
    understanding: TurnUnderstanding,
    ctx: BrainContext,
    *,
    slot_replay_approved: bool = False,
) -> OwnerBrief:
    """Build structured compose guidance for the selected turn owner."""
    goal = understanding.customer_goal or understanding.current_intent

    if owner in {OWNER_SUPPORT, OWNER_POST_PURCHASE}:
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal="acknowledge_issue_ask_for_evidence_do_not_continue_checkout",
            forbidden_objectives=(
                "checkout",
                "ordering",
                "product_upsell",
                "ask_city",
                "ask_last_name",
                *_DISCOVERY_FORBIDDEN,
            ),
            required_evidence=(
                "order_reference_or_product_photo_if_available",
            ),
            tone_guidance="natural empathetic non-template",
            compose_mode=COMPOSE_MODE_PERSONA,
        )

    if owner == OWNER_DISCOVERY:
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal="answer_discount_or_product_question_first_then_offer_help_if_needed",
            forbidden_objectives=(
                "checkout_resume",
                "ask_city",
                "ask_last_name",
                "slot_replay",
            ),
            required_evidence=(),
            tone_guidance="natural concise not sales-pushy",
            compose_mode=COMPOSE_MODE_PERSONA,
        )

    if owner == OWNER_PERSONA_SOCIAL:
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal="respond_socially_or_identity_or_gratitude_naturally",
            forbidden_objectives=(
                "staff_escalation",
                "checkout",
                "product_push",
                "ordering",
            ),
            required_evidence=(),
            tone_guidance="free persona natural non-template",
            compose_mode=COMPOSE_MODE_PERSONA,
        )

    if owner in {OWNER_CHECKOUT, OWNER_ORDERING}:
        replay = "approved" if slot_replay_approved else "denied"
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal=f"continue_checkout_only_if_slot_replay_{replay}",
            forbidden_objectives=(
                "support_reply",
                "staff_escalation",
                "product_upsell",
                *_DISCOVERY_FORBIDDEN,
            ),
            required_evidence=(),
            tone_guidance="natural helpful non-template",
            compose_mode=COMPOSE_MODE_HYBRID,
        )

    if owner == OWNER_PAYMENT:
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal="handle_payment_or_receipt_with_evidence_only",
            forbidden_objectives=(
                "checkout_slot_replay",
                "product_upsell",
                "unsupported_payment_claims",
                *_DISCOVERY_FORBIDDEN,
            ),
            required_evidence=(
                "payment_receipt_or_transfer_proof",
            ),
            tone_guidance="honest operational non-template",
            compose_mode=COMPOSE_MODE_HYBRID,
        )

    if owner == OWNER_TRACKING:
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal="share_tracking_or_order_status_with_evidence_only",
            forbidden_objectives=(
                "checkout",
                "product_upsell",
                "unsupported_shipment_claims",
                *_DISCOVERY_FORBIDDEN,
            ),
            required_evidence=(
                "shipment_or_order_status_evidence",
            ),
            tone_guidance="honest operational non-template",
            compose_mode=COMPOSE_MODE_OPERATIONAL,
        )

    if owner == OWNER_STAFF_ESCALATION:
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal="escalate_to_staff_only_with_contact_evidence",
            forbidden_objectives=(
                "unsupported_staff_contact_claims",
                "checkout",
                "product_push",
                *_DISCOVERY_FORBIDDEN,
            ),
            required_evidence=(
                "staff_contact_evidence",
            ),
            tone_guidance="honest operational non-template",
            compose_mode=COMPOSE_MODE_OPERATIONAL,
        )

    if owner == OWNER_HEALTH_ADVISORY:
        return OwnerBrief(
            owner=owner,
            customer_goal=goal,
            reply_goal="advisory_health_commerce_guidance_without_medical_claims",
            forbidden_objectives=(
                "product_discovery",
                "top_products",
                "catalog_browse",
                "checkout_push",
                "unsupported_medical_claims",
            ),
            required_evidence=(),
            tone_guidance="natural cautious non-template",
            compose_mode=COMPOSE_MODE_PERSONA,
        )

    return OwnerBrief(
        owner=owner,
        customer_goal=goal,
        reply_goal="respond_to_current_turn_naturally",
        forbidden_objectives=(),
        required_evidence=(),
        tone_guidance="natural non-template",
        compose_mode=COMPOSE_MODE_PERSONA,
    )


def format_owner_brief_for_compose(brief: Any) -> str:
    """
    Append-only LLM guidance block — not a template reply.

    Returns empty string when ``brief`` is missing or invalid.
    """
    if not isinstance(brief, dict) or not brief:
        return ""

    owner = str(brief.get("owner") or "")
    reply_goal = str(brief.get("reply_goal") or "")
    customer_goal = str(brief.get("customer_goal") or "")
    tone = str(brief.get("tone_guidance") or "")
    compose_mode = str(brief.get("compose_mode") or "")
    forbidden = brief.get("forbidden_objectives") or []
    required = brief.get("required_evidence") or []

    lines = [
        "[TURN_OWNER_BRIEF]",
        f"owner: {owner}",
        f"customer_goal: {customer_goal}",
        f"reply_goal: {reply_goal}",
        f"compose_mode: {compose_mode}",
        f"tone_guidance: {tone}",
    ]
    if forbidden:
        lines.append(f"forbidden_objectives: {', '.join(str(x) for x in forbidden)}")
    if required:
        lines.append(f"required_evidence: {', '.join(str(x) for x in required)}")
    lines.append(
        "Write naturally in persona voice. Do not use fixed templates. "
        "Respect forbidden_objectives. Do not claim operational facts without required_evidence."
    )
    return "\n".join(lines)


def topic_for_owner(owner: str) -> str:
    """Stable decision topic key for compose routing."""
    return owner.replace("/", "_")


__all__ = [
    "build_owner_brief",
    "format_owner_brief_for_compose",
    "topic_for_owner",
]
