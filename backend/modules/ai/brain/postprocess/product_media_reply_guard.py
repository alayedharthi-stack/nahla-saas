"""
product_media_reply_guard.py
────────────────────────────
Post-compose belt guard for product-media turns (P1-E).

Strips contradictory CS phrasing — does not inject replacement copy.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from modules.ai.brain.commerce.product_media import (
    detect_product_media_turn,
    has_active_order_evidence,
)

logger = logging.getLogger("nahla.brain.postprocess.product_media_reply_guard")

_VIDEO_UNCERTAINTY_MARKERS: tuple[str, ...] = (
    "لم اتمكن من مشاهده الفيديو",
    "لم أتمكن من مشاهدة الفيديو",
    "لم استطع مشاهده الفيديو",
    "لم أستطع مشاهدة الفيديو",
    "لا استطيع رؤيه الفيديو",
    "لا أستطيع رؤية الفيديو",
    "لا اقدر اشوف الفيديو",
    "لا أقدر أشوف الفيديو",
)

_ORDER_SHIPMENT_MARKERS: tuple[str, ...] = (
    "حول طلبك او الشحنه",
    "حول طلبك أو الشحنة",
    "طلبك او الشحنه",
    "طلبك أو الشحنة",
)

_GENERIC_ACK_ONLY: tuple[str, ...] = (
    "شكرا على المعلومات",
    "شكرًا على المعلومات",
)


def _normalize_ar(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _segment_has_marker(segment: str, markers: tuple[str, ...]) -> bool:
    norm = _normalize_ar(segment)
    if not norm:
        return False
    return any(_normalize_ar(m) in norm for m in markers)


def strip_product_media_violations(
    text: str,
    *,
    has_content_signal: bool,
    allow_order_shipment: bool,
) -> tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    kept_paragraphs: list[str] = []

    for paragraph in re.split(r"\n\s*\n", raw):
        p = paragraph.strip()
        if not p:
            continue
        if not allow_order_shipment and _segment_has_marker(p, _ORDER_SHIPMENT_MARKERS):
            stripped_any = True
            continue

        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        kept_lines: list[str] = []
        for ln in lines:
            drop_ln = False
            if has_content_signal and _segment_has_marker(ln, _VIDEO_UNCERTAINTY_MARKERS):
                drop_ln = True
            if not allow_order_shipment and _segment_has_marker(ln, _ORDER_SHIPMENT_MARKERS):
                drop_ln = True
            if drop_ln:
                stripped_any = True
                continue
            kept_lines.append(ln)

        if len(kept_lines) < len(lines):
            stripped_any = True
        if kept_lines:
            kept_paragraphs.append("\n".join(kept_lines))

    result = "\n\n".join(kept_paragraphs).strip()

    # Standalone generic ack with no substance
    if result and _normalize_ar(result) in {_normalize_ar(x) for x in _GENERIC_ACK_ONLY}:
        return "", True

    return result, stripped_any


@dataclass(frozen=True)
class ProductMediaReplyGuardResult:
    reply: str
    stripped: bool


def _has_content_signal(
    inbound_text: str,
    inbound_metadata: dict[str, Any],
) -> bool:
    if inbound_metadata.get("frame_vision_text"):
        return True
    if inbound_metadata.get("frame_vision_status") == "ok":
        return True
    hints = inbound_metadata.get("topic_hints")
    if isinstance(hints, list) and hints:
        return True
    if inbound_metadata.get("product_media_signal"):
        return True
    verdict = detect_product_media_turn(
        inbound_text,
        inbound_metadata=inbound_metadata,
    )
    return verdict.has_vision_evidence or verdict.has_hint_only


def apply_product_media_reply_guard(
    reply: str,
    *,
    inbound_text: str = "",
    inbound_metadata: Optional[dict[str, Any]] = None,
    commerce_bundle: Optional[dict[str, Any]] = None,
    tenant_id: Optional[int] = None,
) -> ProductMediaReplyGuardResult:
    text = (reply or "").strip()
    if not text:
        return ProductMediaReplyGuardResult(reply="", stripped=False)

    meta = inbound_metadata if isinstance(inbound_metadata, dict) else {}
    verdict = detect_product_media_turn(
        inbound_text,
        inbound_metadata=meta,
    )
    if not verdict.matched and not meta.get("product_media_signal"):
        return ProductMediaReplyGuardResult(reply=text, stripped=False)

    allow_order = has_active_order_evidence(commerce_bundle)
    if not allow_order and meta.get("active_order_context"):
        allow_order = has_active_order_evidence({
            "active_order_context": meta.get("active_order_context"),
            "active_order_id": meta.get("active_order_id"),
        })
    has_signal = _has_content_signal(inbound_text, meta)

    cleaned, stripped = strip_product_media_violations(
        text,
        has_content_signal=has_signal,
        allow_order_shipment=allow_order,
    )
    if stripped:
        logger.info(
            "[PRODUCT_MEDIA_REPLY_GUARD] tenant=%s orig_len=%d new_len=%d "
            "vision=%s allow_order=%s preview_in=%r",
            tenant_id if tenant_id is not None else "-",
            len(text),
            len(cleaned),
            has_signal,
            allow_order,
            (inbound_text or "")[:60],
        )

    return ProductMediaReplyGuardResult(reply=cleaned, stripped=stripped)


__all__ = [
    "ProductMediaReplyGuardResult",
    "apply_product_media_reply_guard",
    "strip_product_media_violations",
]
