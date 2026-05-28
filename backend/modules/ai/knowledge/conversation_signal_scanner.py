"""
Lightweight conversation signal extraction for KB Gap Intelligence v1.

Scans recent inbound customer messages (no LLM) and returns aggregate
counts the improvement advisor uses to ground suggestions in real
merchant traffic.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("nahla.ai.knowledge.conversation_signals")

WINDOW_DAYS = 7
MAX_CONVERSATIONS = 200
MAX_MESSAGES = 2000

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_PAYMENT_TERMS: tuple[str, ...] = (
    "تحويل", "دفع", "ادفع", "أدفع", "باركود", "بار كود", "qr",
    "ابل باي", "apple pay", "مدى", "mada", "visa", "فيزا",
    "راجحي", "الأهلي", "الاهلي", "iban", "آيبان", "ايبان",
    "حساب بنك", "حساب الراجحي", "تابي", "tamara", "تمارا",
)

_SHIPPING_TERMS: tuple[str, ...] = (
    "شحن", "توصيل", "متى يوصل", "متى توصل", "كم يوم", "مدة التوصيل",
    "سمسا", "smsa", "aramex", "ارامكس", "dhl", "الشحنة",
)

_LOCATION_TERMS: tuple[str, ...] = (
    "وين", "أين", "اين", "موقع", "فرع", "محل", "معرض", "لوكيشن",
    "location", "خرايط", "خريطة", "google maps",
)

_COMPARE_TERMS: tuple[str, ...] = (
    "الفرق", "فرق بين", "أيهما", "ايهما", "أفضل", "افضل",
    "مقارنة", "وش الفرق", "ايش الفرق",
)

_HESITANT_TERMS: tuple[str, ...] = (
    "مو متأكد", "ما ادري", "ما أدري", "تردد", "محتار", "maybe",
)

_PRICE_QUESTION_RE = re.compile(
    r"(?:كم\s+سعر|سعر|بكم|بكام|price|\?|؟)",
    re.IGNORECASE | re.UNICODE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = _NORM_RE.sub("", str(text).lower())
    return _WS_RE.sub(" ", t).strip()


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    return any(n in text for n in needles)


def _count_matching(messages: Sequence[str], needles: Sequence[str]) -> int:
    return sum(1 for m in messages if _contains_any(m, needles))


@dataclass
class ConversationSignalSummary:
    """Aggregate signals from recent inbound traffic."""

    window_days: int = WINDOW_DAYS
    scanned_messages: int = 0
    scanned_conversations: int = 0
    payment_questions: int = 0
    shipping_questions: int = 0
    location_questions: int = 0
    human_handoff_count: int = 0
    human_handoff_after_payment: int = 0
    price_confusion_detected: bool = False
    product_compare_questions: int = 0
    hesitant_messages: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "scanned_messages": self.scanned_messages,
            "scanned_conversations": self.scanned_conversations,
            "payment_questions": self.payment_questions,
            "shipping_questions": self.shipping_questions,
            "location_questions": self.location_questions,
            "human_handoff_count": self.human_handoff_count,
            "human_handoff_after_payment": self.human_handoff_after_payment,
            "price_confusion_detected": self.price_confusion_detected,
            "product_compare_questions": self.product_compare_questions,
            "hesitant_messages": self.hesitant_messages,
        }


def _summarize_inbound_messages(
    rows: Sequence[Tuple[Any, Any]],
    *,
    handoff_conv_ids: Optional[Set[int]] = None,
    window_days: int = WINDOW_DAYS,
) -> ConversationSignalSummary:
    """Pure helper — aggregate signals from inbound (body, conversation_id) rows."""
    summary = ConversationSignalSummary(window_days=window_days)
    handoff_ids = handoff_conv_ids or set()

    bodies: List[str] = []
    payment_by_conv: dict[int, int] = {}
    conv_ids: Set[int] = set()
    for body, conv_id in rows:
        norm = _norm(body or "")
        if not norm:
            continue
        bodies.append(norm)
        cid = int(conv_id) if conv_id is not None else 0
        if cid:
            conv_ids.add(cid)
        if cid and _contains_any(norm, _PAYMENT_TERMS):
            payment_by_conv[cid] = payment_by_conv.get(cid, 0) + 1

    summary.scanned_conversations = len(conv_ids)
    summary.scanned_messages = len(bodies)
    if not bodies:
        return summary

    summary.payment_questions = _count_matching(bodies, _PAYMENT_TERMS)
    summary.shipping_questions = _count_matching(bodies, _SHIPPING_TERMS)
    summary.location_questions = _count_matching(bodies, _LOCATION_TERMS)
    summary.product_compare_questions = _count_matching(bodies, _COMPARE_TERMS)
    summary.hesitant_messages = _count_matching(bodies, _HESITANT_TERMS)
    summary.price_confusion_detected = any(
        _contains_any(b, _PAYMENT_TERMS + ("سعر", "بكم", "بكام"))
        and _PRICE_QUESTION_RE.search(b)
        for b in bodies
    )
    summary.human_handoff_count = len(handoff_ids)
    summary.human_handoff_after_payment = sum(
        1 for cid in handoff_ids if payment_by_conv.get(cid, 0) > 0
    )
    return summary


def _rollback_db(db: Any) -> None:
    """Clear a poisoned transaction so the caller's session stays usable."""
    if db is None:
        return
    try:
        rollback = getattr(db, "rollback", None)
        if callable(rollback):
            rollback()
    except Exception:  # noqa: BLE001
        pass


