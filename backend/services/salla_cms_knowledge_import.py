"""
DEFERRED — Salla CMS page import helpers (NOT in Pack A1 mergeable runtime).

Source gate: Salla Merchant API does not expose GET /pages (live 404).
These helpers are preserved for a future Pack when a proven CMS source exists.
Pack A1 uses merchant-authored MerchantKnowledgeSection + GET /store/info only.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

IMPORTED_SOURCE = "imported"
SALLA_ORIGIN = "salla"
SOURCE_TYPE_CMS_PAGE = "cms_page"


def content_hash_for_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _meta_page_id(meta: Any) -> str:
    if not isinstance(meta, dict):
        return ""
    return str(
        meta.get("salla_page_id")
        or meta.get("external_page_id")
        or ""
    ).strip()


def find_imported_salla_page_section(
    db: Any,
    tenant_id: int,
    page_id: str,
) -> Any:
    """Find existing imported section for a Salla page id (tenant-scoped)."""
    from models import MerchantKnowledgeSection  # noqa: PLC0415

    page_id = str(page_id or "").strip()
    if not page_id:
        return None
    rows = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.tenant_id == int(tenant_id),
            MerchantKnowledgeSection.source == IMPORTED_SOURCE,
            MerchantKnowledgeSection.deleted_at.is_(None),
        )
        .all()
    )
    for row in rows:
        meta = getattr(row, "metadata_json", None) or {}
        if not isinstance(meta, dict):
            continue
        if str(meta.get("origin") or "").lower() != SALLA_ORIGIN:
            continue
        if str(meta.get("source_type") or "").lower() not in {
            SOURCE_TYPE_CMS_PAGE,
            "salla_cms_page",
        }:
            continue
        if _meta_page_id(meta) == page_id:
            return row
    return None


def upsert_salla_cms_page_section(
    db: Any,
    *,
    tenant_id: int,
    page_id: str,
    title: str,
    slug: str,
    kind: str,
    body: str,
    source_url: str = "",
    source_updated_at: Optional[str] = None,
    page_status: str = "active",
) -> Tuple[Any, bool, bool]:
    """Upsert one Salla CMS page into MerchantKnowledgeSection.

    Returns ``(section, created, body_rewritten)``.
    Unchanged content_hash avoids rewriting body/updated_at churn.
    """
    from models import MerchantKnowledgeSection  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    page_id = str(page_id or "").strip()
    body = str(body or "")
    digest = content_hash_for_text(body)
    now = datetime.now(timezone.utc)
    existing = find_imported_salla_page_section(db, tenant_id, page_id) if page_id else None

    meta = {
        "origin": SALLA_ORIGIN,
        "source_type": SOURCE_TYPE_CMS_PAGE,
        "imported_from": "salla_page",
        "salla_page_id": page_id,
        "external_page_id": page_id,
        "slug": slug,
        "kind": kind,
        "source_url": source_url or None,
        "content_hash": digest,
        "source_updated_at": source_updated_at,
        "last_synced_at": now.isoformat(),
        "page_status": page_status,
        "provenance": {
            "platform": "salla",
            "endpoint": "/pages",
            "tenant_id": int(tenant_id),
        },
    }

    if existing is None:
        section = MerchantKnowledgeSection(
            tenant_id=int(tenant_id),
            kind=kind,
            title=title or None,
            body=body,
            metadata_json=meta,
            priority=100,
            is_active=True,
            source=IMPORTED_SOURCE,
            ai_status="approved",
        )
        db.add(section)
        db.flush()
        return section, True, True

    prev_meta = dict(getattr(existing, "metadata_json", None) or {})
    prev_hash = str(prev_meta.get("content_hash") or "")
    body_rewritten = prev_hash != digest or str(getattr(existing, "body", "") or "") != body
    manual_deactivated = bool(prev_meta.get("manual_deactivated"))

    existing.kind = kind
    existing.title = title or existing.title
    # Respect merchant dashboard deactivation; do not force-reactivate.
    if not manual_deactivated:
        existing.is_active = True
    existing.source = IMPORTED_SOURCE
    if body_rewritten:
        existing.body = body
    # Always refresh sync provenance; keep content_hash current.
    merged_meta = {**prev_meta, **meta}
    if manual_deactivated:
        merged_meta["manual_deactivated"] = True
    existing.metadata_json = merged_meta
    flag_modified(existing, "metadata_json")
    if body_rewritten:
        existing.updated_at = now
    db.flush()
    return existing, False, body_rewritten


def deactivate_missing_salla_pages(
    db: Any,
    *,
    tenant_id: int,
    seen_page_ids: Set[str],
) -> int:
    """Mark imported Salla CMS sections inactive when absent from a complete reconcile."""
    from models import MerchantKnowledgeSection  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    rows = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.tenant_id == int(tenant_id),
            MerchantKnowledgeSection.source == IMPORTED_SOURCE,
            MerchantKnowledgeSection.deleted_at.is_(None),
            MerchantKnowledgeSection.is_active.is_(True),
        )
        .all()
    )
    deactivated = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        meta = dict(getattr(row, "metadata_json", None) or {})
        if str(meta.get("origin") or "").lower() != SALLA_ORIGIN:
            continue
        if str(meta.get("source_type") or "").lower() not in {
            SOURCE_TYPE_CMS_PAGE,
            "salla_cms_page",
        }:
            continue
        page_id = _meta_page_id(meta)
        if not page_id:
            continue
        if page_id in seen_page_ids:
            continue
        row.is_active = False
        meta["last_deactivated_at"] = now.isoformat()
        meta["deactivated_reason"] = "missing_from_successful_reconciliation"
        row.metadata_json = meta
        flag_modified(row, "metadata_json")
        deactivated += 1
    if deactivated:
        db.flush()
    return deactivated


def page_index_entry_from_section(
    *,
    page_id: str,
    title: str,
    slug: str,
    kind: str,
    active: bool,
    content_hash: str,
    section_id: Optional[int],
) -> Dict[str, Any]:
    """Lightweight snapshot/index row — no long-form body."""
    return {
        "id": page_id,
        "page_id": page_id,
        "title": title,
        "slug": slug,
        "kind": kind,
        "active": bool(active),
        "content_hash": content_hash,
        "doc_ref": f"mks:{section_id}" if section_id else None,
        "knowledge_section_id": section_id,
    }


def build_policy_existence_map(
    db: Any,
    tenant_id: int,
    *,
    pages_sync_ok: Optional[bool] = None,
) -> Dict[str, Dict[str, Any]]:
    """Deprecated wrapper — Pack A1 uses PRESENT/UNKNOWN only.

    CMS completeness-based KNOWN_ABSENT is deferred. Prefer
    ``services.merchant_policy_existence.build_policy_existence_map``.
    """
    from services.merchant_policy_existence import (  # noqa: PLC0415
        build_policy_existence_map as _safe_map,
    )
    return _safe_map(db, tenant_id, pages_sync_ok=pages_sync_ok)