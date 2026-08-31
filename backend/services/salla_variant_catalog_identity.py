"""Salla sellable catalog identity: one Meta item ↔ one local variant SKU.

A Salla parent Product is not a Meta identity. Only variants with a
non-empty ``salla_variant_id`` are pushable. Their retailer_id is the
deterministic ``{external_id}-{salla_variant_id}`` key.

The default stub (bare parent ``external_id``, no ``salla_variant_id``)
is not an independent selling identity and must never CREATE.

Missing or ambiguous variant identity → ``ambiguous_variant_identity``,
never CREATE.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

ERROR_AMBIGUOUS_VARIANT_IDENTITY = "ambiguous_variant_identity"
PROVENANCE_VARIANT_PUSH = "salla_variant_push"
PROVENANCE_LITERAL_BIND = "literal_retailer_bind"

CLASS_EXACT = "EXACT_LOCAL_IDENTITY"
CLASS_UNBOUND_VARIANT = "UNBOUND_VARIANT"
CLASS_UNBOUND_PARENT = "UNBOUND_PARENT"
CLASS_AMBIGUOUS = "AMBIGUOUS"


def _strip(value: Any) -> str:
    return str(value or "").strip()


def is_salla_source(parent: Any) -> bool:
    return _strip(getattr(parent, "source", None)).lower() == "salla"


def deterministic_variant_retailer_id(external_id: str, salla_variant_id: str) -> str:
    ext = _strip(external_id)
    svid = _strip(salla_variant_id)
    if not ext or not svid:
        return ""
    return f"{ext}-{svid}"


def is_bare_parent_retailer_id(retailer_id: str, external_id: str) -> bool:
    rid = _strip(retailer_id)
    ext = _strip(external_id)
    return bool(rid and ext and rid == ext and "-" not in rid)


@dataclass(frozen=True)
class SallaVariantIdentity:
    product_id: int
    variant_id: int
    salla_variant_id: str
    retailer_id: str
    is_default: bool


class AmbiguousVariantIdentity(ValueError):
    def __init__(self, reason: str = ERROR_AMBIGUOUS_VARIANT_IDENTITY) -> None:
        super().__init__(reason)
        self.reason = reason


def sellable_salla_identities(
    parent: Any,
    variants: Sequence[Any],
) -> List[SallaVariantIdentity]:
    """Return pushable Salla SKUs. Default stubs without salla_variant_id are omitted.

    Duplicate ``salla_variant_id`` / retailer_id values are fail-closed, never
    silently discarded.
    """
    ext = _strip(getattr(parent, "external_id", None))
    product_id = int(getattr(parent, "id", 0) or 0)
    out: List[SallaVariantIdentity] = []
    seen_rid: set[str] = set()
    seen_svid: set[str] = set()
    for row in variants or []:
        svid = _strip(getattr(row, "salla_variant_id", None))
        if not svid:
            continue
        rid = deterministic_variant_retailer_id(ext, svid)
        if not rid:
            continue
        if rid in seen_rid or svid in seen_svid:
            raise AmbiguousVariantIdentity(ERROR_AMBIGUOUS_VARIANT_IDENTITY)
        seen_rid.add(rid)
        seen_svid.add(svid)
        out.append(
            SallaVariantIdentity(
                product_id=product_id,
                variant_id=int(getattr(row, "id", 0) or 0),
                salla_variant_id=svid,
                retailer_id=rid,
                is_default=bool(getattr(row, "is_default", False)),
            )
        )
    return out


def collect_push_retailer_ids(
    db: Any,
    parent: Any,
    fallback: Optional[str],
) -> List[str]:
    """Retailer ids the drain may CREATE/UPDATE. Salla never includes the bare parent."""
    from models import ProductVariant  # noqa: PLC0415

    tenant_id = int(getattr(parent, "tenant_id", 0) or 0)
    product_id = int(getattr(parent, "id", 0) or 0)
    rows = (
        db.query(ProductVariant)
        .filter(
            ProductVariant.tenant_id == tenant_id,
            ProductVariant.product_id == product_id,
        )
        .all()
    )
    if is_salla_source(parent):
        identities = sellable_salla_identities(parent, rows)
        if not identities:
            raise AmbiguousVariantIdentity(ERROR_AMBIGUOUS_VARIANT_IDENTITY)
        return [item.retailer_id for item in identities]

    ids: List[str] = []
    seen: set[str] = set()
    for row in rows:
        rid = _strip(getattr(row, "retailer_id", None))
        if rid and rid not in seen:
            seen.add(rid)
            ids.append(rid)
    fb = _strip(fallback)
    if fb and fb not in seen:
        ids.append(fb)
    return ids


def identity_for_retailer_id(
    parent: Any,
    variants: Sequence[Any],
    retailer_id: str,
) -> Optional[SallaVariantIdentity]:
    rid = _strip(retailer_id)
    for item in sellable_salla_identities(parent, variants):
        if item.retailer_id == rid:
            return item
    return None


def classify_graph_retailer_id(
    *,
    retailer_id: str,
    external_id: str,
    variants: Sequence[Any],
    product_meta_item_id: str = "",
    graph_meta_item_id: str = "",
    membership_meta_item_id: str = "",
) -> str:
    rid = _strip(retailer_id)
    ext = _strip(external_id)
    if rid.startswith("nahla_p_"):
        if (
            _strip(product_meta_item_id)
            and _strip(graph_meta_item_id)
            and _strip(product_meta_item_id) == _strip(graph_meta_item_id)
        ):
            return CLASS_EXACT
        return CLASS_UNBOUND_PARENT if not _strip(product_meta_item_id) else CLASS_AMBIGUOUS
    if is_bare_parent_retailer_id(rid, ext):
        return CLASS_UNBOUND_PARENT
    matches = [
        row
        for row in variants or []
        if deterministic_variant_retailer_id(ext, _strip(getattr(row, "salla_variant_id", None))) == rid
    ]
    if len(matches) != 1:
        return CLASS_AMBIGUOUS
    if (
        _strip(membership_meta_item_id)
        and _strip(graph_meta_item_id)
        and _strip(membership_meta_item_id) == _strip(graph_meta_item_id)
    ):
        return CLASS_EXACT
    return CLASS_UNBOUND_VARIANT


def _local_identity_conflict(existing: Any, identity: SallaVariantIdentity) -> Optional[str]:
    """Refuse rebinding a membership onto a different local product/variant."""
    ep = getattr(existing, "product_id", None)
    ev = getattr(existing, "variant_id", None)
    es = _strip(getattr(existing, "salla_variant_id", None))
    try:
        existing_pid = int(ep) if ep not in (None, "") else 0
    except (TypeError, ValueError):
        existing_pid = 0
    try:
        existing_vid = int(ev) if ev not in (None, "") else 0
    except (TypeError, ValueError):
        existing_vid = 0
    if existing_pid and existing_pid != int(identity.product_id):
        return "product_id_immutable"
    if existing_vid and int(identity.variant_id) and existing_vid != int(identity.variant_id):
        return "variant_id_immutable"
    if es and es != identity.salla_variant_id:
        return "salla_variant_id_immutable"
    return None


def _find_membership(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    retailer_id: str,
) -> Any:
    from models import MetaCatalogMembership  # noqa: PLC0415

    return (
        db.query(MetaCatalogMembership)
        .filter(
            MetaCatalogMembership.tenant_id == int(tenant_id),
            MetaCatalogMembership.catalog_id == catalog_id,
            MetaCatalogMembership.retailer_id == retailer_id,
        )
        .first()
    )


def ensure_variant_membership_slot(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    identity: SallaVariantIdentity,
    provenance: str = PROVENANCE_VARIANT_PUSH,
) -> Dict[str, Any]:
    """Persist local identity before Graph CREATE. ``meta_item_id`` stays empty."""
    from models import MetaCatalogMembership  # noqa: PLC0415

    cid = _strip(catalog_id)
    rid = _strip(identity.retailer_id)
    if not cid or not rid or not identity.salla_variant_id:
        return {"ok": False, "error": ERROR_AMBIGUOUS_VARIANT_IDENTITY}

    existing = _find_membership(
        db, tenant_id=tenant_id, catalog_id=cid, retailer_id=rid,
    )
    now = datetime.now(timezone.utc)
    if existing is not None:
        conflict = _local_identity_conflict(existing, identity)
        if conflict:
            return {
                "ok": False,
                "error": ERROR_AMBIGUOUS_VARIANT_IDENTITY,
                "reason": conflict,
            }
        existing.verified_at = now
        existing.provenance = provenance
        return {
            "ok": True,
            "created": False,
            "meta_item_id": _strip(getattr(existing, "meta_item_id", None)),
        }

    kwargs: Dict[str, Any] = dict(
        tenant_id=int(tenant_id),
        catalog_id=cid,
        retailer_id=rid,
        product_id=int(identity.product_id),
        variant_id=int(identity.variant_id) or None,
        meta_item_id=None,
        verified_at=now,
        provenance=provenance,
    )
    if "salla_variant_id" in MetaCatalogMembership.__table__.columns:
        kwargs["salla_variant_id"] = identity.salla_variant_id
    db.add(MetaCatalogMembership(**kwargs))
    return {"ok": True, "created": True, "meta_item_id": ""}


def upsert_variant_membership(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    identity: SallaVariantIdentity,
    meta_item_id: str,
    provenance: str = PROVENANCE_VARIANT_PUSH,
) -> Dict[str, Any]:
    """Bind one Graph item to one local variant. Never changes an existing meta_item_id."""
    from models import MetaCatalogMembership  # noqa: PLC0415

    cid = _strip(catalog_id)
    rid = _strip(identity.retailer_id)
    mid = _strip(meta_item_id)
    if not cid or not rid or not mid or not identity.salla_variant_id:
        return {"ok": False, "error": ERROR_AMBIGUOUS_VARIANT_IDENTITY}

    existing = _find_membership(
        db, tenant_id=tenant_id, catalog_id=cid, retailer_id=rid,
    )
    now = datetime.now(timezone.utc)
    if existing is not None:
        conflict = _local_identity_conflict(existing, identity)
        if conflict:
            return {
                "ok": False,
                "error": ERROR_AMBIGUOUS_VARIANT_IDENTITY,
                "reason": conflict,
            }
        already = _strip(getattr(existing, "meta_item_id", None))
        if already and already != mid:
            return {
                "ok": False,
                "error": ERROR_AMBIGUOUS_VARIANT_IDENTITY,
                "reason": "meta_item_id_immutable",
                "existing_meta_item_id": already,
            }
        existing.meta_item_id = already or mid
        existing.verified_at = now
        existing.provenance = provenance
        return {
            "ok": True,
            "created": False,
            "meta_item_id": existing.meta_item_id,
            "identity_unchanged": bool(already),
        }

    collision = (
        db.query(MetaCatalogMembership)
        .filter(
            MetaCatalogMembership.tenant_id == int(tenant_id),
            MetaCatalogMembership.catalog_id == cid,
            MetaCatalogMembership.meta_item_id == mid,
        )
        .first()
    )
    if collision is not None and _strip(collision.retailer_id) != rid:
        return {
            "ok": False,
            "error": ERROR_AMBIGUOUS_VARIANT_IDENTITY,
            "reason": "meta_item_id_owned_other",
        }

    kwargs: Dict[str, Any] = dict(
        tenant_id=int(tenant_id),
        catalog_id=cid,
        retailer_id=rid,
        product_id=int(identity.product_id),
        variant_id=int(identity.variant_id) or None,
        meta_item_id=mid,
        verified_at=now,
        provenance=provenance,
    )
    if "salla_variant_id" in MetaCatalogMembership.__table__.columns:
        kwargs["salla_variant_id"] = identity.salla_variant_id
    db.add(MetaCatalogMembership(**kwargs))
    return {"ok": True, "created": True, "meta_item_id": mid, "identity_unchanged": False}


def _graph_price_minor(value: Any) -> str:
    """Normalize a Graph price to minor-unit string.

    Raw ints are already minor units. Decimal display strings are major.
    Digit-only strings are treated as Graph minor units.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(int(value))
    text = _strip(value)
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    from services.meta_catalog_export import meta_price_minor_units  # noqa: PLC0415

    minor = meta_price_minor_units(value)
    return str(minor) if minor is not None else ""


