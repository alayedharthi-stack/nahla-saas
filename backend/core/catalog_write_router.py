"""
core.catalog_write_router
─────────────────────────
Pure decision helpers for catalog write paths (no DB, no I/O).

Phase 2: Meta import ownership — determines whether an incoming Meta
catalog row may CREATE, REFRESH, SKIP, or FLAG_CONFLICT against an
existing Nahla product row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.catalog import (
    EXTERNAL_PLATFORM_SOURCES,
    META_EXISTING_SOURCES,
    NAHLA_NATIVE_SOURCES,
    OWNERSHIP_ARCHIVED_OR_DISCONNECTED,
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED,
    OWNERSHIP_NAHLA_MANAGED_META,
    SOURCE_UNKNOWN,
    infer_ownership_mode,
    normalize_source,
)

ACTION_CREATE = "CREATE"
ACTION_REFRESH_META = "REFRESH_META"
ACTION_SKIP_PROTECTED = "SKIP_PROTECTED"
ACTION_FLAG_CONFLICT = "FLAG_CONFLICT"


@dataclass(frozen=True)
class MetaImportDecision:
    action: str
    reason: str = ""


def resolve_meta_import_action(
    existing: Optional[Any],
    incoming_meta: Dict[str, Any],
) -> MetaImportDecision:
    """Decide how Meta import should treat *existing* vs *incoming_meta*.

    *incoming_meta* must include:
      - ``meta_id``     — Meta Graph product id
      - ``retailer_id`` — Meta retailer_id / SKU

    When *existing* is ``None``, always returns ``CREATE``.
    Never raises.
    """
    if existing is None:
        return MetaImportDecision(ACTION_CREATE, "new_row")

    meta_id = str(incoming_meta.get("meta_id") or "").strip()
    retailer_id = str(incoming_meta.get("retailer_id") or "").strip()

    src = normalize_source(getattr(existing, "source", None))
    ownership = infer_ownership_mode(existing)

    if ownership == OWNERSHIP_ARCHIVED_OR_DISCONNECTED:
        return MetaImportDecision(ACTION_SKIP_PROTECTED, "archived_or_disconnected")

    if ownership == OWNERSHIP_EXTERNAL_MANAGED or src in EXTERNAL_PLATFORM_SOURCES:
        return MetaImportDecision(ACTION_SKIP_PROTECTED, "external_platform")

    if ownership == OWNERSHIP_NAHLA_MANAGED or src in NAHLA_NATIVE_SOURCES:
        return MetaImportDecision(ACTION_FLAG_CONFLICT, "nahla_native_match")

    if ownership == OWNERSHIP_NAHLA_MANAGED_META:
        return MetaImportDecision(ACTION_REFRESH_META, "nahla_managed_meta")

    if ownership == OWNERSHIP_META_READONLY or src in META_EXISTING_SOURCES:
        return MetaImportDecision(ACTION_REFRESH_META, "meta_existing")

    # Legacy rows without ownership_mode — infer from stored ids.
    if src == SOURCE_UNKNOWN:
        ext = str(getattr(existing, "external_id", None) or "").strip()
        if ext and ext == meta_id:
            return MetaImportDecision(ACTION_REFRESH_META, "legacy_meta_by_graph_id")
        if ext:
            return MetaImportDecision(ACTION_SKIP_PROTECTED, "legacy_unknown_with_external_id")
        mrid = str(getattr(existing, "meta_retailer_id", None) or "").strip()
        if mrid and mrid == retailer_id:
            return MetaImportDecision(ACTION_FLAG_CONFLICT, "legacy_unknown_retailer_match")
        return MetaImportDecision(ACTION_FLAG_CONFLICT, "legacy_unknown")

    return MetaImportDecision(ACTION_FLAG_CONFLICT, "unclassified_source")


def conflict_detail_payload(
    *,
    existing: Any,
    meta_id: str,
    retailer_id: str,
    reason: str,
) -> Dict[str, Any]:
    """JSON-safe conflict detail for ``source_conflict_detail``."""
    return {
        "reason": reason,
        "meta_id": meta_id or None,
        "retailer_id": retailer_id or None,
        "existing_source": getattr(existing, "source", None),
        "existing_external_id": getattr(existing, "external_id", None),
        "existing_meta_retailer_id": getattr(existing, "meta_retailer_id", None),
        "existing_product_id": getattr(existing, "id", None),
    }


__all__ = [
    "ACTION_CREATE",
    "ACTION_REFRESH_META",
    "ACTION_SKIP_PROTECTED",
    "ACTION_FLAG_CONFLICT",
    "MetaImportDecision",
    "resolve_meta_import_action",
    "conflict_detail_payload",
]
