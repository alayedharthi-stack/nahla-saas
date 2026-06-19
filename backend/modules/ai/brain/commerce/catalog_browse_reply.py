"""
catalog_browse_reply.py
───────────────────────
Deterministic catalog browse presentation — names, prices, variants,
descriptions, and product-card attachments from synced catalog evidence.

Platform-wide: never invent products; only format executor/search results.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.catalog_browse_reply")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_VAGUE_BROWSE_MARKERS = (
    "عندنا أنواع مميزة",
    "عندنا انواع مميزة",
    "أي نوع يناسبك",
    "اي نوع يناسبك",
    "أي نوع تفضّل",
    "اي نوع تفضل",
    "أي نوع تحب",
)

_EMPTY_CATALOG_AR = (
    "ما ظهرت لي منتجات متزامنة في الكتالوج الآن. "
    "وش نوع العسل أو الحجم اللي تبحث عنه؟"
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = str(text).lower()
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def _format_price(product: Dict[str, Any]) -> str:
    sale = str(product.get("sale_price") or "").strip()
    price = str(product.get("price") or "").strip()
    chosen = sale or price
    if not chosen:
        return ""
    if re.match(r"^\s*\d+(\.\d+)?\s*$", chosen):
        return f"{chosen} ريال"
    return chosen


def _format_variant_line(product: Dict[str, Any]) -> str:
    variants = list(product.get("variants") or [])
    if not variants:
        summary = str(product.get("variants_summary") or "").strip()
        if summary:
            return summary[:120]
        return ""
    parts: List[str] = []
    for v in variants[:4]:
        if not isinstance(v, dict):
            continue
        label = str(v.get("title") or v.get("name") or "").strip()
        vprice = str(v.get("price") or v.get("sale_price") or "").strip()
        if label and vprice:
            if re.match(r"^\s*\d+(\.\d+)?\s*$", vprice):
                vprice = f"{vprice} ريال"
            parts.append(f"{label} — {vprice}")
        elif label:
            parts.append(label)
    return "، ".join(parts)


def _description_excerpt(product: Dict[str, Any], *, max_len: int = 120) -> str:
    raw = str(product.get("description") or "").strip()
    if not raw:
        return ""
    first = re.split(r"[.\n!؟]", raw, maxsplit=1)[0].strip()
    if not first:
        return ""
    if len(first) > max_len:
        return first[: max_len - 1].rstrip() + "…"
    return first


def _availability_label(product: Dict[str, Any]) -> str:
    in_stock = product.get("in_stock", product.get("orderable", True))
    if in_stock is False:
        return "غير متوفر"
    if product.get("can_checkout") is False:
        return "للاستفسار"
    return ""


def format_catalog_product_line(
    product: Dict[str, Any],
    *,
    index: int = 1,
    include_description: bool = True,
) -> str:
    """Single numbered catalog line for browse replies."""
    title = str(product.get("title") or "منتج").strip()
    parts = [f"{index}. *{title}*"]
    variant_line = _format_variant_line(product)
    price_line = _format_price(product)
    detail_bits: List[str] = []
    if variant_line:
        detail_bits.append(variant_line)
    elif price_line:
        detail_bits.append(price_line)
    avail = _availability_label(product)
    if avail:
        detail_bits.append(avail)
    if detail_bits:
        parts.append(" — ".join(detail_bits))
    block = parts[0]
    if len(parts) > 1:
        block = f"{parts[0]}\n   {parts[1]}"
    if include_description:
        desc = _description_excerpt(product)
        if desc:
            block += f"\n   {desc}"
    return block


def build_catalog_browse_reply(
    products: Sequence[Dict[str, Any]],
    *,
    intro: str = "",
    max_items: int = 5,
    include_descriptions: bool = True,
    show_more_hint: bool = False,
) -> str:
    """Build a deterministic multi-product browse reply from catalog rows."""
    items = [p for p in (products or []) if isinstance(p, dict) and str(p.get("title") or "").strip()]
    if not items:
        return _EMPTY_CATALOG_AR

    header = (intro or "هذه بعض الأنواع المتوفرة لدينا حالياً:").strip()
    lines = [header, ""]
    for i, product in enumerate(items[: max(1, max_items)], 1):
        lines.append(format_catalog_product_line(
            product,
            index=i,
            include_description=include_descriptions,
        ))
        lines.append("")

    closing = "اختر رقم المنتج أو اسمه وأكمل معك."
    if show_more_hint:
        closing += "\n\nتبغى أرسل لك باقي الخيارات؟"
    lines.append(closing)
    return "\n".join(lines).strip()


def build_catalog_browse_attachments(
    products: Sequence[Dict[str, Any]],
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Product-card attachment dicts for WhatsApp dispatch (catalog search path)."""
    from services.product_resolver import (  # noqa: PLC0415
        format_product_card_caption,
    )
    from services.product_resolver import _dict_to_resolution  # noqa: PLC0415

    out: List[Dict[str, Any]] = []
    for product in list(products or [])[: max(1, limit)]:
        if not isinstance(product, dict):
            continue
        title = str(product.get("title") or "").strip()
        if not title:
            continue
        res = _dict_to_resolution(product, confidence="catalog_browse")
        image_url = str(product.get("image_url") or "").strip()
        if not image_url and not str(product.get("product_url") or product.get("url") or "").strip():
            continue
        out.append({
            "kind": "product_card",
            "id": int(product.get("id") or 0),
            "title": res.title,
            "media_type": "image" if image_url else "text",
            "file_url": image_url,
            "caption": format_product_card_caption(res, include_description=True),
            "product_url": res.product_url or "",
            "price": res.price or "",
            "in_stock": res.in_stock,
            "external_id": res.external_id or "",
            "confidence": "catalog_browse",
            "needs_variant_choice": res.needs_variant_choice,
            "variants": list(res.variants or []),
            "has_variants": res.has_variants,
            "default_variant_retailer_id": res.default_variant_retailer_id,
            "dispatch_source": "catalog_browse",
        })
    return out


