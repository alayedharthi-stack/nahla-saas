"""
conversation_recovery.py
────────────────────────
Platform-wide recovery when compose/guards would otherwise inject
deterministic ACK stubs («حاضر 🌷», «تمام 🌷 وصلت رسالتك», …).

Operational facts stay evidence-backed; personality stays on persona compose.
This layer asks naturally from conversation state — never canned ACK pools.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.postprocess.conversation_recovery")

RECOVERY_TOPIC = "conversation_recovery"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Markers that must never appear as generic fallbacks (P0 audit list).
_GENERIC_ACK_MARKERS = (
    "تمام 🌷 وصلت رسالتك",
    "وصلت رسالتك",
    "حياك الله، وصلت رسالتك",
)

# Short standalone ACK stubs — only generic when the whole reply is stubby.
_SHORT_ACK_STUBS = frozenset({
    "حاضر 🌷",
    "حاضر",
    "تم 🌷",
    "تم",
    "أبشر 🌷",
    "أبشر",
    "تمام 🌷",
    "تمام",
})


@dataclass(frozen=True)
class ConversationRecoveryResult:
    reply: str = ""
    source: str = ""
    needs_persona_compose: bool = False


def _normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def is_generic_ack_stub_text(text: str) -> bool:
    """True when outbound is a banned generic ACK stub (P0 success criteria)."""
    raw = (text or "").strip()
    if not raw:
        return False
    if raw in _SHORT_ACK_STUBS:
        return True
    norm = _normalize(raw)
    if norm in {_normalize(x) for x in _SHORT_ACK_STUBS}:
        return True
    for marker in _GENERIC_ACK_MARKERS:
        if marker in raw and len(raw) <= len(marker) + 24:
            return True
    if norm in {_normalize(m) for m in _GENERIC_ACK_MARKERS}:
        return True
    return False


def _last_outbound_question(state: Any, history: Optional[list] = None) -> str:
    if state is not None:
        q = str(getattr(state, "last_question_asked", "") or "").strip()
        if q:
            return q
        if isinstance(state, dict):
            q = str(state.get("last_question_asked") or "").strip()
            if q:
                return q
    for turn in reversed(history or []):
        if turn.get("direction") not in ("out", "outbound"):
            continue
        body = str(turn.get("body") or "").strip()
        if body and ("؟" in body or "?" in body):
            return body
    return ""


def _last_outbound_snippet(history: Optional[list], *, limit: int = 160) -> str:
    for turn in reversed(history or []):
        if turn.get("direction") not in ("out", "outbound"):
            continue
        body = str(turn.get("body") or "").strip()
        if body:
            return body[:limit]
    return ""


def compose_conversation_recovery_goal(
    *,
    inbound_text: str = "",
    last_question: str = "",
    last_outbound: str = "",
    recovery_reason: str = "",
) -> str:
    """Principle-based goal for persona compose recovery turns."""
    ctx_bits: list[str] = []
    if last_question:
        ctx_bits.append(f"Last bot question: {last_question[:120]}")
    elif last_outbound:
        ctx_bits.append(f"Recent bot message: {last_outbound[:120]}")
    context_line = " | ".join(ctx_bits) if ctx_bits else "No pending bot question in state."
    reason = (recovery_reason or "unclear_context").strip()
    return (
        f"conversation_recovery — The customer's message needs a natural "
        f"Saudi Arabic WhatsApp reply ({reason}). "
        f"{context_line} "
        "Use conversation continuity: if they answered a prior question, "
        "acknowledge it specifically and advance naturally. "
        "If intent is unclear, ask one short contextual question tied to "
        "what was just discussed — not a generic receipt ACK. "
        "For playful identity/nationality probes (e.g. «انت تركي»), respond "
        "in Nahla's warm persona with natural banter — not silence. "
        "Never reply with only «حاضر» / «تمام» / «أبشر» / «وصلت رسالتك». "
        "Do NOT pitch products unless the customer asked to buy. "
        "Do NOT use customer-service closers."
    )


def try_guard_recovery_reply(
    *,
    inbound_text: str = "",
    state: Any = None,
    history: Optional[list] = None,
) -> ConversationRecoveryResult:
    """
    Synchronous contextual recovery for post-compose guards.

    Returns ``needs_persona_compose=True`` when guards should defer to LLM
    instead of injecting a canned stub.
    """
    raw = (inbound_text or "").strip()
    if not raw:
        return ConversationRecoveryResult(needs_persona_compose=True, source="empty_inbound")

    try:
        from modules.ai.brain.commerce.conversation_state_isolation import (  # noqa: PLC0415
            inbound_breaks_fulfillment_ownership,
            should_replay_pending_question,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — isolation import must not break recovery
        inbound_breaks_fulfillment_ownership = lambda _m: False  # type: ignore[assignment,misc]
        should_replay_pending_question = lambda **_: False  # type: ignore[assignment,misc]

    try:
        from modules.ai.brain.commerce.start_order_verb_guard import (  # noqa: PLC0415
            is_bare_start_order_phrase,
        )
        from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: PLC0415
            build_bare_start_order_guard_reply,
            build_short_honey_order_clarify_reply,
            is_short_honey_order_request,
        )

        if is_bare_start_order_phrase(raw):
            return ConversationRecoveryResult(
                reply=build_bare_start_order_guard_reply(raw),
                source="bare_start_order",
            )
        if is_short_honey_order_request(raw):
            return ConversationRecoveryResult(
                reply=build_short_honey_order_clarify_reply(raw),
                source="short_honey_order",
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — recovery probe must not break compose
        pass

    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            resolve_social_thanks_guard_reply,
        )

        social = resolve_social_thanks_guard_reply(raw)
        if social:
            return ConversationRecoveryResult(reply=social, source="social_thanks_mirror")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — social mirror probe must not break recovery
        pass

    try:
        from modules.ai.brain.intent import rules as intent_rules  # noqa: PLC0415
        from modules.ai.brain.types import INTENT_WHO_ARE_YOU  # noqa: PLC0415

        matched = intent_rules.match(raw)
        if matched is not None and matched.name == INTENT_WHO_ARE_YOU:
            return ConversationRecoveryResult(
                needs_persona_compose=True,
                source="persona_identity_probe",
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — identity probe must not break recovery
        pass

    last_q = _last_outbound_question(state, history)
    if last_q and should_replay_pending_question(
        inbound_text=raw,
        last_question=last_q,
    ):
        snippet = last_q[:80].rstrip("؟?").strip()
        return ConversationRecoveryResult(
            reply=f"فهمت — بخصوص «{snippet}»، وضّح لي أكثر عشان أكمل معك.",
            source="last_question_clarify",
        )

    if last_q and inbound_breaks_fulfillment_ownership(raw):
        return ConversationRecoveryResult(
            needs_persona_compose=True,
            source="fulfillment_topic_break",
        )

    last_out = _last_outbound_snippet(history)
    if last_out:
        return ConversationRecoveryResult(
            needs_persona_compose=True,
            source="contextual_persona_compose",
        )

    return ConversationRecoveryResult(
        needs_persona_compose=True,
        source="persona_compose_fallback",
    )


def build_recovery_decision_args(
    *,
    inbound_text: str = "",
    state: Any = None,
    history: Optional[list] = None,
    recovery_reason: str = "",
) -> dict[str, Any]:
    """Args for ``ACTION_LLM_REPLY`` + ``conversation_recovery`` topic."""
    last_q = _last_outbound_question(state, history)
    last_out = _last_outbound_snippet(history)
    return {
        "topic": RECOVERY_TOPIC,
        "block_commerce_escalation": True,
        "recovery_reason": recovery_reason or "guard_stub_avoidance",
        "last_question_asked": last_q,
        "last_outbound_snippet": last_out,
        "inbound_preview": (inbound_text or "")[:120],
    }


__all__ = [
    "ConversationRecoveryResult",
    "RECOVERY_TOPIC",
    "build_recovery_decision_args",
    "compose_conversation_recovery_goal",
    "is_generic_ack_stub_text",
    "try_guard_recovery_reply",
]
