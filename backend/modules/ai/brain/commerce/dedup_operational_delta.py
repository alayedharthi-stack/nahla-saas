"""
brain/commerce/dedup_operational_delta.py
──────────────────────────────────────────
Conservative unlock for CHAT_DEDUP hard-tier when the customer adds new
operational information (product, weight, location, buy intent) that was not
already present in prior inbound turns or the last outbound reply.

Tenant-agnostic — pure helpers, no I/O.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, FrozenSet, List, Optional

_STOPWORDS = frozenset({
    "في", "من", "على", "عن", "هل", "كيف", "احنا", "إحنا", "انا", "أنا",
    "هذا", "هذه", "ذلك", "تلك", "اللي", "الي", "و", "او", "أو", "مع",
    "لي", "لك", "بعد", "قبل", "شي", "شيء", "something", "the", "a", "an",
    # Generic commerce / ack words — never product tokens.
    "تمام", "طيب", "اوكي", "ok", "موجود", "سعر", "شحن", "توصيل", "كم", "بكم",
})

_BUY_INTENT_RE = re.compile(
    r"(?:"
    r"(?:نبغ(?:ى|ي)?|اب(?:غ|ي)|أب(?:غ|ي)|اريد|أريد|بغيت|ود(?:ي|ين)|حاب)"
    r"(?:\s+\S+){0,6}\s+(?:اشتري|أشتري|نشتري|اطلب|أطلب|طلب)"
    r"|(?:اشتري|أشتري|نشتري|اطلب|أطلب)\s+"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_WANT_PRODUCT_RE = re.compile(
    r"(?:"
    r"(?:نبغ(?:ى|ي)?|اب(?:غ|ي)|أب(?:غ|ي)|اريد|أريد|بغيت|ود(?:ي|ين)|حاب)"
    r"(?:\s+\S+){0,4}\s+(?:اشتري|أشتري|نشتري|اطلب|أطلب|طلب)"
    r"|(?:اشتري|أشتري|نشتري|اطلب|أطلب)"
    r")\s+(?:ال)?([\u0600-\u06FFa-zA-Z]{2,24})",
    re.UNICODE | re.IGNORECASE,
)

_WANT_SHORT_RE = re.compile(
    r"^(?:نبغ(?:ى|ي)?|اب(?:غ|ي)|أب(?:غ|ي)|اريد|أريد|بغيت|ود(?:ي|ين)|حاب)"
    r"\s+(?:ال)?([\u0600-\u06FFa-zA-Z]{2,24})\s*$",
    re.UNICODE | re.IGNORECASE,
)

_WEIGHT_SLOT_RE = re.compile(
    r"(?:^|\s)(?:\d+(?:[.,]\d+)?\s*)?(?:نصف\s+|ربع\s+)?"
    r"(?:كilo|كيلo|كيلو|kg|جرام|gram|grams)(?:\s|$)",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_AFTER_WEIGHT_RE = re.compile(
    r"(?:^|\s)(?:\d+(?:[.,]\d+)?\s*)?(?:نصف\s+|ربع\s+)?"
    r"(?:كilo|كيلo|كيلو|kg|جرام|gram)\s+(?:ال)?([\u0600-\u06FFa-zA-Z]{2,24})",
    re.UNICODE | re.IGNORECASE,
)

_SHIPPING_RE = re.compile(
    r"توصيل|شحن|delivery|shipping|يوصل|توصل",
    re.UNICODE | re.IGNORECASE,
)

_CITY_CONTEXT_RE = re.compile(
    r"(?:"
    r"(?:في|ل|إلى|الى|لـ)\s+(?:ال)?([\u0600-\u06FF]{2,24})"
    r"|(?:^|\s)ل(?:ل)([\u0600-\u06FF]{2,24})"
    r")",
    re.UNICODE,
)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or "").strip().lower())
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def _add_product_token(slots: set[str], token: str) -> None:
    tok = _norm(token)
    if not tok or tok in _STOPWORDS or len(tok) < 2:
        return
    slots.add(f"product_token:{tok}")


def operational_order_reference_slot(text: str) -> str:
    """
    Return ``order_ref:<digits>`` when inbound carries a recognized order reference.

    Reuses ``order_tracking_intent_guard`` bare/labeled extractors — no duplicate regex.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            extract_bare_order_reference,
            extract_order_reference_from_history,
        )

        bare = extract_bare_order_reference(raw)
        if bare:
            return f"order_ref:{bare}"
        labeled = extract_order_reference_from_history(
            [{"direction": "inbound", "body": raw}],
        )
        if labeled:
            return f"order_ref:{labeled}"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional tracking guard import
        return ""
    return ""


