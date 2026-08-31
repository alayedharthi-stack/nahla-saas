"""Canonical sibling identity for Meta catalog rows.

A local parent whose literal ``retailer_id`` is missing from Meta may
share a hyphenated sibling ``{salla_product_id}-{salla_variant_id}``.
That class is ``EXISTING_CANONICAL_SIBLING`` — never ``EXISTING_EXACT``.

Titles and fuzzy names are never used. Multiple live siblings, a
foreign ``meta_item_id``, a lineage mismatch, or content mismatch
yield ``ambiguous_sibling`` (block). They never CREATE or LINK.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

IDENTITY_CANONICAL_SIBLING = "EXISTING_CANONICAL_SIBLING"
ERROR_AMBIGUOUS_SIBLING = "ambiguous_sibling"

ACTION_CREATE = "create"
ACTION_LINK = "link_canonical_sibling"
ACTION_BLOCK = "block_ambiguous_sibling"

REASON_MULTIPLE = "multiple_siblings"
REASON_LINEAGE = "lineage_mismatch"
REASON_FOREIGN_META = "foreign_meta_item"
REASON_CONTENT = "content_mismatch"
REASON_ALREADY_BOUND = "already_bound_other"
REASON_LOOKUP = "lookup_unproven"
REASON_UNPROVEN_BIND = "bound_identity_unproven"

# One local Product row ↔ one Meta item. LINK binds identity only and
# never POSTs the local row's payload onto the sibling Variant item.
CANONICAL_SIBLING_RULE = "one_local_row_one_meta_item_identity_bind_no_content_post"


def _strip(value: Any) -> str:
    return str(value or "").strip()


def canonical_sibling_retailer_ids(
    parent: Any,
    *,
    exclude_rid: str = "",
    variants: Optional[List[Any]] = None,
) -> List[str]:
    """Hyphenated ``{external_id}-{salla_variant_id}`` keys for *this* parent only."""
    current = _strip(exclude_rid)
    ext = _strip(getattr(parent, "external_id", None))
    rows = variants if variants is not None else (getattr(parent, "variants", None) or [])
    svids = {_strip(getattr(row, "salla_variant_id", None)) for row in rows}
    svids.discard("")
    ordered: List[str] = []

    def _add(rid: str) -> None:
        if not rid or rid == current or rid in ordered:
            return
        if not ext or not rid.startswith(f"{ext}-"):
            return
        suffix = rid[len(ext) + 1 :]
        if suffix not in svids:
            return
        ordered.append(rid)

    for row in rows:
        svid = _strip(getattr(row, "salla_variant_id", None))
        if ext and svid:
            _add(f"{ext}-{svid}")
        _add(_strip(getattr(row, "retailer_id", None)))
    return ordered


def legacy_identity_retailer_ids(
    parent: Any,
    *,
    exclude_rid: str,
    variants: Optional[List[Any]] = None,
) -> List[str]:
    """Broad identity keys. Canonical sibling matching must not use this list."""
    current = _strip(exclude_rid)
    ordered: List[str] = []

    def _add(value: Any) -> None:
        rid = _strip(value)
        if not rid or rid == current or rid in ordered:
            return
        ordered.append(rid)

    _add(getattr(parent, "meta_retailer_id", None))
    _add(getattr(parent, "external_id", None))
    _add(getattr(parent, "canonical_retailer_id", None))
    _add(getattr(parent, "source_external_id", None))
    ext = _strip(getattr(parent, "external_id", None))
    rows = variants if variants is not None else (getattr(parent, "variants", None) or [])
    for variant in rows:
        stored = _strip(getattr(variant, "retailer_id", None))
        _add(stored)
        svid = _strip(getattr(variant, "salla_variant_id", None))
        _add(svid)
        if ext and svid:
            _add(f"{ext}-{svid}")
    return ordered[:12]


def live_canonical_sibling_hits(
    parent: Any,
    live_retailer_ids: Any,
    *,
    current_rid: str = "",
    variants: Optional[List[Any]] = None,
) -> List[str]:
    """Canonical sibling rids that are present in the live catalog key set."""
    live = {_strip(item) for item in (live_retailer_ids or []) if _strip(item)}
    return [
        rid for rid in canonical_sibling_retailer_ids(
            parent, exclude_rid=current_rid, variants=variants,
        )
        if rid in live
    ]


def existing_identity_retailer_id(
    parent: Any,
    live_retailer_ids: Any,
    *,
    current_rid: str = "",
    variants: Optional[List[Any]] = None,
) -> Optional[str]:
    """Unique canonical sibling rid in *live_retailer_ids*, else None."""
    hits = live_canonical_sibling_hits(
        parent, live_retailer_ids, current_rid=current_rid, variants=variants,
    )
    if len(hits) == 1:
        return hits[0]
    return None


def parent_would_create_in_meta(
    parent: Any,
    live_retailer_ids: Any,
    *,
    variants: Optional[List[Any]] = None,
) -> bool:
    """True only when no canonical sibling rid is live (unique or ambiguous)."""
    return not live_canonical_sibling_hits(
        parent, live_retailer_ids, current_rid="", variants=variants,
    )


def _norm_availability(value: Any) -> str:
    text = _strip(value).lower().replace("_", " ")
    if text in {"in stock", "available"}:
        return "in stock"
    if text in {"out of stock", "unavailable"}:
        return "out of stock"
    return text


def _norm_currency(value: Any) -> str:
    return _strip(value).upper()


def _price_minor_units(value: Any) -> Optional[int]:
    """Normalize to integer minor units. Ints stay minor; dotted strings are major."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    text = _strip(value).replace(",", "")
    if not text:
        return None
    try:
        if "." in text:
            major = float(text)
            minor = round(major * 100)
            if abs(major * 100 - minor) > 1e-6:
                return None
            return int(minor)
        return int(text)
    except (TypeError, ValueError):
        return None


