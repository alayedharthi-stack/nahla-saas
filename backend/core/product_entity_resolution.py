"""
core/product_entity_resolution.py
──────────────────────────────────
Platform-wide entity resolution for product availability evidence.

Pure functions — no DB, no LLM. Used by the availability truth guard.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from modules.ai.knowledge.product_matcher import (
    CatalogProductForMatch,
    match_products,
)

_YEAR_RE = re.compile(r"\b(14\d{2}|20\d{2})\b")
_WEIGHT_RE = re.compile(
    r"(\d+)\s*(?:\u062c\u0631\u0627\u0645|\u0643\u064a\u0644\u0648|\u0643\u062c\u0645|g|kg)\b",
    re.I,
)

_RESOLUTION_FOCUS = "focus_id"
_RESOLUTION_RECOMMENDED = "recommended_id"
_RESOLUTION_INBOUND = "inbound_match"
_RESOLUTION_FAMILY = "family"
_RESOLUTION_NONE = "none"


@dataclass(frozen=True)
class EntityResolutionResult:
    resolved: bool
    resolution_mode: str
    product_id: Optional[int]
    family_key: Optional[str]
    confidence: float
    candidate_product_ids: Tuple[int, ...] = ()
    primary_year: Optional[str] = None
    conflict_flags: Tuple[str, ...] = ()


def extract_years(text: str) -> List[str]:
    return _YEAR_RE.findall(text or "")


def extract_weights(text: str) -> List[str]:
    return [m.group(1) for m in _WEIGHT_RE.finditer(text or "")]


def family_key_from_title(title: str) -> str:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    t = normalize_arabic(title or "")
    t = _YEAR_RE.sub(" ", t)
    t = _WEIGHT_RE.sub(" ", t)
    t = re.sub(r"\d+", " ", t)
    toks = sorted({w for w in tokenize(t) if len(w) >= 3})
    return "|".join(toks[:5]) if toks else normalize_arabic(title or "")[:40]


def primary_year_from_text(title: str, body: str) -> Optional[str]:
    title_years = extract_years(title)
    if title_years:
        return title_years[0]
    body_years = extract_years(body)
    return body_years[0] if body_years else None


def _catalog_by_id(catalog_skus: Sequence[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(p["id"]): p for p in catalog_skus if p.get("id") is not None}


def _family_members(
    catalog_skus: Sequence[Dict[str, Any]],
    key: str,
) -> List[Dict[str, Any]]:
    if not key:
        return []
    return [p for p in catalog_skus if (p.get("family_key") or "") == key]


def resolve_availability_entity(
    *,
    focus_product: Optional[Dict[str, Any]],
    recommended_product_ids: Sequence[int],
    inbound_text: str,
    catalog_skus: Sequence[Dict[str, Any]],
) -> EntityResolutionResult:
    """Resolve which catalog entity the turn is about."""
    by_id = _catalog_by_id(catalog_skus)
    if not by_id:
        return EntityResolutionResult(
            resolved=False,
            resolution_mode=_RESOLUTION_NONE,
            product_id=None,
            family_key=None,
            confidence=0.0,
        )

    focus_id = None
    if isinstance(focus_product, dict):
        raw = focus_product.get("id")
        if isinstance(raw, int) and raw in by_id:
            focus_id = raw
        elif isinstance(raw, str) and raw.isdigit() and int(raw) in by_id:
            focus_id = int(raw)

    if focus_id is not None:
        p = by_id[focus_id]
        return EntityResolutionResult(
            resolved=True,
            resolution_mode=_RESOLUTION_FOCUS,
            product_id=focus_id,
            family_key=p.get("family_key"),
            confidence=1.0,
            candidate_product_ids=(focus_id,),
            primary_year=(p.get("years") or [None])[0] if p.get("years") else None,
        )

    for rid in recommended_product_ids:
        if isinstance(rid, int) and rid in by_id:
            p = by_id[rid]
            return EntityResolutionResult(
                resolved=True,
                resolution_mode=_RESOLUTION_RECOMMENDED,
                product_id=rid,
                family_key=p.get("family_key"),
                confidence=0.85,
                candidate_product_ids=(rid,),
                primary_year=(p.get("years") or [None])[0] if p.get("years") else None,
            )

    products_for_match = [
        CatalogProductForMatch(
            id=int(p["id"]),
            title=str(p.get("title") or ""),
            sku=p.get("sku"),
            external_id=p.get("external_id"),
        )
        for p in catalog_skus
        if p.get("id") is not None
    ]
    matches = match_products(inbound_text or "", products_for_match, limit=5, min_confidence=0.5)
    if len(matches) == 1:
        pid = matches[0].product_id
        p = by_id.get(pid, {})
        return EntityResolutionResult(
            resolved=True,
            resolution_mode=_RESOLUTION_INBOUND,
            product_id=pid,
            family_key=p.get("family_key"),
            confidence=matches[0].confidence,
            candidate_product_ids=(pid,),
            primary_year=(p.get("years") or [None])[0] if p.get("years") else None,
        )

    if len(matches) >= 2:
        fam_keys: Set[str] = set()
        pids: List[int] = []
        for m in matches:
            p = by_id.get(m.product_id, {})
            fam_keys.add(p.get("family_key") or "")
            pids.append(m.product_id)
        if len(fam_keys) == 1 and next(iter(fam_keys), ""):
            fk = next(iter(fam_keys))
            return EntityResolutionResult(
                resolved=True,
                resolution_mode=_RESOLUTION_FAMILY,
                product_id=None,
                family_key=fk,
                confidence=max(m.confidence for m in matches),
                candidate_product_ids=tuple(pids),
            )

    return EntityResolutionResult(
        resolved=False,
        resolution_mode=_RESOLUTION_NONE,
        product_id=None,
        family_key=None,
        confidence=0.0,
    )


def family_checkout_summary(
    catalog_skus: Sequence[Dict[str, Any]],
    family_key: str,
) -> Dict[str, List[int]]:
    members = _family_members(catalog_skus, family_key)
    true_ids = [int(p["id"]) for p in members if p.get("can_checkout")]
    false_ids = [int(p["id"]) for p in members if not p.get("can_checkout")]
    return {"checkout_true": true_ids, "checkout_false": false_ids}