def extract_operational_slots(text: str) -> FrozenSet[str]:
    """Return normalized operational slot keys extracted from ``text``."""
    raw = (text or "").strip()
    norm = _norm(raw)
    if not norm:
        return frozenset()

    slots: set[str] = set()

    order_ref_slot = operational_order_reference_slot(raw)
    if order_ref_slot:
        slots.add(order_ref_slot)

    if _BUY_INTENT_RE.search(norm):
        slots.add("intent:buy")

    if _WEIGHT_SLOT_RE.search(norm):
        slots.add("weight:unit")

    try:
        from .fallback_guard import _semantic_product_entity  # noqa: PLC0415

        entity = _semantic_product_entity(raw)
        if entity:
            slots.add(f"product:{entity}")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional semantic entity import
        pass

    for match in _WANT_PRODUCT_RE.finditer(raw):
        _add_product_token(slots, match.group(1))

    for match in _WANT_SHORT_RE.finditer(raw):
        _add_product_token(slots, match.group(1))

    for match in _PRODUCT_AFTER_WEIGHT_RE.finditer(raw):
        _add_product_token(slots, match.group(1))

    try:
        from .solution_seeking import detect_solution_seeking_suppression  # noqa: PLC0415

        topic = detect_solution_seeking_suppression(raw, skip_recent_topic=True)
        if topic in {"delivery_intent", "location_intent", "order_intent"}:
            slots.add(f"intent:{topic}")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional semantic entity import
        pass

    if _SHIPPING_RE.search(norm):
        city_match = _CITY_CONTEXT_RE.search(raw)
        if city_match:
            city = _norm(city_match.group(1) or city_match.group(2) or "")
            if city and city not in _STOPWORDS:
                slots.add(f"location:{city}")

    return frozenset(slots)


def _prior_inbound_slots(
    history: Optional[List[Any]],
    *,
    exclude_current: str = "",
) -> set[str]:
    exclude_norm = _norm(exclude_current)
    inbound_bodies: List[str] = []
    for turn in history or []:
        direction = str((turn or {}).get("direction") or "").lower()
        if direction not in {"in", "inbound"}:
            continue
        body = str((turn or {}).get("body") or (turn or {}).get("text") or "").strip()
        if body:
            inbound_bodies.append(body)

    if (
        exclude_norm
        and inbound_bodies
        and _norm(inbound_bodies[-1]) == exclude_norm
    ):
        inbound_bodies = inbound_bodies[:-1]

    slots: set[str] = set()
    for body in inbound_bodies:
        slots |= set(extract_operational_slots(body))
    return slots


def last_outbound_body(history: Optional[List[Any]]) -> str:
    """Most recent outbound message body from webhook-style history."""
    for turn in reversed(history or []):
        direction = str((turn or {}).get("direction") or "").lower()
        if direction not in {"out", "outbound"}:
            continue
        body = str((turn or {}).get("body") or (turn or {}).get("text") or "").strip()
        if body:
            return body
    return ""


def has_operational_delta_since_last_reply(
    current_inbound: str,
    candidate_reply: str,
    previous_outbound: str,
    *,
    history: Optional[List[Any]] = None,
    context: Any = None,
) -> bool:
    """
    True when ``current_inbound`` introduces operational slots that were
    neither in prior customer turns nor already reflected in
    ``previous_outbound``, and a non-empty ``candidate_reply`` exists.

    ``context`` is reserved for future state-aware checks; unused today.
    """
    _ = context
    if not (candidate_reply or "").strip():
        return False

    current_slots = extract_operational_slots(current_inbound)
    if not current_slots:
        return False

    prior_inbound = _prior_inbound_slots(history, exclude_current=current_inbound)
    new_vs_prior = set(current_slots) - prior_inbound
    if not new_vs_prior:
        return False

    outbound_slots = set(extract_operational_slots(previous_outbound))
    unanswered = new_vs_prior - outbound_slots
    return bool(unanswered)