def _prices_compatible(local: Any, live: Any) -> bool:
    left = _price_minor_units(local)
    right = _price_minor_units(live)
    return left is not None and right is not None and left == right


def _norm_url(value: Any) -> str:
    raw = _strip(value)
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        query = parsed.query
        base = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
        return f"{base}?{query}" if query else base
    return raw.rstrip("/")


def _is_meta_cdn_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return (
        host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fbcdn.net"
        or host.endswith(".fbcdn.net")
    )


def _url_origin_path(url: str) -> str:
    parsed = urlparse(_strip(url))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _images_compatible(local: Any, live: Any) -> bool:
    local_url = _strip(local)
    live_url = _strip(live)
    if not local_url or not live_url:
        return False
    if _norm_url(local_url) == _norm_url(live_url):
        return True
    local_cdn = _is_meta_cdn_host(local_url)
    live_cdn = _is_meta_cdn_host(live_url)
    if local_cdn and live_cdn:
        return _url_origin_path(local_url) == _url_origin_path(live_url)
    return local_cdn != live_cdn


def sibling_content_mismatches(
    local_payload: Optional[Dict[str, Any]],
    live_item: Optional[Dict[str, Any]],
) -> List[str]:
    """Compare operational fields. Name is ignored and is never evidence."""
    local = dict(local_payload or {})
    live = dict(live_item or {})
    mismatches: List[str] = []
    if not _prices_compatible(local.get("price"), live.get("price")):
        mismatches.append("price")
    if _norm_currency(local.get("currency")) != _norm_currency(live.get("currency")):
        mismatches.append("currency")
    if _norm_availability(local.get("availability")) != _norm_availability(live.get("availability")):
        mismatches.append("availability")
    local_url = _norm_url(local.get("url") or local.get("product_url"))
    live_url = _norm_url(live.get("url") or live.get("product_url"))
    if not local_url or not live_url or local_url != live_url:
        mismatches.append("url")
    if not _images_compatible(local.get("image_url"), live.get("image_url")):
        mismatches.append("image_url")
    return mismatches


def occupied_active_meta_item_ids(
    rows: Iterable[Any],
    *,
    exclude_product_id: int = 0,
) -> Dict[str, int]:
    """Map Meta item id → other active local Product.id."""
    from core.catalog import CATALOG_STATUS_ACTIVE, catalog_status_of

    occupied: Dict[str, int] = {}
    exclude = int(exclude_product_id or 0)
    for row in rows or []:
        row_id = int(getattr(row, "id", 0) or 0)
        if not row_id or row_id == exclude:
            continue
        if catalog_status_of(row) != CATALOG_STATUS_ACTIVE:
            continue
        mid = _strip(getattr(row, "meta_item_id", None))
        if mid:
            occupied[mid] = row_id
    return occupied


