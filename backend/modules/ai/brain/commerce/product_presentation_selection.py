"""
Product presentation selection — multi choices vs single rich card.

Platform-wide semantics only (cardinality + catalog identity).
No merchant/platform hardcoding. No LLM/prompt instructions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

PRESENTATION_MULTI_CHOICES = "multi_candidate_choices"
PRESENTATION_SINGLE_RICH = "single_resolved_rich"
PRESENTATION_NONE = "none"

DISPATCH_SOURCE_SINGLE_RESOLVED = "single_resolved_presentation"


@dataclass(frozen=True)
class ProductPresentationDecision:
    kind: str
    candidate_count: int = 0
    resolved_product: Optional[Dict[str, Any]] = None
    reason: str = ""


def _has_catalog_identity(product: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(product, dict) or not product:
        return False
    return any(
        str(product.get(key) or "").strip()
        for key in ("external_id", "id", "product_id", "sku")
    )


def _product_identity_tokens(product: Optional[Dict[str, Any]]) -> set[str]:
    if not isinstance(product, dict) or not product:
        return set()
    tokens: set[str] = set()
    for key in ("external_id", "id", "product_id", "sku"):
        raw = str(product.get(key) or "").strip()
        if raw:
            tokens.add(raw)
    return tokens


def resolve_browse_presentation_candidates(
    *,
    display_candidates: Sequence[Dict[str, Any]] | None,
    compose_products: Sequence[Dict[str, Any]] | None = None,
    executor_products: Sequence[Dict[str, Any]] | None = None,
    resolved_product: Optional[Dict[str, Any]] = None,
    catalog_product_ids: Sequence[Any] | None = None,
) -> List[Dict[str, Any]]:
    """
    Resolve the product rows used for browse presentation cardinality.

    Contract:
    * Prefer identity-bearing display/compose rows (drop title-only junk for count).
    * Never invent a card from an id alone.
    * If the display slice is empty but executor still has a single identified
      product (or ``resolved_product``) matching a singleton catalog id, recover
      that row so persona-success cannot skip SINGLE_RICH by accident.
    * If 2+ identified products remain → keep MULTI (do not collapse to one id).
    """
    display_rows = [
        dict(p) for p in (display_candidates or []) if isinstance(p, dict)
    ]
    compose_rows = [
        dict(p) for p in (compose_products or []) if isinstance(p, dict)
    ]
    executor_rows = [
        dict(p) for p in (executor_products or []) if isinstance(p, dict)
    ]

    pool = display_rows or compose_rows
    identified = [p for p in pool if _has_catalog_identity(p)]
    if len(identified) >= 2:
        return identified
    if len(identified) == 1:
        return identified

    # Title-only / empty display — attempt singleton recovery from executor truth.
    id_hints = [
        str(x).strip()
        for x in (catalog_product_ids or [])
        if str(x or "").strip()
    ]
    singleton_hint = id_hints[0] if len(id_hints) == 1 else ""

    focus = resolved_product if isinstance(resolved_product, dict) else None
    if focus and _has_catalog_identity(focus):
        focus_tokens = _product_identity_tokens(focus)
        if not singleton_hint or singleton_hint in focus_tokens:
            return [dict(focus)]

    if singleton_hint:
        for row in list(executor_rows) + list(compose_rows) + list(display_rows):
            if singleton_hint in _product_identity_tokens(row):
                return [dict(row)]

    # Preserve title-only singleton so presentation can emit
    # singleton_missing_catalog_identity (no invented card).
    if len(pool) == 1:
        return [dict(pool[0])]
    return [dict(p) for p in pool]


def stamp_presentation_observability(
    result_data: Dict[str, Any],
    *,
    candidate_count: Optional[int] = None,
) -> None:
    """Narrow audit fields for production: kind/reason/counts/ids."""
    if not isinstance(result_data, dict):
        return
    cards = [
        dict(c)
        for c in (result_data.get("pending_product_cards") or [])
        if isinstance(c, dict)
    ]
    if candidate_count is not None:
        result_data["presentation_candidate_count"] = int(candidate_count)
    elif "presentation_candidate_count" not in result_data:
        pending_candidates = [
            dict(c)
            for c in (result_data.get("pending_candidates") or [])
            if isinstance(c, dict)
        ]
        result_data["presentation_candidate_count"] = len(pending_candidates)
    result_data["pending_product_card_count"] = len(cards)
    result_data["pending_product_card_ids"] = [
        c.get("id") or c.get("external_id") or c.get("product_id")
        for c in cards
    ]


def resolve_product_presentation(
    candidates: Sequence[Dict[str, Any]] | None,
    *,
    resolved_product: Optional[Dict[str, Any]] = None,
) -> ProductPresentationDecision:
    """
    Decide outbound presentation for search/discovery product results.

    * 0 candidates → none
    * 1 candidate with catalog identity → rich product presentation
    * 2+ candidates → reply-button choices (pick_N)
    """
    rows = [dict(p) for p in (candidates or []) if isinstance(p, dict)]
    count = len(rows)

    if count <= 0:
        return ProductPresentationDecision(
            kind=PRESENTATION_NONE,
            candidate_count=0,
            reason="no_candidates",
        )

    if count >= 2:
        return ProductPresentationDecision(
            kind=PRESENTATION_MULTI_CHOICES,
            candidate_count=count,
            reason="ambiguous_or_multi_candidates",
        )

    single = rows[0]
    focus = resolved_product if isinstance(resolved_product, dict) else None
    chosen = single
    if focus and _has_catalog_identity(focus):
        # Prefer the already-resolved focus when it matches the singleton.
        focus_id = str(
            focus.get("external_id")
            or focus.get("id")
            or focus.get("product_id")
            or ""
        ).strip()
        single_id = str(
            single.get("external_id")
            or single.get("id")
            or single.get("product_id")
            or ""
        ).strip()
        if not focus_id or not single_id or focus_id == single_id:
            chosen = {**single, **{k: v for k, v in focus.items() if v not in (None, "")}}

    if not _has_catalog_identity(chosen):
        # Title-only singleton cannot drive a trusted rich card; fall back to choices.
        return ProductPresentationDecision(
            kind=PRESENTATION_MULTI_CHOICES,
            candidate_count=count,
            resolved_product=chosen,
            reason="singleton_missing_catalog_identity",
        )

    return ProductPresentationDecision(
        kind=PRESENTATION_SINGLE_RICH,
        candidate_count=1,
        resolved_product=chosen,
        reason="single_resolved_catalog_identity",
    )


def build_product_card_attachment_from_catalog(
    product: Dict[str, Any],
    *,
    dispatch_source: str = DISPATCH_SOURCE_SINGLE_RESOLVED,
) -> Dict[str, Any]:
    """Shape a catalog product dict into the webhook product_card attachment."""
    from services.product_resolver import (  # noqa: PLC0415
        _dict_to_resolution,
        format_product_card_caption,
    )

    res = _dict_to_resolution(
        dict(product or {}),
        matched_query=None,
        confidence="single_resolved",
    )
    return {
        "kind": "product_card",
        "id": res.id,
        "title": res.title,
        "media_type": "image",
        "file_url": res.image_url,
        "caption": format_product_card_caption(res, include_description=False),
        "product_url": res.product_url,
        "price": res.price,
        "in_stock": res.in_stock,
        "external_id": res.external_id,
        "confidence": res.confidence,
        "needs_variant_choice": bool(getattr(res, "needs_variant_choice", False)),
        "variants": list(getattr(res, "variants", []) or []),
        "has_variants": bool(getattr(res, "has_variants", False)),
        "default_variant_retailer_id": getattr(res, "default_variant_retailer_id", None),
        "dispatch_source": dispatch_source,
        "description": res.description,
    }


def apply_search_product_presentation(
    result_data: Dict[str, Any],
    *,
    candidates: Sequence[Dict[str, Any]],
    resolved_product: Optional[Dict[str, Any]] = None,
    build_buttons: Optional[Any] = None,
) -> ProductPresentationDecision:
    """
    Mutate compose ``result.data`` with either pick buttons or a rich card stamp.

    ``build_buttons`` is a callable ``(candidates) -> list`` used only for multi.
    """
    rows = [dict(p) for p in (candidates or []) if isinstance(p, dict)]
    decision = resolve_product_presentation(
        rows,
        resolved_product=resolved_product,
    )
    result_data["product_presentation_kind"] = decision.kind
    result_data["product_presentation_reason"] = decision.reason
    result_data["presentation_candidate_count"] = int(decision.candidate_count or len(rows))

    if decision.kind == PRESENTATION_MULTI_CHOICES:
        if build_buttons is not None:
            result_data["pending_buttons"] = list(build_buttons(rows) or [])
        result_data["pending_candidates"] = list(rows)
        result_data.pop("pending_product_cards", None)
        stamp_presentation_observability(
            result_data,
            candidate_count=int(decision.candidate_count or len(rows)),
        )
        return decision

    if decision.kind == PRESENTATION_SINGLE_RICH and decision.resolved_product:
        card = build_product_card_attachment_from_catalog(decision.resolved_product)
        result_data["pending_product_cards"] = [card]
        result_data["pending_candidates"] = [dict(decision.resolved_product)]
        # Explicitly clear choices — customer must not re-pick the only product.
        result_data["pending_buttons"] = []
        stamp_presentation_observability(
            result_data,
            candidate_count=int(decision.candidate_count or 1),
        )
        return decision

    result_data.pop("pending_product_cards", None)
    result_data["pending_buttons"] = []
    result_data["pending_candidates"] = list(rows)
    stamp_presentation_observability(
        result_data,
        candidate_count=int(decision.candidate_count or len(rows)),
    )
    return decision


def build_standard_pick_buttons(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """WhatsApp reply buttons pick_1..pick_3 from candidate titles."""
    wa_buttons: List[Dict[str, Any]] = []
    for i, p in enumerate(list(candidates or [])[:3], 1):
        from core.product_button_label import (  # noqa: PLC0415
            compact_whatsapp_product_button_title,
        )

        raw_title = str((p or {}).get("title") or "")
        title = compact_whatsapp_product_button_title(raw_title)
        wa_buttons.append({
            "type": "reply",
            "reply": {"id": f"pick_{i}", "title": title or str(i)},
        })
    return wa_buttons


__all__ = [
    "DISPATCH_SOURCE_SINGLE_RESOLVED",
    "PRESENTATION_MULTI_CHOICES",
    "PRESENTATION_NONE",
    "PRESENTATION_SINGLE_RICH",
    "ProductPresentationDecision",
    "apply_search_product_presentation",
    "build_product_card_attachment_from_catalog",
    "build_standard_pick_buttons",
    "resolve_browse_presentation_candidates",
    "resolve_product_presentation",
    "stamp_presentation_observability",
]
