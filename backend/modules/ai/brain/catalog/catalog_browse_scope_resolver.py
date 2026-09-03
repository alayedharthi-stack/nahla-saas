"""
catalog/catalog_browse_scope_resolver.py
──────────────────────────────────────
Phase 3 — deterministic browse scope from merchant catalog intelligence groups.

Maps customer browse turns to ``ProductGroup`` evidence (slug, label,
catalog_match, membership) instead of inferring collections only from
product category strings or stale token guards.

Group lock requires current-turn structured scope: the turn must uniquely
name exactly one merchant group by label or slug identity, or continue a
previously selected group when this turn introduces no new catalog subject.
catalog_match is recall-only and cannot exclusive-lock a child group.
A broad family token must not lock a more specific child group.

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
class CatalogCategoryScope:
    intent: str = ""
    matched_category: str = ""
    category_id: str = ""
    catalog_group_id: Optional[int] = None
    query_subject: str = ""
    must_filter_by_category: bool = False
    use_catalog_prices_only: bool = False
    specific_product: bool = False
    product_ids: Tuple[int, ...] = ()
    match_source: str = ""


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


def _identity_specifiers(group: Mapping[str, Any]) -> List[str]:
    """Label and slug only — catalog_match is recall, not exclusive ownership."""
    out: List[str] = []
    for key in ("label", "slug"):
        raw = str(group.get(key) or "").strip()
        if raw:
            out.append(raw)
    return out


def _identity_named(specifier: str, text: str) -> bool:
    """True when current-turn text names this group label/slug identity.

    Definite-article variants of the same noun count as the same identity.
    A shorter family token must not count as naming a longer child label.
    """
    c_norm = _norm_token(specifier)
    q_norm = _norm_token(text)
    if not c_norm or not q_norm:
        return False
    if c_norm == q_norm or c_norm in q_norm:
        return True
    if c_norm.startswith("ال") and c_norm[2:] == q_norm:
        return True
    if q_norm.startswith("ال") and q_norm[2:] == c_norm:
        return True
    return False


def _best_identity_hit(
    text: str,
    group: Mapping[str, Any],
) -> Optional[Tuple[int, str]]:
    """Longest label/slug alias of THIS group named by text. Not catalog_match."""
    best: Optional[Tuple[int, str]] = None
    for candidate in _identity_specifiers(group):
        if not _identity_named(candidate, text):
            continue
        scored = (len(_norm_token(candidate)), candidate)
        if best is None or scored[0] > best[0]:
            best = scored
    return best


def _best_covered_specifier(
    text: str,
    group: Mapping[str, Any],
) -> Optional[Tuple[int, str]]:
    """Recall overlap including catalog_match — not exclusive ownership."""
    q_norm = _norm_token(text)
    if not q_norm:
        return None
    best: Optional[Tuple[int, str]] = None
    for candidate in _group_match_candidates(group):
        c_norm = _norm_token(candidate)
        if not c_norm:
            continue
        if c_norm == q_norm or c_norm in q_norm:
            scored = (len(c_norm), candidate)
            if best is None or scored[0] > best[0]:
                best = scored
    return best


def _drop_nested_named_groups(
    named: Sequence[Tuple[Dict[str, Any], str]],
) -> List[Tuple[Dict[str, Any], str]]:
    """Drop a named group whose identity is only a prefix of another named group.

    Sibling groups whose labels do not contain each other stay independent.
    Length is not used to pick a winner among independent groups.
    """
    kept: List[Tuple[Dict[str, Any], str]] = []
    norms = [(item, _norm_token(item[1])) for item in named]
    for item, item_norm in norms:
        if not item_norm:
            continue
        nested = False
        for other, other_norm in norms:
            if other[0] is item[0] or int(other[0]["id"]) == int(item[0]["id"]):
                continue
            if item_norm != other_norm and item_norm in other_norm:
                nested = True
                break
        if not nested:
            kept.append(item)
    return kept


def _explicitly_named_groups(
    groups: Sequence[Mapping[str, Any]],
    text: str,
) -> List[Tuple[Dict[str, Any], str]]:
    by_id: Dict[int, Tuple[Dict[str, Any], str]] = {}
    for group in groups:
        hit = _best_identity_hit(text, group)
        if hit is None:
            continue
        gid = int(group["id"])
        prev = by_id.get(gid)
        if prev is None or hit[0] > len(_norm_token(prev[1])):
            by_id[gid] = (dict(group), hit[1])
    return _drop_nested_named_groups(list(by_id.values()))


def _unique_named_group_from_text(
    groups: Sequence[Mapping[str, Any]],
    *texts: str,
) -> Optional[Tuple[Dict[str, Any], str]]:
    """Exclusive lock only when current-turn text names exactly one group id."""
    by_id: Dict[int, Tuple[Dict[str, Any], str]] = {}
    for text in texts:
        blob = str(text or "").strip()
        if not blob:
            continue
        for group, hit in _explicitly_named_groups(groups, blob):
            gid = int(group["id"])
            prev = by_id.get(gid)
            if prev is None or len(_norm_token(hit)) > len(_norm_token(prev[1])):
                by_id[gid] = (group, hit)
    independent = _drop_nested_named_groups(list(by_id.values()))
    if len(independent) != 1:
        return None
    return independent[0]


def _current_turn_names_group(text: str, group: Mapping[str, Any]) -> bool:
    """True when current-turn text names this group's label or slug."""
    return _best_identity_hit(text, group) is not None