def prior_outbound_was_unhelpful_availability_rewrite(outbound: str) -> bool:
    """True when the last bot reply was the availability truth-guard canned rewrite."""
    ob = (outbound or "").strip()
    if not ob:
        return False
    if "معلومات متعارضة حول التوفر" in ob:
        return True
    if "ما أقدر أأكد التوفر الحالي بدقة" in ob:
        return True
    if "ما نقدر نأكد التوفر بدقة" in ob:
        return True
    if "ما نقدر نؤكد التوفر بدقة" in ob:
        return True
    return False


def prior_outbound_was_wrong_social_only_reply(outbound: str) -> bool:
    """True when the prior outbound was a short social/greeting ack without commerce substance."""
    ob = (outbound or "").strip()
    if not ob or len(ob) > 120:
        return False
    if prior_outbound_was_unhelpful_availability_rewrite(ob):
        return False
    try:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            is_commerce_inquiry_turn,
            has_price_inquiry_signal,
        )

        if is_commerce_inquiry_turn(ob) or has_price_inquiry_signal(ob):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional commerce inquiry probe
        pass
    social_markers = (
        "صباح النور",
        "صباح الخير",
        "مساء النور",
        "social_persona_ack",
        "👋",
        "🌿",
    )
    return any(marker in ob for marker in social_markers)


def _inbound_is_availability_or_commerce_inquiry(text: str) -> bool:
    is_direct = False
    try:
        from core.product_entity_resolution import direct_product_availability_ask  # noqa: PLC0415

        is_direct = bool(direct_product_availability_ask(text))
    except Exception:  # noqa: BLE001
        is_direct = False
    try:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            is_commerce_inquiry_turn,
        )

        return bool(is_commerce_inquiry_turn(text) or is_direct)
    except Exception:  # noqa: BLE001
        return is_direct


def should_bypass_hard_dedup_repeat_availability(
    current_inbound: str,
    previous_outbound: str,
) -> bool:
    """
    Allow a new reply when the customer repeats a direct availability ask
    after the guard replaced the prior answer with an unhelpful canned line,
    or after a wrong social-only reply dropped the commerce question.
    """
    if not _inbound_is_availability_or_commerce_inquiry(current_inbound):
        return False
    if prior_outbound_was_unhelpful_availability_rewrite(previous_outbound):
        return True
    return prior_outbound_was_wrong_social_only_reply(previous_outbound)


def should_restore_brain_reply_after_dedup_silence(
    *,
    current_inbound: str,
    candidate_reply: str,
    previous_outbound: str = "",
) -> bool:
    """True when hard dedup would silence a commerce inquiry that already composed."""
    if not (candidate_reply or "").strip():
        return False
    # Bare/labeled order-reference turns (including same-ref retries after
    # track_order_not_found) must not end in zero outbound when brain composed.
    if is_local_order_status_inquiry(current_inbound):
        return True
    if not _inbound_is_availability_or_commerce_inquiry(current_inbound):
        return False
    if should_bypass_hard_dedup_repeat_availability(current_inbound, previous_outbound):
        return True
    # Any commerce inquiry must not end in zero outbound when brain composed text.
    return True


_LOCAL_ORDER_STATUS_MARKERS = (
    "وين طلبي",
    "فين طلبي",
    "كم رقم الطلب",
    "رقم الطلب",
    "حالة الطلب",
    "تتبع الطلب",
    "track my order",
    "order status",
)


def is_local_order_status_inquiry(message: str) -> bool:
    """True when inbound is a track/order-number question (not social/general)."""
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.order_flow_v2.triggers import (  # noqa: PLC0415
            is_checkout_order_number_intent,
        )

        if is_checkout_order_number_intent(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional trigger import
        pass
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            is_order_tracking_follow_up,
        )

        if is_order_tracking_follow_up(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional tracking guard import
        pass
    norm = _norm(raw)
    return any(marker in norm for marker in _LOCAL_ORDER_STATUS_MARKERS)