def _local_price_minor(value: Any) -> str:
    from services.meta_catalog_export import meta_price_minor_units  # noqa: PLC0415

    minor = meta_price_minor_units(value)
    return str(minor) if minor is not None else ""


def content_signature_from_graph(item: Any) -> Dict[str, str]:
    row = item if isinstance(item, dict) else {}
    image = row.get("image_url") or row.get("image") or ""
    if isinstance(image, dict):
        image = image.get("url") or ""
    return {
        "price": _graph_price_minor(row.get("price")),
        "currency": _strip(row.get("currency")).upper(),
        "availability": _strip(row.get("availability")).lower(),
        "image_url": _strip(image),
    }


def content_signature_from_variant(variant: Any) -> Dict[str, str]:
    image = getattr(variant, "image_url", None)
    extra = getattr(variant, "extra_metadata", None) or {}
    if not image and isinstance(extra, dict):
        image = extra.get("image_url")
    availability = "in stock"
    if getattr(variant, "in_stock", True) is False:
        availability = "out of stock"
    return {
        "price": _local_price_minor(getattr(variant, "price", None)),
        "currency": _strip(getattr(variant, "currency", None) or (extra.get("currency") if isinstance(extra, dict) else "")).upper(),
        "availability": availability,
        "image_url": _strip(image),
    }


