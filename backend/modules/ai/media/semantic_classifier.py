"""
media/semantic_classifier.py
────────────────────────────
Semantic media classification BEFORE payment / attachment acknowledgments.

Transport-aware acks (``وصلني الملف 👍`` + payment continuation) must not fire
until semantic analysis positively confirms payment-related content.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.media.semantic")

MEDIA_PAYMENT_RECEIPT = "payment_receipt"
MEDIA_INVOICE = "invoice"
MEDIA_PRODUCT_IMAGE = "product_image"
MEDIA_SOCIAL_IMAGE = "social_image"
MEDIA_RELIGIOUS_SOCIAL = "religious_social_forward"
MEDIA_MAP_LOCATION = "map_location"
MEDIA_DOCUMENT = "document"
MEDIA_SCREENSHOT = "screenshot"
MEDIA_UNRELATED = "unrelated_media"
MEDIA_UNKNOWN = "unknown_media"

ACK_NEUTRAL = "neutral"
ACK_PAYMENT = "payment"
ACK_SOCIAL = "social"
ACK_PRODUCT = "product"

_PAYMENT_SEMANTIC = frozenset({MEDIA_PAYMENT_RECEIPT, MEDIA_INVOICE})
_NON_PAYMENT_SEMANTIC = frozenset({
    MEDIA_SOCIAL_IMAGE,
    MEDIA_RELIGIOUS_SOCIAL,
    MEDIA_PRODUCT_IMAGE,
    MEDIA_MAP_LOCATION,
    MEDIA_UNRELATED,
    MEDIA_UNKNOWN,
    MEDIA_DOCUMENT,
    MEDIA_SCREENSHOT,
})

_PRODUCT_HINTS = (
    "product", "منتج", "عسل", "honey", "item", "sku", "price", "سعر",
    "catalog", "package", "packaging", "bottle", "jar", "علبة", "عبوة",
)
_SOCIAL_HINTS = (
    "greeting", "eid", "عيد", "تهن", "دعاء", "اللهم", "forwarded",
    "congrat", "celebration", "poster", "flyer", "invitation",
)

# Slots produced by ``core.payment_evidence`` + normalizer Stage 2.
# Semantic may gate acks but must not erase these deterministic verdicts.
_PAYMENT_EVIDENCE_KINDS = frozenset({
    "payment_pre_review",
    "payment_pending_evidence",
    "payment_receipt",
})
_PAYMENT_EVIDENCE_STATUSES = frozenset({
    "confirmed",
    "pre_transfer_review",
    "needs_confirmation",
})


def _has_payment_evidence_kind_slots(
    *,
    pdf_kind: Optional[str] = None,
    image_kind: Optional[str] = None,
) -> bool:
    pk = str(pdf_kind or "").strip()
    ik = str(image_kind or "").strip()
    return pk in _PAYMENT_EVIDENCE_KINDS or ik in _PAYMENT_EVIDENCE_KINDS


def _has_deterministic_payment_evidence(
    *,
    payment_evidence_status: Optional[str] = None,
    pdf_kind: Optional[str] = None,
    image_kind: Optional[str] = None,
) -> bool:
    pe = str(payment_evidence_status or "").strip()
    if pe in _PAYMENT_EVIDENCE_STATUSES:
        return True
    return _has_payment_evidence_kind_slots(
        pdf_kind=pdf_kind,
        image_kind=image_kind,
    )


@dataclass(frozen=True)
class MediaSemanticResult:
    category: str
    ack_mode: str
    confidence: str
    reason: str

    def to_metadata(self) -> Dict[str, str]:
        return {
            "media_semantic_category": self.category,
            "attachment_ack_mode": self.ack_mode,
            "media_semantic_confidence": self.confidence,
            "media_semantic_reason": self.reason,
        }


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    return re.sub(r"\s+", " ", t)


def log_media_classification(
    *,
    tenant_id: Any,
    category: str,
    ack_mode: str,
    reason: str = "",
    normalized_type: str = "",
) -> None:
    try:
        logger.info(
            "[MEDIA_CLASSIFICATION] tenant=%s category=%s ack_mode=%s "
            "type=%s reason=%s",
            tenant_id,
            category,
            ack_mode,
            normalized_type or "-",
            reason or "-",
        )
    except Exception:  # noqa: BLE001
        pass


def log_payment_media_confirmed(*, tenant_id: Any, source: str = "") -> None:
    logger.info(
        "[PAYMENT_MEDIA_CONFIRMED] tenant=%s source=%s",
        tenant_id,
        source or "-",
    )


def log_payment_media_rejected(*, tenant_id: Any, reason: str, category: str = "") -> None:
    logger.info(
        "[PAYMENT_MEDIA_REJECTED] tenant=%s reason=%s category=%s",
        tenant_id,
        reason,
        category or "-",
    )


def log_attachment_ack_mode(*, tenant_id: Any, mode: str, category: str = "") -> None:
    logger.info(
        "[ATTACHMENT_ACK_MODE] tenant=%s mode=%s category=%s",
        tenant_id,
        mode,
        category or "-",
    )


def compose_neutral_attachment_ack(normalized_type: str) -> str:
    t = (normalized_type or "").strip().lower()
    if t in {"document", "pdf"}:
        return "تم استلام الملف 👍"
    if t in {"audio", "voice"}:
        return "وصلني التسجيل 👍"
    if t in {"video"}:
        return "وصلني الفيديو 👍"
    return "وصلتني الصورة 👍"


def classify_media_semantic(
    *,
    text_blob: str = "",
    caption: str = "",
    filename: str = "",
    normalized_type: str = "",
    non_commerce_category: Optional[str] = None,
    payment_evidence_status: Optional[str] = None,
    pdf_kind: Optional[str] = None,
    image_kind: Optional[str] = None,
) -> MediaSemanticResult:
    """Classify inbound media semantically for ack / routing policy."""
    blob = _norm(" ".join(filter(None, [caption, filename, text_blob])))

    if non_commerce_category in {"religious_media", "eid_greeting", "dua", "condolence"}:
        return MediaSemanticResult(
            category=MEDIA_RELIGIOUS_SOCIAL,
            ack_mode=ACK_SOCIAL,
            confidence="high",
            reason="non_commerce_classifier",
        )
    if non_commerce_category:
        return MediaSemanticResult(
            category=MEDIA_SOCIAL_IMAGE,
            ack_mode=ACK_SOCIAL,
            confidence="high",
            reason=f"non_commerce_{non_commerce_category}",
        )

    if image_kind == "map_screenshot" or "map_screenshot" in str(image_kind or ""):
        return MediaSemanticResult(
            category=MEDIA_MAP_LOCATION,
            ack_mode=ACK_NEUTRAL,
            confidence="high",
            reason="map_image_kind",
        )

    if pdf_kind == "invoice" or (blob and "فاتور" in blob):
        return MediaSemanticResult(
            category=MEDIA_INVOICE,
            ack_mode=ACK_NEUTRAL,
            confidence="medium",
            reason="invoice_kind",
        )

    if (
        payment_evidence_status == "confirmed"
        or image_kind == "payment_receipt"
        or pdf_kind == "payment_receipt"
    ):
        return MediaSemanticResult(
            category=MEDIA_PAYMENT_RECEIPT,
            ack_mode=ACK_PAYMENT,
            confidence="high",
            reason="payment_evidence_confirmed",
        )

    if any(h in blob for h in _SOCIAL_HINTS):
        return MediaSemanticResult(
            category=MEDIA_SOCIAL_IMAGE,
            ack_mode=ACK_SOCIAL,
            confidence="medium",
            reason="social_hint_in_text",
        )

    if any(h in blob for h in _PRODUCT_HINTS):
        return MediaSemanticResult(
            category=MEDIA_PRODUCT_IMAGE,
            ack_mode=ACK_PRODUCT,
            confidence="medium",
            reason="product_hint_in_text",
        )

    if normalized_type in {"document", "pdf"} and blob:
        if not _has_deterministic_payment_evidence(
            payment_evidence_status=payment_evidence_status,
            pdf_kind=pdf_kind,
            image_kind=image_kind,
        ):
            return MediaSemanticResult(
                category=MEDIA_DOCUMENT,
                ack_mode=ACK_NEUTRAL,
                confidence="low",
                reason="generic_document",
            )

    if payment_evidence_status in {"needs_confirmation", "pre_transfer_review"}:
        return MediaSemanticResult(
            category=MEDIA_UNRELATED,
            ack_mode=ACK_NEUTRAL,
            confidence="medium",
            reason="weak_payment_evidence_downgraded",
        )

    if blob:
        return MediaSemanticResult(
            category=MEDIA_UNKNOWN,
            ack_mode=ACK_NEUTRAL,
            confidence="low",
            reason="text_present_unclassified",
        )

    return MediaSemanticResult(
        category=MEDIA_UNKNOWN,
        ack_mode=ACK_NEUTRAL,
        confidence="low",
        reason="empty_semantic_signal",
    )


def allows_payment_media_ack(
    *,
    semantic_category: str,
    payment_evidence_status: Optional[str],
    awaiting_payment_receipt: bool = False,
    has_active_order: bool = False,
) -> bool:
    """True only when semantic + payment context justify payment ack copy."""
    cat = str(semantic_category or "").strip()
    pe = str(payment_evidence_status or "").strip()

    if cat in _NON_PAYMENT_SEMANTIC:
        return False

    if not cat:
        if pe == "confirmed":
            return has_active_order or awaiting_payment_receipt
        if pe in {"needs_confirmation", "pre_transfer_review"}:
            return bool(awaiting_payment_receipt and has_active_order)
        return False

    if cat not in _PAYMENT_SEMANTIC and pe != "confirmed":
        return False
    if pe == "confirmed":
        return cat in _PAYMENT_SEMANTIC or (has_active_order and awaiting_payment_receipt)
    if pe in {"needs_confirmation", "pre_transfer_review"}:
        return bool(
            awaiting_payment_receipt
            and has_active_order
            and cat in _PAYMENT_SEMANTIC.union({MEDIA_SCREENSHOT, MEDIA_UNKNOWN})
        )
    return False


def metadata_has_payment_evidence_kind_slots(metadata: Dict[str, Any]) -> bool:
    """True when normalizer already stamped a payment-evidence kind slot."""
    return _has_payment_evidence_kind_slots(
        pdf_kind=(metadata or {}).get("pdf_kind"),
        image_kind=(metadata or {}).get("image_kind"),
    )


def apply_semantic_payment_override(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Downgrade payment-evidence slots when semantic says non-payment."""
    md = dict(metadata or {})
    sem_cat = str(md.get("media_semantic_category") or "")
    pe = str(md.get("payment_evidence_status") or "")

    if sem_cat in _NON_PAYMENT_SEMANTIC and pe in _PAYMENT_EVIDENCE_STATUSES:
        if _has_payment_evidence_kind_slots(
            pdf_kind=md.get("pdf_kind"),
            image_kind=md.get("image_kind"),
        ):
            return md
        md["payment_evidence_status"] = "not_payment"
        md["payment_evidence_reason"] = f"semantic_rejected_{sem_cat}"
        for key in ("image_kind", "pdf_kind"):
            kind = str(md.get(key) or "")
            if kind in {
                "payment_pending_evidence",
                "payment_pre_review",
                "payment_receipt",
            }:
                md.pop(key, None)
    return md


__all__ = [
    "ACK_NEUTRAL",
    "ACK_PAYMENT",
    "ACK_PRODUCT",
    "ACK_SOCIAL",
    "MEDIA_INVOICE",
    "MEDIA_MAP_LOCATION",
    "MEDIA_PAYMENT_RECEIPT",
    "MEDIA_PRODUCT_IMAGE",
    "MEDIA_RELIGIOUS_SOCIAL",
    "MEDIA_SOCIAL_IMAGE",
    "MEDIA_UNRELATED",
    "MEDIA_UNKNOWN",
    "MediaSemanticResult",
    "allows_payment_media_ack",
    "apply_semantic_payment_override",
    "classify_media_semantic",
    "compose_neutral_attachment_ack",
    "log_attachment_ack_mode",
    "log_media_classification",
    "log_payment_media_confirmed",
    "log_payment_media_rejected",
    "metadata_has_payment_evidence_kind_slots",
]
