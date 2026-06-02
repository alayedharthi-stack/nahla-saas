"""
Customer-origin vs evidence text split for outbound payment intent.

Invariant
─────────
Outbound payment artifacts (barcode, IBAN, transfer instructions, payment
media) require *customer-authored* intent — never OCR/vision vocabulary
alone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.brain.customer_origin_intent")

_MEDIA_INBOUND_TYPES = frozenset({"image", "document", "pdf", "video"})
_TEXT_INBOUND_TYPES = frozenset({"text", "interactive", "catalog_order"})

_VISION_FRAME_RE = re.compile(
    r"^\[(?:"
    r"وصف\s*الصورة(?:\s*المرسلة)?|"
    r"وصف\s*الفيديو|"
    r"تصنيف\s*الصورة[^\]]*"
    r")\]\s*",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class InboundTextSplit:
    customer_origin: str
    evidence: str
    merged: str
    normalized_type: str
    customer_origin_source: str = ""  # text | caption | empty
    evidence_source: str = ""         # vision | ocr | pdf | mixed | empty


def _norm_type(normalized_type: Optional[str], meta: Dict[str, Any]) -> str:
    n = str(normalized_type or meta.get("normalized_type") or meta.get("source_type") or "").strip().lower()
    if n == "document" and str(meta.get("mime_type") or "").lower().endswith("pdf"):
        return "pdf"
    return n


def _strip_leading_media_frames(text: str) -> str:
    s = (text or "").strip()
    while s:
        nxt = _VISION_FRAME_RE.sub("", s).strip()
        if nxt == s:
            break
        s = nxt
    return s


def extract_evidence_text(inbound_metadata: Optional[dict] = None) -> Tuple[str, str]:
    """Return ``(evidence_blob, evidence_source)`` from normalizer metadata."""
    meta = inbound_metadata or {}
    vision_parts: List[str] = []
    ocr_parts: List[str] = []
    pdf_parts: List[str] = []

    for key in ("vision_text", "frame_vision_text"):
        v = str(meta.get(key) or "").strip()
        if v:
            vision_parts.append(v)

    for key in ("ocr_text",):
        v = str(meta.get(key) or "").strip()
        if v:
            ocr_parts.append(v)

    for key in ("pdf_text_preview", "pdf_full_text", "pdf_text"):
        v = str(meta.get(key) or "").strip()
        if v:
            pdf_parts.append(v)

    if vision_parts:
        return "\n".join(vision_parts), "vision" if not (ocr_parts or pdf_parts) else "mixed"
    if ocr_parts:
        return "\n".join(ocr_parts), "ocr"
    if pdf_parts:
        return "\n".join(pdf_parts), "pdf"
    return "", "empty"


def extract_customer_origin_text(
    message: str = "",
    *,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(customer_origin_text, source)`` — caption or text body only."""
    meta = inbound_metadata or {}
    ntype = _norm_type(normalized_type, meta)
    caption = str(meta.get("caption") or "").strip()

    if ntype in _MEDIA_INBOUND_TYPES:
        if caption:
            return caption, "caption"
        return "", "empty"

    if ntype in _TEXT_INBOUND_TYPES or not ntype:
        body = _strip_leading_media_frames((message or "").strip())
        if body:
            return body, "text"
        if caption:
            return caption, "caption"
        return "", "empty"

    # Unknown / legacy — treat non-framed body as customer when no evidence fields.
    evidence, _ = extract_evidence_text(meta)
    if not evidence:
        body = _strip_leading_media_frames((message or "").strip())
        return body, "text" if body else "empty"
    if caption:
        return caption, "caption"
    return "", "empty"


def split_inbound_text(
    message: str = "",
    *,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
) -> InboundTextSplit:
    """Split webhook ``text`` into customer-origin vs evidence channels."""
    meta = inbound_metadata or {}
    ntype = _norm_type(normalized_type, meta)
    customer_origin, co_source = extract_customer_origin_text(
        message,
        inbound_metadata=meta,
        normalized_type=ntype,
    )
    evidence, ev_source = extract_evidence_text(meta)
    merged = (message or "").strip()
    if not merged and customer_origin:
        merged = customer_origin
    return InboundTextSplit(
        customer_origin=customer_origin,
        evidence=evidence,
        merged=merged,
        normalized_type=ntype,
        customer_origin_source=co_source,
        evidence_source=ev_source,
    )


def _evidence_has_payment_vocabulary(evidence: str) -> bool:
    if not (evidence or "").strip():
        return False
    try:
        from core.ai_libraries import is_payment_query  # noqa: PLC0415
        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
            has_payment_semantics,
        )
        if is_payment_query(evidence):
            return True
        if has_payment_semantics(evidence):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def customer_origin_has_payment_request(
    customer_origin: str,
    *,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    state: Any = None,
) -> bool:
    """True only when customer-authored text explicitly requests payment info."""
    text = (customer_origin or "").strip()
    if not text:
        return False
    try:
        from modules.ai.brain.commerce.conversational_priority import (  # noqa: PLC0415
            detect_payment_intent_strength,
        )
        verdict = detect_payment_intent_strength(
            text,
            state=state,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        )
        if verdict.source == "blocked":
            return False
        if verdict.strength >= 0.65:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.ai_libraries import is_payment_query  # noqa: PLC0415
        return bool(is_payment_query(text))
    except Exception:  # noqa: BLE001
        return False


