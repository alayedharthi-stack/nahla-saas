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
_NUMBERED_LIST_RE = re.compile(
    r"^(?:[-•*]|\d+[\.\)])\s+",
    re.MULTILINE,
)


def _title_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("title") or row.get("name") or row.get("display_label") or "").strip()


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
    ext = str(row.get("external_id") or "").strip()
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

    listed = bool(_NUMBERED_LIST_RE.search(str(reply or ""))) or len(mapped) >= 2
    if listed and len(mapped) >= 2:
        presented = [
            _stamp_row(row, provenance="assistant_presented", turn=turn)
            for row in mapped
        ]
        state.last_presented_products = presented
        state.last_recommended_products = []
        logger.info(
            "[PRESENTED_PROVENANCE] stamped_presented count=%s titles=%s",
            len(presented),
            [row.get("title") for row in presented[:6]],
        )
        return presented

    if len(mapped) == 1:
        recommended = _stamp_row(
            mapped[0],
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
        return [recommended]
    return []


__all__ = [
    "map_named_catalog_entities",
    "stamp_assistant_named_catalog_from_reply",
]
