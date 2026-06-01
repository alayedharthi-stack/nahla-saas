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
from typing import Any, Dict, List, Optional

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
) -> bool:
    """Do not replay the same fallback within a short conversational window."""
    if explicit_reask:
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
    "detect_semantic_dead_end",
    "fallback_fingerprint",
    "get_recent_topic",
    "infer_topic_from_messages",
    "is_ack_only_message",
    "is_recent_topic_active",
    "log_fallback_repeat_blocked",
    "log_workflow_exhausted",
    "record_fallback_sent",
    "resolve_active_topic",
    "should_block_fallback_repeat",
    "stamp_recent_topic",
]
