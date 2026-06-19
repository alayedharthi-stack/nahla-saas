"""
social_single_reply_guard.py
────────────────────────────
Platform guard: exactly one social/greeting outbound per inbound turn.

Competing paths (Layer 0 templates, Brain persona compose, ACTION_GREET /
ACTION_SOCIAL_REPLY, webhook fallbacks) must not each emit a separate
WhatsApp message for the same phatic turn.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from modules.ai.brain.decision.actions import (
    ACTION_GREET,
    ACTION_LLM_REPLY,
    ACTION_SOCIAL_REPLY,
)

logger = logging.getLogger("nahla.brain.postprocess.social_single_reply_guard")

try:
    _RIYADH = ZoneInfo("Asia/Riyadh")
except Exception:  # noqa: BLE001 — Windows dev env may lack tzdata
    _RIYADH = timezone(timedelta(hours=3))

_WELLBEING_PHRASES_RE = re.compile(
    r"(?:^|[\s،,.!?])"
    r"(?:"
    r"كيف\s+حال(?:ك|كم)?|كيف\s+الحال|كيف\s+احوالك|كيف\s+أ?حوالك|"
    r"كيفك|شلون\s+حال(?:ك|كم)?|شلونك|"
    r"اب?شر(?:ك|كم|هم|ها)?|"
    r"الحمد\s+لله\s+(?:و?)?(?:ال)?(?:ب?)?خير|"
    r"ب?خير\s*ال?حمد\s*لله"
    r")"
    r"(?:[\s،,.!?]|$)",
    re.UNICODE | re.IGNORECASE,
)

SOCIAL_GREETING_ACTIONS = frozenset({
    ACTION_GREET,
    ACTION_SOCIAL_REPLY,
})

_PERSONA_SOCIAL_TOPICS = frozenset({
    "persona_social",
    "social_persona_ack",
    "social",
})


@dataclass(frozen=True)
class SocialReplySelection:
    action: str
    source: str
    category: str = ""


def is_morning_greeting_time(*, now: Optional[datetime] = None) -> bool:
    """True during Saudi morning window (05:00–11:59 Asia/Riyadh)."""
    dt = now or datetime.now(timezone.utc)
    local = dt.astimezone(_RIYADH)
    return 5 <= local.hour < 12


def resolve_time_aware_social_category(
    category: str,
    *,
    inbound_text: str = "",
    now: Optional[datetime] = None,
) -> str:
    """Never use ``morning_greeting`` templates outside the morning window."""
    cat = (category or "").strip().lower()
    if cat != "morning_greeting":
        return cat
    if is_morning_greeting_time(now=now):
        return cat
    dt = now or datetime.now(timezone.utc)
    logger.info(
        "[SOCIAL_TIME_GUARD] morning_greeting suppressed local_hour=%d inbound=%r",
        dt.astimezone(_RIYADH).hour,
        (inbound_text or "")[:60],
    )
    if is_wellbeing_greeting_message(inbound_text):
        return "general_courtesy"
    return "general_courtesy"


def is_wellbeing_greeting_message(message: str) -> bool:
    return bool(_WELLBEING_PHRASES_RE.search(message or ""))


def is_social_greeting_decision(
    decision: Any,
    *,
    social_human_context: Any = None,
) -> bool:
    action = str(getattr(decision, "action", "") or "")
    args = dict(getattr(decision, "args", None) or {})

    if action in SOCIAL_GREETING_ACTIONS:
        return True

    if action == ACTION_LLM_REPLY:
        topic = str(args.get("topic") or "").strip().lower()
        if topic in _PERSONA_SOCIAL_TOPICS:
            return True
        if str(args.get("persona_kind") or "").strip().lower() == "greeting":
            return True
        if args.get("block_commerce_escalation") and (
            args.get("social_category") or args.get("social_human_intent")
        ):
            return True

    if social_human_context is not None and getattr(
        social_human_context, "is_pure_social_turn", False
    ):
        if getattr(social_human_context, "reply_type", "") in {"social", "persona_social"}:
            return True

    return False


def should_defer_layer0_for_brain_social(message: str) -> bool:
    """Wellbeing / pure greeting turns belong in Brain — not Layer 0 templates."""
    text = (message or "").strip()
    if not text:
        return False
    if is_wellbeing_greeting_message(text):
        return True
    try:
        from modules.ai.brain.intent.rules import (  # noqa: PLC0415
            is_pure_greeting_without_commerce,
        )

        if is_pure_greeting_without_commerce(text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional rules import in defer gate
        pass
    return False


def claim_social_reply_selection(
    trace: Any,
    *,
    selection: SocialReplySelection,
) -> None:
    if trace is None:
        return
    extra = dict(getattr(trace, "extra", None) or {})
    if extra.get("social_reply_claimed"):
        return
    extra["social_reply_claimed"] = True
    extra["social_reply_action"] = selection.action
    extra["social_reply_source"] = selection.source
    extra["social_reply_category"] = selection.category
    trace.extra = extra


def social_reply_already_claimed(trace: Any) -> bool:
    if trace is None:
        return False
    extra = getattr(trace, "extra", None) or {}
    return bool(extra.get("social_reply_claimed"))


def should_suppress_competing_social_outbound(
    trace: Any,
    *,
    source: str,
    action: str = "",
    inbound_text: str = "",
) -> bool:
    """True when a social/greeting reply was already sent for this inbound."""
    if trace is None:
        return False
    if not getattr(trace, "outbound_sent", False):
        return False
    extra = dict(getattr(trace, "extra", None) or {})
    if not extra.get("social_reply_claimed") and not _looks_social_outbound_source(source):
        return False
    prior_source = str(extra.get("social_reply_source") or trace.reply_source or "")
    logger.warning(
        "[SOCIAL_SINGLE_REPLY_GUARD] suppressed competing outbound "
        "source=%s action=%s prior_source=%s inbound=%r",
        source,
        action,
        prior_source,
        (inbound_text or "")[:80],
    )
    return True


def _looks_social_outbound_source(source: str) -> bool:
    src = (source or "").strip().lower()
    return src in {
        "layer0",
        "layer0_router",
        "brain",
        "brain_social",
        "social_reply",
        "greet",
    } or "layer0" in src or "social" in src


__all__ = [
    "SocialReplySelection",
    "claim_social_reply_selection",
    "is_morning_greeting_time",
    "is_social_greeting_decision",
    "is_wellbeing_greeting_message",
    "resolve_time_aware_social_category",
    "should_defer_layer0_for_brain_social",
    "should_suppress_competing_social_outbound",
    "social_reply_already_claimed",
]
