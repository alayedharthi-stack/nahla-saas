"""
operational_reply_validator.py
──────────────────────────────
Post-compose validator for constrained operational replies.

Guards enforce operational truth — they validate and fail closed to
legacy copy; they must not become the primary copy author long-term.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

from core.reply_instruction import (
    CONSTRAINT_NO_PAYMENT_CONFIRM,
    CONSTRAINT_NO_SHIPPING_PROMISE,
    FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
    PATH_ADDRESS_INGEST_ACK,
    PATH_PAYMENT_RECEIPT_ACK,
    ReplyInstruction,
)

_DIA = re.compile(r"[\u064B-\u065F\u0670]")
_WS = re.compile(r"\s+")

_RECEIPT_UNGROUNDED_PRODUCT_RE = re.compile(
    r"(?:"
    r"ل(?:ـ|ل)\s*\d+|"
    r"لطلب\s|"
    r"استلمت(?:\s+\w+){0,4}\s+ل(?:ـ|ل)?\s*\S"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _DIA.sub("", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return _WS.sub(" ", t).strip().lower()


_SHIP_PROMISE_MARKERS = (
    "تم الشحن",
    "شحناه",
    "في الطريق",
    "سيتم التوصيل",
    "بيوصلك",
)

_ADDRESS_ASK_MARKERS = (
    "عنوان التوصيل",
    "عنوان البيت",
    "احتاج منك عنوان",
    "أحتاج منك عنوان",
    "شاركنا عنوان",
    "رابط قوقل ماب",
    "العنوان الوطني",
)


@dataclass(frozen=True)
class OperationalReplyValidation:
    ok: bool
    reason: str = ""


def validate_operational_reply(
    reply: str,
    instruction: ReplyInstruction,
) -> OperationalReplyValidation:
    """Return ok=False when reply violates instruction constraints."""
    text = (reply or "").strip()
    if not text:
        return OperationalReplyValidation(ok=False, reason="empty_reply")
    if len(text) > 800:
        return OperationalReplyValidation(ok=False, reason="reply_too_long")

    norm = _norm(text)
    constraints = set(instruction.constraints or ())

    if CONSTRAINT_NO_PAYMENT_CONFIRM in constraints:
        markers = instruction.forbidden_claims or FORBIDDEN_PAYMENT_CONFIRM_MARKERS
        for marker in markers:
            if _norm(marker) in norm:
                return OperationalReplyValidation(
                    ok=False,
                    reason=f"forbidden_payment_marker:{marker[:40]}",
                )

    if CONSTRAINT_NO_SHIPPING_PROMISE in constraints:
        for marker in _SHIP_PROMISE_MARKERS:
            if _norm(marker) in norm:
                return OperationalReplyValidation(
                    ok=False,
                    reason=f"forbidden_ship_promise:{marker}",
                )

    if instruction.path == PATH_ADDRESS_INGEST_ACK:
        facts = instruction.facts or {}
        payment_committed = bool(
            facts.get("payment_state_committed")
            or facts.get("payment_evidence_received")
            or facts.get("payment_receipt_received")
        )
        if facts.get("address_ack_scope") == "delivery_only" and not payment_committed:
            paymentish = any(
                token in norm
                for token in ("تحويل", "حواله", "حوالة", "دفع", "ايصال", "إيصال")
            )
            reviewish = any(
                token in norm
                for token in ("مراجع", "تأكيد الدفع", "تاكيد الدفع", "تم استلام الايصال")
            )
            if paymentish and reviewish:
                return OperationalReplyValidation(
                    ok=False,
                    reason="address_ack_uncommitted_payment_claim",
                )

    if instruction.path == PATH_PAYMENT_RECEIPT_ACK:
        facts = instruction.facts or {}
        if facts.get("needs_order_linking_or_review") or facts.get("needs_merchant_amount_review"):
            for marker in _ADDRESS_ASK_MARKERS:
                if _norm(marker) in norm:
                    return OperationalReplyValidation(
                        ok=False,
                        reason="receipt_address_without_order_evidence",
                    )
            product_fact = str(facts.get("selected_product") or "").strip()
            if product_fact and _norm(product_fact) in norm:
                return OperationalReplyValidation(
                    ok=False,
                    reason="receipt_product_without_order_evidence",
                )
            if facts.get("needs_order_linking_or_review") and _RECEIPT_UNGROUNDED_PRODUCT_RE.search(
                text
            ):
                return OperationalReplyValidation(
                    ok=False,
                    reason="receipt_product_without_order_evidence",
                )

    return OperationalReplyValidation(ok=True)


__all__ = [
    "OperationalReplyValidation",
    "validate_operational_reply",
]
