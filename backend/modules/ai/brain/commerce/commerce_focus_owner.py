"""
commerce/commerce_focus_owner.py
────────────────────────────────
Single writer for multi-turn product focus ownership.

Preserves ``previous_product_focus`` across switches, ``suspended_product_focus``
across shipping/tracking digressions, and ``conversation_focus`` mode so
pronoun/ordinal follow-ups resolve from structured state — not phrase trees.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger("nahla.brain.commerce_focus_owner")

FOCUS_PRODUCT = "product"
FOCUS_ORDER_TRACKING = "order_tracking"
FOCUS_SHIPPING_POLICY = "shipping_policy"

_PRODUCT_COMMERCE_INTENTS = frozenset({
    "ask_product",
    "ask_price",
    "product_visual_request",
    "pick_list_item",
    "start_order",
})
_DIGRESSION_INTENTS = frozenset({
    "track_order",
    "ask_shipping",
    "order_history_count",
    "latest_order_summary",
})
# State-only signal: reject the later pick and restore previous_product_focus.
# Does not compose customer text — only updates structured focus.
_ORDINAL_REJECT_RE = re.compile(
    r"(?:لا|مو)\s*(?:أ?قصد|اقصد)?\s*(?:ال)?(?:ثاني|ثانيه|ثانية|2|٢)",
    re.UNICODE | re.IGNORECASE,
)


_STRUCTURED_IDENTITY_KEYS = (
    "external_id",
    "product_retailer_id",
    "sku",
    "id",
    "product_id",
    "variant_id",
)

_STRUCTURED_FACT_KEYS = (
    "id",
    "product_id",
    "variant_id",
    "external_id",
    "sku",
    "product_retailer_id",
    "title",
    "price",
    "sale_price",
    "currency",
    "in_stock",
    "can_checkout",
    "orderable",
    "product_url",
    "image_url",
    "image",
    "thumbnail_url",
    "media_count",
    "media_type",
    "description",
    "body",
    "variants",
    "variants_summary",
    "has_variants",
    "provenance",
    "customer_selected",
    "from_catalog_order",
    "from_native_catalog_order",
)


def product_focus_identity(product: Any) -> str:
    if not isinstance(product, dict):
        return ""
    for key in _STRUCTURED_IDENTITY_KEYS:
        val = str(product.get(key) or "").strip()
        if val:
            return val
    title = str(product.get("title") or product.get("display_label") or "").strip().lower()
    return title


def has_structured_catalog_identity(product: Any) -> bool:
    """True when a catalog row already carries a non-title identity."""
    if not isinstance(product, dict):
        return False
    for key in _STRUCTURED_IDENTITY_KEYS:
        if str(product.get(key) or "").strip():
            return True
    return False


def is_customer_selected_checkout_referent(product: Any) -> bool:
    """Family 2 customer-selected catalog item — not assistant recommendation."""
    if not isinstance(product, dict) or not product:
        return False
    if product.get("customer_selected"):
        return True
    if str(product.get("provenance") or "") == "catalog_order_selected":
        return True
    if product.get("from_catalog_order") or product.get("from_native_catalog_order"):
        return True
    return False


def normalize_structured_product_referent(
    product: Any,
    *,
    provenance: str = "",
    customer_selected: bool = False,
) -> Optional[Dict[str, Any]]:
    """Copy known structured catalog facts only — never invent price or media."""
    if not isinstance(product, dict) or not has_structured_catalog_identity(product):
        return None
    row: Dict[str, Any] = {}
    for key in _STRUCTURED_FACT_KEYS:
        if key not in product:
            continue
        val = product.get(key)
        if val is None or val == "":
            continue
        row[key] = val
    title = str(product.get("title") or product.get("name") or product.get("display_label") or "").strip()
    if title:
        row["title"] = title
    if provenance:
        row["provenance"] = provenance
    elif not row.get("provenance"):
        row["provenance"] = "structured_catalog"
    if customer_selected or row.get("customer_selected"):
        row["customer_selected"] = True
    return row


def checkout_selected_referent(state: Any) -> Optional[Dict[str, Any]]:
    """Consume Family 2 selected-product persistence — do not re-own it."""
    if state is None:
        return None
    try:
        from .assistant_presented_provenance import (  # noqa: PLC0415
            structured_selected_referent,
        )

        ref = structured_selected_referent(state)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — selected referent probe must not block focus
        ref = None
    if isinstance(ref, dict) and has_structured_catalog_identity(ref):
        return dict(ref)
    focus = getattr(state, "current_product_focus", None)
    if is_customer_selected_checkout_referent(focus) and has_structured_catalog_identity(focus):
        return dict(focus)
    return None


def canonical_product_referent(
    state: Any,
    *,
    checkout_active: bool = False,
) -> Optional[Dict[str, Any]]:
    """Single conversational catalog referent.

    Customer-selected checkout referent outranks recommendation/discovery
    while checkout is active. Otherwise effective focus, then a unique
    structured recommendation.
    """
    selected = checkout_selected_referent(state)
    if selected and (checkout_active or is_customer_selected_checkout_referent(selected)):
        return dict(selected)

    focus = get_effective_product_focus(state)
    if focus and has_structured_catalog_identity(focus):
        return dict(focus)

    recommended = [
        dict(row)
        for row in (getattr(state, "last_recommended_products", None) or [])
        if isinstance(row, dict) and has_structured_catalog_identity(row)
    ]
    if len(recommended) == 1:
        return recommended[0]

    presented = [
        dict(row)
        for row in (getattr(state, "last_presented_products", None) or [])
        if isinstance(row, dict) and has_structured_catalog_identity(row)
    ]
    unique_presented = [
        row for row in presented
        if not is_customer_selected_checkout_referent(row)
    ]
    selected_presented = [
        row for row in presented
        if is_customer_selected_checkout_referent(row)
    ]
    if len(selected_presented) == 1:
        return selected_presented[0]
    if len(unique_presented) == 1 and len(presented) == 1:
        return unique_presented[0]

    if selected:
        return dict(selected)
    return dict(focus) if focus else None


def search_results_are_new_customer_product_goal(
    focus: Any,
    candidates: Sequence[Any],
) -> bool:
    """True when this turn's catalog hits are a different product than current focus.

    Identity-only: does not inspect customer phrasing. Empty or unstructured
    candidate lists are not a new product goal.
    """
    focus_id = product_focus_identity(focus)
    if not focus_id:
        return False
    candidate_ids = [
        product_focus_identity(row)
        for row in (candidates or [])
        if product_focus_identity(row)
    ]
    if not candidate_ids:
        return False
    return focus_id not in candidate_ids


def should_keep_live_order_focus_after_product_list(
    focus: Any,
    candidates: Sequence[Any],
    *,
    has_live_order: bool,
    state: Any = None,
) -> bool:
    """Live-order preserve must not block a current-turn different product goal.

    Submitted/draft order rows are out of scope here — this only decides
    whether conversational ``current_product_focus`` stays pinned.
    """
    if search_results_are_new_customer_product_goal(focus, candidates):
        return False
    if has_live_order:
        return True
    return should_preserve_focus_after_product_list_display(
        focus,
        candidates,
        state=state,
    )


def _demote_stale_checkout_selection(state: Any, new_identity: str) -> None:
    """Drop active checkout-selection flags on a different product.

    Does not delete presented history, order_prep, or submitted order ids.
    """
    if state is None or not new_identity:
        return
    presented = list(getattr(state, "last_presented_products", None) or [])
    changed = False
    for row in presented:
        if not isinstance(row, dict):
            continue
        if product_focus_identity(row) == new_identity:
            continue
        if (
            row.get("customer_selected")
            or str(row.get("provenance") or "") == "catalog_order_selected"
            or row.get("from_catalog_order")
            or row.get("from_native_catalog_order")
        ):
            row["customer_selected"] = False
            if str(row.get("provenance") or "") == "catalog_order_selected":
                row["provenance"] = "previous_checkout_selected"
            row.pop("from_catalog_order", None)
            row.pop("from_native_catalog_order", None)
            changed = True
    if changed:
        state.last_presented_products = presented


def _clear_foreign_selected_variant(state: Any, new_identity: str) -> None:
    """A prior product's variant must not travel onto a new product identity."""
    if state is None:
        return
    variant = getattr(state, "selected_variant", None)
    if not isinstance(variant, dict) or not variant:
        return
    variant_product = product_focus_identity(
        {
            "id": variant.get("product_id") or variant.get("id"),
            "external_id": variant.get("external_id") or variant.get("product_retailer_id"),
            "sku": variant.get("sku"),
        }
    )
    if variant_product and new_identity and variant_product == new_identity:
        return
    state.selected_variant = None
    focus = getattr(state, "current_product_focus", None)
    if isinstance(focus, dict) and focus:
        for key in ("variant_id", "variant_label", "unit"):
            focus.pop(key, None)
        state.current_product_focus = focus


