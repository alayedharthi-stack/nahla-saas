"""
brain/commerce/fallback_guard.py
────────────────────────────────
Global SaaS safeguards: multi-turn topic memory, fallback replay exhaustion,
and semantic dead-end / customer-goal inference.

Tenant-agnostic — used by product discovery, solution-seeking, and clarify paths.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.brain.fallback_guard")

RECENT_TOPIC_TTL_TURNS = 4
FALLBACK_COOLDOWN_TURNS = 2
CLARIFY_LOOP_THRESHOLD = 3

_PRICE_SIZE_LOOP_RE = re.compile(
    r"(?:"
    r"كم\s*السعر|بكم|سعر|الحجم|حجم|احجام|أحجام|الاحجام|"
    r"كل\s*ال(?:حج|أح|اح)جام|كل\s*الاحجام|"
    r"اي\s*منتج|أي\s*منتج|price|size|variant"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_ALL_VARIANTS_RE = re.compile(
    r"(?:"
    r"كل\s*ال(?:حج|أح|اح)?(?:ج)?(?:ام|ام|ام)|"
    r"كل\s*ال(?:حج|أح|اح)جام|كل\s*الاحجام|"
    r"كل\s*ال(?:مقاس|مقاسات|الوان|ألوان)|"
    r"سعر\s*كل|اسعار\s*كل|أسعار\s*كل|all\s*(?:sizes|variants|prices)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_ACK_ONLY_RE = re.compile(
    r"^\s*(?:"
    r"نعم|ايوه|أيوه|ايوة|أيوة|تمام|اوكي|ok|yes|"
    r"👍|🙏|❤|😊|🙂|✅|"
    r"[\u0600-\u06FF]{0,3}"
    r")\s*$",
    re.UNICODE,
)

# Hard topic-shift — invalidates replay / exhaustion / stale clarify memory.
# Patterns use _norm-compatible spellings (ى→ي, ة→ه, …).
_AVAILABILITY_ASK_RE = re.compile(
    r"(?:"
    r"متي\s*(?:يتوفر|يوفر|راح\s*يوفر|تتوفر)|"
    r"هل\s*(?:متوفر|مو\s*جود|موجود)|"
    r"متي\s*(?:راح|بيكون)|"
    r"when\s*(?:available|in\s*stock)|"
    r"out\s*of\s*stock"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_ENTITY_RE = re.compile(
    r"(?:"
    r"عسل(?:\s*(?:سدر|طلح|سمر|ضهيان|السدر|الطلح|السمر))?|"
    r"غذاء\s*ملكات|حبوب\s*لقاح|برو(?:ب|)وليس|سم\s*النحل|"
    r"منتج|sku|"
    r"honey|royal\s*jelly|propolis"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_INQUIRY_ABOUT_PRODUCT_RE = re.compile(
    r"(?:"
    r"استفسار\s*عن|استفسر\s*عن|"
    r"(?:ابغ|ابي|أبغ|أبي|اريد|أريد).{0,20}(?:اعرف|أعرف|استفسر|استفسار)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_INTENT_RE = re.compile(
    r"(?:"
    r"(?:ابغ|ابي|أبغ|أبي|اريد|أريد|بغيت).{0,12}(?:اطلب|أطلب|اشتري|أشتري)|"
    r"(?:اطلب|أطلب|اشتري|أشتري)\s+(?!شي(?:ء)?\s+(?:ل|لل))"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CLARIFY_LOOP_GOALS = frozenset({
    "all_variant_prices",
    "show_all_variants_prices",
    "general_attribute",
})

_PRICE_SIZE_CONTINUATION_RE = re.compile(
    r"(?:"
    r"^كم\s*السعر|^بكم|^سعر|^الحجم|^حجم|^احجام|^أحجام|^الاحجام|"
    r"^كل\s*ال(?:حج|أح|اح)?(?:ج)?(?:ام|ام|ام)|^كل\s*ال(?:حج|أح|اح)جام|"
    r"^طيب\s*و|^وال|^اي\s*حجم|^أي\s*حجم|"
    r"كilo|كيلو|جرام|حجام|مقاس"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class HardTopicShiftVerdict:
    detected: bool
    reason: str = ""
    new_topic: str = ""


def _semantic_product_entity(text: str) -> str:
    """
    Canonical product-family key — ignores cosmetic lexical differences
    (``عسل طبيعي`` vs ``العسل الطبيعي`` → same ``honey_generic``).
    """
    norm = _norm(text or "")
    if not norm:
        return ""
    if re.search(r"عسل\s*سدر|السدر", norm):
        return "honey_sdr"
    if re.search(r"عسل\s*طلح|الطلح", norm):
        return "honey_tlh"
    if re.search(r"عسل\s*سمر|السمر", norm):
        return "honey_smr"
    if re.search(r"عسل\s*ضهيان|الضهيان|الضهيان", norm):
        return "honey_dhyan"
    if re.search(r"غذاء\s*ملكات|ملكات", norm):
        return "royal_jelly"
    if re.search(r"حبوب\s*لقاح|لقاح", norm):
        return "pollen"
    if re.search(r"برو(?:ب|)وليس|عكبر", norm):
        return "propolis"
    if re.search(r"سم\s*النحل", norm):
        return "bee_venom"
    if "عسل" in norm.split() or norm.endswith("عسل") or norm.startswith("عسل"):
        return "honey_generic"
    if re.search(r"\bعسل\b|honey", norm):
        return "honey_generic"
    return ""


def _previous_was_product_context(prev: str, state: Any) -> bool:
    if _semantic_product_entity(prev):
        return True
    if _INQUIRY_ABOUT_PRODUCT_RE.search(prev or "") and _PRODUCT_ENTITY_RE.search(prev or ""):
        return True
    return get_recent_topic(state) in {
        "general_attribute",
        "product_inquiry",
        "product_availability",
    }


def _infer_new_topic(message: str, reason: str) -> str:
    if reason.startswith("non_product_intent:"):
        return reason.split(":", 1)[-1]
    if reason == "availability_ask":
        return "product_availability"
    if reason == "product_inquiry":
        return "product_inquiry"
    if reason == "order_intent":
        return "order_intent"
    if reason == "product_visual":
        return "product_visual"
    if reason == "commerce_topic_change":
        return "product_browse"
    if reason.startswith("semantic_entity_change:"):
        return reason.split(":", 1)[-1]
    try:
        from .solution_seeking import detect_solution_seeking_suppression  # noqa: PLC0415

        hit = detect_solution_seeking_suppression(message or "", skip_recent_topic=True)
        if hit:
            return hit
    except Exception:  # noqa: BLE001
        pass
    entity = _semantic_product_entity(message or "")
    return entity or "commerce_turn"


def _customer_messages(
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    exclude_current: str = "",
) -> List[str]:
    exclude_norm = _norm(exclude_current)
    out: List[str] = []
    for turn in reversed(history or []):
        if str(turn.get("direction") or "") not in {"in", "inbound"}:
            continue
        body = str(turn.get("body") or turn.get("text") or "").strip()
        if not body:
            continue
        if exclude_norm and _norm(body) == exclude_norm:
            continue
        out.append(body)
    return list(reversed(out))


def _is_price_size_continuation(
    message: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Price / size / variant / deictic follow-ups stay in the same thread."""
    norm = _norm(message or "")
    if not norm:
        return False
    prev_msgs = _customer_messages(history, exclude_current=message or "")
    if not prev_msgs:
        return False
    prev_had_price_size = any(
        _PRICE_SIZE_LOOP_RE.search(_norm(m)) or _ALL_VARIANTS_RE.search(_norm(m))
        for m in prev_msgs
    )
    if not prev_had_price_size:
        return False
    if _PRICE_SIZE_LOOP_RE.search(norm) or _ALL_VARIANTS_RE.search(norm):
        return True
    if _PRICE_SIZE_CONTINUATION_RE.search(norm):
        return True
    return False


