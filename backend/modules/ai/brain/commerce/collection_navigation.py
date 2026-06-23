"""
commerce/collection_navigation.py
──────────────────────────────────
P0 — deterministic catalog group navigation (list → pick → products → back).

Operational routing only; reply wording stays with DiscoveryPresentationComposer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..decision.actions import ACTION_SEARCH_PRODUCTS
from ..discovery.entry import GLOBAL_BROWSE
from ..types import BrainContext, Decision
from .discovery_strategy import (
    CatalogContextSnapshot,
    resolve_discovery_strategy,
    strategy_to_decision_args,
)
from .selection_context import SELECTION_CONTEXT_TTL_TURNS, get_presented_products

logger = logging.getLogger("nahla.brain.collection_navigation")

_DIA = r"[\u064B-\u065F\u0640]"

_BACK_TO_COLLECTIONS_RE = re.compile(
    r"(?:"
    r"رجع(?:ني|وني|)\s*(?:ل)?(?:ل)?(?:ال)?(?:اقسام|أقسام|مجموعات|الأقسام|الاقسام)"
    r"|ارجع\s*(?:ل)?(?:ال)?(?:اقسام|أقسام|مجموعات)"
    r"|(?:ارجع|رجع)\s*(?:ل)?(?:قائمة|list)\s*(?:ال)?(?:اقسام|أقسام|مجموعات)"
    r"|عرض\s*(?:ال)?(?:اقسام|أقسام|مجموعات)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SWITCH_COLLECTION_RE = re.compile(
    r"(?:"
    r"(?:اب[ىي]|أب[ىي]|ودي)\s*(?:مجموع[ةه]|قسم)\s*(?:ثاني(?:[ةه])?|اخر(?:[ىي])|أخر(?:[ىي]))"
    r"|(?:مجموع[ةه]|قسم)\s*ثاني(?:[ةه])?"
    r"|(?:اقسام|أقسام)\s*(?:ثانية|ثاني|اخرى|أخرى)"
    r"|وش\s*(?:ال)?(?:اقسام|أقسام)\s*(?:ال)?(?:ثانية|ثاني|اخر(?:[ىي])|أخر(?:[ىي]))"
    r"|غير\s*(?:ال)?(?:قسم|مجموع[ةه])"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_COLLECTION_PICK_RE = re.compile(
    r"^(?:اب[ىي]|أب[ىي]|اختر|اختار|اريد|أريد|ودي|this|that)?\s*"
    r"(?:ال)?(\d+|الاول|الأول|اول|الثاني|الثانية|ثاني|الثالث|الثالثة|ثالث|"
    r"١|٢|٣|1|2|3|4|5|6|7|8|9|10)\s*[?؟.]?\s*$",
    re.UNICODE | re.IGNORECASE,
)

_NAME_PICK_RE = re.compile(
    r"^(?:اب[ىي]|أب[ىي]|اختر|اختار|اريد|أريد|ودي|this|that)\s+(?:ال)?(.{2,60})\s*[?؟.]?\s*$",
    re.UNICODE | re.IGNORECASE,
)

_ORDINAL_INDEX: Dict[str, int] = {
    "الاول": 1, "الأول": 1, "اول": 1, "١": 1, "1": 1,
    "الثاني": 2, "الثانية": 2, "ثاني": 2, "٢": 2, "2": 2,
    "الثالث": 3, "الثالثة": 3, "ثالث": 3, "٣": 3, "3": 3,
    "4": 4, "٤": 4, "5": 5, "٥": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
}


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(_DIA, "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def get_presented_collections(state: Any) -> List[Dict[str, Any]]:
    rows = list(getattr(state, "last_presented_collections", None) or [])
    return [dict(r) if isinstance(r, dict) else {"label": str(r)} for r in rows]


def has_active_collection_navigation_context(state: Any) -> bool:
    collections = get_presented_collections(state)
    if len(collections) < 2:
        return False
    turn = int(getattr(state, "turn", 0) or 0)
    ctx_turn = int(getattr(state, "selection_context_turn", 0) or 0)
    if ctx_turn <= 0:
        return True
    return (turn - ctx_turn) <= SELECTION_CONTEXT_TTL_TURNS


def is_back_to_collections_request(message: str) -> bool:
    norm = _normalize_ar(message)
    return bool(norm and _BACK_TO_COLLECTIONS_RE.search(norm))


def is_switch_collection_request(message: str) -> bool:
    norm = _normalize_ar(message)
    return bool(norm and _SWITCH_COLLECTION_RE.search(norm))


def is_collection_navigation_message(message: str) -> bool:
    return (
        is_back_to_collections_request(message)
        or is_switch_collection_request(message)
        or is_collection_pick_message(message)
    )


def is_collection_pick_message(message: str) -> bool:
    norm = _normalize_ar(message)
    if not norm:
        return False
    if _COLLECTION_PICK_RE.match(norm):
        return True
    if _NAME_PICK_RE.match(norm):
        return True
    tokens = norm.split()
    if len(tokens) == 1 and tokens[0] in _ORDINAL_INDEX:
        return True
    return False


def _collection_label(row: Dict[str, Any]) -> str:
    return str(
        row.get("group_name")
        or row.get("label")
        or row.get("name")
        or ""
    ).strip()


def _collection_db_id(row: Dict[str, Any]) -> Optional[int]:
    raw = row.get("group_db_id", row.get("db_id"))
    if raw is None:
        raw = row.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _collection_id(row: Dict[str, Any]) -> str:
    return str(
        row.get("group_slug")
        or row.get("group_id")
        or row.get("slug")
        or _collection_label(row)
    ).strip()


def _match_collection_by_name(name: str, collections: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    norm = _normalize_ar(name)
    if not norm or len(norm) <= 1:
        return []
    hits: List[Dict[str, Any]] = []
    for row in collections:
        label = _normalize_ar(_collection_label(row))
        if not label:
            continue
        if norm == label or norm in label or label in norm:
            hits.append(row)
            continue
        if any(tok in label for tok in norm.split() if len(tok) > 2):
            hits.append(row)
    return hits


@dataclass(frozen=True)
class CollectionResolution:
    group_id: str
    group_slug: str
    group_name: str
    group_db_id: Optional[int] = None
    list_index: int = 0


def _resolution_from_row(row: Dict[str, Any], *, list_index: int) -> CollectionResolution:
    slug = str(row.get("group_slug") or row.get("group_id") or row.get("slug") or "").strip()
    return CollectionResolution(
        group_id=_collection_id(row),
        group_slug=slug,
        group_name=_collection_label(row),
        group_db_id=_collection_db_id(row),
        list_index=list_index,
    )


def resolve_collection_pick(
    message: str,
    collections: Sequence[Dict[str, Any]],
) -> Optional[CollectionResolution]:
    rows = [dict(c) for c in collections if _collection_label(c)]
    if not rows:
        return None
    norm = _normalize_ar(message)

    m = _COLLECTION_PICK_RE.match(norm)
    if m:
        token = (m.group(1) or "").strip()
        idx = _ORDINAL_INDEX.get(token, None)
        if idx is None and token.isdigit():
            idx = int(token)
        if idx is not None and 1 <= idx <= len(rows):
            row = rows[idx - 1]
            return _resolution_from_row(row, list_index=idx)

    if len(norm.split()) == 1 and norm in _ORDINAL_INDEX:
        idx = _ORDINAL_INDEX[norm]
        if 1 <= idx <= len(rows):
            row = rows[idx - 1]
            return _resolution_from_row(row, list_index=idx)

    name_m = _NAME_PICK_RE.match(norm)
    name = (name_m.group(1) if name_m else norm).strip(" ؟?!.")
    name = re.sub(r"^(?:ال|the)\s+", "", name, flags=re.UNICODE)
    if name in _ORDINAL_INDEX:
        return None
    hits = _match_collection_by_name(name, rows)
    if len(hits) == 1:
        row = hits[0]
        return _resolution_from_row(
            row,
            list_index=int(row.get("list_index") or 0),
        )
    return None


def _strategy_args(ctx: BrainContext, *, collection_count: int) -> Dict[str, Any]:
    from .commerce_objective import COMMERCE_OBJECTIVE_DISCOVERY, get_commerce_objective  # noqa: PLC0415

    facts = getattr(ctx, "facts", None)
    strategy = resolve_discovery_strategy(
        commerce_objective=get_commerce_objective(ctx.state) or COMMERCE_OBJECTIVE_DISCOVERY,
        entry_type=GLOBAL_BROWSE,
        catalog_context=CatalogContextSnapshot(
            product_count=int(getattr(facts, "product_count", 0) or 0),
            collection_count=max(collection_count, len(get_presented_collections(ctx.state))),
        ),
    )
    return strategy_to_decision_args(strategy)


def _collections_list_decision(ctx: BrainContext, *, reason: str) -> Decision:
    collections = get_presented_collections(ctx.state)
    args = _strategy_args(ctx, collection_count=len(collections))
    args.update({
        "query": "",
        "source": "browse_catalog_groups",
        "discovery_entry_type": GLOBAL_BROWSE,
        "collection_navigation_patch": {
            "selected_collection": "",
            "last_presented_products": [],
        },
    })
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args=args,
        reason=reason,
        confidence=0.91,
    )


def _collection_products_decision(
    ctx: BrainContext,
    resolution: CollectionResolution,
    *,
    reason: str,
) -> Decision:
    args = _strategy_args(
        ctx,
        collection_count=len(get_presented_collections(ctx.state)),
    )
    args.update({
        "query": resolution.group_name,
        "source": "collections_first",
        "catalog_group_id": resolution.group_id,
        "catalog_group_slug": resolution.group_slug,
        "catalog_group_db_id": resolution.group_db_id,
        "discovery_entry_type": GLOBAL_BROWSE,
        "collection_navigation_patch": {
            "selected_collection": resolution.group_id or resolution.group_slug,
            "last_presented_products": [],
            "current_catalog_group": {
                "group_db_id": resolution.group_db_id,
                "group_id": resolution.group_id,
                "group_slug": resolution.group_slug,
                "group_name": resolution.group_name,
            },
        },
    })
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args=args,
        reason=reason,
        confidence=0.92,
    )


def try_collection_navigation_decision(ctx: BrainContext) -> Optional[Decision]:
    """Resolve group list navigation turns before product selection."""
    msg = ctx.message or ""
    state = ctx.state
    if not getattr(ctx.facts, "has_products", False):
        return None

    if is_back_to_collections_request(msg) or is_switch_collection_request(msg):
        if get_presented_collections(state) or has_active_collection_navigation_context(state):
            logger.info(
                "[COLLECTION_NAV] back tenant=%s preview=%r",
                getattr(ctx, "tenant_id", None),
                msg[:60],
            )
            return _collections_list_decision(
                ctx,
                reason="collection navigation — back to group list",
            )
        return None

    if not has_active_collection_navigation_context(state):
        return None

    if get_presented_products(state) and str(getattr(state, "selected_collection", "") or "").strip():
        return None

    if not is_collection_pick_message(msg):
        return None

    resolution = resolve_collection_pick(msg, get_presented_collections(state))
    if resolution is None:
        return None

    logger.info(
        "[COLLECTION_NAV] pick tenant=%s group=%r idx=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        resolution.group_name,
        resolution.list_index or "-",
        msg[:60],
    )
    return _collection_products_decision(
        ctx,
        resolution,
        reason=f"collection navigation — selected group {resolution.group_name!r}",
    )


__all__ = [
    "CollectionResolution",
    "get_presented_collections",
    "has_active_collection_navigation_context",
    "is_back_to_collections_request",
    "is_collection_navigation_message",
    "is_collection_pick_message",
    "is_switch_collection_request",
    "resolve_collection_pick",
    "try_collection_navigation_decision",
]
