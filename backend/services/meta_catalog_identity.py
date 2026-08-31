"""Parent-scoped Meta catalog identity matching.

Exact and sibling ``retailer_id`` keys only. Titles and fuzzy name
matches are never used.
"""
from __future__ import annotations

from typing import Any, List, Optional


def legacy_identity_retailer_ids(
    parent: Any,
    *,
    exclude_rid: str,
    variants: Optional[List[Any]] = None,
) -> List[str]:
    """Strong identity keys for a parent — never name-only."""
    current = (exclude_rid or "").strip()
    ordered: List[str] = []

    def _add(value: Any) -> None:
        rid = str(value or "").strip()
        if not rid or rid == current or rid in ordered:
            return
        ordered.append(rid)

    _add(getattr(parent, "meta_retailer_id", None))
    _add(getattr(parent, "external_id", None))
    _add(getattr(parent, "canonical_retailer_id", None))
    _add(getattr(parent, "source_external_id", None))
    ext = str(getattr(parent, "external_id", None) or "").strip()
    rows = variants if variants is not None else (getattr(parent, "variants", None) or [])
    for variant in rows:
        stored = str(getattr(variant, "retailer_id", None) or "").strip()
        _add(stored)
        svid = str(getattr(variant, "salla_variant_id", None) or "").strip()
        _add(svid)
        if ext and svid:
            _add(f"{ext}-{svid}")
    return ordered[:12]


def existing_identity_retailer_id(
    parent: Any,
    live_retailer_ids: Any,
    *,
    current_rid: str = "",
    variants: Optional[List[Any]] = None,
) -> Optional[str]:
    """Return a live Meta retailer_id that already identifies *parent*."""
    live = {
        str(item).strip()
        for item in (live_retailer_ids or [])
        if str(item).strip()
    }
    current = (current_rid or "").strip()
    if current and current in live:
        return current
    for candidate in legacy_identity_retailer_ids(
        parent, exclude_rid=current, variants=variants,
    ):
        if candidate in live:
            return candidate
    return None


def parent_would_create_in_meta(
    parent: Any,
    live_retailer_ids: Any,
    *,
    variants: Optional[List[Any]] = None,
) -> bool:
    """True only when no exact or sibling retailer_id is already in Meta."""
    return existing_identity_retailer_id(
        parent, live_retailer_ids, variants=variants,
    ) is None