def literal_bind_plan(
    *,
    graph_item: Dict[str, Any],
    product: Any,
    variants: Sequence[Any],
    membership_meta_item_id: str = "",
) -> Dict[str, Any]:
    """Read-only bind decision. Literal retailer_id only. Never matches by name."""
    rid = _strip(graph_item.get("retailer_id"))
    mid = _strip(graph_item.get("id") or graph_item.get("meta_item_id"))
    classification = classify_graph_retailer_id(
        retailer_id=rid,
        external_id=_strip(getattr(product, "external_id", None)),
        variants=variants,
        product_meta_item_id=_strip(getattr(product, "meta_item_id", None)),
        graph_meta_item_id=mid,
        membership_meta_item_id=membership_meta_item_id,
    )
    identity = identity_for_retailer_id(product, variants, rid)
    graph_sig = content_signature_from_graph(graph_item)
    local_row = None
    if identity is not None:
        local_row = next(
            (
                row
                for row in variants
                if int(getattr(row, "id", 0) or 0) == identity.variant_id
            ),
            None,
        )
    local_sig = content_signature_from_variant(local_row) if local_row is not None else {}
    content_exact = bool(identity) and bool(graph_sig) and graph_sig == local_sig and bool(graph_sig.get("price") or graph_sig.get("currency"))
    would_bind = (
        classification == CLASS_UNBOUND_VARIANT
        and identity is not None
        and content_exact
        and not _strip(membership_meta_item_id)
    )
    quarantine = classification in {CLASS_UNBOUND_PARENT, CLASS_AMBIGUOUS} or (
        classification == CLASS_UNBOUND_VARIANT and not content_exact
    )
    return {
        "retailer_id": rid,
        "meta_item_id": mid,
        "class": classification,
        "would_bind": would_bind,
        "quarantine": quarantine,
        "content_exact": content_exact,
        "name_match_used": False,
        "identity_variant_id": identity.variant_id if identity else None,
        "salla_variant_id": identity.salla_variant_id if identity else None,
    }
