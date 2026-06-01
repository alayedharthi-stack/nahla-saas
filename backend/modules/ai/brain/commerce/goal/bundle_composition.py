"""
Deterministic bundle composition from KB entries + catalog resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .goal_retrieval import GoalKBEntry
from .goal_schema import GoalProductRef
from .telemetry import log_goal_resolution_failed


@dataclass
class RegimenBundleItem:
    ref: str
    role: str
    note: str = ""
    product_id: Optional[int] = None
    title: str = ""
    external_id: str = ""
    resolved: bool = False
    resolution_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "role": self.role,
            "note": self.note,
            "product_id": self.product_id,
            "title": self.title,
            "external_id": self.external_id,
            "resolved": self.resolved,
            "resolution_reason": self.resolution_reason,
        }


@dataclass
class RegimenBundle:
    goal: str
    section_id: int
    title: str
    items: List[RegimenBundleItem] = field(default_factory=list)
    usage_guidance: List[str] = field(default_factory=list)
    soft_claims: List[str] = field(default_factory=list)
    followup_questions: List[str] = field(default_factory=list)
    compliance: List[str] = field(default_factory=list)
    unresolved_refs: List[str] = field(default_factory=list)

    @property
    def resolved_count(self) -> int:
        return sum(1 for i in self.items if i.resolved)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "section_id": self.section_id,
            "title": self.title,
            "items": [i.to_dict() for i in self.items],
            "usage_guidance": list(self.usage_guidance),
            "soft_claims": list(self.soft_claims),
            "followup_questions": list(self.followup_questions),
            "compliance": list(self.compliance),
            "unresolved_refs": list(self.unresolved_refs),
            "resolved_count": self.resolved_count,
        }


def _product_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": int(getattr(row, "id", 0) or 0),
        "title": str(getattr(row, "title", "") or "").strip(),
        "external_id": str(getattr(row, "external_id", "") or "").strip(),
        "sku": str(getattr(row, "sku", "") or "").strip(),
        "price": getattr(row, "price", None),
        "is_active": bool(getattr(row, "is_active", True)),
    }


def _resolve_product_ref(
    db: Any,
    tenant_id: int,
    pref: GoalProductRef,
    *,
    catalog_rows: Optional[List[Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve one KB product ref — fail closed on ambiguity."""
    if pref.product_id:
        try:
            from models import Product  # noqa: PLC0415

            row = (
                db.query(Product)
                .filter(
                    Product.tenant_id == int(tenant_id),
                    Product.id == int(pref.product_id),
                )
                .first()
            )
            if row is None:
                return None, "product_id_not_found"
            if not bool(getattr(row, "is_active", True)):
                return None, "product_disabled"
            return _product_row_to_dict(row), "product_id"
        except Exception:  # noqa: BLE001
            return None, "product_id_lookup_error"

    ref = (pref.ref or "").strip()
    if not ref:
        return None, "empty_ref"

    rows = catalog_rows
    if rows is None:
        try:
            from models import Product  # noqa: PLC0415

            rows = list(
                db.query(Product)
                .filter(Product.tenant_id == int(tenant_id))
                .limit(2000)
                .all()
            )
        except Exception:  # noqa: BLE001
            return None, "catalog_fetch_error"

    try:
        from modules.ai.knowledge.product_matcher import (  # noqa: PLC0415
            CatalogProductForMatch,
            match_products,
        )

        catalog = [
            CatalogProductForMatch(
                id=int(getattr(r, "id", 0) or 0),
                title=str(getattr(r, "title", "") or ""),
                sku=str(getattr(r, "sku", "") or "") or None,
                external_id=str(getattr(r, "external_id", "") or "") or None,
            )
            for r in rows or []
        ]
        matches = match_products(ref, catalog, limit=2, min_confidence=0.40)
        if not matches:
            return None, "no_catalog_match"
        if len(matches) >= 2 and matches[0].confidence - matches[1].confidence < 0.12:
            return None, "ambiguous_sku"
        best = matches[0]
        for r in rows or []:
            if int(getattr(r, "id", 0) or 0) == best.product_id:
                if not bool(getattr(r, "is_active", True)):
                    return None, "product_disabled"
                return _product_row_to_dict(r), "title_match"
        return None, "match_row_missing"
    except Exception:  # noqa: BLE001
        return None, "matcher_error"


def compose_regimen_bundle(
    db: Any,
    tenant_id: int,
    goal: str,
    entry: GoalKBEntry,
) -> RegimenBundle:
    """
    Compose a deterministic ``RegimenBundle`` from one KB entry.

    Unresolved products are excluded — never hallucinated.
    """
    meta = entry.metadata
    bundle = RegimenBundle(
        goal=goal,
        section_id=entry.section_id,
        title=entry.title or goal,
        usage_guidance=list(meta.usage_guidance),
        soft_claims=list(meta.soft_claims),
        followup_questions=list(meta.followup_questions),
        compliance=list(meta.compliance),
    )

    catalog_rows: Optional[List[Any]] = None
    try:
        from models import Product  # noqa: PLC0415

        catalog_rows = list(
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id))
            .limit(2000)
            .all()
        )
    except Exception:  # noqa: BLE001
        catalog_rows = None

    for pref in meta.products:
        product_dict, reason = _resolve_product_ref(
            db,
            tenant_id,
            pref,
            catalog_rows=catalog_rows,
        )
        item = RegimenBundleItem(
            ref=pref.ref or str(pref.product_id or ""),
            role=pref.role,
            note=pref.note,
        )
        if product_dict:
            item.product_id = product_dict.get("id")
            item.title = str(product_dict.get("title") or "")
            item.external_id = str(product_dict.get("external_id") or "")
            item.resolved = True
            item.resolution_reason = reason
            bundle.items.append(item)
        else:
            bundle.unresolved_refs.append(pref.ref or str(pref.product_id or ""))
            log_goal_resolution_failed(
                tenant_id=tenant_id,
                goal=goal,
                ref=pref.ref or str(pref.product_id or ""),
                reason=reason,
            )

    return bundle
