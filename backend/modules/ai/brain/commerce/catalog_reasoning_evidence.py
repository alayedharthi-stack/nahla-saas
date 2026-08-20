"""
Bounded catalog evidence for LLM reasoning.

Existence, stock, and checkout eligibility stay distinct. Discovery and
recommendation turns need real tenant titles even when a SKU is not
currently checkout-eligible. Checkout paths keep using orderable-only lists.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

_DEFAULT_LIMIT = 8


def _title_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(
        row.get("title")
        or row.get("name")
        or row.get("display_label")
        or ""
    ).strip()


def _row_id(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("id", "product_id", "external_id", "sku"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return _title_of(row).lower()


def _can_checkout(row: Any) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
    if "can_checkout" in row:
        return bool(row.get("can_checkout"))
    if "orderable" in row:
        return bool(row.get("orderable"))
    ext = str(row.get("external_id") or "").strip()
    if ext:
        return True
    return None


def _normalize_candidate(row: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    title = _title_of(row)
    if not title:
        return None
    item: Dict[str, Any] = {"title": title}
    pid = row.get("id") or row.get("product_id")
    if pid is not None:
        item["id"] = pid
    ext = str(row.get("external_id") or "").strip()
    if ext:
        item["external_id"] = ext
    price = row.get("price")
    if price not in (None, ""):
        item["price"] = price
    if "in_stock" in row:
        item["in_stock"] = bool(row.get("in_stock"))
    checkout = _can_checkout(row)
    if checkout is not None:
        item["can_checkout"] = checkout
    category = str(row.get("category") or row.get("category_name") or "").strip()
    if category:
        item["category"] = category
    image_url = str(
        row.get("image_url")
        or row.get("image")
        or row.get("product_image_url")
        or row.get("thumbnail_url")
        or ""
    ).strip()
    if image_url:
        item["image_url"] = image_url
    description = str(row.get("description") or row.get("body") or "").strip()
    if description:
        item["description"] = description
    variants = row.get("variants")
    if isinstance(variants, list) and variants:
        item["variants"] = list(variants)
    variants_summary = str(row.get("variants_summary") or "").strip()
    if variants_summary:
        item["variants_summary"] = variants_summary
    return item


def _extend_unique(
    dest: List[Dict[str, Any]],
    rows: Sequence[Any],
    *,
    seen: set[str],
    limit: int,
) -> None:
    for raw in rows or []:
        if len(dest) >= limit:
            return
        item = _normalize_candidate(raw)
        if not item:
            continue
        key = _row_id(item) or item["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        dest.append(item)


def _identity_of(row: Any) -> str:
    try:
        from .commerce_focus_owner import product_focus_identity  # noqa: PLC0415

        return product_focus_identity(row)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — identity probe must not block catalog facts
        if not isinstance(row, dict):
            return ""
        for key in ("id", "product_id", "external_id", "sku"):
            val = str(row.get(key) or "").strip()
            if val:
                return val
        return ""


def _merge_catalog_fact_fields(dest: Dict[str, Any], source: Any) -> Dict[str, Any]:
    out = dict(dest or {})
    item = _normalize_candidate(source) if isinstance(source, dict) else None
    if not item:
        return out
    for key in ("description", "body", "variants", "variants_summary", "title", "price"):
        if key not in out or out.get(key) in (None, "", []):
            if item.get(key) not in (None, "", []):
                out[key] = item[key]
    return out


def project_canonical_referent_catalog_facts(
    *,
    state: Any = None,
    facts: Any = None,
    merchant_context: Any = None,
    catalog_row: Any = None,
) -> Optional[Dict[str, Any]]:
    """Merge tenant-scoped catalog facts onto the canonical structured referent."""
    try:
        from .commerce_focus_owner import (  # noqa: PLC0415
            canonical_product_referent,
            has_structured_catalog_identity,
            normalize_structured_product_referent,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — referent projection must not block compose
        return None

    referent = canonical_product_referent(state) if state is not None else None
    if not has_structured_catalog_identity(referent):
        return None
    projected = dict(referent)
    rid = _identity_of(projected)
    sources: List[Any] = []
    if isinstance(catalog_row, dict) and catalog_row:
        sources.append(catalog_row)
    if facts is not None:
        sources.extend(list(getattr(facts, "discovery_products", None) or []))
        sources.extend(list(getattr(facts, "top_products", None) or []))
    ctx = merchant_context if isinstance(merchant_context, dict) else {}
    cached_proj = ctx.get("_canonical_referent_catalog_facts")
    if isinstance(cached_proj, dict) and cached_proj:
        sources.insert(0, cached_proj)
    sources.extend(list(ctx.get("products") or []))
    for row in sources:
        if not isinstance(row, dict):
            continue
        if rid and _identity_of(row) == rid:
            projected = _merge_catalog_fact_fields(projected, row)
    normalized = normalize_structured_product_referent(projected)
    return normalized or projected


def load_tenant_scoped_catalog_row(
    db: Any,
    tenant_id: Any,
    product_id: Any,
) -> Optional[Dict[str, Any]]:
    """Load one catalog row by tenant + product id. Never cross tenants."""
    if db is None or tenant_id is None or product_id in (None, ""):
        return None
    try:
        pid = int(product_id)
        tid = int(tenant_id)
    except (TypeError, ValueError):
        return None
    try:
        from models import Product  # noqa: PLC0415
        from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog hydrate is best-effort
        return None
    try:
        row = (
            db.query(Product)
            .filter(Product.tenant_id == tid, Product.id == pid)
            .first()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog hydrate must not break the turn
        return None
    if row is None:
        return None
    try:
        formatted = CatalogContextBuilder(db, tid)._format(row)  # noqa: SLF001
    except Exception:  # noqa: BLE001  # noqa: silent-ok — formatter fallback uses raw columns
        formatted = {
            "id": getattr(row, "id", None),
            "external_id": getattr(row, "external_id", None),
            "sku": getattr(row, "sku", None),
            "title": getattr(row, "title", None),
            "description": getattr(row, "description", None),
            "price": getattr(row, "price", None),
        }
    return formatted if isinstance(formatted, dict) else None


def ensure_canonical_referent_catalog_projection(
    *,
    db: Any = None,
    tenant_id: Any = None,
    state: Any = None,
    merchant_context: Any = None,
    facts: Any = None,
    bind_to_merchant_context: bool = False,
) -> Optional[Dict[str, Any]]:
    """Project the canonical referent's catalog facts into Brain context.

    Does not compose customer text. Linked merchant knowledge still flows
    through tenant overlay via ``resolve_kb_active_product_ids``.
    """
    try:
        from .commerce_focus_owner import (  # noqa: PLC0415
            canonical_product_referent,
            has_structured_catalog_identity,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — projection must not break the turn
        return None

    referent = canonical_product_referent(state) if state is not None else None
    if not has_structured_catalog_identity(referent):
        return None

    catalog_row = load_tenant_scoped_catalog_row(
        db,
        tenant_id,
        referent.get("id") or referent.get("product_id"),
    )
    projected = project_canonical_referent_catalog_facts(
        state=state,
        facts=facts,
        merchant_context=merchant_context,
        catalog_row=catalog_row,
    )
    if not projected:
        return None

    ctx = merchant_context if isinstance(merchant_context, dict) else None
    if ctx is not None and bind_to_merchant_context:
        products = [
            dict(row)
            for row in (ctx.get("products") or [])
            if isinstance(row, dict)
        ]
        rid = _identity_of(projected)
        products = [row for row in products if _identity_of(row) != rid]
        products.insert(0, dict(projected))
        ctx["products"] = products[:8]
        conversation = dict(ctx.get("conversation") or {})
        conversation["selected_product"] = dict(projected)
        ctx["conversation"] = conversation
        ctx["_canonical_referent_projected"] = True
        ctx["_canonical_referent_catalog_facts"] = dict(projected)

    return projected


def collect_catalog_reasoning_candidates(
    *,
    facts: Any = None,
    merchant_context: Any = None,
    state: Any = None,
    limit: int = _DEFAULT_LIMIT,
) -> List[Dict[str, Any]]:
    """Return a bounded, tenant-scoped catalog evidence set for compose.

    Preference order:
    1. canonical structured product referent (current focus identity)
    2. facts.discovery_products (existence-capable active catalog)
    3. facts.top_products (synced/orderable subset)
    4. merchant_context.products
    5. state.last_search_candidates / last_recommended_products
    """
    cap = max(1, min(int(limit or _DEFAULT_LIMIT), 12))
    ctx = merchant_context if isinstance(merchant_context, dict) else {}
    cached = ctx.get("_catalog_reasoning_candidates")
    if isinstance(cached, list) and cached:
        return list(cached)[:cap]
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    if state is not None:
        try:
            projected = project_canonical_referent_catalog_facts(
                state=state,
                facts=facts,
                merchant_context=merchant_context,
            )
            if projected:
                _extend_unique(out, [projected], seen=seen, limit=cap)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — referent probe must not block catalog facts
            pass

    if facts is not None:
        _extend_unique(
            out,
            list(getattr(facts, "discovery_products", None) or []),
            seen=seen,
            limit=cap,
        )
        _extend_unique(
            out,
            list(getattr(facts, "top_products", None) or []),
            seen=seen,
            limit=cap,
        )

    ctx = merchant_context if isinstance(merchant_context, dict) else {}
    _extend_unique(out, list(ctx.get("products") or []), seen=seen, limit=cap)

    if state is not None:
        _extend_unique(
            out,
            list(getattr(state, "last_search_candidates", None) or []),
            seen=seen,
            limit=cap,
        )
        _extend_unique(
            out,
            list(getattr(state, "last_recommended_products", None) or []),
            seen=seen,
            limit=cap,
        )

    if isinstance(merchant_context, dict) and out:
        merchant_context["_catalog_reasoning_candidates"] = list(out)
    return out


def catalog_reasoning_titles(
    *,
    facts: Any = None,
    merchant_context: Any = None,
    state: Any = None,
    limit: int = _DEFAULT_LIMIT,
) -> List[str]:
    return [
        str(item.get("title") or "").strip()
        for item in collect_catalog_reasoning_candidates(
            facts=facts,
            merchant_context=merchant_context,
            state=state,
            limit=limit,
        )
        if str(item.get("title") or "").strip()
    ]


__all__ = [
    "catalog_reasoning_titles",
    "collect_catalog_reasoning_candidates",
    "ensure_canonical_referent_catalog_projection",
    "load_tenant_scoped_catalog_row",
    "project_canonical_referent_catalog_facts",
]