def reply_references_product_titles(
    reply: str,
    products: Sequence[Dict[str, Any]],
) -> bool:
    """True when *reply* mentions at least one catalog product title."""
    text = _norm(reply or "")
    if not text:
        return False
    for product in products or []:
        if not isinstance(product, dict):
            continue
        title = _norm(str(product.get("title") or ""))
        if len(title) >= 4 and title in text:
            return True
        # Short token match: first significant word of title
        tokens = [t for t in title.split() if len(t) >= 3]
        if tokens and tokens[0] in text:
            return True
    return False


def is_vague_browse_reply(reply: str) -> bool:
    """True when reply is generic browse fluff without product substance."""
    raw = (reply or "").strip()
    if not raw:
        return True
    norm = _norm(raw)
    if any(_norm(marker) in norm for marker in _VAGUE_BROWSE_MARKERS):
        return True
    if len(norm) <= 48 and not re.search(r"\d+\.", raw):
        if any(w in norm for w in ("انواع", "أنواع", "مميزة", "يناسبك")):
            return True
    return False


def should_rewrite_vague_browse_reply(
    reply: str,
    products: Sequence[Dict[str, Any]],
) -> bool:
    if not products:
        return False
    if reply_references_product_titles(reply, products):
        return False
    return is_vague_browse_reply(reply)


def log_catalog_browse_reply(
    *,
    tenant_id: Any,
    product_count: int,
    attachment_count: int,
    source: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[CATALOG_BROWSE_REPLY] tenant=%s products=%d attachments=%d "
            "source=%s preview=%r",
            tenant_id,
            product_count,
            attachment_count,
            source or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break browse reply
        pass


__all__ = [
    "build_catalog_browse_attachments",
    "build_catalog_browse_reply",
    "format_catalog_product_line",
    "is_vague_browse_reply",
    "log_catalog_browse_reply",
    "reply_references_product_titles",
    "should_rewrite_vague_browse_reply",
]