def bind_structured_catalog_referent(
    state: Any,
    product: Optional[Dict[str, Any]],
    *,
    reason: str,
    turn: int = 0,
    customer_selected: bool = False,
    current_turn_customer_referent: bool = False,
) -> Optional[Dict[str, Any]]:
    """Bind a product that already has catalog identity via set_product_focus.

    Conversational recommendation must not overwrite a Family 2 selected
    checkout referent. A structured referent resolved for the current
    customer turn may take conversational ownership without deleting
    submitted/draft order rows.
    """
    if state is None:
        return None
    row = normalize_structured_product_referent(
        product,
        provenance=str((product or {}).get("provenance") or reason or "structured_catalog"),
        customer_selected=customer_selected,
    )
    if not row:
        return None

    selected = checkout_selected_referent(state)
    new_id = product_focus_identity(row)
    selected_id = product_focus_identity(selected)
    current_turn_goal = bool(current_turn_customer_referent or customer_selected)
    if (
        selected
        and is_customer_selected_checkout_referent(selected)
        and not current_turn_goal
        and selected_id
        and new_id
        and selected_id != new_id
    ):
        logger.info(
            "[COMMERCE_FOCUS] skip_conversational_overwrite checkout_selected=%r "
            "candidate=%r reason=%s",
            selected_id,
            new_id,
            reason,
        )
        try:
            from .assistant_presented_provenance import (  # noqa: PLC0415
                stamp_structured_presented_products,
            )

            stamp_structured_presented_products(
                state,
                [row],
                provenance=str(row.get("provenance") or "assistant_presented"),
                customer_selected=False,
                turn=turn,
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — presentation stamp must not block selected referent
            pass
        return dict(selected)

    if current_turn_goal and selected_id and new_id and selected_id != new_id:
        _demote_stale_checkout_selection(state, new_id)

    set_product_focus(state, row, reason=reason, turn=turn)
    if current_turn_goal and selected_id and new_id and selected_id != new_id:
        _clear_foreign_selected_variant(state, new_id)
    try:
        from .assistant_presented_provenance import (  # noqa: PLC0415
            stamp_structured_presented_products,
        )

        stamp_structured_presented_products(
            state,
            [row],
            provenance=str(row.get("provenance") or reason),
            customer_selected=bool(row.get("customer_selected")),
            turn=turn,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — presentation stamp is best-effort after focus write
        pass
    return row


def should_preserve_focus_after_product_list_display(
    focus: Any,
    candidates: Sequence[Any],
    state: Any = None,
) -> bool:
    """Preserve focus for checkout-selected items or a matching single search hit."""
    if is_customer_selected_checkout_referent(focus):
        return True
    if state is not None:
        selected = checkout_selected_referent(state)
        if (
            selected
            and product_focus_identity(selected)
            and product_focus_identity(selected) == product_focus_identity(focus)
        ):
            return True
    if not isinstance(focus, dict) or not focus:
        return False
    if len(candidates) != 1:
        return False
    candidate = candidates[0]
    if not isinstance(candidate, dict) or not candidate:
        return False
    focus_id = product_focus_identity(focus)
    candidate_id = product_focus_identity(candidate)
    return bool(focus_id and candidate_id and focus_id == candidate_id)


def get_effective_product_focus(state: Any) -> Optional[Dict[str, Any]]:
    """Current focus, else suspended digression snapshot."""
    current = getattr(state, "current_product_focus", None)
    if isinstance(current, dict) and current:
        return dict(current)
    suspended = getattr(state, "suspended_product_focus", None)
    if isinstance(suspended, dict) and suspended:
        return dict(suspended)
    return None


def set_product_focus(
    state: Any,
    product: Optional[Dict[str, Any]],
    *,
    reason: str,
    turn: int = 0,
    preserve_previous: bool = True,
) -> None:
    """Authoritative product focus write — chains previous on identity change."""
    if state is None:
        return

    new_focus = dict(product) if isinstance(product, dict) and product else None
    current = getattr(state, "current_product_focus", None)
    cur_dict = dict(current) if isinstance(current, dict) and current else None

    new_id = product_focus_identity(new_focus)
    cur_id = product_focus_identity(cur_dict)
    from .catalog_reasoning_evidence import _rows_same_identity  # noqa: PLC0415

    same_identity_rebind = bool(
        new_focus and cur_dict and _rows_same_identity(new_focus, cur_dict)
    )

    if (
        preserve_previous
        and new_id
        and cur_id
        and new_id != cur_id
        and cur_dict
    ):
        state.previous_product_focus = cur_dict

    if same_identity_rebind and cur_dict and new_focus:
        # Ranking must not strip a prior selected/canonical referent (AI-D03).
        for key in (
            "customer_selected",
            "provenance",
            "from_catalog_order",
            "from_native_catalog_order",
        ):
            if cur_dict.get(key) and not new_focus.get(key):
                new_focus[key] = cur_dict.get(key)

    state.current_product_focus = new_focus
    if new_focus:
        state.conversation_focus = FOCUS_PRODUCT
        if turn:
            prior_focus_turn = int(getattr(state, "product_focus_turn", 0) or 0)
            if same_identity_rebind and prior_focus_turn:
                pass
            else:
                state.product_focus_turn = int(turn)
        state.suspended_product_focus = None
        try:
            from .product_visual import stamp_product_focus_metadata  # noqa: PLC0415

            stamp_product_focus_metadata(state, new_focus)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — focus metadata stamp is best-effort
            pass
    elif not getattr(state, "suspended_product_focus", None):
        if str(getattr(state, "conversation_focus", "") or "") == FOCUS_PRODUCT:
            state.conversation_focus = ""

    logger.info(
        "[COMMERCE_FOCUS] set_product_focus reason=%s new_id=%r prev_id=%r "
        "has_previous=%s turn=%s",
        reason,
        new_id or "-",
        cur_id or "-",
        bool(getattr(state, "previous_product_focus", None)),
        turn,
    )


def archive_current_product_focus(state: Any, *, reason: str) -> None:
    """Move current focus to previous without assigning a new product."""
    if state is None:
        return
    current = getattr(state, "current_product_focus", None)
    if not isinstance(current, dict) or not current:
        return
    cur_id = product_focus_identity(current)
    prev = getattr(state, "previous_product_focus", None)
    prev_id = product_focus_identity(prev)
    if cur_id and cur_id != prev_id:
        state.previous_product_focus = dict(current)
    state.current_product_focus = None
    logger.info(
        "[COMMERCE_FOCUS] archived_current reason=%s identity=%r",
        reason,
        cur_id or "-",
    )


def suspend_product_focus(state: Any, *, digression: str) -> None:
    """Preserve product focus while the turn handles shipping/tracking."""
    if state is None:
        return
    current = getattr(state, "current_product_focus", None)
    if isinstance(current, dict) and current:
        state.suspended_product_focus = dict(current)
    state.conversation_focus = str(digression or "").strip() or FOCUS_ORDER_TRACKING
    logger.info(
        "[COMMERCE_FOCUS] suspended digression=%s has_snapshot=%s",
        state.conversation_focus,
        bool(getattr(state, "suspended_product_focus", None)),
    )


def restore_suspended_product_focus(state: Any, *, reason: str = "digression_return") -> bool:
    if state is None:
        return False
    suspended = getattr(state, "suspended_product_focus", None)
    if not isinstance(suspended, dict) or not suspended:
        return False
    set_product_focus(
        state,
        suspended,
        reason=reason,
        turn=int(getattr(state, "turn", 0) or 0),
        preserve_previous=True,
    )
    state.suspended_product_focus = None
    return True


def revert_to_previous_product_focus(state: Any, *, reason: str = "user_correction") -> bool:
    if state is None:
        return False
    previous = getattr(state, "previous_product_focus", None)
    if not isinstance(previous, dict) or not previous:
        return False
    current = getattr(state, "current_product_focus", None)
    cur_dict = dict(current) if isinstance(current, dict) and current else None
    state.previous_product_focus = cur_dict
    set_product_focus(
        state,
        previous,
        reason=reason,
        turn=int(getattr(state, "turn", 0) or 0),
        preserve_previous=False,
    )
    return True


def try_ordinal_correction_focus_swap(state: Any, message: str) -> bool:
    """«لا أقصد الثاني» — swap to previous_product when structured state allows."""
    if state is None or not _ORDINAL_REJECT_RE.search(message or ""):
        return False
    if not isinstance(getattr(state, "previous_product_focus", None), dict):
        return False
    return revert_to_previous_product_focus(state, reason="ordinal_correction_revert")


def clear_order_tracking_focus(state: Any, *, reason: str) -> None:
    """Prevent order-tracking mode from leaking into a fresh product browse."""
    if state is None:
        return
    focus_mode = str(getattr(state, "conversation_focus", "") or "")
    if focus_mode != FOCUS_ORDER_TRACKING:
        return
    state.conversation_focus = ""
    suspended = getattr(state, "suspended_product_focus", None)
    if isinstance(suspended, dict) and suspended and not getattr(state, "current_product_focus", None):
        restore_suspended_product_focus(state, reason=reason)
    logger.info("[COMMERCE_FOCUS] cleared_order_tracking_leak reason=%s", reason)


def bind_variant_to_focus(state: Any, variant_binding: Dict[str, Any]) -> None:
    """Update selected_variant + price on focus without swapping product identity."""
    if state is None or not isinstance(variant_binding, dict):
        return
    state.selected_variant = dict(variant_binding)
    focus = dict(getattr(state, "current_product_focus", None) or {})
    if not focus:
        suspended = getattr(state, "suspended_product_focus", None)
        if isinstance(suspended, dict) and suspended:
            focus = dict(suspended)
    if not focus:
        return
    if variant_binding.get("price") is not None:
        focus["price"] = variant_binding.get("price")
    for key in ("variant_id", "variant_label", "unit"):
        if variant_binding.get(key) is not None:
            focus[key] = variant_binding.get(key)
    pid = product_focus_identity(focus)
    cur_pid = product_focus_identity(getattr(state, "current_product_focus", None))
    if pid and pid == cur_pid:
        state.current_product_focus = focus
    elif getattr(state, "suspended_product_focus", None):
        state.suspended_product_focus = focus


def apply_commerce_focus_lifecycle(
    state: Any,
    *,
    intent_name: str,
    action: str,
    message: str = "",
    turn: int = 0,
) -> None:
    """
    Post-turn focus lifecycle — call once after decision execution patches state.
    """
    if state is None:
        return

    intent = str(intent_name or "").strip().lower()
    act = str(action or "").strip().lower()

    if try_ordinal_correction_focus_swap(state, message):
        return

    if intent in _DIGRESSION_INTENTS or act == "track_order":
        digression = (
            FOCUS_SHIPPING_POLICY
            if intent == "ask_shipping"
            else FOCUS_ORDER_TRACKING
        )
        if get_effective_product_focus(state):
            suspend_product_focus(state, digression=digression)
        else:
            state.conversation_focus = digression
        return

    if intent in _PRODUCT_COMMERCE_INTENTS or act in {
        "search_products",
        "catalog_navigate",
        "propose_draft_order",
        "narrow_results",
    }:
        focus_mode = str(getattr(state, "conversation_focus", "") or "")
        if focus_mode in {FOCUS_ORDER_TRACKING, FOCUS_SHIPPING_POLICY}:
            restore_suspended_product_focus(
                state,
                reason=f"return_from_{focus_mode}",
            )
        if str(getattr(state, "conversation_focus", "") or "") == FOCUS_ORDER_TRACKING:
            clear_order_tracking_focus(state, reason="shopping_continuation")

    if act == "catalog_navigate" or (
        intent in {"ask_product", "start_order"} and act in {"search_products", "catalog_navigate"}
    ):
        clear_order_tracking_focus(state, reason="fresh_product_browse")


__all__ = [
    "FOCUS_ORDER_TRACKING",
    "FOCUS_PRODUCT",
    "FOCUS_SHIPPING_POLICY",
    "apply_commerce_focus_lifecycle",
    "archive_current_product_focus",
    "bind_structured_catalog_referent",
    "bind_variant_to_focus",
    "canonical_product_referent",
    "checkout_selected_referent",
    "clear_order_tracking_focus",
    "get_effective_product_focus",
    "has_structured_catalog_identity",
    "is_customer_selected_checkout_referent",
    "normalize_structured_product_referent",
    "product_focus_identity",
    "restore_suspended_product_focus",
    "revert_to_previous_product_focus",
    "search_results_are_new_customer_product_goal",
    "set_product_focus",
    "should_keep_live_order_focus_after_product_list",
    "should_preserve_focus_after_product_list_display",
    "suspend_product_focus",
    "try_ordinal_correction_focus_swap",
]
