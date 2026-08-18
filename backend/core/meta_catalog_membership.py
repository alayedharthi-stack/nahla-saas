"""Canonical Meta catalog membership — derived provider truth.

Product / ProductVariant remain commerce identity. This module owns the
persisted fact:

    Graph verified retailer_id R exists in catalog C for tenant T and
    maps unambiguously to one local product / variant.

Writers: complete successful Meta Graph reconcile, and exact 131009
products-not-found invalidation.

Readers: native catalog send / browse capability. Never Graph on a
customer turn. Never authorize from Product.meta_catalog_published_at,
external_id, extra_metadata, or a test-only catalog attribute.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.catalog import is_synthetic_retailer_id

logger = logging.getLogger("nahla.meta_catalog_membership")

PROVENANCE_GRAPH_RECONCILE = "meta_graph_reconcile"
DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING = "ambiguous_local_mapping"


@dataclass(frozen=True)
class MetaCatalogMembershipFact:
    """Structured membership fact for a pure decision helper."""

    tenant_id: int
    catalog_id: str
    retailer_id: str
    product_id: int
    variant_id: Optional[int]
    meta_item_id: Optional[str]
    verified_at: Optional[datetime]
    provenance: str


@dataclass(frozen=True)
class LocalRetailerClaim:
    product_id: int
    variant_id: Optional[int]
    is_default: bool
    has_variants: bool
    default_variant_id: Optional[int]


@dataclass(frozen=True)
class DesiredMembership:
    retailer_id: str
    product_id: int
    variant_id: Optional[int]
    meta_item_id: Optional[str]


@dataclass
class MembershipJoinReport:
    desired: List[DesiredMembership] = field(default_factory=list)
    ambiguous: List[Dict[str, Any]] = field(default_factory=list)
    unmatched_graph_ids: List[str] = field(default_factory=list)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_unusable_retailer_id(retailer_id: str) -> bool:
    rid = _norm(retailer_id)
    if not rid:
        return True
    if is_synthetic_retailer_id(rid):
        return True
    if rid.startswith("nahla_v_") or rid.startswith("nahla_p_"):
        return True
    return False


def fact_from_row(row: Any) -> Optional[MetaCatalogMembershipFact]:
    if row is None:
        return None
    catalog_id = _norm(getattr(row, "catalog_id", ""))
    retailer_id = _norm(getattr(row, "retailer_id", ""))
    product_id = _optional_int(getattr(row, "product_id", None))
    tenant_id = _optional_int(getattr(row, "tenant_id", None))
    if not catalog_id or not retailer_id or product_id is None or tenant_id is None:
        return None
    return MetaCatalogMembershipFact(
        tenant_id=int(tenant_id),
        catalog_id=catalog_id,
        retailer_id=retailer_id,
        product_id=int(product_id),
        variant_id=_optional_int(getattr(row, "variant_id", None)),
        meta_item_id=_norm(getattr(row, "meta_item_id", None)) or None,
        verified_at=getattr(row, "verified_at", None),
        provenance=_norm(getattr(row, "provenance", "")) or PROVENANCE_GRAPH_RECONCILE,
    )


def load_meta_catalog_membership(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    retailer_id: str,
) -> Optional[MetaCatalogMembershipFact]:
    """Exact lookup. No sibling / title / other-catalog scan."""
    if db is None or not tenant_id:
        return None
    cid = _norm(catalog_id)
    rid = _norm(retailer_id)
    if not cid or not rid or _is_unusable_retailer_id(rid):
        return None
    try:
        from models import MetaCatalogMembership  # noqa: PLC0415

        row = (
            db.query(MetaCatalogMembership)
            .filter(
                MetaCatalogMembership.tenant_id == int(tenant_id),
                MetaCatalogMembership.catalog_id == cid,
                MetaCatalogMembership.retailer_id == rid,
            )
            .first()
        )
        return fact_from_row(row)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[META_MEMBERSHIP] load failed tenant=%s catalog=%s err=%s",
            tenant_id,
            cid,
            type(exc).__name__,
        )
        return None


def list_memberships_for_catalog(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    limit: int = 200,
) -> List[MetaCatalogMembershipFact]:
    cid = _norm(catalog_id)
    if db is None or not tenant_id or not cid:
        return []
    try:
        from models import MetaCatalogMembership  # noqa: PLC0415

        rows = (
            db.query(MetaCatalogMembership)
            .filter(
                MetaCatalogMembership.tenant_id == int(tenant_id),
                MetaCatalogMembership.catalog_id == cid,
            )
            .order_by(MetaCatalogMembership.id.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        facts: List[MetaCatalogMembershipFact] = []
        for row in rows:
            fact = fact_from_row(row)
            if fact is not None:
                facts.append(fact)
        return facts
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[META_MEMBERSHIP] list failed tenant=%s catalog=%s err=%s",
            tenant_id,
            cid,
            type(exc).__name__,
        )
        return []


def count_memberships_for_catalog(db: Any, *, tenant_id: int, catalog_id: str) -> int:
    return len(list_memberships_for_catalog(db, tenant_id=tenant_id, catalog_id=catalog_id))


def first_membership_retailer_id(db: Any, *, tenant_id: int, catalog_id: str) -> str:
    facts = list_memberships_for_catalog(
        db, tenant_id=tenant_id, catalog_id=catalog_id, limit=1
    )
    return facts[0].retailer_id if facts else ""


def membership_authorizes_send(
    fact: Optional[MetaCatalogMembershipFact],
    *,
    tenant_id: int,
    catalog_id: str,
    retailer_id: str,
    product_id: Optional[int],
    bound_variant_id: Optional[int] = None,
    explicit_variant: bool = False,
    product_has_variants: bool = False,
    canonical_default_variant_id: Optional[int] = None,
) -> bool:
    """Exact-key authorization. No sibling, stamp, or title fallback."""
    if fact is None or product_id is None:
        return False
    if int(fact.tenant_id) != int(tenant_id):
        return False
    if _norm(fact.catalog_id) != _norm(catalog_id):
        return False
    if _norm(fact.retailer_id) != _norm(retailer_id):
        return False
    if int(fact.product_id) != int(product_id):
        return False
    if explicit_variant:
        if bound_variant_id is None or fact.variant_id is None:
            return False
        return int(fact.variant_id) == int(bound_variant_id)
    if product_has_variants:
        return False
    if fact.variant_id is None:
        return True
    if canonical_default_variant_id is None:
        return False
    return int(fact.variant_id) == int(canonical_default_variant_id)


def _collapse_alias_claims(
    claims: Sequence[LocalRetailerClaim],
) -> List[LocalRetailerClaim]:
    """Collapse parent + canonical default-variant alias of the same SKU."""
    if len(claims) <= 1:
        return list(claims)
    product_ids = {c.product_id for c in claims}
    if len(product_ids) != 1:
        return list(claims)
    product_id = next(iter(product_ids))
    has_variants = any(c.has_variants for c in claims)
    default_ids = {
        c.default_variant_id for c in claims if c.default_variant_id is not None
    }
    default_variant_id = next(iter(default_ids)) if len(default_ids) == 1 else None
    if has_variants:
        concrete = [c for c in claims if c.variant_id is not None]
        concrete_ids = {c.variant_id for c in concrete}
        parent_rows = [c for c in claims if c.variant_id is None]
        if len(concrete_ids) != 1:
            return list(claims)
        chosen = concrete[0]
        is_canonical_default = bool(
            chosen.is_default
            or (
                default_variant_id is not None
                and int(chosen.variant_id) == int(default_variant_id)
            )
        )
        if parent_rows and not is_canonical_default:
            return list(claims)
        return [chosen]
    default_rows = [
        c
        for c in claims
        if c.variant_id is not None
        and (
            c.is_default
            or (
                default_variant_id is not None
                and int(c.variant_id) == int(default_variant_id)
            )
        )
    ]
    parent_rows = [c for c in claims if c.variant_id is None]
    other = [c for c in claims if c not in default_rows and c not in parent_rows]
    if other:
        return list(claims)
    if default_rows and not other:
        chosen = default_rows[0]
        return [
            LocalRetailerClaim(
                product_id=product_id,
                variant_id=chosen.variant_id,
                is_default=True,
                has_variants=False,
                default_variant_id=chosen.variant_id,
            )
        ]
    if parent_rows and not default_rows:
        return [parent_rows[0]]
    return list(claims)


def _collect_local_claims(db: Any, tenant_id: int) -> Dict[str, List[LocalRetailerClaim]]:
    from models import Product, ProductVariant  # noqa: PLC0415

    products = db.query(Product).filter(Product.tenant_id == int(tenant_id)).all()
    variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.tenant_id == int(tenant_id))
        .all()
    )
    product_map = {int(p.id): p for p in products}
    claims: Dict[str, List[LocalRetailerClaim]] = {}

    def _add(rid: str, claim: LocalRetailerClaim) -> None:
        if _is_unusable_retailer_id(rid):
            return
        claims.setdefault(rid, []).append(claim)

    for variant in variants:
        product = product_map.get(int(variant.product_id))
        if product is None:
            continue
        rid = _norm(getattr(variant, "retailer_id", ""))
        _add(
            rid,
            LocalRetailerClaim(
                product_id=int(product.id),
                variant_id=int(variant.id),
                is_default=bool(getattr(variant, "is_default", False)),
                has_variants=bool(getattr(product, "has_variants", False)),
                default_variant_id=_optional_int(getattr(product, "default_variant_id", None)),
            ),
        )

    for product in products:
        rid = _norm(getattr(product, "meta_retailer_id", None))
        if not rid:
            rid = _norm(getattr(product, "external_id", None))
        _add(
            rid,
            LocalRetailerClaim(
                product_id=int(product.id),
                variant_id=None,
                is_default=False,
                has_variants=bool(getattr(product, "has_variants", False)),
                default_variant_id=_optional_int(getattr(product, "default_variant_id", None)),
            ),
        )
    return claims


def join_graph_to_local_memberships(
    db: Any,
    *,
    tenant_id: int,
    live_products: Dict[str, Dict[str, Any]],
) -> MembershipJoinReport:
    """Map Graph retailer ids to exact local referents. Ambiguity fails closed."""
    claims_by_rid = _collect_local_claims(db, tenant_id)
    desired: List[DesiredMembership] = []
    ambiguous: List[Dict[str, Any]] = []
    unmatched: List[str] = []

    for rid, graph_row in (live_products or {}).items():
        retailer_id = _norm(rid)
        if _is_unusable_retailer_id(retailer_id):
            continue
        claims = _collapse_alias_claims(claims_by_rid.get(retailer_id, ()))
        unique_pairs = {(c.product_id, c.variant_id) for c in claims}
        if not unique_pairs:
            unmatched.append(retailer_id)
            continue
        if len(unique_pairs) != 1:
            ambiguous.append(
                {
                    "diagnostic": DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING,
                    "retailer_id": retailer_id,
                    "local_referents": [
                        {"product_id": p, "variant_id": v}
                        for p, v in sorted(unique_pairs, key=lambda item: (item[0], item[1] or -1))
                    ],
                }
            )
            continue
        claim = claims[0]
        meta_item = (graph_row or {}).get("meta_product_id") or (graph_row or {}).get(
            "meta_item_id"
        )
        desired.append(
            DesiredMembership(
                retailer_id=retailer_id,
                product_id=int(claim.product_id),
                variant_id=_optional_int(claim.variant_id),
                meta_item_id=_norm(meta_item) or None,
            )
        )
    return MembershipJoinReport(
        desired=desired,
        ambiguous=ambiguous,
        unmatched_graph_ids=unmatched,
    )


def apply_membership_snapshot(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    desired: Sequence[DesiredMembership],
    verified_at: Optional[datetime] = None,
    provenance: str = PROVENANCE_GRAPH_RECONCILE,
) -> Dict[str, int]:
    """Replace memberships for tenant+catalog with the desired complete snapshot."""
    from models import MetaCatalogMembership, Product  # noqa: PLC0415

    cid = _norm(catalog_id)
    now = verified_at or datetime.now(timezone.utc)
    desired_by_rid = {_norm(d.retailer_id): d for d in desired if _norm(d.retailer_id)}

    existing = (
        db.query(MetaCatalogMembership)
        .filter(
            MetaCatalogMembership.tenant_id == int(tenant_id),
            MetaCatalogMembership.catalog_id == cid,
        )
        .all()
    )
    upserted = 0
    removed = 0
    seen: set[str] = set()
    for row in existing:
        rid = _norm(row.retailer_id)
        want = desired_by_rid.get(rid)
        if want is None:
            db.delete(row)
            removed += 1
            continue
        row.product_id = int(want.product_id)
        row.variant_id = _optional_int(want.variant_id)
        row.meta_item_id = _norm(want.meta_item_id) or None
        row.verified_at = now
        row.provenance = provenance
        upserted += 1
        seen.add(rid)
    for rid, want in desired_by_rid.items():
        if rid in seen:
            continue
        db.add(
            MetaCatalogMembership(
                tenant_id=int(tenant_id),
                catalog_id=cid,
                retailer_id=rid,
                product_id=int(want.product_id),
                variant_id=_optional_int(want.variant_id),
                meta_item_id=_norm(want.meta_item_id) or None,
                verified_at=now,
                provenance=provenance,
            )
        )
        upserted += 1

    member_product_ids = {int(d.product_id) for d in desired_by_rid.values()}
    products = db.query(Product).filter(Product.tenant_id == int(tenant_id)).all()
    for product in products:
        if int(product.id) in member_product_ids:
            product.meta_catalog_published_at = now
        elif getattr(product, "meta_catalog_published_at", None) is not None:
            still = (
                db.query(MetaCatalogMembership.id)
                .filter(
                    MetaCatalogMembership.tenant_id == int(tenant_id),
                    MetaCatalogMembership.product_id == int(product.id),
                )
                .first()
            )
            if still is None:
                product.meta_catalog_published_at = None

    db.flush()
    logger.info(
        "[META_MEMBERSHIP] snapshot tenant=%s catalog=%s upserted=%d removed=%d",
        tenant_id,
        cid,
        upserted,
        removed,
    )
    return {"upserted": upserted, "removed": removed}


def invalidate_meta_catalog_membership(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    retailer_id: str,
) -> int:
    """Delete the exact tenant+catalog+retailer membership. No siblings."""
    cid = _norm(catalog_id)
    rid = _norm(retailer_id)
    if db is None or not tenant_id or not cid or not rid:
        return 0
    try:
        from models import MetaCatalogMembership, Product  # noqa: PLC0415

        row = (
            db.query(MetaCatalogMembership)
            .filter(
                MetaCatalogMembership.tenant_id == int(tenant_id),
                MetaCatalogMembership.catalog_id == cid,
                MetaCatalogMembership.retailer_id == rid,
            )
            .first()
        )
        if row is None:
            return 0
        product_id = int(row.product_id)
        db.delete(row)
        remaining = (
            db.query(MetaCatalogMembership.id)
            .filter(
                MetaCatalogMembership.tenant_id == int(tenant_id),
                MetaCatalogMembership.product_id == product_id,
            )
            .first()
        )
        if remaining is None:
            product = (
                db.query(Product)
                .filter(Product.tenant_id == int(tenant_id), Product.id == product_id)
                .first()
            )
            if product is not None:
                product.meta_catalog_published_at = None
        db.flush()
        logger.info(
            "[META_MEMBERSHIP] invalidated tenant=%s catalog=%s retailer_id=%s",
            tenant_id,
            cid,
            rid,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[META_MEMBERSHIP] invalidate failed tenant=%s catalog=%s err=%s",
            tenant_id,
            cid,
            type(exc).__name__,
        )
        return 0


count_memberships_for_catalog = count_memberships_for_catalog
first_membership_retailer_id = first_membership_retailer_id
invalidate_meta_catalog_membership = invalidate_meta_catalog_membership
membership_authorizes_send = membership_authorizes_send
join_graph_to_local_memberships = join_graph_to_local_memberships
apply_membership_snapshot = apply_membership_snapshot
load_meta_catalog_membership = load_meta_catalog_membership

__all__ = [
    "DIAGNOSTIC_AMBIGUOUS_LOCAL_MAPPING",
    "DesiredMembership",
    "LocalRetailerClaim",
    "MembershipJoinReport",
    "MetaCatalogMembershipFact",
    "PROVENANCE_GRAPH_RECONCILE",
    "apply_membership_snapshot",
    "apply_membership_snapshot",
    "count_memberships_for_catalog",
    "count_memberships_for_catalog",
    "fact_from_row",
    "first_membership_retailer_id",
    "first_membership_retailer_id",
    "invalidate_meta_catalog_membership",
    "invalidate_meta_catalog_membership",
    "join_graph_to_local_memberships",
    "join_graph_to_local_memberships",
    "list_memberships_for_catalog",
    "load_meta_catalog_membership",
    "load_meta_catalog_membership",
    "membership_authorizes_send",
    "membership_authorizes_send",
]
