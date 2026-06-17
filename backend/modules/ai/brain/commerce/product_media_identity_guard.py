"""
product_media_identity_guard.py
───────────────────────────────
Deterministic product identity verification from inbound media evidence
(OCR + vision description) matched against synced catalog — platform-wide.

Operational only: no LLM knowledge, no distributor/country/supplier claims.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from modules.ai.knowledge.product_matcher import (
    CatalogProductForMatch,
    ProductMatch,
    match_products,
)

logger = logging.getLogger("nahla.brain.commerce.product_media_identity_guard")

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})

CONFIDENT_MIN_SCORE = 0.72
WEAK_MIN_SCORE = 0.48
MIN_READABLE_CHARS = 8

MSG_CONFIDENT_AR = (
    "يبدو أن المنتج مطابق للمنتج:\n"
    "{product_name}\n\n"
    "بحسب المنتجات المتزامنة حالياً."
)
MSG_WEAK_AR = (
    "أرى تشابهاً مع بعض المنتجات الموجودة لدينا،\n"
    "لكن لا أستطيع التأكيد بشكل كامل من الصورة فقط."
)
MSG_NO_MATCH_AR = (
    "لم يظهر لي تطابق واضح مع المنتجات المتزامنة حالياً."
)
MSG_UNREADABLE_AR = (
    "الصورة غير واضحة بما يكفي للتحقق من المنتج."
)

_FORBIDDEN_CLAIM_RE = re.compile(
    r"(?:"
    r"و(?:كيل|کل)|موزع|مورد|distributor|supplier|"
    r"مصر|egypt|country\s+of\s+origin|بلد\s+المنش(?:أ|ا)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_IMAGE_TYPE_TOKENS = frozenset({
    "image",
    "photo",
    "picture",
    "sticker",
    "product_photo",
    "customer_photo",
})

_VISION_LINE_MARKERS = (
    "[وصف الصورة",
    "[وصف الصورة المرسلة]",
)


def product_media_identity_guard_enabled() -> bool:
    raw = os.getenv("PRODUCT_MEDIA_IDENTITY_GUARD_ENABLED", "1").strip().lower()
    return raw not in _FLAG_FALSY


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


def _extract_meta_text(meta: Dict[str, Any]) -> tuple[str, str]:
    ocr_parts: List[str] = []
    vision_parts: List[str] = []
    for key in ("ocr_text", "ocr_text_preview", "extracted_text_preview"):
        val = str(meta.get(key) or "").strip()
        if val:
            ocr_parts.append(val)
    for key in ("frame_vision_text", "vision_text"):
        val = str(meta.get(key) or "").strip()
        if val:
            vision_parts.append(val)
    ocr = " ".join(dict.fromkeys(ocr_parts)).strip()
    vision = " ".join(dict.fromkeys(vision_parts)).strip()
    return ocr, vision


def _vision_from_message_body(message: str) -> str:
    parts: List[str] = []
    for line in (message or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for marker in _VISION_LINE_MARKERS:
            if marker in stripped:
                body = stripped.split("]", 1)[-1].strip() if "]" in stripped else stripped
                body = body.replace(marker, "").strip(" :")
                if len(body) >= MIN_READABLE_CHARS:
                    parts.append(body)
                break
    return " ".join(dict.fromkeys(parts)).strip()


def _vision_from_history(history: Sequence[Dict[str, Any]], *, max_turns: int = 8) -> str:
    parts: List[str] = []
    for turn in reversed(list(history or [])[-max_turns:]):
        direction = str((turn or {}).get("direction") or "").lower()
        if direction not in {"in", "inbound", "customer", "user", ""}:
            continue
        body = str((turn or {}).get("body") or "").strip()
        fragment = _vision_from_message_body(body)
        if fragment:
            parts.append(fragment)
            break
    return parts[0] if parts else ""


@dataclass(frozen=True)
class MediaIdentityEvidence:
    ocr_text: str = ""
    vision_text: str = ""
    combined_text: str = ""
    readable: bool = False
    has_current_image: bool = False
    source: str = ""


@dataclass(frozen=True)
class MediaIdentityVerdict:
    status: str  # confident | weak | no_match | unreadable | skipped
    reply_text: str = ""
    matched_product_id: Optional[int] = None
    matched_product_title: str = ""
    confidence: float = 0.0
    match_count: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)


def collect_media_identity_evidence(
    *,
    message: str,
    profile: Optional[Dict[str, Any]] = None,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    state: Any = None,
) -> MediaIdentityEvidence:
    profile = profile or {}
    meta = dict((profile or {}).get("inbound_metadata") or {})
    has_image = _has_image_attachment(profile)

    ocr_parts: List[str] = []
    vision_parts: List[str] = []
    source = ""

    if has_image:
        ocr, vision = _extract_meta_text(meta)
        if ocr:
            ocr_parts.append(ocr)
        if vision:
            vision_parts.append(vision)
        source = "current_image"

    body_vision = _vision_from_message_body(message or "")
    if body_vision:
        vision_parts.append(body_vision)
        source = source or "message_framing"

    if not vision_parts and not ocr_parts:
        hist_vision = _vision_from_history(history or [])
        if hist_vision:
            vision_parts.append(hist_vision)
            source = source or "history_image"

    obj_ev = dict(getattr(state, "objective_evidence", None) or {})
    if obj_ev.get("media_ocr_preview"):
        ocr_parts.append(str(obj_ev["media_ocr_preview"]))
        source = source or "objective_cache"
    if obj_ev.get("media_vision_preview"):
        vision_parts.append(str(obj_ev["media_vision_preview"]))
        source = source or "objective_cache"

    ocr_text = " ".join(dict.fromkeys(ocr_parts)).strip()
    vision_text = " ".join(dict.fromkeys(vision_parts)).strip()
    combined = " ".join(x for x in (ocr_text, vision_text) if x).strip()
    readable = len(combined) >= MIN_READABLE_CHARS

    return MediaIdentityEvidence(
        ocr_text=ocr_text,
        vision_text=vision_text,
        combined_text=combined,
        readable=readable,
        has_current_image=has_image,
        source=source,
    )


def _catalog_from_merchant_context(ctx: Any) -> List[CatalogProductForMatch]:
    mc = getattr(ctx, "merchant_context", None) or {}
    products = list(mc.get("products") or []) if isinstance(mc, dict) else []
    out: List[CatalogProductForMatch] = []
    seen: set[int] = set()
    for row in products:
        if not isinstance(row, dict):
            continue
        pid = row.get("id")
        if pid is None:
            continue
        try:
            iid = int(pid)
        except (TypeError, ValueError):
            continue
        if iid in seen:
            continue
        if not str(row.get("external_id") or "").strip():
            continue
        seen.add(iid)
        out.append(
            CatalogProductForMatch(
                id=iid,
                title=str(row.get("title") or ""),
                sku=row.get("sku"),
                external_id=row.get("external_id"),
            )
        )
    return out


def load_synced_catalog_for_match(db: Any, tenant_id: int, *, limit: int = 250) -> List[CatalogProductForMatch]:
    if db is None or not tenant_id:
        return []
    try:
        from core.catalog import apply_active_catalog_query_filters  # noqa: PLC0415
        from database.models import Product  # noqa: PLC0415

        rows = (
            apply_active_catalog_query_filters(
                db.query(Product).filter(
                    Product.tenant_id == int(tenant_id),
                    Product.external_id.isnot(None),
                    Product.external_id != "",
                ),
                Product,
            )
            .order_by(Product.id)
            .limit(int(limit))
            .all()
        )
        return [
            CatalogProductForMatch(
                id=int(p.id),
                title=str(p.title or ""),
                sku=getattr(p, "sku", None),
                external_id=getattr(p, "external_id", None),
            )
            for p in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[PRODUCT_MEDIA_IDENTITY] catalog_load_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return []


def resolve_catalog_for_match(ctx: Any) -> List[CatalogProductForMatch]:
    db = getattr(ctx, "_db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    catalog = load_synced_catalog_for_match(db, tenant_id)
    if catalog:
        return catalog
    return _catalog_from_merchant_context(ctx)


def classify_media_catalog_match(
    evidence: MediaIdentityEvidence,
    catalog: Sequence[CatalogProductForMatch],
) -> MediaIdentityVerdict:
    ev_dict = {
        "ocr_chars": len(evidence.ocr_text),
        "vision_chars": len(evidence.vision_text),
        "source": evidence.source,
        "readable": evidence.readable,
    }

    if not evidence.readable:
        return MediaIdentityVerdict(
            status="unreadable",
            reply_text=MSG_UNREADABLE_AR,
            evidence=ev_dict,
        )

    if not catalog:
        return MediaIdentityVerdict(
            status="no_match",
            reply_text=MSG_NO_MATCH_AR,
            evidence={**ev_dict, "catalog_size": 0},
        )

    matches: List[ProductMatch] = match_products(
        evidence.combined_text,
        catalog,
        limit=5,
        min_confidence=WEAK_MIN_SCORE,
    )

    if not matches:
        return MediaIdentityVerdict(
            status="no_match",
            reply_text=MSG_NO_MATCH_AR,
            evidence={**ev_dict, "catalog_size": len(catalog), "match_count": 0},
        )

    best = matches[0]
    second_score = matches[1].confidence if len(matches) > 1 else 0.0
    gap = best.confidence - second_score

    if best.confidence >= CONFIDENT_MIN_SCORE and (len(matches) == 1 or gap >= 0.12):
        reply = MSG_CONFIDENT_AR.format(product_name=best.title)
        return MediaIdentityVerdict(
            status="confident",
            reply_text=reply,
            matched_product_id=best.product_id,
            matched_product_title=best.title,
            confidence=best.confidence,
            match_count=len(matches),
            evidence={**ev_dict, "catalog_size": len(catalog), "top_confidence": best.confidence},
        )

    if best.confidence >= WEAK_MIN_SCORE:
        return MediaIdentityVerdict(
            status="weak",
            reply_text=MSG_WEAK_AR,
            matched_product_id=best.product_id,
            matched_product_title=best.title,
            confidence=best.confidence,
            match_count=len(matches),
            evidence={**ev_dict, "catalog_size": len(catalog), "top_confidence": best.confidence},
        )

    return MediaIdentityVerdict(
        status="no_match",
        reply_text=MSG_NO_MATCH_AR,
        match_count=len(matches),
        evidence={**ev_dict, "catalog_size": len(catalog), "top_confidence": best.confidence},
    )


def should_run_media_identity_guard(
    *,
    message: str,
    profile: Optional[Dict[str, Any]] = None,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    state: Any = None,
) -> bool:
    from modules.ai.brain.intent.conversation_objective_guard import (  # noqa: PLC0415
        is_product_origin_objective_active,
        is_product_ownership_question,
    )

    if not is_product_ownership_question(message):
        return False

    evidence = collect_media_identity_evidence(
        message=message,
        profile=profile,
        history=history,
        state=state,
    )
    if evidence.has_current_image:
        return True
    if evidence.readable and evidence.source in {"history_image", "objective_cache", "message_framing"}:
        return True
    if is_product_origin_objective_active(state) and evidence.readable:
        return True
    return False


def stamp_media_identity_on_objective(
    state: Any,
    *,
    evidence: MediaIdentityEvidence,
    verdict: MediaIdentityVerdict,
) -> None:
    obj_ev = dict(getattr(state, "objective_evidence", None) or {})
    if evidence.ocr_text:
        obj_ev["media_ocr_preview"] = evidence.ocr_text[:400]
    if evidence.vision_text:
        obj_ev["media_vision_preview"] = evidence.vision_text[:400]
    if verdict.matched_product_id is not None:
        obj_ev["catalog_match_product_id"] = verdict.matched_product_id
        obj_ev["catalog_match_title"] = verdict.matched_product_title
        obj_ev["catalog_match_confidence"] = verdict.confidence
    obj_ev["catalog_match_status"] = verdict.status
    state.objective_evidence = obj_ev


def evaluate_product_media_identity(
    ctx: Any,
) -> MediaIdentityVerdict:
    message = str(getattr(ctx, "message", "") or "")
    profile = getattr(ctx, "profile", None) or {}
    history = getattr(ctx, "history", None) or []
    state = getattr(ctx, "state", None)

    if not should_run_media_identity_guard(
        message=message,
        profile=profile if isinstance(profile, dict) else {},
        history=history,
        state=state,
    ):
        return MediaIdentityVerdict(status="skipped")

    evidence = collect_media_identity_evidence(
        message=message,
        profile=profile if isinstance(profile, dict) else {},
        history=history,
        state=state,
    )
    catalog = resolve_catalog_for_match(ctx)
    verdict = classify_media_catalog_match(evidence, catalog)

    if state is not None and verdict.status != "skipped":
        stamp_media_identity_on_objective(state, evidence=evidence, verdict=verdict)

    if verdict.reply_text and _FORBIDDEN_CLAIM_RE.search(verdict.reply_text):
        logger.warning(
            "[PRODUCT_MEDIA_IDENTITY] forbidden_claim_blocked tenant=%s",
            getattr(ctx, "tenant_id", None),
        )
        return MediaIdentityVerdict(
            status="no_match",
            reply_text=MSG_NO_MATCH_AR,
            evidence=verdict.evidence,
        )

    return verdict


def try_product_media_identity_decision(ctx: Any) -> Optional[Any]:
    """Return a deterministic decision when image + ownership ask is detected."""
    if not product_media_identity_guard_enabled():
        return None

    from modules.ai.brain.decision.actions import ACTION_PRODUCT_MEDIA_IDENTITY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    verdict = evaluate_product_media_identity(ctx)
    if verdict.status == "skipped" or not verdict.reply_text:
        return None

    logger.info(
        "[PRODUCT_MEDIA_IDENTITY] tenant=%s status=%s confidence=%.2f "
        "product_id=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        verdict.status,
        verdict.confidence,
        verdict.matched_product_id,
        (getattr(ctx, "message", "") or "")[:60],
    )

    return Decision(
        action=ACTION_PRODUCT_MEDIA_IDENTITY,
        args={
            "reply_text": verdict.reply_text,
            "topic": "product_media_identity",
            "block_commerce_escalation": True,
            "block_purchase_flow": True,
            "media_identity_status": verdict.status,
            "matched_product_id": verdict.matched_product_id,
            "matched_product_title": verdict.matched_product_title,
            "match_confidence": verdict.confidence,
        },
        reason=f"product media identity — {verdict.status}",
        confidence=max(verdict.confidence, 0.86),
    )


__all__ = [
    "MediaIdentityEvidence",
    "MediaIdentityVerdict",
    "MSG_CONFIDENT_AR",
    "MSG_NO_MATCH_AR",
    "MSG_UNREADABLE_AR",
    "MSG_WEAK_AR",
    "classify_media_catalog_match",
    "collect_media_identity_evidence",
    "evaluate_product_media_identity",
    "product_media_identity_guard_enabled",
    "resolve_catalog_for_match",
    "should_run_media_identity_guard",
    "try_product_media_identity_decision",
]
