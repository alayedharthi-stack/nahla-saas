"""
core/knowledge.py
─────────────────
Shared visibility helpers for ``merchant_knowledge_sections``.

Visible KB (dashboard list/search): ``deleted_at IS NULL``.
AI-visible KB (retrieval): ``deleted_at IS NULL AND is_active = true``.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Set

from core.catalog import is_catalog_active

# Merchant/admin states that must not reach customer Brain.
_NOT_CUSTOMER_AI_STATUS = frozenset({
    "needs_review",
    "pending",
    "draft",
    "rejected",
    "review",
})


def kb_row_is_ai_visible(row: Any) -> bool:
    """Deterministic visibility check mirroring AI query filters."""
    if getattr(row, "deleted_at", None) is not None:
        return False
    if not bool(getattr(row, "is_active", True)):
        return False
    status = str(getattr(row, "ai_status", "") or "").strip().lower()
    if status in _NOT_CUSTOMER_AI_STATUS:
        return False
    return True


def is_imported_document_section(row: Any) -> bool:
    """True for deferred Salla CMS imported documents (not used by A1 runtime).

    Kept for deferred CMS helpers / historical rows. Pack A1 overlay exclusion
    uses ``is_long_form_document_section`` (source-agnostic by kind).
    """
    if str(getattr(row, "source", "") or "").strip().lower() != "imported":
        return False
    meta = getattr(row, "metadata_json", None) or {}
    if not isinstance(meta, dict):
        return False
    origin = str(meta.get("origin") or "").strip().lower()
    source_type = str(meta.get("source_type") or "").strip().lower()
    if origin == "salla" and source_type in {"cms_page", "salla_cms_page"}:
        return True
    if str(meta.get("imported_from") or "").strip().lower() in {
        "salla_page",
        "salla_cms_page",
    }:
        return True
    return False


def is_long_form_document_section(row: Any) -> bool:
    """True when the section kind is long-form document/policy content.

    Source-agnostic: manual and future imports are excluded from always-on
    overlay and retrieved via capped relevance retrieval instead.
    """
    try:
        from services.merchant_document_retrieval import (  # noqa: PLC0415
            is_long_form_document_kind,
        )
        return is_long_form_document_kind(getattr(row, "kind", None))
    except Exception:  # noqa: BLE001
        kind = str(getattr(row, "kind", "") or "").strip().lower()
        return kind in {
            "store_story",
            "return_policy",
            "refund_policy",
            "exchange_policy",
            "shipping_policy",
            "terms_policy",
            "privacy_policy",
            "warranty",
            "faq",
        }


def apply_visible_kb_query_filters(query: Any, *, include_deleted: bool = False) -> Any:
    """Exclude soft-deleted rows unless ``include_deleted`` is True."""
    if include_deleted:
        return query
    return query.filter(_deleted_at_col().is_(None))


def apply_ai_visible_kb_query_filters(query: Any) -> Any:
    """Rows eligible for AI retrieval overlays and operational KB scans."""
    from sqlalchemy import or_  # noqa: PLC0415

    status_col = _ai_status_col()
    return (
        query.filter(_deleted_at_col().is_(None))
        .filter(_is_active_col().is_(True))
        .filter(
            or_(
                status_col.is_(None),
                ~status_col.in_(tuple(_NOT_CUSTOMER_AI_STATUS)),
            )
        )
    )


def apply_kb_list_query_filters(
    query: Any,
    *,
    only_active: bool = False,
    include_deleted: bool = False,
) -> Any:
    """Dashboard list/search filters."""
    query = apply_visible_kb_query_filters(query, include_deleted=include_deleted)
    if only_active:
        query = query.filter(_is_active_col().is_(True))
    return query


def _deleted_at_col() -> Any:
    from models import MerchantKnowledgeSection  # noqa: PLC0415

    return MerchantKnowledgeSection.deleted_at


def _is_active_col() -> Any:
    from models import MerchantKnowledgeSection  # noqa: PLC0415

    return MerchantKnowledgeSection.is_active


def _ai_status_col() -> Any:
    from models import MerchantKnowledgeSection  # noqa: PLC0415

    return MerchantKnowledgeSection.ai_status


def section_product_ids(section: Any) -> Set[int]:
    linked: Set[int] = set()
    for lk in getattr(section, "product_links", None) or []:
        try:
            linked.add(int(lk.product_id))
        except (TypeError, ValueError):
            continue
    return linked


def section_has_catalog_active_product(db: Any, tenant_id: int, section: Any) -> bool:
    """True when a product-scoped section has at least one catalog-active link."""
    linked = section_product_ids(section)
    if not linked:
        return True
    if db is None or not tenant_id:
        return True
    try:
        from models import Product  # noqa: PLC0415
    except Exception:
        return True
    try:
        rows = (
            db.query(Product)
            .filter(
                Product.tenant_id == int(tenant_id),
                Product.id.in_(linked),
            )
            .all()
        )
    except Exception:
        return True
    if not rows:
        return False
    return any(is_catalog_active(r) for r in rows)


def goal_product_ids_from_metadata(metadata_json: Any) -> Set[int]:
    if not isinstance(metadata_json, dict):
        return set()
    pids: Set[int] = set()
    for item in metadata_json.get("products") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("product_id")
        if raw in (None, ""):
            continue
        try:
            pids.add(int(raw))
        except (TypeError, ValueError):
            continue
    return pids


def goal_metadata_has_catalog_active_product(
    db: Any,
    tenant_id: int,
    metadata_json: Any,
) -> bool:
    """True when a goal KB entry has no product_id refs or at least one active product."""
    pids = goal_product_ids_from_metadata(metadata_json)
    if not pids:
        return True
    if db is None or not tenant_id:
        return True
    try:
        from models import Product  # noqa: PLC0415
    except Exception:
        return True
    try:
        rows = (
            db.query(Product)
            .filter(
                Product.tenant_id == int(tenant_id),
                Product.id.in_(pids),
            )
            .all()
        )
    except Exception:
        return True
    if not rows:
        return False
    return any(is_catalog_active(r) for r in rows)


def metadata_searchable_strings(metadata_json: Any) -> Iterable[str]:
    """Yield string leaf values from metadata for safe keyword search."""
    if not isinstance(metadata_json, dict):
        return
    for key in ("usage_guidance", "soft_claims", "followup_questions", "compliance"):
        raw = metadata_json.get(key)
        if isinstance(raw, str) and raw.strip():
            yield raw.strip()
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    yield item.strip()
    for item in metadata_json.get("products") or []:
        if not isinstance(item, dict):
            continue
        for field in ("ref", "title", "note", "role"):
            val = item.get(field)
            if isinstance(val, str) and val.strip():
                yield val.strip()
    for tag in metadata_json.get("goal_tags") or []:
        if isinstance(tag, str) and tag.strip():
            yield tag.strip()