def _text_matches_group(text: str, group: Mapping[str, Any]) -> Optional[str]:
    hit = _best_identity_hit(text, group)
    return hit[1] if hit is not None else None


def _resolution_from_group(
    group: Mapping[str, Any],
    *,
    match_source: str,
    hit: str = "",
    current_turn_group_scope: bool = False,
    session_continuation: bool = False,
) -> BrowseScopeResolution:
    gid = int(group["id"])
    evidence: Dict[str, Any] = {
        "group_id": gid,
        "slug": group.get("slug"),
        "current_turn_group_scope": bool(current_turn_group_scope),
        "session_continuation": bool(session_continuation),
    }
    if hit:
        evidence["matched_on"] = hit
    return BrowseScopeResolution(
        matched=True,
        group_id=gid,
        group_slug=str(group.get("slug") or ""),
        group_label=str(group.get("label") or ""),
        scope_query=str(group.get("catalog_match") or group.get("label") or hit or ""),
        match_source=match_source,
        evidence=evidence,
    )


def _extract_current_turn_subject(message: str, query: str) -> str:
    subject = str(query or "").strip()
    if subject:
        return subject
    try:
        from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            extract_browse_category_scope,
        )

        return str(extract_browse_category_scope(message or "", query or "") or "").strip()
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_SCOPE] subject_extract_failed")
        return ""


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


def group_by_db_id(groups: Sequence[Mapping[str, Any]], group_id: int) -> Optional[Dict[str, Any]]:
    """Resolve a merchant group by integer ProductGroup.id — no fuzzy name match."""
    try:
        target = int(group_id)
    except (TypeError, ValueError):
        return None
    for group in groups:
        if not group.get("is_active", True):
            continue
        try:
            if int(group.get("id")) == target:
                return dict(group)
        except (TypeError, ValueError):
            continue
    return None


def read_group_membership_ids(db: Any, tenant_id: int, group_id: int) -> Tuple[int, ...]:
    """ProductGroupItem membership for a group — identity lookup only."""
    return _group_product_ids(db, tenant_id, int(group_id))


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


def _category_labels_match(subject: str, candidate: str) -> bool:
    s_norm = _norm_token(subject)
    c_norm = _norm_token(candidate)
    if not s_norm or not c_norm:
        return False
    if c_norm == s_norm or c_norm in s_norm or s_norm in c_norm:
        return True
    if s_norm.startswith("ال") and s_norm[2:] == c_norm:
        return True
    if c_norm.startswith("ال") and c_norm[2:] == s_norm:
        return True
    return False


