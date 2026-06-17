"""
social_reply_context_guard.py
─────────────────────────────
Final social safety guard (P1): block opening-greeting replies («هلا وغلا»)
when the customer's last message was dua / thanks / blessing — not an
explicit salaam.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("nahla.brain.postprocess.social_reply_context_guard")


@dataclass(frozen=True)
class SocialReplyContextGuardResult:
    reply: str
    replaced: bool


def apply_social_reply_context_guard(
    reply: str,
    *,
    inbound_text: str = "",
    tenant_id: Optional[int] = None,
) -> SocialReplyContextGuardResult:
    """Replace greeting-style replies on dua/thanks inbound turns."""
    text = (reply or "").strip()
    if not text or not (inbound_text or "").strip():
        return SocialReplyContextGuardResult(reply=text, replaced=False)

    try:
        from modules.ai.brain.compose.persona_template_engine import (  # noqa: PLC0415
            enforce_social_context_reply_guard,
        )

        guarded = enforce_social_context_reply_guard(text, inbound_text=inbound_text)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — belt guard must never break outbound
        return SocialReplyContextGuardResult(reply=text, replaced=False)

    replaced = guarded != text
    if replaced:
        logger.info(
            "[SOCIAL_REPLY_CONTEXT_GUARD] tenant=%s orig=%r new=%r inbound=%r",
            tenant_id if tenant_id is not None else "-",
            text[:80],
            guarded[:80],
            (inbound_text or "")[:80],
        )
    return SocialReplyContextGuardResult(reply=guarded, replaced=replaced)


__all__ = [
    "SocialReplyContextGuardResult",
    "apply_social_reply_context_guard",
]
