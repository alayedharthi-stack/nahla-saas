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
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    msg = (getattr(ctx, "message", None) or "").strip()
    if not msg:
        return None
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