def _load_snapshot_categories(db: Any, tenant_id: int) -> List[str]:
    if db is None or tenant_id is None:
        return []
    try:
        from core.store_knowledge import StoreKnowledgeLoader  # noqa: PLC0415

        summary = StoreKnowledgeLoader(db, int(tenant_id)).catalog_summary() or {}
        raw = summary.get("categories") or []
        out: List[str] = []
        for item in raw:
            label = str(item or "").strip()
            if label:
                out.append(label)
        return out
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_BROWSE_SCOPE] snapshot_categories_failed tenant=%s",
            tenant_id,
        )
        return []


def _load_product_metadata_categories(db: Any, tenant_id: int) -> List[str]:
    if db is None or tenant_id is None:
        return []
    try:
        from database.models import Product  # noqa: PLC0415

        rows = (
            db.query(Product.extra_metadata)
            .filter(Product.tenant_id == int(tenant_id))
            .limit(500)
            .all()
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_BROWSE_SCOPE] metadata_categories_failed tenant=%s",
            tenant_id,
        )
        return []
    out: List[str] = []
    seen: set[str] = set()
    for (meta,) in rows:
        if not isinstance(meta, dict):
            continue
        cat = meta.get("category")
        if isinstance(cat, str) and cat.strip():
            label = cat.strip()
            key = _norm_token(label)
            if key and key not in seen:
                seen.add(key)
                out.append(label)
        elif isinstance(cat, list):
            for entry in cat:
                if isinstance(entry, str) and entry.strip():
                    label = entry.strip()
                    key = _norm_token(label)
                    if key and key not in seen:
                        seen.add(key)
                        out.append(label)
    return out


def resolve_catalog_category_scope(
    db: Any,
    tenant_id: int,
    user_text: str,
    query: str = "",
    *,
    active_group_slug: str = "",
    active_category: str = "",
) -> CatalogCategoryScope:
    """Resolve a category-scoped browse turn against merchant catalog evidence."""
    try:
        from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            extract_browse_category_scope,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_SCOPE] subject_extract_import_failed")
        return CatalogCategoryScope(specific_product=True)

    subject = extract_browse_category_scope(user_text or "", query or "")
    if not subject:
        return CatalogCategoryScope(specific_product=True)

    groups = load_merchant_catalog_groups(db, tenant_id)
    group_match = match_catalog_group(
        groups,
        message=user_text or "",
        query=subject,
        active_group_slug=active_group_slug,
        active_category=active_category,
    )
    if group_match is not None and group_match.matched:
        product_ids: Tuple[int, ...] = ()
        if group_match.group_id is not None:
            product_ids = _group_product_ids(db, tenant_id, int(group_match.group_id))
        return CatalogCategoryScope(
            intent="category_price_browse",
            matched_category=str(group_match.group_label or subject),
            category_id=str(group_match.group_slug or ""),
            catalog_group_id=group_match.group_id,
            query_subject=subject,
            must_filter_by_category=True,
            use_catalog_prices_only=True,
            specific_product=False,
            product_ids=product_ids,
            match_source=str(group_match.match_source or "product_group"),
        )

    s_norm = _norm_token(subject)
    label_hits: List[Tuple[int, str, str]] = []
    for source_name, labels in (
        ("snapshot_category", _load_snapshot_categories(db, tenant_id)),
        ("product_metadata_category", _load_product_metadata_categories(db, tenant_id)),
    ):
        for label in labels:
            c_norm = _norm_token(label)
            if not c_norm or not s_norm:
                continue
            if c_norm == s_norm or c_norm in s_norm:
                label_hits.append((len(c_norm), source_name, label))
    if label_hits:
        max_len = max(item[0] for item in label_hits)
        winners = [item for item in label_hits if item[0] == max_len]
        unique_labels = {item[2] for item in winners}
        if len(unique_labels) == 1:
            _hit_len, source_name, label = winners[0]
            return CatalogCategoryScope(
                intent="category_price_browse",
                matched_category=label,
                category_id=_norm_token(label),
                query_subject=subject,
                must_filter_by_category=True,
                use_catalog_prices_only=True,
                specific_product=False,
                match_source=source_name,
            )

    try:
        from ..product_discovery_gate import is_generic_category_noun  # noqa: PLC0415

        if is_generic_category_noun(subject):
            # Family noun without a uniquely named child group is broad
            # discovery, not an exclusive category lock.
            return CatalogCategoryScope(
                intent="category_price_browse",
                matched_category=subject,
                category_id=_norm_token(subject),
                query_subject=subject,
                must_filter_by_category=False,
                use_catalog_prices_only=True,
                specific_product=False,
                match_source="subject_token",
            )
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_SCOPE] generic_category_probe_failed")

    return CatalogCategoryScope(
        query_subject=subject,
        specific_product=True,
    )