def _previous_topic_label(state: Any) -> str:
    topic = get_recent_topic(state)
    if topic:
        return topic
    goal = str(getattr(state, "customer_goal", "") or "").strip()
    return goal or "-"


def _had_suppression_memory(state: Any) -> bool:
    if state is None:
        return False
    return bool(
        get_recent_topic(state)
        or str(getattr(state, "last_fallback_fingerprint", "") or "").strip()
        or str(getattr(state, "customer_goal", "") or "").strip() in _CLARIFY_LOOP_GOALS
    )


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or "").strip().lower())
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def fallback_fingerprint(text: str) -> str:
    """Stable short hash for replay detection."""
    norm = _norm(text)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def log_fallback_repeat_blocked(
    *,
    tenant_id: Any = None,
    fingerprint: str = "",
    reason: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[FALLBACK_REPEAT_BLOCKED] tenant=%s fingerprint=%s reason=%s preview=%r",
            tenant_id,
            fingerprint or "-",
            reason or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_hard_topic_shift(
    *,
    tenant_id: Any = None,
    reason: str = "",
    preview: str = "",
    previous_topic: str = "",
    new_topic: str = "",
    suppression_invalidated: bool = False,
) -> None:
    try:
        logger.info(
            "[HARD_TOPIC_SHIFT] tenant=%s previous_topic=%s new_topic=%s "
            "reason=%s suppression_invalidated=%s preview=%r",
            tenant_id,
            previous_topic or "-",
            new_topic or "-",
            reason or "-",
            str(bool(suppression_invalidated)).lower(),
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_workflow_exhausted(
    *,
    tenant_id: Any = None,
    workflow: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[WORKFLOW_EXHAUSTED] tenant=%s workflow=%s preview=%r",
            tenant_id,
            workflow or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def get_recent_topic(state: Any) -> str:
    return str(getattr(state, "recent_topic", "") or "").strip()


def is_recent_topic_active(state: Any, *, current_turn: int = 0) -> bool:
    topic = get_recent_topic(state)
    if not topic:
        return False
    topic_turn = int(getattr(state, "recent_topic_turn", 0) or 0)
    turn = current_turn or int(getattr(state, "turn", 0) or 0)
    return (turn - topic_turn) <= RECENT_TOPIC_TTL_TURNS


def stamp_recent_topic(state: Any, topic: str, *, turn: Optional[int] = None) -> None:
    if not topic or state is None:
        return
    state.recent_topic = str(topic)
    state.recent_topic_turn = int(
        turn if turn is not None else getattr(state, "turn", 0) or 0
    )


def _last_customer_message(
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    exclude_current: str = "",
) -> str:
    msgs = _customer_messages(history, exclude_current=exclude_current)
    return msgs[-1] if msgs else ""


def evaluate_hard_topic_shift(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    state: Any = None,
) -> HardTopicShiftVerdict:
    """
    True when the current turn introduces a fresh commerce entity or intent.

    Suppression / replay memory must NOT carry over across these turns.
    """
    msg = (message or "").strip()
    norm = _norm(msg)
    if not norm:
        return HardTopicShiftVerdict(False)

    if _is_price_size_continuation(msg, history):
        return HardTopicShiftVerdict(False)

    if _AVAILABILITY_ASK_RE.search(norm):
        return HardTopicShiftVerdict(
            True,
            reason="availability_ask",
            new_topic="product_availability",
        )

    try:
        from ..product_discovery_gate import (  # noqa: PLC0415
            extract_types_overview_query,
            has_types_overview_ask,
        )

        if has_types_overview_ask(msg) and extract_types_overview_query(msg):
            return HardTopicShiftVerdict(
                True,
                reason="types_overview_ask",
                new_topic="product_types_overview",
            )
    except Exception:  # noqa: BLE001
        logger.exception("[HARD_TOPIC_SHIFT] types_overview_gate_failed")

    if _INQUIRY_ABOUT_PRODUCT_RE.search(msg) and _PRODUCT_ENTITY_RE.search(msg):
        return HardTopicShiftVerdict(
            True,
            reason="product_inquiry",
            new_topic="product_inquiry",
        )

    if _ORDER_INTENT_RE.search(msg):
        return HardTopicShiftVerdict(
            True,
            reason="order_intent",
            new_topic="order_intent",
        )

    try:
        from ..commerce.product_visual import is_product_visual_request  # noqa: PLC0415

        if is_product_visual_request(msg):
            return HardTopicShiftVerdict(
                True,
                reason="product_visual",
                new_topic="product_visual",
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        from ..order_context_gate import has_explicit_commerce_topic_change  # noqa: PLC0415

        if has_explicit_commerce_topic_change(msg):
            return HardTopicShiftVerdict(
                True,
                reason="commerce_topic_change",
                new_topic="product_browse",
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        from .solution_seeking import detect_solution_seeking_suppression  # noqa: PLC0415

        current_non_product = detect_solution_seeking_suppression(
            msg, skip_recent_topic=True,
        )
        if current_non_product and not is_ack_only_message(msg):
            prev = _last_customer_message(history, exclude_current=msg)
            if _previous_was_product_context(prev, state):
                return HardTopicShiftVerdict(
                    True,
                    reason=f"non_product_intent:{current_non_product}",
                    new_topic=current_non_product,
                )
    except Exception:  # noqa: BLE001
        pass

    current_entity = _semantic_product_entity(msg)
    if current_entity:
        prev = _last_customer_message(history, exclude_current=msg)
        if prev:
            prev_entity = _semantic_product_entity(prev)
            if (
                prev_entity
                and current_entity
                and prev_entity != current_entity
                and not (
                    prev_entity == "honey_generic"
                    and current_entity == "honey_generic"
                )
            ):
                return HardTopicShiftVerdict(
                    True,
                    reason=f"semantic_entity_change:{prev_entity}->{current_entity}",
                    new_topic=current_entity,
                )

    return HardTopicShiftVerdict(False)


def detect_hard_topic_shift(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    state: Any = None,
) -> bool:
    return evaluate_hard_topic_shift(
        message, history=history, state=state,
    ).detected


def _product_entity_signature(text: str) -> frozenset[str]:
    """Backward-compatible alias — prefer ``_semantic_product_entity``."""
    key = _semantic_product_entity(text)
    return frozenset({key}) if key else frozenset()


def invalidate_suppression_memory(
    state: Any,
    *,
    reason: str = "",
    tenant_id: Any = None,
    preview: str = "",
    history: Optional[List[Dict[str, Any]]] = None,
    verdict: Optional[HardTopicShiftVerdict] = None,
) -> None:
    """Clear replay / topic / clarify-loop memory after a hard topic shift."""
    if state is None:
        return
    previous_topic = _previous_topic_label(state)
    had_memory = _had_suppression_memory(state)
    _verdict = verdict or evaluate_hard_topic_shift(
        preview or "", history=history, state=state,
    )
    new_topic = _verdict.new_topic or _infer_new_topic(preview or "", _verdict.reason or reason)
    shift_reason = reason or _verdict.reason or "invalidate"

    state.recent_topic = ""
    state.recent_topic_turn = 0
    state.last_fallback_fingerprint = ""
    state.last_fallback_turn = 0
    goal = str(getattr(state, "customer_goal", "") or "")
    if goal in _CLARIFY_LOOP_GOALS:
        state.customer_goal = ""

    log_hard_topic_shift(
        tenant_id=tenant_id,
        reason=shift_reason,
        preview=preview,
        previous_topic=previous_topic,
        new_topic=new_topic,
        suppression_invalidated=had_memory,
    )


def infer_topic_from_messages(
    message: str,
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    include_history: bool = True,
) -> Optional[str]:
    """Infer delivery/payment/support topic from current + recent customer text."""
    try:
        from .solution_seeking import detect_solution_seeking_suppression  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    chunks: List[str] = []
    if (message or "").strip():
        chunks.append(message.strip())
    if include_history:
        for turn in reversed(history or []):
            if str(turn.get("direction") or "") not in {"in", "inbound"}:
                continue
            body = str(turn.get("body") or turn.get("text") or "").strip()
            if body:
                chunks.append(body)
            if len(chunks) >= 4:
                break
    for chunk in chunks:
        reason = detect_solution_seeking_suppression(chunk, skip_recent_topic=True)
        if reason:
            return reason
    return None


def resolve_active_topic(
    message: str,
    state: Any,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Multi-turn topic: current message OR recent_topic window OR history."""
    try:
        from .solution_seeking import detect_solution_seeking_suppression  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    if evaluate_hard_topic_shift(message, history=history, state=state).detected:
        return detect_solution_seeking_suppression(
            message or "", skip_recent_topic=True,
        )

    direct = detect_solution_seeking_suppression(
        message or "", skip_recent_topic=True,
    )
    if direct:
        return direct
    if is_recent_topic_active(state):
        return get_recent_topic(state)
    return infer_topic_from_messages(message, history)


def should_block_fallback_repeat(
    state: Any,
    text: str,
    *,
    explicit_reask: bool = False,
    current_turn: Optional[int] = None,
    message: str = "",
    history: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Do not replay the same fallback within a short conversational window."""
    if explicit_reask:
        return False
    if detect_hard_topic_shift(message or "", history=history, state=state):
        return False
    fp = fallback_fingerprint(text)
    if not fp:
        return False
    last_fp = str(getattr(state, "last_fallback_fingerprint", "") or "")
    if fp != last_fp:
        return False
    turn = int(current_turn if current_turn is not None else getattr(state, "turn", 0) or 0)
    last_turn = int(getattr(state, "last_fallback_turn", 0) or 0)
    return (turn - last_turn) <= FALLBACK_COOLDOWN_TURNS


def record_fallback_sent(
    state: Any,
    text: str,
    *,
    turn: Optional[int] = None,
) -> None:
    if state is None:
        return
    fp = fallback_fingerprint(text)
    if not fp:
        return
    state.last_fallback_fingerprint = fp
    state.last_fallback_turn = int(
        turn if turn is not None else getattr(state, "turn", 0) or 0
    )


def is_ack_only_message(message: str) -> bool:
    return bool(_ACK_ONLY_RE.match((message or "").strip()))


def detect_semantic_dead_end(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    state: Any = None,
    previous_goal: str = "",
) -> Optional[str]:
    """
    Infer persisted customer_goal when clarify loops fail.

    Returns goal token e.g. ``all_variant_prices`` or ``None``.
    """
    if detect_hard_topic_shift(message, history=history, state=state):
        return None

    if is_ack_only_message(message):
        if is_recent_topic_active(state):
            return None
        return None

    customer_msgs: List[str] = []
    for turn in (history or [])[-8:]:
        if str(turn.get("direction") or "") not in {"in", "inbound"}:
            continue
        body = str(turn.get("body") or turn.get("text") or "").strip()
        if body:
            customer_msgs.append(body)
    if (message or "").strip():
        customer_msgs.append(message.strip())

    norm_current = _norm(message or "")
    if _ALL_VARIANTS_RE.search(norm_current):
        if any(
            _PRICE_SIZE_LOOP_RE.search(_norm(m))
            for m in customer_msgs[:-1]
        ):
            log_workflow_exhausted(workflow="clarify_all_variants_after_price")
            return "all_variant_prices"

    price_size_hits = sum(
        1 for m in customer_msgs[-5:]
        if _PRICE_SIZE_LOOP_RE.search(_norm(m))
    )
    if price_size_hits >= CLARIFY_LOOP_THRESHOLD:
        for m in reversed(customer_msgs):
            if _ALL_VARIANTS_RE.search(_norm(m)):
                log_workflow_exhausted(workflow="clarify_loop_all_variants")
                return "all_variant_prices"
        log_workflow_exhausted(workflow="clarify_loop_price_size")
        return "all_variant_prices"

    if previous_goal in {"all_variant_prices", "show_all_variants_prices"}:
        if _ALL_VARIANTS_RE.search(_norm(message or "")):
            return "all_variant_prices"

    return None


__all__ = [
    "HardTopicShiftVerdict",
    "detect_hard_topic_shift",
    "detect_semantic_dead_end",
    "evaluate_hard_topic_shift",
    "fallback_fingerprint",
    "get_recent_topic",
    "infer_topic_from_messages",
    "invalidate_suppression_memory",
    "is_ack_only_message",
    "is_recent_topic_active",
    "log_fallback_repeat_blocked",
    "log_hard_topic_shift",
    "log_workflow_exhausted",
    "record_fallback_sent",
    "resolve_active_topic",
    "should_block_fallback_repeat",
    "stamp_recent_topic",
]
