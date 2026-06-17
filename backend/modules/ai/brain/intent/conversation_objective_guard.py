"""
conversation_objective_guard.py
───────────────────────────────
Session-level conversation objective tracking (platform-wide).

Locks multi-turn threads such as product-origin verification so follow-up
turns (agent, contact, supply chain) stay grounded in the same story.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..intent_priority.types import (
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_PRODUCT_ORIGIN_VERIFICATION,
    GOAL_STAFF_CONTACT,
)
from .agent_distributor_classifier import is_agent_distributor_inquiry

OBJECTIVE_PRODUCT_ORIGIN = "product_origin_verification"
OBJECTIVE_TTL_TURNS = 6

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_WS = re.compile(r"\s+")

_IMAGE_TYPE_TOKENS = frozenset({
    "image",
    "photo",
    "picture",
    "sticker",
    "product_photo",
    "customer_photo",
})

_OWNERSHIP_RE = re.compile(
    r"(?:"
    r"(?:ده|هذا|هذي|هذه|المنتج\s*(?:ده|هذا|هذي|هذه)?)\s*(?:تبع(?:كم|ك|ه|ها|هم)|"
    r"من\s*(?:عند(?:كم|ك|ه|ها|هم)|كم|ك|ه|ها|هم)|"
    r"تابع\s*(?:ل)?(?:كم|ك|ه|ها|هم)|"
    r"منتج(?:كم|ك|ه|ها|هم))"
    r"|(?:هل|ه(?:ل|)\s*(?:هذا|هذي|هذه|المنتج))\s*(?:منتج(?:كم|ك|ه|ها|هم)|"
    r"تبع(?:كم|ك|ه|ها|هم)|من\s*(?:عند(?:كم|ك)|كم|ك))"
    r"|(?:is\s+this|are\s+you)\s*(?:product|yours|from\s+you)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_RECEIVED_ORDER_RE = re.compile(
    r"(?:"
    r"(?:جالي|جتني|وصل(?:ني|نا|وا)?|استلم(?:ت|نا|وا)?)\s*(?:اوردر|أوردر|order|طلب|منتج|شحنة|بضاع(?:ه|ة))"
    r"|(?:اوردر|أوردر|order|طلب)\s*(?:جاني|وصل(?:ني|نا)?)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SUPPLY_CHAIN_RE = re.compile(
    r"(?:"
    r"(?:وصل(?:ني|نا)|جالي|استلم(?:ت|نا))\s*(?:ازاي|إزاي|كيف|how)\s*(?:من\s*)?(?:عند(?:كم|ك)|منكم|منك|from\s+you)"
    r"|(?:ازاي|إزاي|كيف|how)\s*(?:وصل(?:ني|نا)|جاني|جتني)\s*(?:ال)?(?:منتج|طلب|اوردر|أوردر|order)?\s*(?:من\s*)?(?:عند(?:كم|ك)|منكم|from\s+you)?"
    r"|(?:من\s*(?:وين|أين|اين|where))\s*(?:وصل(?:ني|نا)|جاني|جت(?:ني|نا))"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_PURCHASE_CLEAR_RE = re.compile(
    r"(?:"
    r"(?:ابي|ابغى|أبي|أبغى|بدي|اريد|أريد|want\s+to)\s*(?:اطلب|أطلب|اشتري|أشتري|buy|order|purchase)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_AVAILABILITY_EXPLICIT_RE = re.compile(
    r"(?:"
    r"(?:عند(?:كم|ك)|لد(?:يكم|يك)|متوفر|موجود|available|in\s*stock)"
    r"|(?:هل|ه(?:ل|))\s+\S+\s+(?:متوفر|موجود|available)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _DIA.sub("", s)
    s = (
        s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    )
    return _WS.sub(" ", s.lower()).strip()


def _has_image_attachment(profile: Dict[str, Any]) -> bool:
    meta = dict((profile or {}).get("inbound_metadata") or {})
    for key in ("normalized_type", "message_type", "media_type", "type"):
        val = str(meta.get(key) or "").strip().lower()
        if val in _IMAGE_TYPE_TOKENS:
            return True
    for key in ("has_image", "has_media", "is_image"):
        if meta.get(key):
            return True
    image_kind = str(meta.get("image_kind") or "").strip()
    if image_kind and image_kind not in {"text", ""}:
        return True
    return False


def _detect_origin_trigger(message: str, *, has_image: bool) -> str:
    raw = (message or "").strip()
    norm = _norm(raw)
    if not norm and not has_image:
        return ""
    if has_image and (not norm or _OWNERSHIP_RE.search(norm)):
        return "inbound_image_ownership"
    if _OWNERSHIP_RE.search(norm):
        return "ownership_ask"
    if _RECEIVED_ORDER_RE.search(norm):
        return "received_order"
    if _SUPPLY_CHAIN_RE.search(norm):
        return "supply_chain"
    if is_agent_distributor_inquiry(raw):
        return "agent_distributor"
    return ""


def _objective_age_turns(state: Any, *, current_turn: int) -> int:
    reinforced = int(getattr(state, "objective_last_reinforced_turn", 0) or 0)
    if reinforced <= 0:
        return 999
    return max(0, int(current_turn) - reinforced)


def is_product_origin_objective_active(state: Any, *, current_turn: Optional[int] = None) -> bool:
    objective = str(getattr(state, "active_conversation_objective", "") or "").strip()
    if objective != OBJECTIVE_PRODUCT_ORIGIN:
        return False
    turn = int(current_turn if current_turn is not None else (getattr(state, "turn", 0) or 0) + 1)
    if _objective_age_turns(state, current_turn=turn) > OBJECTIVE_TTL_TURNS:
        return False
    return True


def _clear_objective(state: Any) -> None:
    state.active_conversation_objective = ""
    state.objective_started_turn = 0
    state.objective_last_reinforced_turn = 0
    state.objective_evidence = {}


def _stamp_objective(
    state: Any,
    *,
    current_turn: int,
    trigger: str,
    has_image: bool,
) -> None:
    evidence = dict(getattr(state, "objective_evidence", None) or {})
    if has_image:
        evidence["has_inbound_image"] = True
        evidence["image_turn"] = current_turn
    if trigger:
        evidence["last_trigger"] = trigger
    if not getattr(state, "active_conversation_objective", ""):
        state.objective_started_turn = current_turn
    state.active_conversation_objective = OBJECTIVE_PRODUCT_ORIGIN
    state.objective_last_reinforced_turn = current_turn
    state.objective_evidence = evidence


@dataclass
class ObjectiveTurnResult:
    active: bool = False
    objective: str = ""
    trigger: str = ""
    cleared: bool = False
    reinforced: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)


def refresh_conversation_objective(
    state: Any,
    message: str,
    profile: Optional[Dict[str, Any]] = None,
) -> ObjectiveTurnResult:
    """
    Update session objective for the current inbound turn.

    Mutates ``state`` in place. Pure deterministic — no DB/LLM.
    """
    profile = profile or {}
    current_turn = int(getattr(state, "turn", 0) or 0) + 1
    raw = (message or "").strip()
    norm = _norm(raw)
    has_image = _has_image_attachment(profile)

    if _EXPLICIT_PURCHASE_CLEAR_RE.search(norm):
        was_active = is_product_origin_objective_active(state, current_turn=current_turn)
        _clear_objective(state)
        return ObjectiveTurnResult(
            active=False,
            cleared=was_active,
        )

    if (
        getattr(state, "active_conversation_objective", "")
        and _objective_age_turns(state, current_turn=current_turn) > OBJECTIVE_TTL_TURNS
    ):
        _clear_objective(state)

    trigger = _detect_origin_trigger(raw, has_image=has_image)

    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            is_store_channel_phone_phrase,
        )
    except Exception:  # noqa: silent-ok — entity guard optional at objective boundary
        is_store_channel_phone_phrase = lambda _m: False  # type: ignore[assignment,misc]

    reinforces = bool(
        trigger
        or (
            is_product_origin_objective_active(state, current_turn=current_turn)
            and (
                is_agent_distributor_inquiry(raw)
                or is_store_channel_phone_phrase(raw)
                or _SUPPLY_CHAIN_RE.search(norm)
                or _OWNERSHIP_RE.search(norm)
                or _RECEIVED_ORDER_RE.search(norm)
            )
        )
    )

    if reinforces and trigger:
        _stamp_objective(state, current_turn=current_turn, trigger=trigger, has_image=has_image)
    elif reinforces and is_product_origin_objective_active(state, current_turn=current_turn):
        _stamp_objective(
            state,
            current_turn=current_turn,
            trigger=str(
                (getattr(state, "objective_evidence", None) or {}).get("last_trigger")
                or "continuation"
            ),
            has_image=has_image,
        )
    elif trigger:
        _stamp_objective(state, current_turn=current_turn, trigger=trigger, has_image=has_image)

    active = is_product_origin_objective_active(state, current_turn=current_turn)
    return ObjectiveTurnResult(
        active=active,
        objective=str(getattr(state, "active_conversation_objective", "") or ""),
        trigger=trigger,
        reinforced=bool(reinforces and active),
        evidence=dict(getattr(state, "objective_evidence", None) or {}),
    )


def apply_objective_to_primary_goal(
    primary_goal: str,
    *,
    message: str,
    state: Any,
) -> str:
    """Bias per-turn goal when a product-origin objective is active."""
    if not is_product_origin_objective_active(state):
        return primary_goal

    raw = (message or "").strip()
    norm = _norm(raw)

    if _AVAILABILITY_EXPLICIT_RE.search(norm):
        return primary_goal

    if is_agent_distributor_inquiry(raw) or _SUPPLY_CHAIN_RE.search(norm):
        return GOAL_PRODUCT_ORIGIN_VERIFICATION

    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            is_store_channel_phone_phrase,
        )

        if is_store_channel_phone_phrase(raw):
            return GOAL_STAFF_CONTACT
    except Exception:  # noqa: silent-ok — entity guard optional at objective boundary
        pass

    if primary_goal == GOAL_PRODUCT_AVAILABILITY:
        return GOAL_PRODUCT_ORIGIN_VERIFICATION

    if getattr(state, "active_conversation_objective", "") == OBJECTIVE_PRODUCT_ORIGIN:
        return GOAL_PRODUCT_ORIGIN_VERIFICATION

    return primary_goal


def should_block_availability_fallback(
    *,
    inbound_text: str,
    intent_name: str = "",
    primary_customer_goal: str = "",
    conversation_objective: str = "",
) -> bool:
    """True when commerce empty-reply fallback must NOT use availability wording."""
    raw = (inbound_text or "").strip()
    norm = _norm(raw)
    goal = (primary_customer_goal or "").strip().lower()
    intent = (intent_name or "").strip().lower()

    if (conversation_objective or "").strip() == OBJECTIVE_PRODUCT_ORIGIN:
        return True

    if is_agent_distributor_inquiry(raw):
        return True

    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            is_generic_store_contact_phrase,
            is_store_channel_phone_phrase,
        )

        if is_store_channel_phone_phrase(raw) or is_generic_store_contact_phrase(raw):
            return True
    except Exception:  # noqa: silent-ok — entity guard optional at objective boundary
        pass

    if _SUPPLY_CHAIN_RE.search(norm) or _OWNERSHIP_RE.search(norm):
        return True

    if _RECEIVED_ORDER_RE.search(norm):
        return True

    if goal in {GOAL_PRODUCT_ORIGIN_VERIFICATION, "staff_contact"} and not _AVAILABILITY_EXPLICIT_RE.search(norm):
        if goal == "staff_contact" or intent in {"ask_owner_contact", "talk_to_human"}:
            return True

    if intent in {"general", "social", "greeting", "who_are_you", "ask_location"}:
        return True

    if (goal == GOAL_PRODUCT_AVAILABILITY or intent in {
        "ask_product",
        "solution_seeking_commerce",
        "product_availability",
    }) and not _AVAILABILITY_EXPLICIT_RE.search(norm):
        return True

    return False


__all__ = [
    "OBJECTIVE_PRODUCT_ORIGIN",
    "OBJECTIVE_TTL_TURNS",
    "ObjectiveTurnResult",
    "apply_objective_to_primary_goal",
    "is_product_origin_objective_active",
    "refresh_conversation_objective",
    "should_block_availability_fallback",
]