def filter_products_by_category_metadata(
    products: Sequence[Mapping[str, Any]],
    *,
    category: str,
) -> List[Dict[str, Any]]:
    """Keep products whose structured category matches the resolved scope."""
    label = str(category or "").strip()
    if not label:
        return [dict(p) for p in products if isinstance(p, Mapping)]
    kept: List[Dict[str, Any]] = []
    for product in products or []:
        if not isinstance(product, Mapping):
            continue
        product_category = str(product.get("category") or "").strip()
        if product_category and _category_labels_match(label, product_category):
            kept.append(dict(product))
    return kept


def match_catalog_group(
    groups: Sequence[Mapping[str, Any]],
    *,
    message: str = "",
    query: str = "",
    active_group_slug: str = "",
    active_category: str = "",
) -> Optional[BrowseScopeResolution]:
    """Pick a merchant group only when current-turn identity names exactly one.

    Exclusive lock requires distinct named group ids == 1 (label/slug identity).
    catalog_match overlap is not ownership. Stale session slug/category may
    continue only when this turn does not introduce a different catalog subject.
    """
    active_groups = [dict(g) for g in (groups or []) if g.get("is_active", True)]
    if not active_groups:
        return None

    locked = _group_by_slug(active_groups, active_group_slug)
    text_hit = _unique_named_group_from_text(active_groups, query, message)

    if locked is not None:
        if text_hit is not None:
            hit_group, hit_str = text_hit
            if int(hit_group["id"]) == int(locked["id"]):
                return _resolution_from_group(
                    locked,
                    match_source="text",
                    hit=hit_str,
                    current_turn_group_scope=True,
                    session_continuation=False,
                )
            return _resolution_from_group(
                hit_group,
                match_source="text",
                hit=hit_str,
                current_turn_group_scope=True,
                session_continuation=False,
            )
        if _current_turn_names_group(query or "", locked) or _current_turn_names_group(
            message or "", locked
        ):
            return _resolution_from_group(
                locked,
                match_source="text",
                current_turn_group_scope=True,
                session_continuation=False,
            )
        subject = _extract_current_turn_subject(message, query)
        if subject and any(
            _best_covered_specifier(subject, group) is not None
            for group in active_groups
        ):
            return None
        return _resolution_from_group(
            locked,
            match_source="session_slug",
            current_turn_group_scope=False,
            session_continuation=True,
        )

    if text_hit is not None:
        hit_group, hit_str = text_hit
        return _resolution_from_group(
            hit_group,
            match_source="text",
            hit=hit_str,
            current_turn_group_scope=True,
            session_continuation=False,
        )

    cat = _norm_token(active_category)
    if cat:
        subject = _extract_current_turn_subject(message, query)
        if subject and not _identity_named(active_category, subject) and not _identity_named(
            active_category, message or ""
        ):
            return None
        for group in active_groups:
            for candidate in _identity_specifiers(group):
                if _norm_token(candidate) == cat:
                    return _resolution_from_group(
                        group,
                        match_source="session_category",
                        current_turn_group_scope=False,
                        session_continuation=True,
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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break browse scope
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
    "CatalogCategoryScope",
    "active_catalog_group_slug_from_state",
    "filter_products_by_category_metadata",
    "filter_products_to_merchant_group",
    "group_by_db_id",
    "hydrate_group_products",
    "load_merchant_catalog_groups",
    "match_catalog_group",
    "match_group_by_collection_name",
    "read_group_membership_ids",
    "resolve_browse_scope",
    "resolve_catalog_category_scope",
    "stamp_catalog_group_session",
]
