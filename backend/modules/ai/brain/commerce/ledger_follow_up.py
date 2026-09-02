"""
ledger_follow_up.py
───────────────────
Conditional follow-up routing for customer commerce ledger turns.

When the previous turn was a ledger reply (order history / latest summary /
reference list), short pronoun/reference follow-ups such as «تعرف أرقامها؟»
must route back to the ledger answerer — not staff-contact detection.

All routing is gated on active ledger context so standalone contact-number
asks (e.g. «وش رقم المسؤول؟») keep their existing staff_contact behaviour.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from ..decision.actions import ACTION_CUSTOMER_LEDGER_REPLY
from ..types import (
    Decision,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
)

_LEDGER_RECENT_TOPIC = "customer_ledger"

_LEDGER_INTENTS = frozenset(
    {
        INTENT_ORDER_HISTORY_COUNT,
        INTENT_LATEST_ORDER_SUMMARY,
        INTENT_ORDER_REFERENCE_LIST,
    }
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Whitelist detector — two shapes only (evaluated after Arabic normalization):
# 1. Pronoun head: «أرقامها» / «أرقامهم» optionally preceded by «وش» / «تعرف» /
#    the «عتعرف» typo; nothing may follow except punctuation (blocks «أرقام» + noun).
# 2. Order-bound: «أرقام» immediately followed by an order noun («الطلبات», «طلباتي»,
#    «طلبي», «الطلب»), optionally with a leading verb or «وش».
_PRONOUN_HEAD_RE = re.compile(
    r"(?:"
    r"^(?:وش\s+)?ارقام(?:ها|هم)\s*[\?؟!.]*$"
    r"|^(?:ع?\s*ت?\s*ع?\s*رف|تعرف)\s+(?:وش\s+)?ارقام(?:ها|هم)\s*[\?؟!.]*$"
    r"|^ارقام(?:ها|هم)\s*[\?؟!.]*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)
_ORDER_BOUND_RE = re.compile(
    r"(?:"
    r"^ارقام\s+(?:الطلبات|طلباتي|طلبي|الطلب)\s*[\?؟!.]*$"
    r"|^(?:ارسل|ابعث|ارسلي|أرسل|أرسلي|ابعثي)\s+ارقام\s+(?:الطلبات|طلباتي|طلبي|الطلب)\s*[\?؟!.]*$"
    r"|^وش\s+ارقام\s+(?:الطلبات|طلباتي|طلبي|الطلب)\s*[\?؟!.]*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _NORM_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return _WS_RE.sub(" ", s.lower()).strip()


def stamp_ledger_context(state: Any) -> None:
    """Stamp multi-turn ledger context using the shared recent-topic TTL mechanism."""
    if state is None:
        return
    from .fallback_guard import stamp_recent_topic  # noqa: PLC0415

    stamp_recent_topic(state, _LEDGER_RECENT_TOPIC)


def is_ledger_context_active(state: Any) -> bool:
    """
    True when a recent ledger turn still owns conversational context.

    **Single-transition signals** — ``last_intent`` in ledger intents, or
    ``last_action == ACTION_CUSTOMER_LEDGER_REPLY`` when present on loaded
    persisted state. These are overwritten every transition (``last_intent`` by
    ``DefaultStateStore.transition``; ``last_action`` by the pipeline after
    transition) and therefore only reflect the immediately previous turn — they
    cannot go stale across many idle turns.

    **Multi-turn signal** — ``recent_topic='customer_ledger'`` stamped by
    ``stamp_ledger_context()`` during a ledger decision. Bounded by
    ``RECENT_TOPIC_TTL_TURNS`` (4) via ``is_recent_topic_active()``.
    """
    if state is None:
        return False

    last_intent = str(getattr(state, "last_intent", "") or "")
    if last_intent in _LEDGER_INTENTS:
        return True

    last_action = str(getattr(state, "last_action", "") or "")
    if last_action == ACTION_CUSTOMER_LEDGER_REPLY:
        return True

    recent = str(getattr(state, "recent_topic", "") or "")
    if recent != _LEDGER_RECENT_TOPIC:
        return False

    from .fallback_guard import is_recent_topic_active  # noqa: PLC0415

    turn = int(getattr(state, "turn", 0) or 0)
    return is_recent_topic_active(state, current_turn=turn)


def is_order_reference_follow_up(message: str) -> bool:
    """True for pronoun or order-bound reference follow-ups to a prior ledger turn."""
    text = _norm_ar(str(message or "").strip())
    if not text:
        return False
    return bool(_PRONOUN_HEAD_RE.match(text) or _ORDER_BOUND_RE.match(text))


def try_ledger_follow_up_decision(ctx: Any) -> Optional[Decision]:
    """
    Route ledger reference-list follow-ups when context is active.

    Also handles an explicit ``INTENT_ORDER_REFERENCE_LIST`` classification
    (e.g. «أرسل أرقام الطلبات») before staff-contact probes can hijack it.

    Returns ``ACTION_CUSTOMER_LEDGER_REPLY`` with
    ``ledger_topic=INTENT_ORDER_REFERENCE_LIST`` when matched; otherwise ``None``.
    """
    from .order_support_ownership import (  # noqa: PLC0415
        has_authoritative_order_support_ownership,
        should_stamp_ledger_context,
    )

    state = getattr(ctx, "state", None)
    message = str(getattr(ctx, "message", "") or "")
    intent = getattr(ctx, "intent", None)
    intent_name = str(getattr(intent, "name", "") or "")

    if (
        intent_name == INTENT_ORDER_REFERENCE_LIST
        and has_authoritative_order_support_ownership(intent, state=state)
    ):
        stamp_ledger_context(state)
        return Decision(
            action=ACTION_CUSTOMER_LEDGER_REPLY,
            args={"ledger_topic": INTENT_ORDER_REFERENCE_LIST},
            reason="customer commerce ledger — order reference list",
            confidence=float(getattr(intent, "confidence", 0.0) or 0.94),
        )

    if not is_ledger_context_active(state):
        return None
    if is_order_reference_follow_up(message):
        return Decision(
            action=ACTION_CUSTOMER_LEDGER_REPLY,
            args={"ledger_topic": INTENT_ORDER_REFERENCE_LIST},
            reason="ledger context — order reference list follow-up",
            confidence=0.94,
        )

    # Structural continuation: Order Support still owns the turn after a
    # proven ledger/order-support turn. No customer-phrase detector.
    if not has_authoritative_order_support_ownership(intent, state=state):
        return None
    if intent_name in {INTENT_ORDER_HISTORY_COUNT, INTENT_LATEST_ORDER_SUMMARY}:
        return None
    try:
        from .order_tracking_intent_guard import (  # noqa: PLC0415
            is_explicit_order_tracking_request,
        )

        if is_explicit_order_tracking_request(
            message,
            state=state,
            history=getattr(ctx, "history", None),
            commerce_bundle=getattr(ctx, "commerce_bundle", None),
        ):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — tracking probe must not block ledger continuation
        return None
    if should_stamp_ledger_context(intent, state=state):
        stamp_ledger_context(state)
    return Decision(
        action=ACTION_CUSTOMER_LEDGER_REPLY,
        args={"ledger_topic": INTENT_ORDER_REFERENCE_LIST},
        reason="ledger context — authoritative order-support continuation",
        confidence=0.94,
    )


__all__ = [
    "is_ledger_context_active",
    "is_order_reference_follow_up",
    "stamp_ledger_context",
    "try_ledger_follow_up_decision",
]
