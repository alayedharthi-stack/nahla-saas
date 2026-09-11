"""
identity_collaboration_guard.py
───────────────────────────────
Block commerce purchase assumptions on self-intro / collaboration inbounds.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.identity_collaboration_guard")

TOPIC_IDENTITY_COLLABORATION = "identity_collaboration"


def _has_structured_product_inquiry(ctx: Any) -> bool:
    """True when upstream extraction already found a concrete product subject.

    Product resolution remains owned by the tenant-scoped catalog path. This
    merely prevents identity/collaboration from consuming that path first.
    """
    intent = getattr(ctx, "intent", None)
    slots = getattr(intent, "slots", None)
    if not isinstance(slots, dict):
        return False
    return bool(
        str(slots.get("product_query") or "").strip()
        or str(slots.get("product_name") or "").strip()
        or slots.get("product_id") not in (None, "")
    )


def compose_identity_collaboration_goal() -> str:
    return (
        "identity_collaboration — The customer introduced themselves professionally "
        "(e.g. beekeeper/teacher) or expressed interest in collaborating, working, "
        "or joining — NOT necessarily buying products. "
        "Reply warmly in natural Saudi Arabic. "
        "Ask ONE clarifying non-sales question: job/collaboration vs product inquiry. "
        "Do NOT assume they want bee packages, honey products, or hive expansion. "
        "Do NOT list product categories unless they explicitly asked to buy. "
        "Do NOT append quantity or checkout prompts."
    )


def try_identity_collaboration_decision(ctx: Any, *, route: str = "") -> Optional[Any]:
    from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
        is_identity_collaboration_without_purchase,
    )
    from modules.ai.brain.commerce.link_intent_media_source_guard import (  # noqa: PLC0415
        link_intent_message,
    )
    from modules.ai.brain.commerce.staff_contact_media_source_guard import (  # noqa: PLC0415
        is_media_framed_inbound_message,
    )
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    full_msg = (getattr(ctx, "message", None) or "").strip()
    if not full_msg:
        return None
    if is_media_framed_inbound_message(full_msg):
        msg = link_intent_message(full_msg).strip()
        if not msg:
            return None
    else:
        msg = full_msg
    if _has_structured_product_inquiry(ctx):
        return None
    try:
        from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
            extract_price_subject,
        )

        if extract_price_subject(msg):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional commerce evidence probe
        pass
    try:
        from modules.ai.brain.state.product_information_topic import (  # noqa: PLC0415
            detect_product_information_topic_shift,
        )

        if detect_product_information_topic_shift(msg):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product info topic import
        pass
    if not is_identity_collaboration_without_purchase(msg):
        return None

    try:
        from modules.ai.brain.commerce.health_advisory_product_safety import (  # noqa: PLC0415
            should_defer_non_health_routes,
        )

        if should_defer_non_health_routes(msg, state=getattr(ctx, "state", None)):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    # Reuse the turn's existing intent only. Do not reclassify the inbound
    # text. Long-sentence hygiene may still match; solution-seeking already
    # owns those turns.
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "").strip()
    try:
        from modules.ai.brain.catalog.navigation_signals import (  # noqa: PLC0415
            is_authoritative_solution_seeking_intent,
        )

        _ss_owned = is_authoritative_solution_seeking_intent(intent_name)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — local name check if catalog helper unavailable
        from modules.ai.brain.types import (  # noqa: PLC0415
            INTENT_NEED_BASED_PRODUCT_ADVICE,
            INTENT_SOLUTION_SEEKING_COMMERCE,
        )

        _ss_owned = intent_name in {
            INTENT_SOLUTION_SEEKING_COMMERCE,
            INTENT_NEED_BASED_PRODUCT_ADVICE,
            "need_based_product_advice",
        }
    if _ss_owned:
        logger.info(
            "[IDENTITY_COLLABORATION_GUARD] yield=authoritative_solution_seeking "
            "tenant=%s route=%s intent=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            route or "-",
            intent_name or "-",
            msg[:80],
        )
        return None

    logger.info(
        "[IDENTITY_COLLABORATION_GUARD] tenant=%s route=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        route or "-",
        msg[:80],
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": TOPIC_IDENTITY_COLLABORATION,
            "block_commerce_escalation": True,
            "response_goal": compose_identity_collaboration_goal(),
        },
        reason="identity/collaboration inbound — no purchase assumption",
        confidence=0.93,
    )


__all__ = [
    "TOPIC_IDENTITY_COLLABORATION",
    "compose_identity_collaboration_goal",
    "try_identity_collaboration_decision",
]