def classify_payment_intent_source(split: InboundTextSplit) -> str:
    """Return ``customer_origin|vision|ocr|merged|none`` for telemetry."""
    co_pay = customer_origin_has_payment_request(split.customer_origin)
    if co_pay:
        return "customer_origin"
    if not _evidence_has_payment_vocabulary(split.evidence):
        return "none"
    src = split.evidence_source or "mixed"
    if src in {"vision", "ocr", "pdf"}:
        return src
    if src == "mixed":
        return "vision"
    # Fallback: evidence vocabulary present but metadata sparse — merged leak.
    return "merged"


def log_customer_origin_intent(
    *,
    tenant_id: Any = None,
    route: str = "",
    split: Optional[InboundTextSplit] = None,
    customer_payment_request: bool = False,
    allow_outbound: bool = False,
    reason: str = "",
    conversation_id: Any = None,
) -> None:
    try:
        sp = split or InboundTextSplit("", "", "", "")
        logger.info(
            "[CUSTOMER_ORIGIN_INTENT] tenant=%s conversation_id=%s route=%s "
            "allow_outbound=%s customer_payment_request=%s reason=%s "
            "normalized_type=%s customer_origin_source=%s "
            "customer_origin_len=%d evidence_len=%d merged_len=%d",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            route or "-",
            "true" if allow_outbound else "false",
            "true" if customer_payment_request else "false",
            reason or "-",
            sp.normalized_type or "-",
            sp.customer_origin_source or "-",
            len(sp.customer_origin or ""),
            len(sp.evidence or ""),
            len(sp.merged or ""),
        )
    except Exception:  # noqa: BLE001
        pass


def log_payment_intent_source(
    *,
    tenant_id: Any = None,
    route: str = "",
    source: str = "",
    conversation_id: Any = None,
) -> None:
    try:
        logger.info(
            "[PAYMENT_INTENT_SOURCE] tenant=%s conversation_id=%s route=%s "
            "source=%s",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            route or "-",
            source or "none",
        )
    except Exception:  # noqa: BLE001
        pass


def emit_payment_intent_telemetry(
    *,
    tenant_id: Any = None,
    route: str = "",
    split: InboundTextSplit,
    allow_outbound: bool = False,
    reason: str = "",
    conversation_id: Any = None,
) -> str:
    """Log both rollout lines; return classified source."""
    co_req = customer_origin_has_payment_request(split.customer_origin)
    src = classify_payment_intent_source(split)
    log_customer_origin_intent(
        tenant_id=tenant_id,
        route=route,
        split=split,
        customer_payment_request=co_req,
        allow_outbound=allow_outbound,
        reason=reason,
        conversation_id=conversation_id,
    )
    log_payment_intent_source(
        tenant_id=tenant_id,
        route=route,
        source=src,
        conversation_id=conversation_id,
    )
    return src


def customer_origin_allows_payment_artifacts(
    message: str = "",
    *,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    state: Any = None,
    tenant_id: Any = None,
    route: str = "",
    conversation_id: Any = None,
) -> Tuple[bool, InboundTextSplit, str]:
    """Split inbound text and return ``(allowed, split, reason)``."""
    split = split_inbound_text(
        message,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
    )
    if not customer_origin_has_payment_request(
        split.customer_origin,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
        state=state,
    ):
        reason = "no_customer_origin_payment_intent"
        if _evidence_has_payment_vocabulary(split.evidence):
            reason = "ocr_only_payment_vocabulary"
        emit_payment_intent_telemetry(
            tenant_id=tenant_id,
            route=route,
            split=split,
            allow_outbound=False,
            reason=reason,
            conversation_id=conversation_id,
        )
        return False, split, reason
    return True, split, "ok"


def is_payment_media_key(media_key: str) -> bool:
    key = (media_key or "").strip().lower()
    if not key:
        return False
    if key.startswith("payment_"):
        return True
    return key.endswith("_barcode") or key.endswith("_qr")


def attachment_is_payment_artifact(att: Dict[str, Any]) -> bool:
    """True when an outbound attachment is a payment barcode / IBAN asset."""
    if not isinstance(att, dict):
        return False
    if is_payment_media_key(str(att.get("media_key") or "")):
        return True
    haystack = " ".join(
        [
            str(att.get("title") or ""),
            str(att.get("usage_context") or ""),
            " ".join(str(t) for t in (att.get("tags") or [])),
        ]
    )
    if not haystack.strip():
        return False
    try:
        from core.ai_libraries import is_payment_query  # noqa: PLC0415
        return bool(is_payment_query(haystack))
    except Exception:  # noqa: BLE001
        return False


def filter_payment_media_attachments(
    attachments: List[Dict[str, Any]],
    *,
    allow_payment: bool,
) -> List[Dict[str, Any]]:
    if allow_payment:
        return list(attachments or [])
    out: List[Dict[str, Any]] = []
    for att in attachments or []:
        if attachment_is_payment_artifact(att):
            continue
        out.append(att)
    return out


__all__ = [
    "InboundTextSplit",
    "attachment_is_payment_artifact",
    "classify_payment_intent_source",
    "customer_origin_allows_payment_artifacts",
    "customer_origin_has_payment_request",
    "emit_payment_intent_telemetry",
    "extract_customer_origin_text",
    "extract_evidence_text",
    "filter_payment_media_attachments",
    "is_payment_media_key",
    "log_customer_origin_intent",
    "log_payment_intent_source",
    "split_inbound_text",
]
