"""Persist Nahla WA draft at checkout completion and resolve customer-facing reference."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.order_flow_v2.order_reference")


def order_display_reference(order: Any, *, db: Any = None) -> str:
    """Customer-facing order reference from a persisted Order row."""
    if order is None:
        return ""
    ref = str(getattr(order, "external_order_number", "") or "").strip()
    if ref:
        return ref
    order_id = getattr(order, "id", None)
    if db is not None and order_id:
        try:
            from models import Order  # noqa: PLC0415

            row = db.query(Order).filter_by(id=int(order_id)).first()
            if row is not None:
                ref = str(getattr(row, "external_order_number", "") or "").strip()
                if ref:
                    return ref
        except Exception:  # noqa: BLE001
            pass
    ext = str(getattr(order, "external_id", "") or "").strip()
    return ext


def persist_checkout_draft_and_resolve_reference(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """
    Upsert draft/order via Nahla bridge and return (reference, state_patch).

    ``order_creation_status=created`` is stamped only when a real reference exists.
    """
    patch: Dict[str, Any] = {}
    if conversation is None or not getattr(conversation, "id", None):
        return "", patch

    merged_bs = {**(brain_state or {}), "order_prep": dict(order_prep or {})}
    prep = dict(order_prep or {})
    order = None
    try:
        from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415

        order = sync_nahla_wa_order(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            brain_state=merged_bs,
            order_prep=prep,
            trigger="order_flow_v2_checkout_complete",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ORDER_FLOW_V2] checkout draft sync failed tenant=%s: %s",
            tenant_id,
            exc,
        )

    reference = order_display_reference(order, db=db)
    if not reference:
        try:
            from core.order_context_builder import _load_active_draft  # noqa: PLC0415

            draft = _load_active_draft(
                db,
                tenant_id=int(tenant_id),
                conversation_id=getattr(conversation, "id", None),
            )
            if draft is not None and draft.order_id and db is not None:
                from models import Order  # noqa: PLC0415

                row = db.query(Order).filter_by(id=int(draft.order_id)).first()
                reference = order_display_reference(row, db=db)
        except Exception:  # noqa: BLE001
            pass

    if reference:
        patch["order_creation_status"] = "created"
        patch["draft_order_reference"] = reference
        if order is not None and getattr(order, "id", None):
            patch["nahla_order_id"] = int(getattr(order, "id"))

    return reference, patch


__all__ = [
    "order_display_reference",
    "persist_checkout_draft_and_resolve_reference",
]
