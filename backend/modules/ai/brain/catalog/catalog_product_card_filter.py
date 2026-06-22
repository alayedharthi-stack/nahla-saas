"""
catalog/catalog_product_card_filter.py
──────────────────────────────────────
Phase 4 — filter outbound product cards against catalog intelligence scope.

When a merchant has configured product groups (Phase 1) and browse scope
is active (Phase 3), product cards must not leak SKUs outside the resolved
group membership or cross-category guard boundaries.

Operational — evidence + state only; explicit per-product requests bypass
group membership drops when the customer named that SKU in the turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.catalog.product_card_filter")


@dataclass(frozen=True)
class ProductCardFilterResult:
    attachments: List[Dict[str, Any]]
    dropped: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)


def _attachment_product_id(attachment: Mapping[str, Any]) -> Optional[int]:
    try:
        return int(attachment.get("id"))
    except (TypeError, ValueError):
        return None


def _load_products_for_attachments(
    db: Any,
    tenant_id: int,
    attachments: Sequence[Mapping[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    ids: List[int] = []
    for att in attachments or []:
        pid = _attachment_product_id(att)
        if pid is not None:
            ids.append(pid)
    if not ids or db is None:
        return {}
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}

    rows = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id), Product.id.in_(sorted(set(ids))))
        .all()
    )
    out: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        meta = dict(getattr(row, "extra_metadata", None) or {})
        category = str(meta.get("category") or meta.get("product_type") or "").strip()
        tags = meta.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        elif not isinstance(tags, list):
            tags = []
        out[int(row.id)] = {
            "id": int(row.id),
            "title": str(getattr(row, "title", "") or ""),
            "category": category,
            "tags": tags,
        }
    return out


def _attachment_explicitly_requested(message: str, attachment_title: str) -> bool:
    title = str(attachment_title or "").strip()
    if not title:
        return False
    try:
        from ..commerce.product_visual import (  # noqa: PLC0415
            attachment_matches_turn_request,
            extract_visual_product_query,
        )

        explicit = extract_visual_product_query(message or "")
        if explicit:
            ok, _reason = attachment_matches_turn_request(
                inbound_message=message or "",
                attachment_title=title,
            )
            return ok
    except Exception:  # noqa: BLE001
        logger.exception("[PRODUCT_CARD_FILTER] explicit_request_probe_failed")
    return False


def filter_product_card_attachments(
    attachments: Sequence[Mapping[str, Any]],
    *,
    db: Any,
    tenant_id: int,
    message: str = "",
    query: str = "",
    source: str = "",
    state: Any = None,
    brain_state: Optional[Mapping[str, Any]] = None,
) -> ProductCardFilterResult:
    """Drop product cards outside merchant group scope or category guard."""
    items = [dict(a) for a in (attachments or []) if isinstance(a, Mapping)]
    if not items or db is None or tenant_id is None:
        return ProductCardFilterResult(attachments=items)

    from .catalog_browse_scope_resolver import (  # noqa: PLC0415
        active_catalog_group_slug_from_state,
        load_merchant_catalog_groups,
        resolve_browse_scope,
    )
    from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
        active_category_from_state,
        resolve_browse_category_scope,
        should_exclude_cross_category_product,
    )

    groups = load_merchant_catalog_groups(db, int(tenant_id))
    if not groups:
        return ProductCardFilterResult(attachments=items, evidence={"merchant_groups": 0})

    locked_slug = active_catalog_group_slug_from_state(state)
    locked_category = active_category_from_state(state)
    if isinstance(brain_state, Mapping):
        cs = brain_state.get("commerce_session")
        if isinstance(cs, Mapping):
            locked_slug = locked_slug or str(cs.get("active_catalog_group_slug") or "").strip()
            locked_category = locked_category or str(cs.get("active_category") or "").strip()

    resolution = resolve_browse_scope(
        db,
        int(tenant_id),
        message or "",
        str(query or ""),
        active_group_slug=locked_slug,
        active_category=locked_category,
    )

    product_rows = _load_products_for_attachments(db, int(tenant_id), items)
    category_scope = resolve_browse_category_scope(
        message or "",
        str(query or ""),
        active_category=locked_category,
        source=str(source or ""),
    )

    kept: List[Dict[str, Any]] = []
    dropped = 0
    drop_reasons: Dict[str, int] = {}

    scoped_items = items
    if resolution.matched and resolution.product_ids:
        allowed_ids = set(int(x) for x in resolution.product_ids)
        membership_kept: List[Dict[str, Any]] = []
        for att in items:
            pid = _attachment_product_id(att)
            if pid is None:
                membership_kept.append(att)
                continue
            if pid in allowed_ids:
                membership_kept.append(att)
                continue
            if _attachment_explicitly_requested(message or "", str(att.get("title") or "")):
                membership_kept.append(att)
                continue
            dropped += 1
            drop_reasons["outside_group"] = drop_reasons.get("outside_group", 0) + 1
        scoped_items = membership_kept

    if category_scope:
        for att in scoped_items:
            pid = _attachment_product_id(att)
            product = product_rows.get(pid or -1) or {
                "id": pid,
                "title": str(att.get("title") or ""),
                "category": "",
                "tags": [],
            }
            if should_exclude_cross_category_product(
                product,
                scope=category_scope,
                message=message or "",
            ):
                dropped += 1
                drop_reasons["cross_category"] = drop_reasons.get("cross_category", 0) + 1
                continue
            kept.append(att)
    else:
        kept = list(scoped_items)

    if dropped:
        logger.info(
            "[PRODUCT_CARD_FILTER] tenant=%s in=%d out=%d dropped=%d "
            "group=%r category_scope=%r reasons=%s source=%r",
            tenant_id,
            len(items),
            len(kept),
            dropped,
            resolution.group_slug or "",
            category_scope or "",
            drop_reasons,
            source,
        )

    evidence = {
        "merchant_groups": len(groups),
        "group_slug": resolution.group_slug,
        "group_matched": resolution.matched,
        "category_scope": category_scope or "",
        "drop_reasons": drop_reasons,
    }
    return ProductCardFilterResult(attachments=kept, dropped=dropped, evidence=evidence)


__all__ = [
    "ProductCardFilterResult",
    "filter_product_card_attachments",
]
