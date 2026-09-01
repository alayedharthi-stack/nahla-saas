"""
assistant_presented_provenance.py
─────────────────────────────────
Map LLM-named catalog titles back onto real catalog entities.

Writes existing ``last_presented_products`` / ``last_recommended_products``
only. Does not set customer selection or ``current_product_focus``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from modules.ai.brain.commerce.commerce_focus_owner import product_focus_identity

logger = logging.getLogger("nahla.brain.assistant_presented_provenance")

_ORDER_OWNER_INTENTS = frozenset({
    "track_order",
    "latest_order_summary",
    "order_history_count",
    "order_reference_list",
    "ask_shipping",
})

_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _title_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("title") or row.get("name") or row.get("display_label") or row.get("product_name") or "").strip()


def _price_of(row: Any) -> Optional[float]:
    if not isinstance(row, dict):
        return None
    raw = row.get("price") if row.get("price") not in (None, "") else row.get("sale_price")
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _prices_in_text(text: str) -> set[float]:
    out: set[float] = set()
    for token in _PRICE_RE.findall(text or ""):
        try:
            out.add(float(token))
        except ValueError:
            continue
    return out


def _image_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(
        row.get("image_url")
        or row.get("image")
        or row.get("product_image_url")
        or row.get("thumbnail_url")
        or ""
    ).strip()


def _prefer_representative(rows: Sequence[Any]) -> Optional[Dict[str, Any]]:
    ranked = [row for row in rows if isinstance(row, dict)]
    if not ranked:
        return None

    def _key(row: Dict[str, Any]) -> tuple[int, int]:
        return (1 if _image_of(row) else 0, 1 if row.get("in_stock", True) else 0)

    return max(ranked, key=_key)


def _collapse_same_title(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _title_of(row).casefold()
        if not title:
            continue
        grouped.setdefault(title, []).append(row)
    collapsed: List[Dict[str, Any]] = []
    for group in grouped.values():
        picked = _prefer_representative(group)
        if picked is not None:
            collapsed.append(picked)
    return collapsed


def _stamp_row(
    row: Dict[str, Any],
    *,
    provenance: str,
    turn: int,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "title": _title_of(row),
        "provenance": provenance,
        "customer_selected": False,
    }
    pid = row.get("id") if row.get("id") is not None else row.get("product_id")
    if pid is not None:
        item["id"] = pid
    ext = str(
        row.get("external_id")
        or row.get("product_retailer_id")
        or row.get("sku")
        or ""
    ).strip()
    if ext:
        item["external_id"] = ext
    image_url = str(
        row.get("image_url")
        or row.get("image")
        or row.get("product_image_url")
        or row.get("thumbnail_url")
        or ""
    ).strip()
    if image_url:
        item["image_url"] = image_url
    price = row.get("price") if row.get("price") not in (None, "") else row.get("sale_price")
    if price not in (None, ""):
        item["price"] = price
    if "in_stock" in row:
        item["in_stock"] = bool(row.get("in_stock"))
    if turn:
        item["presented_turn"] = int(turn)
    return item


def _ensure_in_presented(state: Any, row: Dict[str, Any]) -> None:
    current = list(getattr(state, "last_presented_products", None) or [])
    ident = product_focus_identity(row)
    for existing in current:
        if isinstance(existing, dict) and product_focus_identity(existing) == ident:
            return
    presented = dict(row)
    presented["provenance"] = presented.get("provenance") or "assistant_presented"
    presented["customer_selected"] = False
    current.append(presented)
    state.last_presented_products = current


def map_named_catalog_entities(
    text: str,
    candidates: Sequence[Any],
    *,
    prefer_rows: Optional[Sequence[Any]] = None,
) -> List[Dict[str, Any]]:
    """Return catalog rows whose titles the assistant actually named."""
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        _text_references_product,
    )

    body = str(text or "").strip()
    if not body:
        return []

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for raw in candidates or []:
        if not isinstance(raw, dict):
            continue
        title = _title_of(raw)
        if not title or not _text_references_product(body, title):
            continue
        grouped.setdefault(title.casefold(), []).append(raw)

    prefer_ids = {
        product_focus_identity(row)
        for row in (prefer_rows or [])
        if isinstance(row, dict) and product_focus_identity(row)
    }
    prices = _prices_in_text(body)
    named: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for rows in grouped.values():
        picked = list(rows)
        if prefer_ids:
            subset = [
                row for row in picked
                if product_focus_identity(row) in prefer_ids
            ]
            if subset:
                picked = subset
        if len(picked) > 1 and prices:
            priced = []
            for row in picked:
                price = _price_of(row)
                if price is not None and price in prices:
                    priced.append(row)
            if priced:
                picked = priced
        for row in picked:
            ident = product_focus_identity(row)
            if not ident or ident in seen:
                continue
            seen.add(ident)
            named.append(row)
    return named


def stamp_assistant_named_catalog_from_reply(
    *,
    state: Any,
    reply: str,
    catalog_candidates: Optional[Sequence[Any]] = None,
    intent_name: str = "",
    turn: int = 0,
) -> List[Dict[str, Any]]:
    """Persist assistant-named catalog entities without marking them selected."""
    if state is None:
        return []
    intent = str(intent_name or "").strip()
    if intent in _ORDER_OWNER_INTENTS:
        return []

    candidates = [row for row in (catalog_candidates or []) if isinstance(row, dict)]
    if not candidates:
        return []

    existing_presented = [
        row for row in (getattr(state, "last_presented_products", None) or [])
        if isinstance(row, dict)
    ]
    mapped = map_named_catalog_entities(
        reply,
        candidates,
        prefer_rows=existing_presented,
    )
    if not mapped:
        return []

    titles = {_title_of(row).casefold() for row in mapped if _title_of(row)}
    multi_title = len(titles) >= 2
    if multi_title:
        presented = [
            _stamp_row(row, provenance="assistant_presented", turn=turn)
            for row in _collapse_same_title(mapped)
        ]
        state.last_presented_products = presented
        state.last_recommended_products = []
        logger.info(
            "[PRESENTED_PROVENANCE] stamped_presented count=%s titles=%s",
            len(presented),
            [row.get("title") for row in presented[:6]],
        )
        return presented

    picked = _prefer_representative(mapped)
    if picked is None:
        return []
    recommended = _stamp_row(
        picked,
        provenance="assistant_recommended",
        turn=turn,
    )
    state.last_recommended_products = [recommended]
    _ensure_in_presented(state, recommended)
    logger.info(
        "[PRESENTED_PROVENANCE] stamped_recommended id=%r title=%r",
        recommended.get("id") or recommended.get("external_id"),
        recommended.get("title"),
    )
    try:
        from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: PLC0415
            bind_structured_catalog_referent,
            has_structured_catalog_identity,
        )

        if has_structured_catalog_identity(recommended):
            bind_structured_catalog_referent(
                state,
                recommended,
                reason="assistant_recommended_structured",
                turn=turn,
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — focus bind must not block presented stamp
        logger.exception("[PRESENTED_PROVENANCE] structured focus bind failed")
    return [recommended]


def structured_selected_referent(state: Any) -> Optional[Dict[str, Any]]:
    """Return the customer-selected structured product referent, if any.

    Prefers ``customer_selected`` / ``catalog_order_selected`` rows over
    assistant-presented catalog chrome. Does not invent intent.
    """
    presented = list(getattr(state, "last_presented_products", None) or [])
    selected = [
        row for row in presented
        if isinstance(row, dict) and row.get("customer_selected")
    ]
    if selected:
        return dict(selected[-1])
    catalog_selected = [
        row for row in presented
        if isinstance(row, dict)
        and str(row.get("provenance") or "") == "catalog_order_selected"
    ]
    if catalog_selected:
        return dict(catalog_selected[-1])
    focus = getattr(state, "current_product_focus", None)
    if isinstance(focus, dict) and (
        focus.get("from_catalog_order") or focus.get("from_native_catalog_order")
    ):
        title = str(focus.get("title") or focus.get("product_name") or "").strip()
        if title or focus.get("id") is not None or focus.get("external_id"):
            return dict(focus)
    return None


def restore_selected_product_focus(state: Any) -> Optional[Dict[str, Any]]:
    """Pin structured selected-product referent as current focus when missing.

    Does not invent purchase intent. Lets a later natural purchase reuse the
    already-selected item instead of reopening catalog discovery.
    """
    if state is None:
        return None
    ref = structured_selected_referent(state)
    if not ref:
        return None
    focus = getattr(state, "current_product_focus", None)
    if isinstance(focus, dict) and (
        focus.get("id") is not None
        or str(focus.get("title") or "").strip()
        or focus.get("from_catalog_order")
        or focus.get("from_native_catalog_order")
    ):
        return ref
    try:
        from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: PLC0415
            bind_structured_catalog_referent,
        )

        bound = bind_structured_catalog_referent(
            state,
            {
                "id": ref.get("id"),
                "title": str(ref.get("title") or ref.get("product_name") or "").strip(),
                "price": ref.get("price"),
                "external_id": str(ref.get("external_id") or "").strip(),
                "product_retailer_id": str(
                    ref.get("external_id") or ref.get("product_retailer_id") or ""
                ).strip(),
                "from_catalog_order": True,
                "from_native_catalog_order": True,
                "customer_selected": True,
                "restored_from_structured_referent": True,
            },
            reason="restore_selected_product_focus",
            turn=int(getattr(state, "turn", 0) or 0),
            customer_selected=True,
        )
        if bound:
            return bound
    except Exception:  # noqa: BLE001  # noqa: silent-ok — restore must still pin selected identity
        pass
    state.current_product_focus = {
        "id": ref.get("id"),
        "title": str(ref.get("title") or ref.get("product_name") or "").strip(),
        "price": ref.get("price"),
        "external_id": str(ref.get("external_id") or "").strip(),
        "product_retailer_id": str(
            ref.get("external_id") or ref.get("product_retailer_id") or ""
        ).strip(),
        "from_catalog_order": True,
        "from_native_catalog_order": True,
        "customer_selected": True,
        "restored_from_structured_referent": True,
    }
    return ref


def stamp_structured_presented_products(
    state: Any,
    rows: Sequence[Any],
    *,
    provenance: str,
    customer_selected: bool = False,
    turn: int = 0,
    replace: bool = False,
) -> List[Dict[str, Any]]:
    """Persist structured catalog/UI product referents on existing slots only."""
    if state is None:
        return []
    stamped: List[Dict[str, Any]] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        item = _stamp_row(raw, provenance=provenance, turn=turn)
        if (
            not item.get("title")
            and item.get("id") is None
            and not item.get("external_id")
            and not item.get("product_retailer_id")
        ):
            continue
        item["customer_selected"] = bool(customer_selected)
        item["provenance"] = provenance
        stamped.append(item)
    if not stamped:
        return []
    if replace:
        state.last_presented_products = stamped
        return stamped
    for item in stamped:
        ident = product_focus_identity(item)
        current = list(getattr(state, "last_presented_products", None) or [])
        found = False
        for existing in current:
            if isinstance(existing, dict) and product_focus_identity(existing) == ident:
                existing.update(item)
                found = True
                break
        if not found:
            current.append(item)
        state.last_presented_products = current
    return list(getattr(state, "last_presented_products", None) or [])


def apply_turn_catalog_referent_binding(
    *,
    state: Any,
    reply: str,
    catalog_candidates: Optional[Sequence[Any]] = None,
    intent_name: str = "",
    turn: int = 0,
    structured_product: Optional[Dict[str, Any]] = None,
    customer_selected: bool = False,
    current_turn_customer_referent: bool = True,
) -> List[Dict[str, Any]]:
    """Bind structured identity first; title matching is compatibility fallback only.

    ``current_turn_customer_referent`` is True when ``structured_product`` is an
    explicit customer-owned catalog identity for this turn. Pass False for
    assistant recommendations, unique ``products`` lists, replay, narrow, and
    recommend_addon so Family 2 checkout selection is not stolen.
    """
    try:
        from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: PLC0415
            bind_structured_catalog_referent,
            has_structured_catalog_identity,
        )

        if isinstance(structured_product, dict) and has_structured_catalog_identity(
            structured_product
        ):
            bound = bind_structured_catalog_referent(
                state,
                structured_product,
                reason="structured_turn_product",
                turn=turn,
                customer_selected=customer_selected,
                current_turn_customer_referent=bool(
                    current_turn_customer_referent or customer_selected
                ),
            )
            return [bound] if bound else []
    except Exception:  # noqa: BLE001  # noqa: silent-ok — structured bind must not block fallback stamp
        logger.exception("[PRESENTED_PROVENANCE] structured turn bind failed")
    return stamp_assistant_named_catalog_from_reply(
        state=state,
        reply=reply,
        catalog_candidates=catalog_candidates,
        intent_name=intent_name,
        turn=turn,
    )


def _identity_row(container: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    try:
        from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: PLC0415
            has_structured_catalog_identity,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn identity probe must not block compose stamp
        return None
    cand = container.get(key)
    if isinstance(cand, dict) and has_structured_catalog_identity(cand):
        return dict(cand)
    return None


_NON_CUSTOMER_OWNED_RESULT_TYPES = frozenset({
    "recommend_addon",
    "narrow",
})
_NON_CUSTOMER_OWNED_ACTIONS = frozenset({
    "recommend_addon",
    "narrow",
})


def current_turn_executor_catalog_referent(
    decision: Any = None,
    result: Any = None,
) -> Optional[Dict[str, Any]]:
    """Explicit customer-owned catalog identity for this turn.

    Proven signals: executor/result ``product`` or ``focus_product``.

    Not a customer product goal:
    unique ``products`` lists, ``recommended_product``, ``replay_candidates``,
    ``recommend_addon``, or ``narrow``.
    """
    action = str(getattr(decision, "action", "") or "").strip().lower()
    if action in _NON_CUSTOMER_OWNED_ACTIONS:
        return None
    args = dict(getattr(decision, "args", None) or {})
    data = dict(getattr(result, "data", None) or {})
    result_type = str(data.get("type") or "").strip().lower()
    if result_type in _NON_CUSTOMER_OWNED_RESULT_TYPES:
        return None
    for container in (data, args):
        for key in ("product", "focus_product"):
            found = _identity_row(container, key)
            if found is not None:
                return found
    return None


def structured_product_from_turn(decision: Any = None, result: Any = None) -> Optional[Dict[str, Any]]:
    """Identity already known on the decision/result — not inferred from reply text.

    Explicit executor ``product`` / ``focus_product`` outranks ``recommended_product``.
    Unique ``products`` lists are presentation data, not a customer goal.
    """
    found = current_turn_executor_catalog_referent(decision, result)
    if found is not None:
        return found
    args = dict(getattr(decision, "args", None) or {})
    data = dict(getattr(result, "data", None) or {})
    for container in (data, args):
        rec = _identity_row(container, "recommended_product")
        if rec is not None:
            return rec
    return None


__all__ = [
    "apply_turn_catalog_referent_binding",
    "current_turn_executor_catalog_referent",
    "map_named_catalog_entities",
    "restore_selected_product_focus",
    "stamp_assistant_named_catalog_from_reply",
    "stamp_structured_presented_products",
    "structured_product_from_turn",
    "structured_selected_referent",
]
