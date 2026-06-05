"""
modules/ai/brain/postprocess/product_availability_evidence.py
──────────────────────────────────────────────────────────────
Structured product availability evidence — never infer from LLM wording alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.product_entity_resolution import (
    EntityResolutionResult,
    family_checkout_summary,
    primary_year_from_text,
    resolve_availability_entity,
)

EVIDENCE_RESOLVED_AVAILABLE = "resolved_available"
EVIDENCE_RESOLVED_UNAVAILABLE = "resolved_unavailable"
EVIDENCE_CONFLICT = "conflict"
EVIDENCE_UNKNOWN = "unknown"

CONFLICT_KB_AVAILABLE_CATALOG_UNAVAILABLE = "KB_AVAILABLE_CATALOG_UNAVAILABLE"
CONFLICT_KB_UNAVAILABLE_CATALOG_AVAILABLE = "KB_UNAVAILABLE_CATALOG_AVAILABLE"
CONFLICT_YEAR_MISMATCH = "YEAR_MISMATCH"
CONFLICT_SKU_MISMATCH = "SKU_MISMATCH"
CONFLICT_ENTITY_MISMATCH = "ENTITY_MISMATCH"
CONFLICT_FAMILY_MIXED = "FAMILY_MIXED"
CONFLICT_MISSING_CATALOG_ENTITY = "MISSING_CATALOG_ENTITY"
CONFLICT_STALE_PRODUCT_LINK = "STALE_PRODUCT_LINK"


@dataclass(frozen=True)
class ProductAvailabilityEvidenceResult:
    evidence_state: str
    evidence_ok_for_positive: bool
    evidence_ok_for_negative: bool
    conflict_type: Optional[str]
    entity: EntityResolutionResult
    catalog_checkout: Optional[bool]
    kb_avail_polarity: Optional[str]
    family_checkout_summary: Optional[Dict[str, List[int]]]
    evidence_source: str
    reason: str


def _kb_signals_for_entity(
    kb_signals: Sequence[Dict[str, Any]],
    product_links: Sequence[Dict[str, Any]],
    entity: EntityResolutionResult,
    catalog_by_id: Dict[int, Dict[str, Any]],
) -> Tuple[Optional[str], List[str]]:
    """Return (polarity, conflict_flags) from KB layers."""
    flags: List[str] = []
    polarities: List[str] = []

    linked_map: Dict[int, List[int]] = {}
    for lk in product_links:
        sid = lk.get("section_id")
        pid = lk.get("product_id")
        if sid is not None and pid is not None:
            linked_map.setdefault(int(sid), []).append(int(pid))

    for sig in kb_signals:
        pol = str(sig.get("avail_polarity") or "none")
        if pol in ("positive", "negative"):
            polarities.append(pol)

        sec_id = sig.get("section_id")
        pri_year = sig.get("primary_year")
        linked_ids = linked_map.get(int(sec_id), []) if sec_id is not None else []

        if entity.product_id is not None and linked_ids:
            if entity.product_id not in linked_ids:
                flags.append(CONFLICT_ENTITY_MISMATCH)
            else:
                prod = catalog_by_id.get(entity.product_id, {})
                prod_years = prod.get("years") or []
                if pri_year and prod_years and pri_year not in prod_years:
                    flags.append(CONFLICT_YEAR_MISMATCH)
                    flags.append(CONFLICT_STALE_PRODUCT_LINK)

        if pri_year and entity.product_id is not None:
            prod_years = (catalog_by_id.get(entity.product_id) or {}).get("years") or []
            if prod_years and pri_year not in prod_years:
                flags.append(CONFLICT_YEAR_MISMATCH)

        if pri_year and pol == "positive":
            catalog_years = set()
            for p in catalog_by_id.values():
                catalog_years.update(p.get("years") or [])
            if catalog_years and pri_year not in catalog_years:
                flags.append(CONFLICT_MISSING_CATALOG_ENTITY)

    if "positive" in polarities and "negative" in polarities:
        flags.append(CONFLICT_KB_AVAILABLE_CATALOG_UNAVAILABLE)

    kb_pol: Optional[str] = None
    if "positive" in polarities and "negative" not in polarities:
        kb_pol = "positive"
    elif "negative" in polarities and "positive" not in polarities:
        kb_pol = "negative"

    return kb_pol, list(dict.fromkeys(flags))


def evaluate_product_availability_evidence(
    *,
    availability_context: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> ProductAvailabilityEvidenceResult:
    """Return structured availability evidence for this turn."""
    ctx = availability_context or {}
    catalog_skus: List[Dict[str, Any]] = list(ctx.get("catalog_skus") or [])
    kb_signals: List[Dict[str, Any]] = list(ctx.get("kb_signals") or [])
    product_links: List[Dict[str, Any]] = list(ctx.get("product_links") or [])
    platform_connected = bool(ctx.get("platform_connected"))

    catalog_by_id = {
        int(p["id"]): p for p in catalog_skus if p.get("id") is not None
    }

    entity = resolve_availability_entity(
        focus_product=ctx.get("focus_product"),
        recommended_product_ids=list(ctx.get("recommended_product_ids") or []),
        inbound_text=inbound_text,
        catalog_skus=catalog_skus,
    )

    empty_entity = EntityResolutionResult(
        resolved=False,
        resolution_mode="none",
        product_id=None,
        family_key=None,
        confidence=0.0,
    )

    if not platform_connected or not catalog_skus:
        return ProductAvailabilityEvidenceResult(
            evidence_state=EVIDENCE_UNKNOWN,
            evidence_ok_for_positive=False,
            evidence_ok_for_negative=False,
            conflict_type=None,
            entity=entity if entity.resolved else empty_entity,
            catalog_checkout=None,
            kb_avail_polarity=None,
            family_checkout_summary=None,
            evidence_source="no_catalog",
            reason="platform_not_connected_or_empty_catalog",
        )

    kb_pol, kb_flags = _kb_signals_for_entity(
        kb_signals, product_links, entity, catalog_by_id
    )

    # ── Family-level resolution ──────────────────────────────────────────
    if entity.resolution_mode == "family" and entity.family_key:
        fam = family_checkout_summary(catalog_skus, entity.family_key)
        true_n = len(fam.get("checkout_true") or [])
        false_n = len(fam.get("checkout_false") or [])
        if true_n > 0 and false_n > 0:
            ctype = CONFLICT_FAMILY_MIXED
            if CONFLICT_YEAR_MISMATCH in kb_flags:
                ctype = CONFLICT_YEAR_MISMATCH
            elif CONFLICT_MISSING_CATALOG_ENTITY in kb_flags:
                ctype = CONFLICT_MISSING_CATALOG_ENTITY
            return ProductAvailabilityEvidenceResult(
                evidence_state=EVIDENCE_CONFLICT,
                evidence_ok_for_positive=False,
                evidence_ok_for_negative=False,
                conflict_type=ctype,
                entity=entity,
                catalog_checkout=None,
                kb_avail_polarity=kb_pol,
                family_checkout_summary=fam,
                evidence_source="catalog_family",
                reason="mixed_family_checkout_states",
            )
        if true_n > 0 and false_n == 0:
            if kb_flags:
                return ProductAvailabilityEvidenceResult(
                    evidence_state=EVIDENCE_CONFLICT,
                    evidence_ok_for_positive=False,
                    evidence_ok_for_negative=False,
                    conflict_type=kb_flags[0],
                    entity=entity,
                    catalog_checkout=True,
                    kb_avail_polarity=kb_pol,
                    family_checkout_summary=fam,
                    evidence_source="catalog_family",
                    reason="kb_catalog_divergence",
                )
            return ProductAvailabilityEvidenceResult(
                evidence_state=EVIDENCE_RESOLVED_AVAILABLE,
                evidence_ok_for_positive=True,
                evidence_ok_for_negative=False,
                conflict_type=None,
                entity=entity,
                catalog_checkout=True,
                kb_avail_polarity=kb_pol,
                family_checkout_summary=fam,
                evidence_source="catalog_family",
                reason="all_family_members_checkout",
            )
        if false_n > 0 and true_n == 0:
            if kb_pol == "positive" or kb_flags:
                return ProductAvailabilityEvidenceResult(
                    evidence_state=EVIDENCE_CONFLICT,
                    evidence_ok_for_positive=False,
                    evidence_ok_for_negative=False,
                    conflict_type=kb_flags[0] if kb_flags else CONFLICT_KB_AVAILABLE_CATALOG_UNAVAILABLE,
                    entity=entity,
                    catalog_checkout=False,
                    kb_avail_polarity=kb_pol,
                    family_checkout_summary=fam,
                    evidence_source="catalog_family",
                    reason="kb_positive_family_unavailable",
                )
            return ProductAvailabilityEvidenceResult(
                evidence_state=EVIDENCE_RESOLVED_UNAVAILABLE,
                evidence_ok_for_positive=False,
                evidence_ok_for_negative=True,
                conflict_type=None,
                entity=entity,
                catalog_checkout=False,
                kb_avail_polarity=kb_pol,
                family_checkout_summary=fam,
                evidence_source="catalog_family",
                reason="all_family_members_not_checkout",
            )

    # ── Unresolved entity ────────────────────────────────────────────────
    if not entity.resolved or entity.product_id is None:
        if kb_flags:
            return ProductAvailabilityEvidenceResult(
                evidence_state=EVIDENCE_CONFLICT,
                evidence_ok_for_positive=False,
                evidence_ok_for_negative=False,
                conflict_type=kb_flags[0],
                entity=entity,
                catalog_checkout=None,
                kb_avail_polarity=kb_pol,
                family_checkout_summary=None,
                evidence_source="kb_signals",
                reason="unresolved_entity_with_kb_conflict",
            )
        return ProductAvailabilityEvidenceResult(
            evidence_state=EVIDENCE_UNKNOWN,
            evidence_ok_for_positive=False,
            evidence_ok_for_negative=False,
            conflict_type=None,
            entity=entity,
            catalog_checkout=None,
            kb_avail_polarity=kb_pol,
            family_checkout_summary=None,
            evidence_source="entity_resolution",
            reason="entity_not_resolved",
        )

    # ── Single SKU resolution ────────────────────────────────────────────
    prod = catalog_by_id.get(entity.product_id, {})
    checkout = bool(prod.get("can_checkout"))

    if kb_pol == "positive" and not checkout:
        kb_flags.append(CONFLICT_KB_AVAILABLE_CATALOG_UNAVAILABLE)
    elif kb_pol == "negative" and checkout:
        kb_flags.append(CONFLICT_KB_UNAVAILABLE_CATALOG_AVAILABLE)
    kb_flags = list(dict.fromkeys(kb_flags))

    if kb_flags:
        ctype = kb_flags[0]
        if checkout and kb_pol == "negative":
            ctype = CONFLICT_KB_UNAVAILABLE_CATALOG_AVAILABLE
        elif not checkout and kb_pol == "positive":
            ctype = CONFLICT_KB_AVAILABLE_CATALOG_UNAVAILABLE
        return ProductAvailabilityEvidenceResult(
            evidence_state=EVIDENCE_CONFLICT,
            evidence_ok_for_positive=False,
            evidence_ok_for_negative=False,
            conflict_type=ctype,
            entity=entity,
            catalog_checkout=checkout,
            kb_avail_polarity=kb_pol,
            family_checkout_summary=None,
            evidence_source="catalog_sku",
            reason="kb_catalog_conflict",
        )

    if checkout:
        return ProductAvailabilityEvidenceResult(
            evidence_state=EVIDENCE_RESOLVED_AVAILABLE,
            evidence_ok_for_positive=True,
            evidence_ok_for_negative=False,
            conflict_type=None,
            entity=entity,
            catalog_checkout=True,
            kb_avail_polarity=kb_pol,
            family_checkout_summary=None,
            evidence_source="catalog_sku",
            reason="can_checkout_true",
        )

    return ProductAvailabilityEvidenceResult(
        evidence_state=EVIDENCE_RESOLVED_UNAVAILABLE,
        evidence_ok_for_positive=False,
        evidence_ok_for_negative=True,
        conflict_type=None,
        entity=entity,
        catalog_checkout=False,
        kb_avail_polarity=kb_pol,
        family_checkout_summary=None,
        evidence_source="catalog_sku",
        reason="can_checkout_false",
    )
