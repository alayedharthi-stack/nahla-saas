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


def product_focus_identity(product: Any) -> str:
    if not isinstance(product, dict):
        return ""
    for key in ("external_id", "id", "product_id", "sku"):
        val = str(product.get(key) or "").strip()
        if val:
            return val
    title = str(product.get("title") or product.get("display_label") or "").strip().lower()
    return title


def should_preserve_focus_after_product_list_display(
    focus: Any,
    candidates: Sequence[Any],
) -> bool:
    """Preserve focus when a single displayed candidate matches the current focus identity."""
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

    if (
        preserve_previous
        and new_id
        and cur_id
        and new_id != cur_id
        and cur_dict
    ):
        state.previous_product_focus = cur_dict

    state.current_product_focus = new_focus
    if new_focus:
        state.conversation_focus = FOCUS_PRODUCT
        if turn:
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
        "propose_draft_order",
        "narrow_results",
    }:
        focus_mode = str(getattr(state, "conversation_focus", "") or "")
        if focus_mode in {FOCUS_ORDER_TRACKING, FOCUS_SHIPPING_POLICY}:
            restore_suspended_product_focus(
                state,
                reason=f"return_from_{focus_mode}",
            )

    if intent == "ask_product" and act == "search_products":
        clear_order_tracking_focus(state, reason="fresh_product_browse")


__all__ = [
    "FOCUS_ORDER_TRACKING",
    "FOCUS_PRODUCT",
    "FOCUS_SHIPPING_POLICY",
    "apply_commerce_focus_lifecycle",
    "archive_current_product_focus",
    "bind_variant_to_focus",
    "clear_order_tracking_focus",
    "get_effective_product_focus",
    "product_focus_identity",
    "restore_suspended_product_focus",
    "revert_to_previous_product_focus",
    "set_product_focus",
    "should_preserve_focus_after_product_list_display",
    "suspend_product_focus",
    "try_ordinal_correction_focus_swap",
]