def _fetch_recent_inbound_conversation_ids(
    db: Any,
    *,
    tenant_id: int,
    cutoff: datetime,
    max_conversations: int,
) -> Set[int]:
    """Return conversation ids with recent inbound traffic (newest first).

    Uses ``GROUP BY conversation_id`` + ``MAX(created_at)`` so PostgreSQL
    accepts the ``ORDER BY`` (unlike ``SELECT DISTINCT … ORDER BY created_at``
    when ``created_at`` is not in the select list).
    """
    from sqlalchemy import func  # noqa: PLC0415
    from models import MessageEvent  # noqa: PLC0415

    rows = (
        db.query(
            MessageEvent.conversation_id,
            func.max(MessageEvent.created_at).label("last_inbound_at"),
        )
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.direction == "inbound",
            MessageEvent.conversation_id.isnot(None),
            MessageEvent.created_at >= cutoff,
        )
        .group_by(MessageEvent.conversation_id)
        .order_by(func.max(MessageEvent.created_at).desc())
        .limit(max(1, int(max_conversations)))
        .all()
    )
    return {
        int(r[0]) for r in rows if r and r[0] is not None
    }


def scan_tenant_conversation_signals(
    db: Any,
    tenant_id: int,
    *,
    window_days: int = WINDOW_DAYS,
    max_conversations: int = MAX_CONVERSATIONS,
    max_messages: int = MAX_MESSAGES,
) -> ConversationSignalSummary:
    """Scan recent inbound messages for lightweight commerce signals.

    Never raises — returns an empty summary on DB errors so the
    improvement endpoint stays available.
    """
    summary = ConversationSignalSummary(window_days=window_days)
    try:
        from models import Conversation, MessageEvent  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB_GAP_SIGNALS] models import failed: %s", exc)
        return summary

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(window_days))

        conv_ids = _fetch_recent_inbound_conversation_ids(
            db,
            tenant_id=tenant_id,
            cutoff=cutoff,
            max_conversations=max_conversations,
        )
        summary.scanned_conversations = len(conv_ids)

        if not conv_ids:
            return summary

        rows = (
            db.query(MessageEvent.body, MessageEvent.conversation_id)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.direction == "inbound",
                MessageEvent.conversation_id.in_(conv_ids),
                MessageEvent.created_at >= cutoff,
            )
            .order_by(MessageEvent.id.desc())
            .limit(max(1, int(max_messages)))
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[KB_GAP_SIGNALS] scan failed tenant=%s err=%s", tenant_id, exc,
        )
        _rollback_db(db)
        return summary

    handoff_ids: Set[int] = set()
    try:
        handoff_rows = (
            db.query(Conversation.id)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.id.in_(conv_ids),
            )
            .filter(
                (Conversation.needs_human.is_(True))
                | (Conversation.is_human_handoff.is_(True))
                | (Conversation.handoff_active.is_(True))
            )
            .all()
        )
        handoff_ids = {int(r[0]) for r in handoff_rows if r and r[0]}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[KB_GAP_SIGNALS] handoff scan failed tenant=%s err=%s",
            tenant_id, exc,
        )
        _rollback_db(db)

    summary = _summarize_inbound_messages(
        rows,
        handoff_conv_ids=handoff_ids,
        window_days=window_days,
    )

    logger.info(
        "[KB_GAP_SIGNALS] tenant=%s convs=%d msgs=%d payment=%d shipping=%d "
        "location=%d handoff=%d",
        tenant_id,
        summary.scanned_conversations,
        summary.scanned_messages,
        summary.payment_questions,
        summary.shipping_questions,
        summary.location_questions,
        summary.human_handoff_count,
    )
    return summary
