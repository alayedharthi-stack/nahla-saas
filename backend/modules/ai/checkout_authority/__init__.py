"""Central checkout authority — local DB draft is first-class checkout evidence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class LocalDraftEvidence:
    """Persisted Nahla WhatsApp draft used as checkout truth."""

    order_id: Optional[int]
    external_id: str
    external_order_number: str
    status: str
    line_items: List[Dict[str, Any]]
    total: Optional[float]
    currency: str
    missing_fields: List[str]

    @property
    def active(self) -> bool:
        return bool(self.order_id or (self.external_id or "").startswith("nahla-wa-"))


def _line_items_from_state(order_prep: Dict[str, Any], brain_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    for container in (order_prep, brain_state):
        raw = container.get("line_items") or container.get("cart_items") or []
        if isinstance(raw, list) and raw:
            return [dict(x) for x in raw if isinstance(x, dict)]
    return []


def _checkout_active_now(order_prep: Dict[str, Any]) -> bool:
    return bool(order_prep.get("order_flow_v2_active"))


def _in_flight_catalog_checkout(order_prep: Dict[str, Any], brain_state: Dict[str, Any]) -> bool:
    if _checkout_active_now(order_prep):
        return True
    if not _line_items_from_state(order_prep, brain_state) and not str(
        order_prep.get("product_id") or ""
    ).strip():
        return False
    return bool(
        order_prep.get("order_flow_v2_trusted_price")
        or order_prep.get("catalog_line_items_authoritative")
        or order_prep.get("order_flow_v2_pending")
    )


def _activate_checkout_patch() -> Dict[str, Any]:
    return {
        "order_flow_v2_active": True,
        "order_flow_v2_pending": False,
        "order_status": "collecting_customer_info",
    }


def load_local_draft_evidence(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int],
) -> Optional[LocalDraftEvidence]:
    if db is None or not conversation_id:
        return None
    try:
        from core.order_context_builder import _load_active_draft  # noqa: PLC0415

        draft = _load_active_draft(
            db,
            tenant_id=int(tenant_id),
            conversation_id=int(conversation_id),
        )
        if draft is None:
            return None

        external_order_number = ""
        if draft.order_id:
            try:
                from models import Order  # noqa: PLC0415

                row = db.query(Order).filter_by(id=int(draft.order_id)).first()
                if row is not None:
                    external_order_number = str(
                        getattr(row, "external_order_number", None) or ""
                    ).strip()
            except Exception:  # noqa: BLE001  # noqa: silent-ok — draft reference read is best-effort
                pass

        items: List[Dict[str, Any]] = []
        for raw in list(draft.line_items or []):
            if isinstance(raw, dict):
                items.append(dict(raw))

        return LocalDraftEvidence(
            order_id=draft.order_id,
            external_id=str(draft.external_id or ""),
            external_order_number=external_order_number,
            status=str(draft.status or ""),
            line_items=items,
            total=draft.total,
            currency=str(draft.currency or "SAR"),
            missing_fields=list(draft.missing_fields or []),
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — local draft load must not break checkout owner
        return None


def rehydrate_order_prep_patch(
    draft: Optional[LocalDraftEvidence],
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge persisted draft into volatile prep when checkout evidence is missing."""
    if draft is None or not draft.active:
        return {}

    patch: Dict[str, Any] = {
        "local_draft_authoritative": True,
        "local_draft_order_id": draft.order_id,
    }

    if draft.external_order_number:
        patch["draft_order_reference"] = draft.external_order_number
        patch["order_creation_status"] = "created"

    if draft.line_items and not _line_items_from_state(order_prep, brain_state):
        patch["line_items"] = draft.line_items
        patch["catalog_line_items_authoritative"] = True
        patch["order_flow_v2_trusted_price"] = True
        if draft.total is not None:
            patch["order_flow_v2_catalog_total"] = draft.total
            patch["order_total"] = draft.total
        if draft.currency:
            patch["order_flow_v2_currency"] = draft.currency

    if draft.missing_fields and not order_prep.get("missing_fields"):
        patch["missing_fields"] = list(draft.missing_fields)

    if not _checkout_active_now(order_prep):
        patch.update(_activate_checkout_patch())

    return patch


def active_whatsapp_checkout(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    *,
    draft: Optional[LocalDraftEvidence] = None,
) -> bool:
    """True when checkout owner must control the turn."""
    if _in_flight_catalog_checkout(order_prep, brain_state):
        return True
    if order_prep.get("local_draft_authoritative"):
        return True
    if draft is not None and draft.active:
        return True
    return False


def checkout_has_items(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    *,
    draft: Optional[LocalDraftEvidence] = None,
) -> bool:
    """Incomplete checkout with product evidence — volatile or persisted."""
    if _line_items_from_state(order_prep, brain_state):
        return True
    if str(order_prep.get("product_id") or "").strip():
        return True
    if draft is not None and draft.line_items:
        return True
    if order_prep.get("local_draft_authoritative"):
        return True
    return False


def draft_display_reference(draft: Optional[LocalDraftEvidence]) -> str:
    if draft is None:
        return ""
    if draft.external_order_number:
        return draft.external_order_number
    return str(draft.external_id or "").strip()


def order_prep_from_any(state: Any) -> Dict[str, Any]:
    if state is None:
        return {}
    if isinstance(state, dict):
        raw = state.get("order_prep") or state
        return dict(raw) if isinstance(raw, dict) else {}
    raw = getattr(state, "order_prep", None)
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def state_has_local_draft_authority(state: Any) -> bool:
    prep = order_prep_from_any(state)
    if prep.get("local_draft_authoritative"):
        return True
    if prep.get("draft_order_reference") and prep.get("order_creation_status") == "created":
        return True
    return False


def brain_payment_paths_should_defer_to_checkout_owner(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
) -> bool:
    """True when Brain/webhook payment bypass must not preempt checkout owner."""
    if conversation is None:
        return False
    meta = dict(getattr(conversation, "extra_metadata", None) or {})
    bs = dict(meta.get("brain_state") or {})
    prep = order_prep_from_any(bs)
    draft = load_local_draft_evidence(
        db,
        tenant_id=int(tenant_id),
        conversation_id=getattr(conversation, "id", None),
    )
    return active_whatsapp_checkout(prep, bs, draft=draft)


def resolve_local_draft_reference_from_db(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int],
) -> str:
    draft = load_local_draft_evidence(
        db,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
    )
    return draft_display_reference(draft)


__all__ = [
    "LocalDraftEvidence",
    "active_whatsapp_checkout",
    "brain_payment_paths_should_defer_to_checkout_owner",
    "checkout_has_items",
    "draft_display_reference",
    "load_local_draft_evidence",
    "order_prep_from_any",
    "rehydrate_order_prep_patch",
    "resolve_local_draft_reference_from_db",
    "state_has_local_draft_authority",
]
