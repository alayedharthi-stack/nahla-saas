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
_RESOLUTION_INBOUND_FAMILY = "inbound_family"
_RESOLUTION_NONE = "none"

# Generic availability asks — «هل X متوفر؟» / «X متوفر؟»
_DIRECT_AVAIL_ASK_RE = re.compile(
    r"(?:"
    r"هل\s+(?:ال)?[\u0600-\u06FFa-zA-Z]{2,24}\s+م(?:توفر|تاح)(?:\s|$|[؟?.!])"
    r"|(?:^|\s)(?:ال)?[\u0600-\u06FFa-zA-Z]{2,24}\s+م(?:توفر|تاح)\s*[؟?.!]?\s*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_AVAIL_QUERY_STOPWORDS = frozenset({
    "هل", "في", "من", "على", "عند", "عندكم", "عندك", "لديكم", "لديك",
    "متوفر", "متاح", "موجود", "availability", "available", "stock",
    "عسل", "منتج", "product", "type", "types",
})

# Trailing Arabic/Latin punctuation often sticks to availability tokens («متوفر؟»).
_TRAILING_PUNCT_RE = re.compile(r"[\u061f\u003f\u002e\u0021\u002c]+$")
_AR_DEF_ARTICLE = "\u0627\u0644"  # ال


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


def direct_product_availability_ask(text: str) -> bool:
    """True for short direct availability questions («هل X متوفر؟»)."""
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_DIRECT_AVAIL_ASK_RE.search(raw))


def _strip_trailing_punct(token: str) -> str:
    return _TRAILING_PUNCT_RE.sub("", token or "")


def _strip_ar_definite_article(token: str) -> str:
    """«السمر» → «سمر» for cross-surface token matching."""
    t = token or ""
    if t.startswith(_AR_DEF_ARTICLE) and len(t) > len(_AR_DEF_ARTICLE) + 1:
        return t[len(_AR_DEF_ARTICLE):]
    return t


def _token_match_forms(token: str) -> Set[str]:
    """Expand a token into matchable surface forms (article + punctuation variants)."""
    base = _strip_trailing_punct(token)
    if not base or len(base) < 2:
        return set()
    forms: Set[str] = {base}
    bare = _strip_ar_definite_article(base)
    if bare and bare != base:
        forms.add(bare)
    if not base.startswith(_AR_DEF_ARTICLE):
        forms.add(_AR_DEF_ARTICLE + base)
    return {f for f in forms if len(f) >= 2}


def _distinctive_inbound_product_tokens(inbound_text: str) -> List[str]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    toks = tokenize(normalize_arabic(inbound_text or ""))
    seen: Set[str] = set()
    out: List[str] = []
    for raw_t in toks:
        norm = _strip_trailing_punct(raw_t)
        stem = _strip_ar_definite_article(norm)
        if len(stem) < 3 or stem in _AVAIL_QUERY_STOPWORDS:
            continue
        if stem not in seen:
            seen.add(stem)
            out.append(stem)
    return out


def _catalog_ids_for_product_tokens(
    catalog_skus: Sequence[Dict[str, Any]],
    tokens: Sequence[str],
) -> List[int]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    if not tokens:
        return []
    inbound_forms: Set[str] = set()
    for t in tokens:
        inbound_forms |= _token_match_forms(t)
    if not inbound_forms:
        return []
    ids: List[int] = []
    for p in catalog_skus:
        pid = p.get("id")
        if pid is None:
            continue
        title = str(p.get("title") or "")
        title_norm = normalize_arabic(title)
        title_forms: Set[str] = set()
        for tt in tokenize(title_norm):
            title_forms |= _token_match_forms(tt)
        if inbound_forms & title_forms:
            ids.append(int(pid))
            continue
        if any(form in title_norm for form in inbound_forms):
            ids.append(int(pid))
    return list(dict.fromkeys(ids))


def family_checkout_summary_for_entity(
    catalog_skus: Sequence[Dict[str, Any]],
    entity: EntityResolutionResult,
) -> Dict[str, List[int]]:
    """Checkout split for a family entity (catalog family_key or inbound token group)."""
    by_id = _catalog_by_id(catalog_skus)
    if entity.candidate_product_ids:
        member_ids = [int(i) for i in entity.candidate_product_ids if int(i) in by_id]
    elif entity.family_key and not str(entity.family_key).startswith("inbound:"):
        member_ids = [int(p["id"]) for p in _family_members(catalog_skus, entity.family_key)]
    else:
        member_ids = []
    true_ids = [pid for pid in member_ids if by_id.get(pid, {}).get("can_checkout")]
    false_ids = [pid for pid in member_ids if not by_id.get(pid, {}).get("can_checkout")]
    return {"checkout_true": true_ids, "checkout_false": false_ids}


def _resolve_inbound_product_family(
    inbound_text: str,
    catalog_skus: Sequence[Dict[str, Any]],
    by_id: Dict[int, Dict[str, Any]],
) -> Optional[EntityResolutionResult]:
    """
    Group catalog SKUs by a distinctive inbound token for direct availability asks.

    Closes the gap where ``match_products`` rejects single-token overlap
    («سمر» vs long honey titles) so «هل السمر متوفر؟» never reached family mode.
    """
    if not direct_product_availability_ask(inbound_text):
        return None
    tokens = _distinctive_inbound_product_tokens(inbound_text)
    if not tokens:
        return None
    member_ids = _catalog_ids_for_product_tokens(catalog_skus, tokens)
    if not member_ids:
        return None
    if len(member_ids) == 1:
        pid = member_ids[0]
        p = by_id.get(pid, {})
        return EntityResolutionResult(
            resolved=True,
            resolution_mode=_RESOLUTION_INBOUND,
            product_id=pid,
            family_key=p.get("family_key"),
            confidence=0.75,
            candidate_product_ids=(pid,),
            primary_year=(p.get("years") or [None])[0] if p.get("years") else None,
        )
    virtual_key = "inbound:" + "|".join(sorted(tokens)[:3])
    return EntityResolutionResult(
        resolved=True,
        resolution_mode=_RESOLUTION_INBOUND_FAMILY,
        product_id=None,
        family_key=virtual_key,
        confidence=0.78,
        candidate_product_ids=tuple(member_ids),
    )


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

    inbound_family = _resolve_inbound_product_family(
        inbound_text, catalog_skus, by_id,
    )
    if inbound_family is not None:
        return inbound_family

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


__all__ = [
    "EntityResolutionResult",
    "direct_product_availability_ask",
    "extract_years",
    "extract_weights",
    "family_checkout_summary",
    "family_checkout_summary_for_entity",
    "family_key_from_title",
    "primary_year_from_text",
    "resolve_availability_entity",
]
