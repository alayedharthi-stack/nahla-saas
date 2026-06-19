"""
brain/commerce/cart_state.py
────────────────────────────
PR-4 — Apply extracted cart intents to brain state + order_prep.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.brain.cart_state")


def _intent_to_delta(intent: Dict[str, Any], *, cart: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    from core.wa_cart_line_items import line_item_merge_key, merge_line_items, normalize_line_item  # noqa: PLC0415

    action = str(intent.get("action") or "").strip().lower()
    match = dict(intent.get("match") or {})

    if action == "add_item":
        item = normalize_line_item({
            "product_name": intent.get("product_name"),
            "variant":      intent.get("variant") or "",
            "quantity":     intent.get("quantity") or 1,
            "product_id":   intent.get("product_id") or "",
            "source":       "whatsapp_brain",
        })
        return {"op": "add", "item": item}

    if action == "remove_item":
        if not match and intent.get("product_name"):
            match = {"product_name_contains": str(intent["product_name"]).replace("عسل ", "")}
        return {"op": "remove", "match": match}

    if action == "update_quantity":
        qty = intent.get("quantity")
        if not match and intent.get("product_name"):
            match = {"product_name_contains": str(intent["product_name"]).replace("عسل ", "")}
        return {"op": "update_quantity", "match": match, "quantity": qty}

    if action == "increment_quantity":
        if not match and intent.get("product_name"):
            match = {"product_name_contains": str(intent["product_name"]).replace("عسل ", "")}
        merged = merge_line_items(cart)
        current = 0
        needle = str(match.get("product_name_contains") or "").strip()
        for row in merged:
            name = str(row.get("product_name") or "")
            if needle and needle not in name:
                continue
            current = int(row.get("quantity") or 0)
            break
        delta = int(intent.get("delta") or 1)
        return {
            "op": "update_quantity",
            "match": match,
            "quantity": max(current + delta, 1),
        }

    if action == "update_variant":
        if not match:
            pname = str(intent.get("product_name") or "")
            match = {
                "product_name_contains": pname.replace("عسل ", ""),
                "variant": intent.get("old_variant") or "",
            }
        return {
            "op": "update_variant",
            "match": match,
            "variant": intent.get("new_variant") or intent.get("variant"),
        }

    if action == "clear_cart":
        return {"op": "clear"}

    if action == "update_edition":
        return {
            "op": "update_edition",
            "match": match or {},
            "edition": intent.get("edition") or "الجديد",
        }

    return None


def apply_cart_intents_to_state(
    *,
    state: Any,
    prep: Any,
    intents: List[Dict[str, Any]],
    product_info: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """
    Apply cart intents to ``state.cart_items`` and ``order_prep``.

    Returns ``(cart_items, cart_deltas, changed)``.
    """
    from core.wa_cart_line_items import apply_cart_delta, merge_line_items  # noqa: PLC0415

    if not intents:
        return list(getattr(state, "cart_items", None) or []), [], False

    cart = merge_line_items(list(getattr(state, "cart_items", None) or []))
    if not cart:
        prep_items: List[Dict[str, Any]] = []
        if hasattr(prep, "line_items"):
            prep_items = list(getattr(prep, "line_items", None) or [])
        elif isinstance(prep, dict):
            prep_items = list(prep.get("line_items") or [])
        if prep_items:
            cart = merge_line_items(prep_items)

    deltas: List[Dict[str, Any]] = []
    events_total: List[Dict[str, Any]] = []

    for intent in intents:
        if str(intent.get("action") or "").lower() == "clear_cart":
            cart = []
            deltas.append({"op": "clear"})
            continue
        delta = _intent_to_delta(intent, cart=cart)
        if not delta:
            continue
        if delta.get("op") == "clear":
            cart = []
            deltas.append(delta)
            continue
        if delta.get("op") == "update_edition":
            edition = str(delta.get("edition") or "الجديد")
            needle = str((delta.get("match") or {}).get("product_name_contains") or "")
            for row in cart:
                name = str(row.get("product_name") or "")
                if needle and needle not in name:
                    continue
                row["edition"] = edition
                row["notes"] = edition
                break
            deltas.append(delta)
            events_total.append({"type": "edition_updated", "edition": edition})
            continue
        if delta.get("op") == "add" and isinstance(delta.get("item"), dict):
            item = dict(delta["item"])
            if not item.get("query_hint"):
                item["query_hint"] = str(item.get("product_name") or "")
            if product_info:
                if not item.get("product_id"):
                    item["product_id"] = str(
                        product_info.get("external_id") or product_info.get("id") or ""
                    )
                if not item.get("product_name") or item.get("product_name") == "منتج":
                    item["product_name"] = str(product_info.get("title") or item.get("product_name"))
            # Reuse existing line product_id when names match (keeps merge key stable).
            needle = str(item.get("product_name") or "").replace("عسل ", "")
            for row in cart:
                if needle and needle in str(row.get("product_name") or ""):
                    if row.get("product_id"):
                        item["product_id"] = row["product_id"]
                    else:
                        item.pop("product_id", None)
                    row_variant = str(row.get("variant") or "")
                    item_variant = str(item.get("variant") or "")
                    if row_variant and (not item_variant or item_variant == row_variant):
                        item["variant"] = row_variant
                    break
            delta["item"] = item
        cart, events = apply_cart_delta(cart, delta)
        deltas.append(delta)
        events_total.extend(events)

    changed = bool(deltas)
    if changed:
        if hasattr(state, "cart_items"):
            state.cart_items = cart
        if hasattr(prep, "line_items"):
            prep.line_items = cart
        if hasattr(prep, "cart_deltas"):
            prep.cart_deltas = deltas
        elif isinstance(prep, dict):
            prep["line_items"] = cart
            prep["cart_deltas"] = deltas

        if cart:
            last = cart[-1]
            focus = {
                "id":          last.get("product_id"),
                "external_id": last.get("product_id"),
                "title":       last.get("product_name") or last.get("title"),
                "variant":     last.get("variant"),
                "quantity":    last.get("quantity"),
            }
            if hasattr(state, "current_product_focus"):
                state.current_product_focus = focus
            if hasattr(prep, "product_id"):
                prep.product_id = str(last.get("product_id") or prep.product_id or "")
            if hasattr(prep, "quantity"):
                prep.quantity = int(last.get("quantity") or 1)
            if str(last.get("variant") or "").strip():
                if hasattr(prep, "awaiting_variant_choice"):
                    prep.awaiting_variant_choice = False
                if hasattr(state, "awaiting_variant_choice"):
                    state.awaiting_variant_choice = False

        logger.info(
            "[CART_STATE] applied intents=%d cart_size=%d deltas=%d",
            len(intents), len(cart), len(deltas),
        )

    return cart, deltas, changed


def _active_commerce_from_state_and_prep(state: Any, prep: Any) -> bool:
    from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
        has_active_commerce_from_state,
    )

    merged: Dict[str, Any] = {}
    if isinstance(state, dict):
        merged.update(state)
    elif state is not None:
        if hasattr(state, "order_prep"):
            merged["order_prep"] = getattr(state, "order_prep", None)
        for key in (
            "cart_items",
            "current_product_focus",
            "awaiting_option_confirmation",
            "last_question_asked",
            "pending_cart_confirmation",
        ):
            if hasattr(state, key):
                merged[key] = getattr(state, key)
    if prep is not None and "order_prep" not in merged:
        merged["order_prep"] = prep
    return has_active_commerce_from_state(merged)


def maybe_apply_cart_message(
    *,
    state: Any,
    prep: Any,
    message: str,
    product_info: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    from modules.ai.brain.intent.cart_intent_extractor import (  # noqa: PLC0415
        extract_cart_intents_with_context,
    )

    cart = list(getattr(state, "cart_items", None) or [])
    if not cart and hasattr(prep, "line_items"):
        cart = list(getattr(prep, "line_items", None) or [])
    focus = product_info or getattr(state, "current_product_focus", None)
    active_commerce = _active_commerce_from_state_and_prep(state, prep)
    intents = extract_cart_intents_with_context(
        message,
        cart_items=cart,
        product_focus=focus,
        order_prep=prep,
        active_commerce=active_commerce,
    )
    if not intents:
        return list(getattr(state, "cart_items", None) or []), [], False

    if str(intents[0].get("action") or "").lower() == "active_order_clarify":
        reply = str(intents[0].get("reply") or "").strip()
        if reply:
            if hasattr(prep, "active_order_quantity_clarification"):
                prep.active_order_quantity_clarification = reply
            elif isinstance(prep, dict):
                prep["active_order_quantity_clarification"] = reply
        return list(getattr(state, "cart_items", None) or []), [], False

    if hasattr(prep, "active_order_quantity_clarification"):
        prep.active_order_quantity_clarification = ""
    elif isinstance(prep, dict):
        prep.pop("active_order_quantity_clarification", None)

    return apply_cart_intents_to_state(
        state=state,
        prep=prep,
        intents=intents,
        product_info=product_info,
    )


__all__ = [
    "apply_cart_intents_to_state",
    "maybe_apply_cart_message",
]
