"""
commerce/selection_context.py
─────────────────────────────
Phase 4B — deterministic follow-up resolution against products the customer
just saw during discovery (ordinals, sizes, prices, same-product variants).

Operational claims use catalog evidence only; no LLM routing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from ..types import BrainContext, Decision
from .variant_pricing import (
    UnitSpec,
    bindings_from_catalog_product,
    normalize_text,
    parse_unit_from_text,
)

logger = logging.getLogger("nahla.brain.selection_context")

SELECTION_CONTEXT_TTL_TURNS = 12

_ORDINAL_INDEX: Dict[str, int] = {
    "الاول": 1, "الأول": 1, "اول": 1, "١": 1, "1": 1,
    "الثاني": 2, "الثانيه": 2, "الثانية": 2, "ثاني": 2, "٢": 2, "2": 2,
    "الثالث": 3, "الثالثه": 3, "الثالثة": 3, "ثالث": 3, "٣": 3, "3": 3,
    "الرابع": 4, "رابع": 4, "٤": 4, "4": 4,
}

_SIZE_AVAILABILITY_RE = re.compile(
    r"(?:"
    r"^(?:هل\s+)?(?:فيه|يوجد|متوفر|available)\s+(?:بال)?"
    r"(?:كilo|كيلo|كيلو|kg|جرام|gram|g|لتر|ml|"
    r"كبير|كبيره|كبيرة|large|صغير|small|وسط|medium)"
    r"|"
    r"بال(?:كilo|كيلo|كيلو|kg|جرام|gram|g|لتر|ml)\s*[?؟]?\s*$"
    r"|"
    r"(?:كilo|كيلo|كيلو|kg)\s*[?؟]?\s*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_LARGER_SIZE_RE = re.compile(
    r"فيه\s+(?:أكبر|اكبر|حجم\s+ثاني|حجم\s+اكبر|عبو[ةه]\s+أكبر|عبو[ةه]\s+اكبر|"
    r"size\s+bigger|bigger\s+size|larger)",
    re.UNICODE | re.IGNORECASE,
)

_SAME_PRODUCT_RE = re.compile(
    r"نفس(?:ه|ها|ي|هم)\s*(?:لكن|بس|ب)?\s*(?:بال)?"
    r"(?:كilo|كيلo|كيلو|kg|جرام|gram|g|حجم|size|large|small|medium|كبير|صغير)",
    re.UNICODE | re.IGNORECASE,
)

_PICK_VERB_RE = re.compile(
    r"^(?:ابغ|ابغى|ابغي|ابي|أبغ|أبغى|أبي|اريد|أريد|ودي|اختر|اختار|this|that)\s+",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_ORDINAL_RE = re.compile(
    r"(?:كم\s+سعر|كم\s+السعر|بكم|سعر)\s+(?:ال)?("
    + "|".join(re.escape(k) for k in _ORDINAL_INDEX)
    + r")\s*[?؟]?\s*$",
    re.UNICODE | re.IGNORECASE,
)

_WEIGHT_STRIP_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:g|gram|grams|جر|جرام|kg|kilo|kilos|كilo|كيلo|كيلو|ml|l|لتر|"
    r"pack|packs|حزمه|حزم|عبوه|عبوات)\b",
    re.I | re.UNICODE,
)

_ORDINAL_WORDS: Dict[str, int] = {
    k: v for k, v in _ORDINAL_INDEX.items() if not str(k).isdigit()
}

_EXPLORATORY_PRICE_QUESTION_RE = re.compile(
    r"(?:"
    r"^(?:كم|بكم|قد\s*ايش|how\s*much)\b"
    r"|(?:كم\s+سعر|كم\s+ثمن|كم\s+تمن|بكم\s+)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_NAME_PRICE_SELECTION_RE = re.compile(
    r"^(?:ابغ|ابغى|ابغي|ابي|أبغ|أبغى|أبي|اريد|أريد|ودي|اختر|اختار)\s+"
    r"(?P<name>.+?)\s+"
    r"(?:سعره|سعرها|بسعر|بسعره|بسعرها|السعر)\s+"
    r"(?P<price>\d+(?:[.,]\d+)?)\s*(?:ريال|r|sar|ر\.?\s*)?\s*[?؟.]?\s*$",
    re.UNICODE | re.IGNORECASE,
)

STRUCTURED_UNIQUE_SELECTION_KEY = "structured_unique_selection"
NAME_PRICE_CANDIDATE_MATCH = "name_price_candidate_match"
CANDIDATE_SOURCE_LAST_SEARCH = "last_search_candidates"


def _normalize_ar(text: str) -> str:
    return normalize_text(text or "")


def _product_key(product: Dict[str, Any]) -> str:
    for key in ("id", "external_id", "sku"):
        val = str(product.get(key) or "").strip()
        if val:
            return val
    return str(product.get("title") or "").strip().lower()


def _product_id(product: Dict[str, Any]) -> str:
    return str(product.get("id") or product.get("external_id") or "").strip()


def _format_price(product: Dict[str, Any]) -> str:
    raw = product.get("sale_price")
    if raw is None:
        raw = product.get("price")
    if raw is None:
        return "السعر غير محدد"
    if re.match(r"^\s*\d+(\.\d+)?\s*$", str(raw)):
        return f"{raw} ريال"
    return str(raw)


def _display_label(product: Dict[str, Any]) -> str:
    title = str(product.get("display_label") or product.get("title") or "").strip()
    for key in ("variant_name", "size", "weight", "unit", "option_label"):
        value = str(product.get(key) or "").strip()
        if value and value.lower() not in title.lower():
            return f"{title} {value}".strip()
    return title


def normalize_presented_product(
    product: Dict[str, Any],
    *,
    list_index: int = 0,
    collection_id: str = "",
) -> Dict[str, Any]:
    row = dict(product or {})
    row["list_index"] = list_index or row.get("list_index") or 0
    if collection_id:
        row["collection_id"] = collection_id
    row.setdefault("display_label", _display_label(row))
    return row


def stamp_selection_context_from_products(
    state: Any,
    *,
    products: Sequence[Dict[str, Any]],
    collections: Optional[Sequence[Dict[str, Any]]] = None,
    discovery_mode: str = "",
    selected_collection: str = "",
) -> None:
    """Persist what was shown to the customer for follow-up resolution."""
    coll_id = str(selected_collection or getattr(state, "selected_collection", "") or "").strip()
    presented = [
        normalize_presented_product(p, list_index=i, collection_id=coll_id)
        for i, p in enumerate(list(products or []), start=1)
    ]
    state.last_presented_products = presented
    if collections is not None:
        state.last_presented_collections = [
            dict(c) if isinstance(c, dict) else {"label": str(c)}
            for c in collections
        ]
    if discovery_mode:
        state.last_discovery_mode = str(discovery_mode)
    if coll_id:
        state.selected_collection = coll_id
    state.selection_context_turn = int(getattr(state, "turn", 0) or 0)
    logger.info(
        "[SELECTION_CONTEXT] stamped products=%d collections=%d collection=%r turn=%d",
        len(presented),
        len(state.last_presented_collections or []),
        coll_id or "-",
        state.selection_context_turn,
    )


def apply_selection_context_patch(state: Any, patch: Dict[str, Any]) -> None:
    if not isinstance(patch, dict):
        return
    if patch.get("selected_product_id") is not None:
        state.selected_product_id = str(patch.get("selected_product_id") or "")
    if patch.get("selected_variant_id") is not None:
        state.selected_variant_id = str(patch.get("selected_variant_id") or "")
    if patch.get("selected_collection") is not None:
        state.selected_collection = str(patch.get("selected_collection") or "")
    if patch.get("last_presented_products") is not None:
        state.last_presented_products = list(patch.get("last_presented_products") or [])
    if patch.get("last_presented_collections") is not None:
        state.last_presented_collections = list(patch.get("last_presented_collections") or [])
    if patch.get("collections_pool") is not None:
        state.collections_pool = list(patch.get("collections_pool") or [])
    if patch.get("collections_offset") is not None:
        state.collections_offset = int(patch.get("collections_offset") or 0)
    if patch.get("collections_page_size") is not None:
        state.collections_page_size = int(patch.get("collections_page_size") or 0)
    if patch.get("collections_next_available") is not None:
        state.collections_next_available = bool(patch.get("collections_next_available"))
    if patch.get("last_presented_group_products") is not None:
        state.last_presented_group_products = list(patch.get("last_presented_group_products") or [])
    if patch.get("group_products_pool") is not None:
        state.group_products_pool = list(patch.get("group_products_pool") or [])
    if patch.get("group_products_offset") is not None:
        state.group_products_offset = int(patch.get("group_products_offset") or 0)
    if patch.get("group_products_page_size") is not None:
        state.group_products_page_size = int(patch.get("group_products_page_size") or 0)
    if patch.get("next_page_available") is not None:
        state.next_page_available = bool(patch.get("next_page_available"))
    if patch.get("current_catalog_group") is not None:
        state.current_catalog_group = patch.get("current_catalog_group")
    if patch.get("catalog_navigation_source") is not None:
        state.catalog_navigation_source = str(patch.get("catalog_navigation_source") or "")
    if patch.get("native_catalog_send_failed") is not None:
        state.native_catalog_send_failed = bool(patch.get("native_catalog_send_failed"))
    if patch.get("selection_context_turn") is not None:
        state.selection_context_turn = int(patch.get("selection_context_turn") or 0)
    if str(getattr(state, "selected_product_id", "") or "").strip():
        try:
            from .commerce_objective import COMMERCE_OBJECTIVE_SELECTION  # noqa: PLC0415

            state.commerce_objective = COMMERCE_OBJECTIVE_SELECTION
            state.commerce_objective_turn = int(getattr(state, "turn", 0) or 0)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — commerce objective stamp is optional
            pass


def get_presented_products(state: Any) -> List[Dict[str, Any]]:
    presented = list(getattr(state, "last_presented_products", None) or [])
    if presented:
        return presented
    return list(getattr(state, "last_search_candidates", None) or [])


def selection_product_pool(state: Any) -> List[Dict[str, Any]]:
    """Broader pool: presented list + progressive browse pool (deduped)."""
    seen: set[str] = set()
    pool: List[Dict[str, Any]] = []
    for source in (
        get_presented_products(state),
        list(getattr(state, "catalog_browse_pool", None) or []),
    ):
        for product in source:
            key = _product_key(product)
            if not key or key in seen:
                continue
            seen.add(key)
            pool.append(product)
    return pool


def has_active_selection_context(state: Any) -> bool:
    if not get_presented_products(state):
        return False
    turn = int(getattr(state, "turn", 0) or 0)
    ctx_turn = int(getattr(state, "selection_context_turn", 0) or 0)
    if ctx_turn <= 0:
        return True
    return (turn - ctx_turn) <= SELECTION_CONTEXT_TTL_TURNS


def is_selection_followup_message(message: str) -> bool:
    norm = _normalize_ar(message)
    if not norm:
        return False
    if _SIZE_AVAILABILITY_RE.search(norm):
        return True
    if _LARGER_SIZE_RE.search(norm):
        return True
    if _SAME_PRODUCT_RE.search(norm):
        return True
    if _PRICE_ORDINAL_RE.search(norm):
        return True
    if _extract_ordinal_pick(norm) is not None:
        return True
    if _extract_name_pick(norm):
        return True
    if _NAME_PRICE_SELECTION_RE.match(norm) and not _is_exploratory_price_question(norm):
        return True
    return False


def _extract_ordinal_token(norm: str) -> Optional[int]:
    """Resolve list-index tokens without treating price digits as ordinals."""
    tokens = norm.split()
    for token in tokens:
        clean = token.strip(".,؟?!")
        if clean in _ORDINAL_INDEX:
            return _ORDINAL_INDEX[clean]
    # Arabic ordinal words may appear inside longer phrases; numeric indices must
    # be standalone tokens (never substring of a price like 114 → 1).
    for word, idx in _ORDINAL_WORDS.items():
        if word in norm:
            return idx
    return None


def _is_exploratory_price_question(norm: str) -> bool:
    if not norm:
        return False
    if _EXPLORATORY_PRICE_QUESTION_RE.search(norm):
        return True
    if _PRICE_ORDINAL_RE.search(norm):
        return True
    return False


def _product_normalized_price(product: Dict[str, Any]) -> Optional[int]:
    from ..postprocess.product_claim_grounding_evidence import parse_price_amount  # noqa: PLC0415

    raw = product.get("sale_price")
    if raw is None:
        raw = product.get("price")
    return parse_price_amount(raw)


def _product_is_checkout_eligible(product: Dict[str, Any]) -> bool:
    orderable = product.get("can_checkout", product.get("orderable", True))
    if not orderable:
        return False
    return bool(_product_id(product) or str(product.get("external_id") or "").strip())


def _get_last_search_candidates(state: Any) -> List[Dict[str, Any]]:
    return list(getattr(state, "last_search_candidates", None) or [])


def _build_structured_unique_selection(
    product: Dict[str, Any],
    *,
    kind: str,
    candidate_source: str,
    stated_price: int,
    name_reference: str,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "candidate_source": candidate_source,
        "candidate_id": _product_id(product),
        "external_id": str(product.get("external_id") or "").strip(),
        "stated_price": stated_price,
        "name_reference": _normalize_name_reference(name_reference),
        "verified_unique": True,
    }


def _candidate_identity(product: Dict[str, Any]) -> tuple[str, str]:
    return _product_id(product), str(product.get("external_id") or "").strip()


def _candidate_identity_matches(product: Dict[str, Any], marker: Dict[str, Any]) -> bool:
    pid, ext = _candidate_identity(product)
    marker_id = str(marker.get("candidate_id") or "").strip()
    marker_ext = str(marker.get("external_id") or "").strip()
    if marker_id and pid == marker_id:
        return True
    if marker_ext and ext == marker_ext:
        return True
    return False


def _normalize_name_reference(name_reference: str) -> str:
    return re.sub(r"^ال\s*", "", _normalize_ar(name_reference or "")).strip()


def _eligible_name_price_hits(
    name_reference: str,
    stated_price: int,
    candidates: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    norm_name = _normalize_name_reference(name_reference)
    if not norm_name:
        return []
    hits = _match_product_by_name(norm_name, candidates)
    return [
        product
        for product in hits
        if _product_is_checkout_eligible(product)
        and _product_normalized_price(product) == stated_price
    ]


def verify_structured_unique_selection_against_state(
    decision: Any,
    state: Any,
) -> bool:
    """Verify name+price selection marker against live last_search_candidates."""
    args = dict(getattr(decision, "args", None) or {})
    marker = args.get(STRUCTURED_UNIQUE_SELECTION_KEY)
    if not isinstance(marker, dict):
        return False
    if marker.get("kind") != NAME_PRICE_CANDIDATE_MATCH:
        return False
    if marker.get("candidate_source") != CANDIDATE_SOURCE_LAST_SEARCH:
        return False

    stated_price = marker.get("stated_price")
    name_reference = str(marker.get("name_reference") or "").strip()
    if not isinstance(stated_price, int) or stated_price <= 0 or not name_reference:
        return False
    if not str(marker.get("candidate_id") or marker.get("external_id") or "").strip():
        return False

    candidates = _get_last_search_candidates(state)
    if not candidates:
        return False

    identified: Optional[Dict[str, Any]] = None
    for product in candidates:
        if _candidate_identity_matches(product, marker):
            identified = product
            break
    if identified is None:
        return False
    if _product_normalized_price(identified) != stated_price:
        return False
    if not _product_is_checkout_eligible(identified):
        return False

    hits = _eligible_name_price_hits(name_reference, stated_price, candidates)
    if len(hits) != 1:
        return False
    hit_id, _ = _candidate_identity(hits[0])
    identified_id, _ = _candidate_identity(identified)
    return bool(hit_id) and hit_id == identified_id


def has_verified_structured_unique_selection(decision: Any, state: Any = None) -> bool:
    """Backward-compatible alias when state is available."""
    if state is None:
        return False
    return verify_structured_unique_selection_against_state(decision, state)


def _resolve_name_price_unique_selection(
    norm: str,
    candidates: Sequence[Dict[str, Any]],
) -> Optional[tuple[Dict[str, Any], int, str]]:
    if not candidates or _is_exploratory_price_question(norm):
        return None
    match = _NAME_PRICE_SELECTION_RE.match(norm)
    if not match:
        return None
    name_ref = (match.group("name") or "").strip()
    name_ref = re.sub(r"^ال\s*", "", _normalize_ar(name_ref)).strip()
    stated_price = _product_normalized_price({"price": match.group("price")})
    if not name_ref or stated_price is None:
        return None

    hits = _match_product_by_name(name_ref, candidates)
    hits = [
        product
        for product in hits
        if _product_is_checkout_eligible(product)
        and _product_normalized_price(product) == stated_price
    ]

    if len(hits) != 1:
        return None
    return hits[0], stated_price, name_ref


def _extract_ordinal_pick(norm: str) -> Optional[int]:
    if _PRICE_ORDINAL_RE.search(norm):
        return None
    if _LARGER_SIZE_RE.search(norm):
        return None
    if not _PICK_VERB_RE.search(norm) and not any(
        w in norm for w in ("الاول", "الأول", "اول", "الثاني", "ثاني", "الثالث", "ثالث")
    ):
        return None
    if _PICK_VERB_RE.search(norm) and _SIZE_AVAILABILITY_RE.search(norm):
        return None
    return _extract_ordinal_token(norm)


def _extract_name_pick(norm: str) -> str:
    m = _PICK_VERB_RE.match(norm)
    if not m:
        return ""
    rest = norm[m.end():].strip(" ؟?!.")
    rest = re.sub(r"^(?:ال|the)\s+", "", rest, flags=re.UNICODE)
    if not rest or rest in _ORDINAL_INDEX:
        return ""
    if _extract_ordinal_token(rest):
        return ""
    if len(rest) <= 1:
        return ""
    return rest.strip()


def _family_base(title: str) -> str:
    base = _WEIGHT_STRIP_RE.sub("", title or "")
    base = re.sub(r"\s+", " ", base).strip().lower()
    return base


def find_product_family(
    reference: Dict[str, Any],
    products: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ref_base = _family_base(str(reference.get("title") or reference.get("display_label") or ""))
    if not ref_base:
        return [reference]
    family: List[Dict[str, Any]] = []
    for product in products:
        title = str(product.get("title") or product.get("display_label") or "")
        base = _family_base(title)
        if not base:
            continue
        if ref_base in base or base in ref_base or _token_overlap(ref_base, base) >= 0.6:
            family.append(product)
    return family or [reference]


def _token_overlap(a: str, b: str) -> float:
    ta = {t for t in a.split() if len(t) > 1}
    tb = {t for t in b.split() if len(t) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _product_unit_magnitude(product: Dict[str, Any]) -> Optional[float]:
    unit = parse_unit_from_text(
        " ".join(
            str(product.get(k) or "")
            for k in ("title", "display_label", "variant_name", "size", "weight", "unit")
        )
    )
    if unit and unit.magnitude is not None and unit.kind.value == "weight":
        return float(unit.magnitude)
    bindings = bindings_from_catalog_product(product)
    mags = [
        b.unit.magnitude
        for b in bindings
        if b.unit.magnitude is not None and b.unit.kind.value == "weight"
    ]
    return max(mags) if mags else None


def find_matching_variants(
    products: Sequence[Dict[str, Any]],
    *,
    requested_unit: Optional[UnitSpec] = None,
    message: str = "",
) -> List[Dict[str, Any]]:
    unit = requested_unit or parse_unit_from_text(message or "")
    if not unit:
        return []
    matches: List[Dict[str, Any]] = []
    for product in products:
        label_blob = " ".join(
            str(product.get(k) or "")
            for k in ("title", "display_label", "variant_name", "size", "weight", "unit")
        )
        prod_unit = parse_unit_from_text(label_blob)
        if prod_unit and _units_match(unit, prod_unit):
            matches.append(product)
            continue
        for binding in bindings_from_catalog_product(product):
            if binding.unit and _units_match(unit, binding.unit):
                matches.append(product)
                break
    return matches


def _units_match(requested: UnitSpec, candidate: UnitSpec) -> bool:
    if requested.kind != candidate.kind:
        return False
    if requested.kind.value in {"weight", "volume", "pack", "count"}:
        if requested.magnitude is None or candidate.magnitude is None:
            return requested.normalized_key == candidate.normalized_key
        return abs(requested.magnitude - candidate.magnitude) < 1e-4
    return requested.normalized_key == candidate.normalized_key


def find_larger_sizes(
    reference: Dict[str, Any],
    products: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ref_mag = _product_unit_magnitude(reference)
    if ref_mag is None:
        return []
    family = find_product_family(reference, products)
    larger: List[Dict[str, Any]] = []
    for product in family:
        mag = _product_unit_magnitude(product)
        if mag is not None and mag > ref_mag + 1e-6:
            larger.append(product)
    larger.sort(key=lambda p: _product_unit_magnitude(p) or 0.0)
    return larger


def _resolve_reference_product(state: Any) -> Optional[Dict[str, Any]]:
    selected_id = str(getattr(state, "selected_product_id", "") or "").strip()
    presented = get_presented_products(state)
    if selected_id:
        for product in presented:
            if _product_id(product) == selected_id:
                return product
    if presented:
        return presented[0]
    return None


def _product_at_index(products: Sequence[Dict[str, Any]], index: int) -> Optional[Dict[str, Any]]:
    if index < 1 or index > len(products):
        return None
    return products[index - 1]


def _extract_substantive_fragment_tokens(norm: str) -> List[str]:
    """Distinct tokens for bare multi-word fragment matching (minimum two required)."""
    seen: set[str] = set()
    tokens: List[str] = []
    for raw in (norm or "").split():
        token = raw.strip(".,؟?!")
        if len(token) >= 2 and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _product_label_tokens(product: Dict[str, Any]) -> set[str]:
    parts = [
        str(product.get("display_label") or ""),
        str(product.get("title") or ""),
        str(product.get("label_override") or ""),
    ]
    blob = _normalize_ar(" ".join(p for p in parts if p))
    if not blob:
        return set()
    return {tok for tok in blob.split() if len(tok) >= 2}


def _product_contains_all_tokens(product: Dict[str, Any], tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    label_tokens = _product_label_tokens(product)
    if not label_tokens:
        return False
    return all(token in label_tokens for token in tokens)


def _resolve_unique_presented_fragment(
    message: str,
    presented: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Resolve a bare multi-token fragment against the presented product list."""
    norm = _normalize_ar(message or "")
    if not norm:
        return None
    if any(
        norm == _normalize_ar(str(product.get(key) or "")).strip()
        for product in presented
        for key in ("display_label", "title", "label_override")
        if product.get(key)
    ):
        # Full labels belong to the established explicit-pick flow.
        return None
    tokens = _extract_substantive_fragment_tokens(norm)
    if len(tokens) < 2:
        return None

    hits = [
        product
        for product in presented
        if _product_contains_all_tokens(product, tokens)
        and _product_is_checkout_eligible(product)
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _match_product_by_name(name: str, products: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    norm = _normalize_ar(name)
    if not norm:
        return []
    hits: List[Dict[str, Any]] = []
    for product in products:
        labels = [
            str(product.get("display_label") or ""),
            str(product.get("title") or ""),
            str(product.get("label_override") or ""),
        ]
        for label in labels:
            title = _normalize_ar(label)
            if not title:
                continue
            if norm in title or title in norm:
                hits.append(product)
                break
            if any(tok in title for tok in norm.split() if len(tok) > 2):
                hits.append(product)
                break
    return hits


def _focus_from_product(product: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _product_id(product),
        "title": _display_label(product),
        "price": product.get("price") or product.get("sale_price"),
        "external_id": product.get("external_id") or _product_id(product),
    }


def compose_size_availability(
    products: Sequence[Dict[str, Any]],
    *,
    unit_phrase: str = "بالكيلو",
) -> str:
    lines = [f"نعم، متوفر {unit_phrase}:", ""]
    for i, product in enumerate(products, start=1):
        lines.append(f"{i}. {_display_label(product)} — {_format_price(product)}")
    lines.extend(["", "اكتب رقم المنتج أو اسمه ونكمل طلبك."])
    return "\n".join(lines)


def compose_family_sizes(products: Sequence[Dict[str, Any]]) -> str:
    lines = ["متوفر منه:", ""]
    for product in products:
        lines.append(f"• {_display_label(product)} — {_format_price(product)}")
    lines.extend(["", "أي حجم يناسبك؟"])
    return "\n".join(lines)


def compose_selected_product(product: Dict[str, Any]) -> str:
    return (
        f"تم اختيار:\n{_display_label(product)}\n\n"
        "كم الكمية المطلوبة؟"
    )


@dataclass
class SelectionResolution:
    kind: str
    products: List[Dict[str, Any]] = None
    selected: Optional[Dict[str, Any]] = None
    presentation_text: str = ""
    unit_phrase: str = ""

    def __post_init__(self) -> None:
        if self.products is None:
            self.products = []


def resolve_selection_context(ctx: BrainContext) -> Optional[SelectionResolution]:
    state = ctx.state
    if not has_active_selection_context(state):
        return None
    msg = ctx.message or ""
    norm = _normalize_ar(msg)
    if not norm:
        return None

    presented = get_presented_products(state)
    pool = selection_product_pool(state)
    reference = _resolve_reference_product(state)

    from .candidate_price_selection import (  # noqa: PLC0415
        extract_stated_price_constraint,
        resolve_candidates_by_stated_price,
    )

    # Name+price unique match on last_search_candidates must win over bare
    # stated-price selection. Otherwise a message like «فستان كاجوال بـ 114»
    # collapses to price_select without forced_product / unique marker, and
    # turn_arbiter rewrites the draft to llm_reply (checkout_vs_discovery).
    search_candidates = _get_last_search_candidates(state)
    name_price = _resolve_name_price_unique_selection(norm, search_candidates)
    if name_price is not None:
        product, _stated_price, _name_ref = name_price
        return SelectionResolution(
            kind="name_price_select",
            selected=product,
            presentation_text=compose_selected_product(product),
        )

    # Name+price shaped messages that are not a unique trusted hit must not
    # fall through to bare price_select (draft on price alone). Prefer safe
    # clarify / narrow against last_search_candidates only.
    if _NAME_PRICE_SELECTION_RE.match(norm):
        if not search_candidates:
            return None
        _np_price = resolve_candidates_by_stated_price(msg, search_candidates)
        if _np_price.kind == "clarify":
            return SelectionResolution(
                kind="price_ambiguous",
                products=list(_np_price.candidates),
            )
        if _np_price.kind == "no_match":
            return SelectionResolution(
                kind="price_no_match",
                products=list(_np_price.candidates),
            )
        # Name was present but no unique name+price hit and price alone is
        # ambiguous or unsafe to auto-draft here.
        return None

    # Bare stated-price matching (no name+price shape) stays scoped to
    # last_search_candidates only — never widen when the list is empty.
    if extract_stated_price_constraint(msg) is not None:
        if not search_candidates:
            return None
        _price_pick = resolve_candidates_by_stated_price(msg, search_candidates)
        if _price_pick.kind == "selected" and _price_pick.selected:
            product = _price_pick.selected
            return SelectionResolution(
                kind="price_select",
                selected=product,
                presentation_text=compose_selected_product(product),
            )
        if _price_pick.kind == "clarify":
            return SelectionResolution(
                kind="price_ambiguous",
                products=list(_price_pick.candidates),
            )
        if _price_pick.kind == "no_match":
            return SelectionResolution(
                kind="price_no_match",
                products=list(_price_pick.candidates),
            )

    price_ord = _PRICE_ORDINAL_RE.search(norm)
    if price_ord:
        idx = _ORDINAL_INDEX.get(price_ord.group(1).lower())
        product = _product_at_index(presented, idx or 0)
        if product:
            return SelectionResolution(
                kind="price_ordinal",
                selected=product,
                presentation_text=f"{_display_label(product)} — {_format_price(product)}",
            )
        return None

    if _SAME_PRODUCT_RE.search(norm) and reference:
        unit = parse_unit_from_text(msg)
        family = find_product_family(reference, pool)
        if unit:
            matches = find_matching_variants(family, requested_unit=unit)
            if matches:
                return SelectionResolution(
                    kind="same_product_variant",
                    products=matches,
                    selected=matches[0] if len(matches) == 1 else None,
                    presentation_text=(
                        compose_selected_product(matches[0])
                        if len(matches) == 1
                        else compose_size_availability(
                            matches,
                            unit_phrase=unit.display_label or "بهذا الحجم",
                        )
                    ),
                )
        if len(family) > 1:
            return SelectionResolution(
                kind="family_sizes",
                products=family,
                presentation_text=compose_family_sizes(family),
            )
        return None

    if _LARGER_SIZE_RE.search(norm) and reference:
        larger = find_larger_sizes(reference, pool)
        if larger:
            return SelectionResolution(
                kind="larger_size",
                products=larger,
                presentation_text=compose_size_availability(
                    larger,
                    unit_phrase="بحجم أكبر",
                ),
            )
        return SelectionResolution(
            kind="no_larger_size",
            presentation_text="ما لقينا حجم أكبر من اللي عرضناه — تبغى واحد من الخيارات المعروضة؟",
        )

    if _SIZE_AVAILABILITY_RE.search(norm):
        unit = parse_unit_from_text(msg)
        unit_phrase = unit.display_label if unit else "بهذا الحجم"
        matches = find_matching_variants(pool, requested_unit=unit, message=msg)
        if not matches and unit is None and re.search(r"كيل|kg|kilo", norm):
            unit = parse_unit_from_text("1 kg")
            unit_phrase = "بالكيلو"
            matches = find_matching_variants(pool, requested_unit=unit)
        if matches:
            return SelectionResolution(
                kind="size_availability",
                products=matches,
                unit_phrase=unit_phrase or "بالكيلو",
                presentation_text=compose_size_availability(
                    matches,
                    unit_phrase=unit_phrase or "بالكيلو",
                ),
            )
        return SelectionResolution(
            kind="size_not_found",
            presentation_text="ما لقينا هذا الحجم ضمن الخيارات المعروضة — تبغى نعرض الأحجام المتوفرة؟",
        )

    ordinal = _extract_ordinal_pick(norm)
    if ordinal is not None:
        product = _product_at_index(presented, ordinal)
        if product:
            return SelectionResolution(
                kind="ordinal_select",
                selected=product,
                presentation_text=compose_selected_product(product),
            )
        return None

    name_pick = _extract_name_pick(norm)
    if name_pick:
        hits = _match_product_by_name(name_pick, presented)
        if len(hits) == 1:
            return SelectionResolution(
                kind="name_select",
                selected=hits[0],
                presentation_text=compose_selected_product(hits[0]),
            )
        if len(hits) > 1:
            return SelectionResolution(
                kind="name_ambiguous",
                products=hits,
                presentation_text=compose_size_availability(hits, unit_phrase="من الخيارات"),
            )

    if re.search(r"^(?:ابغ|ابي|أبغ|أبي)\s+(?:ال)?(?:أكبر|اكبر|biggest|larger)\s*[?؟]?\s*$", norm):
        if reference:
            larger = find_larger_sizes(reference, pool)
            if larger:
                return SelectionResolution(
                    kind="want_larger",
                    products=larger,
                    presentation_text=compose_size_availability(larger, unit_phrase="بحجم أكبر"),
                )

    return None


def _selection_patch_from_product(product: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "selected_product_id": _product_id(product),
        "selected_variant_id": str(product.get("variant_id") or product.get("default_variant_id") or ""),
        "selection_context_turn": None,
    }


def try_selection_context_decision(ctx: BrainContext) -> Optional[Decision]:
    """Resolve follow-up turns against last presented discovery products."""
    if not has_active_selection_context(ctx.state):
        return None

    is_explicit_followup = is_selection_followup_message(ctx.message or "")
    if not is_explicit_followup:
        fragment_product = _resolve_unique_presented_fragment(
            ctx.message or "",
            get_presented_products(ctx.state),
        )
        if fragment_product is not None:
            logger.info(
                "[SELECTION_CONTEXT] tenant=%s kind=unique_fragment_select selected=%r preview=%r",
                getattr(ctx, "tenant_id", None),
                (fragment_product or {}).get("title"),
                (ctx.message or "")[:60],
            )
            product_title = str(
                fragment_product.get("title") or fragment_product.get("display_label") or ""
            ).strip()
            return Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={
                    "query": product_title,
                    "source": "selection_context_unique_fragment",
                    "products": [fragment_product],
                    "selection_context_patch": _selection_patch_from_product(fragment_product),
                },
                reason="selection context unique_fragment_select",
                confidence=0.92,
            )

    if not is_explicit_followup:
        return None

    resolution = resolve_selection_context(ctx)
    if resolution is None:
        return None

    logger.info(
        "[SELECTION_CONTEXT] tenant=%s kind=%s products=%d selected=%r preview=%r",
        getattr(ctx, "tenant_id", None),
        resolution.kind,
        len(resolution.products or []),
        (resolution.selected or {}).get("title") if resolution.selected else None,
        (ctx.message or "")[:60],
    )

    if resolution.kind == "price_select" and resolution.selected:
        product = resolution.selected
        return Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "product": _focus_from_product(product),
                "source": "selection_context_price_select",
                "selection_presentation_text": resolution.presentation_text,
                "selection_context_patch": _selection_patch_from_product(product),
                "await_quantity": True,
            },
            reason="selection context stated-price product match",
            confidence=0.93,
        )

    if resolution.kind == "price_ambiguous":
        # Safe clarification — never pick a product or open checkout.
        return Decision(
            action=ACTION_CLARIFY,
            args={
                "topic": "product_price_ambiguity",
                "products": list(resolution.products or []),
            },
            reason="selection context — multiple products at stated price",
            confidence=0.90,
        )

    if resolution.kind == "price_no_match":
        # Present options only — must not create a draft order.
        return Decision(
            action=ACTION_NARROW,
            args={
                "products": list(resolution.products or []),
                "source": "selection_context_price_no_match",
            },
            reason="selection context — stated price unmatched, present options",
            confidence=0.89,
        )

    if resolution.kind in {"ordinal_select", "name_select"} and resolution.selected:
        product = resolution.selected
        return Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "product": _focus_from_product(product),
                "source": "selection_context_ordinal",
                "selection_presentation_text": resolution.presentation_text,
                "selection_context_patch": _selection_patch_from_product(product),
                "await_quantity": True,
            },
            reason=f"selection context {resolution.kind}",
            confidence=0.92,
        )

    if resolution.kind == "name_price_select" and resolution.selected:
        product = resolution.selected
        match = _NAME_PRICE_SELECTION_RE.match(_normalize_ar(ctx.message or ""))
        stated_price = _product_normalized_price({"price": match.group("price")}) if match else None
        name_ref = _normalize_name_reference((match.group("name") or "").strip()) if match else ""
        forced = dict(product)
        forced["candidate_source"] = CANDIDATE_SOURCE_LAST_SEARCH
        marker = _build_structured_unique_selection(
            product,
            kind=NAME_PRICE_CANDIDATE_MATCH,
            candidate_source=CANDIDATE_SOURCE_LAST_SEARCH,
            stated_price=int(stated_price or 0),
            name_reference=name_ref,
        )
        return Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "product": _focus_from_product(product),
                "forced_product": forced,
                "source": "selection_context_name_price",
                "candidate_source": CANDIDATE_SOURCE_LAST_SEARCH,
                STRUCTURED_UNIQUE_SELECTION_KEY: marker,
                "selection_presentation_text": resolution.presentation_text,
                "selection_context_patch": _selection_patch_from_product(product),
                "await_quantity": True,
            },
            reason="selection context name_price_select",
            confidence=0.94,
        )

    if resolution.kind == "price_ordinal" and resolution.selected:
        product = resolution.selected
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "selection_context_price",
                "product": _focus_from_product(product),
                "selection_presentation_text": resolution.presentation_text,
                "selection_context_patch": _selection_patch_from_product(product),
            },
            reason="selection context price for listed product",
            confidence=0.91,
        )

    if resolution.kind == "same_product_variant" and resolution.selected:
        product = resolution.selected
        return Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "product": _focus_from_product(product),
                "source": "selection_context_same_variant",
                "selection_presentation_text": resolution.presentation_text,
                "selection_context_patch": _selection_patch_from_product(product),
                "await_quantity": True,
            },
            reason="selection context same product variant",
            confidence=0.91,
        )

    if resolution.products and resolution.kind in {
        "size_availability",
        "larger_size",
        "want_larger",
        "name_ambiguous",
        "family_sizes",
    }:
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": str(getattr(ctx.state, "last_browse_query", "") or ""),
                "source": f"selection_context_{resolution.kind}",
                "products": list(resolution.products),
                "selection_presentation_text": resolution.presentation_text,
                "selection_context_patch": {
                    "last_presented_products": [
                        normalize_presented_product(p, list_index=i)
                        for i, p in enumerate(resolution.products, start=1)
                    ],
                    "selection_context_turn": int(getattr(ctx.state, "turn", 0) or 0),
                },
            },
            reason=f"selection context {resolution.kind}",
            confidence=0.90,
        )

    if resolution.presentation_text and resolution.kind in {"size_not_found", "no_larger_size"}:
        return Decision(
            action=ACTION_CLARIFY,
            args={
                "question": resolution.presentation_text,
                "topic": "selection_context",
                "source": resolution.kind,
            },
            reason=f"selection context {resolution.kind}",
            confidence=0.84,
        )

    return None


__all__ = [
    "CANDIDATE_SOURCE_LAST_SEARCH",
    "NAME_PRICE_CANDIDATE_MATCH",
    "SELECTION_CONTEXT_TTL_TURNS",
    "STRUCTURED_UNIQUE_SELECTION_KEY",
    "apply_selection_context_patch",
    "compose_family_sizes",
    "compose_selected_product",
    "compose_size_availability",
    "find_larger_sizes",
    "find_matching_variants",
    "find_product_family",
    "get_presented_products",
    "has_active_selection_context",
    "has_verified_structured_unique_selection",
    "verify_structured_unique_selection_against_state",
    "is_selection_followup_message",
    "normalize_presented_product",
    "resolve_selection_context",
    "selection_product_pool",
    "stamp_selection_context_from_products",
    "try_selection_context_decision",
]