@dataclass
class CanonicalSiblingDecision:
    action: str
    identity_class: Optional[str] = None
    error: Optional[str] = None
    reason: Optional[str] = None
    sibling_retailer_id: Optional[str] = None
    meta_product_id: Optional[str] = None
    content_mismatches: List[str] = field(default_factory=list)
    idempotent: bool = False
    canonical_rule: Optional[str] = None

    @property
    def allow_link(self) -> bool:
        return self.action == ACTION_LINK

    @property
    def allow_create(self) -> bool:
        return self.action == ACTION_CREATE


def evaluate_canonical_sibling_bind(
    parent: Any,
    *,
    current_rid: str = "",
    variants: Optional[List[Any]] = None,
    live_by_rid: Optional[Dict[str, Dict[str, Any]]] = None,
    occupied_meta_item_ids: Optional[Dict[str, int]] = None,
    sibling_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
) -> CanonicalSiblingDecision:
    """Decide CREATE / LINK / BLOCK for a missing literal retailer_id."""
    live_by_rid = dict(live_by_rid or {})
    occupied = {
        _strip(mid): int(pid)
        for mid, pid in dict(occupied_meta_item_ids or {}).items()
        if _strip(mid)
    }
    payloads = dict(sibling_payloads or {})
    parent_id = int(getattr(parent, "id", 0) or 0)
    already = _strip(getattr(parent, "meta_item_id", None))

    candidates = canonical_sibling_retailer_ids(
        parent, exclude_rid=current_rid, variants=variants,
    )
    hits = [rid for rid in candidates if rid in live_by_rid]
    if not hits:
        if already:
            return CanonicalSiblingDecision(
                action=ACTION_BLOCK,
                error=ERROR_AMBIGUOUS_SIBLING,
                reason=REASON_UNPROVEN_BIND,
                meta_product_id=already,
            )
        return CanonicalSiblingDecision(action=ACTION_CREATE)

    src = _strip(getattr(parent, "source", None)).lower()
    if src != "salla":
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_LINEAGE,
            sibling_retailer_id=hits[0],
        )
    if len(hits) > 1:
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_MULTIPLE,
            sibling_retailer_id=hits[0],
        )

    sibling_rid = hits[0]
    ext = _strip(getattr(parent, "external_id", None))
    rows = variants if variants is not None else (getattr(parent, "variants", None) or [])
    svids = {_strip(getattr(row, "salla_variant_id", None)) for row in rows}
    suffix = sibling_rid[len(ext) + 1 :] if ext and sibling_rid.startswith(f"{ext}-") else ""
    if not ext or not suffix or suffix not in svids:
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_LINEAGE,
            sibling_retailer_id=sibling_rid,
        )

    live_item = dict(live_by_rid.get(sibling_rid) or {})
    live_rid = _strip(live_item.get("retailer_id"))
    if live_rid and live_rid != sibling_rid:
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_LINEAGE,
            sibling_retailer_id=sibling_rid,
        )
    meta_id = _strip(live_item.get("id") or live_item.get("meta_product_id"))
    if not meta_id:
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_LOOKUP,
            sibling_retailer_id=sibling_rid,
        )

    if already and already != meta_id:
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_ALREADY_BOUND,
            sibling_retailer_id=sibling_rid,
            meta_product_id=meta_id,
        )

    owner = occupied.get(meta_id)
    if owner is not None and owner != parent_id:
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_FOREIGN_META,
            sibling_retailer_id=sibling_rid,
            meta_product_id=meta_id,
        )

    mismatches = sibling_content_mismatches(payloads.get(sibling_rid), live_item)
    if mismatches:
        return CanonicalSiblingDecision(
            action=ACTION_BLOCK,
            error=ERROR_AMBIGUOUS_SIBLING,
            reason=REASON_CONTENT,
            sibling_retailer_id=sibling_rid,
            meta_product_id=meta_id,
            content_mismatches=mismatches,
        )

    return CanonicalSiblingDecision(
        action=ACTION_LINK,
        identity_class=IDENTITY_CANONICAL_SIBLING,
        sibling_retailer_id=sibling_rid,
        meta_product_id=meta_id,
        idempotent=bool(already and already == meta_id),
        canonical_rule=CANONICAL_SIBLING_RULE,
    )
