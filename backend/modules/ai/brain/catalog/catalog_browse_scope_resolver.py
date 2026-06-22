"""
catalog/catalog_browse_scope_resolver.py
──────────────────────────────────────
Phase 3 — deterministic browse scope from merchant catalog intelligence groups.

Maps customer browse turns to ``ProductGroup`` evidence (slug, label,
catalog_match, membership) instead of inferring collections only from
product category strings or stale token guards.

Operational — evidence + state only; no LLM wording.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.catalog.browse_scope")

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class BrowseScopeResolution:
    matched: bool
    group_id: Optional[int] = None
    group_slug: str = ""
    group_label: str = ""
    scope_query: str = ""
    product_ids: Tuple[int, ...] = ()
    match_source: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


def _norm_token(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text).strip().lower())
    s = _DIA.sub("", s)
    s = (
        s.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
    )
    return _WS.sub(" ", s).strip()


def _group_match_candidates(group: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("slug", "label", "catalog_match"):
        raw = str(group.get(key) or "").strip()
        if not raw:
            continue
        if key == "catalog_match":
            parts = [p.strip() for p in re.split(r"[,،|/]+", raw) if p.strip()]
            out.extend(parts or [raw])
        else:
            out.append(raw)
    return out


def _text_matches_group(text: str, group: Mapping[str, Any]) -> Optional[str]:
    q_norm = _norm_token(text)
    if not q_norm:
        return None
    for candidate in _group_match_candidates(group):
        c_norm = _norm_token(candidate)
        if not c_norm:
            continue
        if c_norm == q_norm or c_norm in q_norm or q_norm in c_norm:
            return candidate
    return None


def load_merchant_catalog_groups(db: Any, tenant_id: int) -> List[Dict[str, Any]]:
    """Active merchant product groups from Phase 1 storage."""
    if db is None or tenant_id is None:
        return []
    try:
        from services.catalog_intelligence_service import read_catalog_groups  # noqa: PLC0415

        return list(read_catalog_groups(db, int(tenant_id), active_only=True) or [])
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_BROWSE_SCOPE] group_load_failed tenant=%s",
            tenant_id,
        )
        return []


def active_catalog_group_slug_from_state(state: Any) -> str:
    raw = getattr(state, "commerce_session", None) if state is not None else None
    if isinstance(raw, Mapping):
        return str(raw.get("active_catalog_group_slug") or "").strip()
    if isinstance(raw, dict):
        return str(raw.get("active_catalog_group_slug") or "").strip()
    return ""


def _group_by_slug(groups: Sequence[Mapping[str, Any]], slug: str) -> Optional[Dict[str, Any]]:
    target = _norm_token(slug)
    if not target:
        return None
    for group in groups:
        if _norm_token(str(group.get("slug") or "")) == target:
            return dict(group)
    return None


def _group_product_ids(db: Any, tenant_id: int, group_id: int) -> Tuple[int, ...]:
    try:
        from services.catalog_intelligence_service import read_group_products  # noqa: PLC0415

        items = read_group_products(db, int(tenant_id), int(group_id)) or []
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_BROWSE_SCOPE] group_items_failed tenant=%s group=%s",
            tenant_id,
            group_id,
        )
        return ()
    ids: List[int] = []
    for item in items:
        try:
            ids.append(int(item.get("product_id")))
        except (TypeError, ValueError):
            continue
    return tuple(ids)


def match_catalog_group(
    groups: Sequence[Mapping[str, Any]],
    *,
    message: str = "",
    query: str = "",
    active_group_slug: str = "",
    active_category: str = "",
) -> Optional[BrowseScopeResolution]:
    """Pick the best merchant group for this browse turn."""
    active_groups = [dict(g) for g in (groups or []) if g.get("is_active", True)]
    if not active_groups:
        return None

    locked = _group_by_slug(active_groups, active_group_slug)
    if locked:
        gid = int(locked["id"])
        return BrowseScopeResolution(
            matched=True,
            group_id=gid,
            group_slug=str(locked.get("slug") or ""),
            group_label=str(locked.get("label") or ""),
            scope_query=str(locked.get("catalog_match") or locked.get("label") or ""),
            match_source="session_slug",
            evidence={"group_id": gid, "slug": locked.get("slug")},
        )

    for candidate in (query, message):
        text = str(candidate or "").strip()
        if not text:
            continue
        for group in sorted(active_groups, key=lambda g: (g.get("priority", 100), g.get("label", ""))):
            hit = _text_matches_group(text, group)
            if hit:
                gid = int(group["id"])
                return BrowseScopeResolution(
                    matched=True,
                    group_id=gid,
                    group_slug=str(group.get("slug") or ""),
                    group_label=str(group.get("label") or ""),
                    scope_query=str(group.get("catalog_match") or group.get("label") or hit),
                    match_source="text",
                    evidence={"matched_on": hit, "group_id": gid},
                )

    cat = _norm_token(active_category)
    if cat:
        for group in active_groups:
            for candidate in _group_match_candidates(group):
                if _norm_token(candidate) == cat:
                    gid = int(group["id"])
                    return BrowseScopeResolution(
                        matched=True,
                        group_id=gid,
                        group_slug=str(group.get("slug") or ""),
                        group_label=str(group.get("label") or ""),
                        scope_query=str(group.get("catalog_match") or group.get("label") or ""),
                        match_source="session_category",
                        evidence={"group_id": gid},
                    )

    return None


def resolve_browse_scope(
    db: Any,
    tenant_id: int,
    message: str,
    query: str = "",
    *,
    active_group_slug: str = "",
    active_category: str = "",
) -> BrowseScopeResolution:
    groups = load_merchant_catalog_groups(db, tenant_id)
    matched = match_catalog_group(
        groups,
        message=message,
        query=query,
        active_group_slug=active_group_slug,
        active_category=active_category,
    )
    if matched is None:
        return BrowseScopeResolution(matched=False)

    product_ids: Tuple[int, ...] = ()
    if matched.group_id is not None:
        product_ids = _group_product_ids(db, tenant_id, matched.group_id)

    resolution = BrowseScopeResolution(
        matched=True,
        group_id=matched.group_id,
        group_slug=matched.group_slug,
        group_label=matched.group_label,
        scope_query=matched.scope_query,
        product_ids=product_ids,
        match_source=matched.match_source,
        evidence={
            **dict(matched.evidence),
            "product_count": len(product_ids),
        },
    )
    if resolution.matched:
        logger.info(
            "[CATALOG_BROWSE_SCOPE] tenant=%s slug=%r label=%r source=%s products=%d preview=%r",
            tenant_id,
            resolution.group_slug,
            resolution.group_label,
            resolution.match_source,
            len(resolution.product_ids),
            (message or query or "")[:80],
        )
        try:
            from modules.ai.brain.catalog.catalog_intelligence_telemetry import (  # noqa: PLC0415
                emit_catalog_intelligence_event,
            )

            emit_catalog_intelligence_event(
                "browse_scope",
                tenant_id=tenant_id,
                slug=resolution.group_slug,
                source=resolution.match_source,
                products=len(resolution.product_ids),
            )
        except Exception:  # noqa: BLE001
            pass
    return resolution


def match_group_by_collection_name(
    groups: Sequence[Mapping[str, Any]],
    collection_name: str,
) -> Optional[Dict[str, Any]]:
    name = str(collection_name or "").strip()
    if not name:
        return None
    norm = _norm_token(name)
    for group in groups:
        if not group.get("is_active", True):
            continue
        for candidate in _group_match_candidates(group):
            c_norm = _norm_token(candidate)
            if c_norm and (c_norm == norm or c_norm in norm or norm in c_norm):
                return dict(group)
    return None


def hydrate_group_products(
    builder: Any,
    product_ids: Sequence[int],
    *,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Return orderable catalog dicts for explicit group membership."""
    ids = [int(pid) for pid in product_ids if pid is not None]
    if not ids or builder is None:
        return []
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    rows = (
        builder.db.query(Product)
        .filter(
            Product.tenant_id == builder.tenant_id,
            Product.id.in_(ids),
        )
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    ordered = [by_id[pid] for pid in ids if pid in by_id]
    formatted = builder._filter_orderable(ordered, source="catalog_group")  # noqa: SLF001
    return list(formatted or [])[: max(1, int(limit or 12))]


def filter_products_to_merchant_group(
    products: Sequence[Mapping[str, Any]],
    *,
    product_ids: Sequence[int],
) -> List[Dict[str, Any]]:
    """Keep only products that belong to the resolved merchant group."""
    id_set = {int(pid) for pid in product_ids}
    if not id_set:
        return [dict(p) for p in products if isinstance(p, Mapping)]
    kept: List[Dict[str, Any]] = []
    for product in products or []:
        if not isinstance(product, Mapping):
            continue
        try:
            pid = int(product.get("id"))
        except (TypeError, ValueError):
            continue
        if pid in id_set:
            kept.append(dict(product))
    if kept:
        order = {pid: idx for idx, pid in enumerate(id_set)}
        kept.sort(key=lambda p: order.get(int(p.get("id") or 0), 9999))
    return kept


def stamp_catalog_group_session(state: Any, resolution: BrowseScopeResolution) -> None:
    """Persist locked merchant group scope on commerce session."""
    if state is None or not resolution.matched:
        return
    try:
        from ..commerce.commerce_conversation_guard import (  # noqa: PLC0415
            apply_commerce_session,
            load_commerce_session,
        )

        session = load_commerce_session(state)
        session.active_catalog_group_slug = resolution.group_slug
        if resolution.group_label:
            session.active_category = resolution.group_label
        apply_commerce_session(state, session)
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_SCOPE] session_stamp_failed")


__all__ = [
    "BrowseScopeResolution",
    "active_catalog_group_slug_from_state",
    "filter_products_to_merchant_group",
    "hydrate_group_products",
    "load_merchant_catalog_groups",
    "match_catalog_group",
    "match_group_by_collection_name",
    "resolve_browse_scope",
    "stamp_catalog_group_session",
]
