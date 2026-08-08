"""
commerce/state_continuity_identity.py
─────────────────────────────────────
Field-scoped checkout suspend that retains slim product identity hints,
plus fresh catalog re-resolution for state-continuity enforce paths.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.state_continuity_identity")

_IDENTITY_FIELDS = ("id", "external_id", "title")


def extract_identity_hint(state: Any) -> Optional[Dict[str, str]]:
    """Extract identity-only hint before mutating checkout authority state."""
    focus = getattr(state, "current_product_focus", None)
    if isinstance(focus, dict):
        hint: Dict[str, str] = {}
        for key in _IDENTITY_FIELDS:
            val = focus.get(key)
            if val is not None and str(val).strip():
                hint[key] = str(val).strip()
        if hint.get("id") or hint.get("external_id"):
            return hint

    op = getattr(state, "order_prep", None)
    if op is None:
        return None

    # Prefer variant-session parent id — not bare order_prep.product_id alone,
    # which may exist only as checkout slot context without a talk referent.
    pending = str(getattr(op, "pending_variant_product_id", "") or "").strip()
    if pending:
        return {"id": pending}
    return None


def slim_identity_focus(hint: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Retain only catalog identity keys — never price/stock/orderability."""
    if not isinstance(hint, dict):
        return None
    out: Dict[str, str] = {}
    for key in _IDENTITY_FIELDS:
        val = hint.get(key)
        if val is not None and str(val).strip():
            out[key] = str(val).strip()
    if not out.get("id") and not out.get("external_id"):
        return None
    return out


def suspend_checkout_authority_retain_identity(state: Any, *, reason: str) -> None:
    """Suspend checkout/variant authority while keeping identity-only product focus.

    Preserves customer/address order evidence. Does not treat focus snapshots
    (price/stock) as truth — those are stripped from the retained hint.
    """
    if state is None:
        return

    slim = slim_identity_focus(extract_identity_hint(state))

    op = getattr(state, "order_prep", None)
    if op is not None:
        # Variant / checkout progression authority only — not PII or address evidence.
        op.awaiting_variant_choice = False
        op.pending_variant_product_id = ""
        op.selected_variant_id = ""
        op.selected_variant_retailer_id = ""
        op.missing_fields = []
        op.product_id = ""
        op.product_options = {}
        op.product_options_meta = []
        op.product_has_required_options = False
        op.product_options_loaded = False
        op.product_variants_raw = []
        op.predicted_options = {}
        op.awaiting_option_confirmation = False
        op.active_order_quantity_clarification = ""
        op.pending_cart_confirmation = {}
        op.awaiting_checkout_channel = False
        op.catalog_line_items_authoritative = False
        op.catalog_checkout_total = None

    state.draft_order_id = None
    state.checkout_url = None
    state.selected_variant = None
    state.pending_action = ""
    state.last_question_asked = ""
    state.last_question_answered = True
    state.current_product_focus = slim

    if str(getattr(state, "stage", "") or "") in ("ordering", "deciding", "checkout"):
        state.stage = "discovery"

    logger.info(
        "[STATE_CONTINUITY] suspend_checkout retain_identity product_id=%s external_id=%s reason=%s",
        (slim or {}).get("id") or "",
        (slim or {}).get("external_id") or "",
        reason,
    )


def resolve_product_for_state_continuity(
    db: Any,
    tenant_id: int,
    *,
    product_id: str = "",
    external_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Fetch a fresh catalog row for the tenant; empty when not found or tenant mismatch."""
    if db is None or not tenant_id:
        return None

    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    builder = CatalogContextBuilder(db, int(tenant_id))
    ext = str(external_id or "").strip()
    if ext:
        raw = builder.get_by_external_id(ext)
        if raw:
            return dict(raw)

    pid_raw = str(product_id or "").strip()
    if not pid_raw:
        return None

    try:
        from database.models import Product  # noqa: PLC0415

        pid = int(pid_raw)
    except (TypeError, ValueError):
        return None

    row = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id), Product.id == pid)
        .first()
    )
    if row is None:
        return None
    return builder._format(row)


__all__ = [
    "extract_identity_hint",
    "resolve_product_for_state_continuity",
    "slim_identity_focus",
    "suspend_checkout_authority_retain_identity",
]
