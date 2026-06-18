"""
core/wa_cart_catalog_resolver.py
────────────────────────────────
P0 — Catalog-grounded resolution for WhatsApp cart line items.

Operational only: maps free-text cart intents to catalog product_id /
variant_id, or marks items ``needs_review`` / ``custom_unmatched_item``
when no confident match exists. Never treats customer free text as a
confirmed catalog product.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.wa_cart_catalog_resolver")

ITEM_STATUS_CONFIRMED = "confirmed"
ITEM_STATUS_NEEDS_REVIEW = "needs_review"
ITEM_STATUS_CUSTOM_UNMATCHED = "custom_unmatched_item"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")

_VARIANT_HINT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "250g": ("250g", "250 g", "ربع", "ربع كilo", "ربع كيلo", "ربع كيلو"),
    "500g": ("500g", "500 g", "نصف", "نص", "نصف كilo", "نصف كيلo", "نصف كيلo"),
    "1kg":  ("1kg", "1 kg", "كilo", "كيلo", "كيلو", "ك", "كبير"),
    "10kg": ("10kg", "10 kg", "10كilo", "10 كilo", "10 كيلo", "10 كيلو", "سطل"),
}

_HONEY_AMBIGUITY: Dict[str, Tuple[str, str]] = {
    "طلح": ("عسل طلح نجد", "عسل طلح"),
    "سمر": ("عسل سمر الحجاز", "عسل سمر"),
}


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return re.sub(r"\s+", " ", t).strip()


def _variant_hint_tokens(hint: str) -> List[str]:
    hint = _norm(hint)
    if not hint:
        return []
    tokens = [hint]
    for canonical, aliases in _VARIANT_HINT_ALIASES.items():
        for alias in aliases:
            if _norm(alias) == hint or alias in hint or hint in _norm(alias):
                tokens.append(canonical)
                tokens.extend(aliases)
    return list(dict.fromkeys(tokens))


def _variant_label(variant: Dict[str, Any]) -> str:
    parts = [
        variant.get("option_summary"),
        variant.get("name"),
        variant.get("sku"),
        variant.get("title"),
    ]
    return _norm(" ".join(str(p or "") for p in parts if p))


def _match_variant_in_catalog(
    resolution: Any,
    variant_hint: str,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """
    Return ``(matched_variant, requested_but_missing)``.

    ``requested_but_missing`` is True when the customer named a size
    that does not exist on the matched product.
    """
    hint = _norm(variant_hint)
    if not hint:
        default_id = getattr(resolution, "default_variant_id", None)
        variants = list(getattr(resolution, "variants", None) or [])
        if default_id and variants:
            for v in variants:
                if v.get("id") == default_id:
                    return v, False
        if len(variants) == 1:
            return variants[0], False
        return None, False

    tokens = _variant_hint_tokens(hint)
    variants = list(getattr(resolution, "variants", None) or [])
    if not variants:
        hint = _norm(variant_hint)
        if hint and (
            any(k in hint for k in ("10", "سطل", "bucket"))
            or hint in _VARIANT_HINT_ALIASES.get("10kg", ())
            or hint in _VARIANT_HINT_ALIASES.get("1kg", ())
            or hint in _VARIANT_HINT_ALIASES.get("500g", ())
            or hint in _VARIANT_HINT_ALIASES.get("250g", ())
        ):
            return None, True
        return None, False

    for v in variants:
        label = _variant_label(v)
        if not label:
            continue
        for tok in tokens:
            nt = _norm(tok)
            if nt and (nt in label or label in nt):
                return v, False

    # Customer asked for a size bucket (e.g. 10kg bucket) not in catalog.
    if any(k in hint for k in ("10", "سطل", "bucket")) or hint in _VARIANT_HINT_ALIASES.get("10kg", ()):
        return None, True
    if hint in _VARIANT_HINT_ALIASES.get("1kg", ()) + _VARIANT_HINT_ALIASES.get("500g", ()) + _VARIANT_HINT_ALIASES.get("250g", ()):
        return None, True
    return None, False


def _find_ambiguous_honey_candidates(query: str) -> Optional[Tuple[str, str]]:
    nq = _norm(query)
    for key, (a, b) in _HONEY_AMBIGUITY.items():
        if key in nq and not any(x in nq for x in ("نجد", "حجاز", "1447")):
            return a, b
    return None


def _find_catalog_candidates(
    db: Any,
    tenant_id: int,
    query: str,
    *,
    limit: int = 5,
) -> List[Any]:
    from services.product_resolver import resolve_by_query  # noqa: PLC0415
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    q = (query or "").strip()
    if len(q) < 2:
        return []

    builder = CatalogContextBuilder(db, tenant_id)
    raw = builder.search_products(q, limit=limit) or []
    if raw:
        from services.product_resolver import _dict_to_resolution  # noqa: PLC0415
        return [_dict_to_resolution(row, matched_query=q, confidence="fts") for row in raw[:limit]]

    single = resolve_by_query(db, tenant_id, q, limit=limit)
    return [single] if single else []


@dataclass
class CartCatalogResolution:
    items: List[Dict[str, Any]] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str = ""
    variant_unavailable: List[Dict[str, Any]] = field(default_factory=list)
    unmatched_items: List[Dict[str, Any]] = field(default_factory=list)
    closest_suggestions: List[str] = field(default_factory=list)


def resolve_cart_line_item(
    db: Any,
    tenant_id: int,
    item: Dict[str, Any],
) -> Tuple[Dict[str, Any], CartCatalogResolution]:
    """
    Resolve one cart line item against the tenant catalog.

    Returns the enriched item plus side-channel signals (clarification,
    variant unavailable, etc.).
    """
    side = CartCatalogResolution()
    raw = dict(item or {})
    product_id = str(raw.get("product_id") or raw.get("catalog_id") or "").strip()
    variant_hint = str(raw.get("variant") or raw.get("size") or "").strip()
    query_name = str(
        raw.get("query_hint")
        or raw.get("customer_text")
        or raw.get("product_name")
        or raw.get("title")
        or ""
    ).strip()

    enriched = dict(raw)
    enriched.setdefault("source", "whatsapp_brain")

    from core.wa_cart_line_items import normalize_variant  # noqa: PLC0415

    if product_id:
        enriched["match_status"] = ITEM_STATUS_CONFIRMED
        variant_check_needed = bool(variant_hint)
        if variant_check_needed:
            res = None
            try:
                from services.product_resolver import (  # noqa: PLC0415
                    resolve_best_effort,
                    resolve_by_external_id,
                )
                res = resolve_by_external_id(db, tenant_id, product_id)
                if (res is None or not (getattr(res, "variants", None) or [])) and query_name:
                    res = resolve_best_effort(db, tenant_id, query_name)
                if res:
                    matched, missing = _match_variant_in_catalog(res, variant_hint)
                    if matched:
                        enriched["variant_id"] = str(
                            matched.get("salla_variant_id")
                            or matched.get("retailer_id")
                            or matched.get("id")
                            or ""
                        )
                        enriched["variant"] = matched.get("option_summary") or variant_hint
                        if matched.get("price"):
                            enriched["unit_price"] = matched.get("price")
                    elif missing:
                        enriched["match_status"] = ITEM_STATUS_NEEDS_REVIEW
                        enriched.pop("variant_id", None)
                        side.variant_unavailable.append({
                            "query_name": query_name,
                            "variant_hint": variant_hint,
                            "product_title": res.title,
                            "available_variants": [
                                v.get("option_summary") or v.get("name")
                                for v in (res.variants or [])
                                if v.get("option_summary") or v.get("name")
                            ],
                        })
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[WA_CART_CATALOG] variant_enrich_failed tenant=%s product_id=%s variant_hint=%r",
                    tenant_id,
                    product_id,
                    variant_hint,
                )
        return enriched, side

    from services.product_resolver import resolve_best_effort  # noqa: PLC0415

    resolution = resolve_best_effort(db, tenant_id, query_name)
    if resolution is None or not resolution.id:
        honey = _find_ambiguous_honey_candidates(query_name)
        if honey:
            side.needs_clarification = True
            side.clarification_question = f"تقصد {honey[0]} أو {honey[1]}؟"
            enriched["match_status"] = ITEM_STATUS_NEEDS_REVIEW
            enriched["query_hint"] = query_name
            enriched.pop("product_id", None)
            return enriched, side

        candidates = _find_catalog_candidates(db, tenant_id, query_name, limit=3)
        if candidates:
            side.closest_suggestions = [c.title for c in candidates[:2] if c.title]
            if side.closest_suggestions:
                opts = " أو ".join(f"*{t}*" for t in side.closest_suggestions[:2])
                side.needs_clarification = True
                side.clarification_question = f"ما لقيت منتج مطابق بالضبط. تقصد {opts}؟"

        enriched["match_status"] = ITEM_STATUS_CUSTOM_UNMATCHED
        enriched["query_hint"] = query_name
        enriched["product_name"] = query_name or "منتج"
        enriched.pop("product_id", None)
        side.unmatched_items.append({"query_name": query_name, "item": enriched})
        return enriched, side

    matched_variant, variant_missing = _match_variant_in_catalog(resolution, variant_hint)
    enriched["product_id"] = str(resolution.external_id or resolution.id)
    enriched["catalog_product_id"] = resolution.id
    enriched["product_name"] = resolution.title
    enriched["title"] = resolution.title
    enriched["name"] = resolution.title
    enriched["display_name"] = resolution.title
    enriched["match_status"] = ITEM_STATUS_CONFIRMED

    if resolution.price:
        enriched["unit_price"] = resolution.price
    if matched_variant:
        enriched["variant_id"] = str(
            matched_variant.get("salla_variant_id")
            or matched_variant.get("retailer_id")
            or matched_variant.get("id")
            or ""
        )
        enriched["variant"] = matched_variant.get("option_summary") or variant_hint
        if matched_variant.get("price"):
            enriched["unit_price"] = matched_variant.get("price")
    elif variant_missing:
        enriched["match_status"] = ITEM_STATUS_NEEDS_REVIEW
        side.variant_unavailable.append({
            "query_name": query_name,
            "variant_hint": variant_hint,
            "product_title": resolution.title,
            "available_variants": [
                v.get("option_summary") or v.get("name")
                for v in (resolution.variants or [])
                if v.get("option_summary") or v.get("name")
            ],
        })
    elif variant_hint and resolution.needs_variant_choice:
        enriched["match_status"] = ITEM_STATUS_NEEDS_REVIEW

    return enriched, side


def resolve_cart_line_items(
    db: Any,
    tenant_id: int,
    items: List[Dict[str, Any]],
) -> CartCatalogResolution:
    """Resolve all cart line items; merge side signals."""
    merged = CartCatalogResolution(items=[])
    for raw in list(items or []):
        if not isinstance(raw, dict):
            continue
        item, side = resolve_cart_line_item(db, tenant_id, raw)
        merged.items.append(item)
        merged.variant_unavailable.extend(side.variant_unavailable)
        merged.unmatched_items.extend(side.unmatched_items)
        merged.closest_suggestions.extend(side.closest_suggestions)
        if side.needs_clarification and side.clarification_question:
            merged.needs_clarification = True
            merged.clarification_question = side.clarification_question
    return merged


def resolve_and_enrich_cart_state(
    db: Any,
    tenant_id: int,
    state: Any,
    prep: Any,
) -> CartCatalogResolution:
    """
    Resolve ``state.cart_items`` / ``prep.line_items`` in place.

    Called from the brain pipeline after cart intents are applied.
    """
    from core.wa_cart_line_items import merge_line_items, normalize_variant  # noqa: PLC0415

    cart = merge_line_items(list(getattr(state, "cart_items", None) or []))
    if not cart and hasattr(prep, "line_items"):
        cart = merge_line_items(list(getattr(prep, "line_items", None) or []))
    if not cart:
        return CartCatalogResolution()

    resolution = resolve_cart_line_items(db, tenant_id, cart)

    if hasattr(state, "cart_items"):
        state.cart_items = resolution.items
    if hasattr(prep, "line_items"):
        prep.line_items = resolution.items
    elif isinstance(prep, dict):
        prep["line_items"] = resolution.items

    if resolution.items:
        last = resolution.items[-1]
        focus = {
            "id":          last.get("product_id"),
            "external_id": last.get("product_id"),
            "title":       last.get("product_name") or last.get("title"),
            "variant":     last.get("variant"),
            "quantity":    last.get("quantity"),
            "price":       last.get("unit_price") or last.get("price"),
        }
        if hasattr(state, "current_product_focus"):
            state.current_product_focus = focus
        if hasattr(prep, "product_id") and last.get("product_id"):
            prep.product_id = str(last.get("product_id"))

    try:
        meta_key = "wa_cart_catalog_resolution"
        if hasattr(prep, "__dict__"):
            setattr(prep, meta_key, {
                "needs_clarification": resolution.needs_clarification,
                "clarification_question": resolution.clarification_question,
                "variant_unavailable": resolution.variant_unavailable,
                "unmatched_items": resolution.unmatched_items,
                "closest_suggestions": resolution.closest_suggestions,
            })
    except Exception:  # noqa: BLE001  # noqa: silent-ok - optional order_prep metadata stamp must not block cart resolution
        pass

    logger.info(
        "[WA_CART_CATALOG] resolved tenant=%s items=%d unmatched=%d variant_miss=%d clarify=%s",
        tenant_id,
        len(resolution.items),
        len(resolution.unmatched_items),
        len(resolution.variant_unavailable),
        resolution.needs_clarification,
    )
    return resolution


__all__ = [
    "CartCatalogResolution",
    "ITEM_STATUS_CONFIRMED",
    "ITEM_STATUS_CUSTOM_UNMATCHED",
    "ITEM_STATUS_NEEDS_REVIEW",
    "resolve_and_enrich_cart_state",
    "resolve_cart_line_item",
    "resolve_cart_line_items",
]
