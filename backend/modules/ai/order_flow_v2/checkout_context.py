"""Bridge OrderContext saved-address truth into OrderFlowV2 deterministic replies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.order_context_prefill import MODE_CONFIRM
from core.wa_order_lifecycle import has_accepted_delivery_address

from .missing_fields import compute_v2_missing_fields


@dataclass(frozen=True)
class CheckoutReplyContext:
    missing_fields: List[str]
    field_modes: Dict[str, str]
    known_previous: Dict[str, str]
    identity_first_name: str = ""
    # Every durable address the customer may explicitly choose between.
    # Populated whatever the resolution, so several candidates are visible
    # and selectable instead of simply absent.
    address_choices: List[Dict[str, Any]] = field(default_factory=list)


def _shipping_context_dict(previous: Any) -> Dict[str, str]:
    """Project the saved address for the reply layer.

    ``short_address`` is the national SHORT address and stays its own key —
    it is never merged with, or rendered as, a postal code.
    """
    if previous is None:
        return {}
    return {
        "city": str(getattr(previous, "city", "") or "").strip(),
        "district": str(getattr(previous, "district", "") or "").strip(),
        "address_line": str(getattr(previous, "address_line", "") or "").strip(),
        "short_address": str(getattr(previous, "short_address", "") or "").strip(),
        "maps_url": str(getattr(previous, "maps_url", "") or "").strip(),
        "selection_state": (
            "selected"
            if bool(getattr(previous, "explicitly_selected", False))
            else "candidate"
        ),
        "sufficient": "true" if bool(getattr(previous, "sufficient", False)) else "false",
    }


# ── Offered-address lifecycle (R4) ──────────────────────────────────────
#
# Confirmation has to mean "yes, THAT address". Re-reading whatever the row
# holds at confirmation time and trusting its fresh fingerprint proves
# nothing: the row may have been refreshed between the offer and the reply,
# and the customer would be recorded as approving content they never saw.
# So the offer is recorded when it is made, and the confirmation is checked
# against it.
_OFFER_KEY = "address_offer"
_OFFER_SET_KEY = "address_offer_set"


def _conversation_metadata(conversation: Any) -> Dict[str, Any]:
    raw = getattr(conversation, "extra_metadata", None)
    return dict(raw) if isinstance(raw, dict) else {}


def record_offered_address(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    previous: Any,
) -> None:
    """Remember which address revision was put in front of the customer."""
    if conversation is None or previous is None:
        return
    address_id = getattr(previous, "address_id", None)
    fingerprint = str(getattr(previous, "content_fingerprint", "") or "")
    if not address_id or not fingerprint:
        return
    meta = _conversation_metadata(conversation)
    current = meta.get(_OFFER_KEY)
    offer = {
        "address_id": int(address_id),
        "fingerprint": fingerprint,
        "customer_id": int(getattr(conversation, "customer_id", 0) or 0),
        "tenant_id": int(tenant_id),
        "offered_at": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(current, dict) and all(
        current.get(k) == offer[k] for k in ("address_id", "fingerprint", "customer_id", "tenant_id")
    ):
        return
    meta[_OFFER_KEY] = offer
    try:
        conversation.extra_metadata = meta
        db.add(conversation)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — the offer is an aid to a later confirmation; failing to record it makes confirmation refuse, which is the safe direction
        return


def record_offered_address_set(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    candidates: Any,
) -> None:
    """Remember the set of addresses offered for an explicit choice.

    With several candidates there is deliberately no reusable default, so
    the only way forward is the customer naming one. Recording what was
    offered is what makes that naming bindable.
    """
    if conversation is None or not candidates:
        return
    offered = [
        {"address_id": int(c["address_id"]), "fingerprint": str(c["fingerprint"])}
        for c in candidates
        if c.get("address_id") and c.get("fingerprint")
    ]
    if not offered:
        return
    meta = _conversation_metadata(conversation)
    payload = {
        "customer_id": int(getattr(conversation, "customer_id", 0) or 0),
        "tenant_id": int(tenant_id),
        "offered_at": datetime.now(timezone.utc).isoformat(),
        "addresses": offered,
    }
    if meta.get(_OFFER_SET_KEY, {}).get("addresses") == offered:
        return
    meta[_OFFER_SET_KEY] = payload
    try:
        conversation.extra_metadata = meta
        db.add(conversation)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — failing to record the offer makes a later selection refuse, which is the safe direction
        return


def apply_explicit_address_selection(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    address_id: int,
    order_prep: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Select one of the addresses this conversation offered, by id.

    The bounded structured selection path for the several-candidates case.
    It accepts only an address that was actually offered here, at the
    revision it was offered at, and returns the ordinary confirmed-address
    patch so checkout continues exactly as it does after any confirmation.
    """
    from core.customer_address_candidates import (  # noqa: PLC0415
        SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        resolve_customer_address_selection,
    )
    from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415
    from core.order_context_builder import _resolved_address_to_shipping_context  # noqa: PLC0415

    meta = _conversation_metadata(conversation)
    offer_set = meta.get(_OFFER_SET_KEY)
    if not isinstance(offer_set, dict):
        return {}
    if int(offer_set.get("tenant_id") or 0) != int(tenant_id):
        return {}
    customer_id = int(getattr(conversation, "customer_id", 0) or 0)
    if customer_id and int(offer_set.get("customer_id") or 0) != customer_id:
        return {}
    offered = {
        int(o["address_id"]): str(o["fingerprint"])
        for o in (offer_set.get("addresses") or [])
        if o.get("address_id")
    }
    if int(address_id) not in offered:
        return {}
    if has_accepted_delivery_address(dict(order_prep or {})):
        return {}

    resolution = resolve_customer_address_selection(
        db, tenant_id=int(tenant_id), customer_id=customer_id,
    )
    chosen = next(
        (a for a in resolution.selectable if a.address_id == int(address_id)), None
    )
    if chosen is None or chosen.fingerprint != offered[int(address_id)]:
        # Not offered, or changed since it was offered.
        return {}

    previous = _resolved_address_to_shipping_context(chosen)
    if not _record_selection_for_confirmed_address(
        db,
        tenant_id=int(tenant_id),
        previous=previous,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=chosen.fingerprint,
        operation_ref=str(offer_set.get("offered_at") or ""),
    ):
        return {}
    patch = _shipping_context_to_prep_patch(previous)
    patch["customer_confirmed_previous_address"] = True
    patch["shipping_source"] = "customer_selected_address"
    return patch


def read_offered_address(
    *,
    tenant_id: int,
    conversation: Any,
) -> Optional[Dict[str, Any]]:
    """The address revision this conversation last offered, if any."""
    offer = _conversation_metadata(conversation).get(_OFFER_KEY)
    if not isinstance(offer, dict):
        return None
    try:
        if int(offer.get("tenant_id") or 0) != int(tenant_id):
            return None
        conversation_customer = int(getattr(conversation, "customer_id", 0) or 0)
        if conversation_customer and int(offer.get("customer_id") or 0) != conversation_customer:
            return None
        if not int(offer.get("address_id") or 0) or not str(offer.get("fingerprint") or ""):
            return None
    except (TypeError, ValueError):
        return None
    return dict(offer)


def _record_selection_for_confirmed_address(
    db: Any,
    *,
    tenant_id: int,
    previous: Any,
    selection_source: str,
    expected_fingerprint: str = "",
    operation_ref: str = "",
) -> bool:
    """Persist the customer's explicit choice of THIS address revision.

    Bound to the exact revision that was just reviewed: if the stored
    content changed since it was read, nothing is written and the caller
    treats the address as unconfirmed. Idempotent — confirming the same
    revision twice writes once.
    """
    address_id = getattr(previous, "address_id", None)
    if not address_id:
        return False
    try:
        from core.customer_address_candidates import (  # noqa: PLC0415
            record_explicit_address_selection,
        )
        from models import CustomerAddress  # noqa: PLC0415

        row = (
            db.query(CustomerAddress)
            .filter_by(tenant_id=int(tenant_id), id=int(address_id))
            .first()
        )
        if row is None or not getattr(row, "customer_id", None):
            return False
        result = record_explicit_address_selection(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(row.customer_id),
            address_id=int(address_id),
            selection_source=selection_source,
            expected_fingerprint=(
                expected_fingerprint
                or str(getattr(previous, "content_fingerprint", "") or "")
            ),
            operation_ref=operation_ref,
        )
        return bool(result.selected)
    except Exception:  # noqa: BLE001
        return False


# Fields that, on their own, make ``has_accepted_delivery_address`` true.
# An unselected candidate must never contribute them.
_ACCEPTING_PREP_FIELDS = (
    "short_address_code",
    "google_maps_url",
    "delivery_address_url",
    "latitude",
    "longitude",
    "delivery_location_lat",
    "delivery_location_lng",
    "delivery_address_status",
    "pending_delivery_location",
    "whatsapp_location",
)


def _candidate_context_only(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Strip everything that would mark the order's address as accepted."""
    out = {k: v for k, v in patch.items() if k not in _ACCEPTING_PREP_FIELDS}
    out.pop("shipping_source", None)
    out["address_candidate_only"] = True
    return out


def _identity_first_name(ctx: Any) -> str:
    identity = getattr(ctx, "identity", None)
    if identity is None:
        return ""
    first = str(getattr(identity, "first_name", "") or "").strip()
    if first:
        return first
    operational = str(getattr(identity, "operational_name", "") or "").strip()
    if operational:
        return operational.split()[0]
    return ""


def _engine_modes_to_v2(result: Any) -> Dict[str, str]:
    modes = dict(getattr(result, "missing_modes", None) or {})
    out: Dict[str, str] = {}
    if modes.get("name"):
        out["customer_name"] = str(modes["name"])
    if modes.get("city"):
        out["city"] = str(modes["city"])
    if modes.get("delivery_address"):
        out["delivery_address"] = str(modes["delivery_address"])
    return out


def _fallback_modes_from_known_previous(
    order_prep: Dict[str, Any],
    known_previous: Dict[str, str],
) -> Dict[str, str]:
    modes: Dict[str, str] = {}
    if known_previous.get("city") and not str(order_prep.get("city") or "").strip():
        modes["city"] = MODE_CONFIRM
    if (
        (known_previous.get("short_address") or known_previous.get("maps_url"))
        and not has_accepted_delivery_address(order_prep)
    ):
        modes["delivery_address"] = MODE_CONFIRM
    return modes


def load_checkout_reply_context(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> CheckoutReplyContext:
    """Load missing slots + confirm/ask modes from persisted customer/order context."""
    prep = dict(order_prep or {})
    bs = dict(brain_state or {})
    missing = compute_v2_missing_fields(
        prep,
        brain_state=bs,
        whatsapp_phone=customer_phone,
        db=db,
        tenant_id=tenant_id,
        conversation=conversation,
        inbound_metadata=inbound_metadata,
    )

    ctx = None
    known_previous: Dict[str, str] = {}
    address_choices: List[Dict[str, Any]] = []
    field_modes: Dict[str, str] = {}
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415
        from core.order_missing_fields_engine import resolve_flow_missing_fields  # noqa: PLC0415

        ctx = build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            phone=str(customer_phone or ""),
            brain_state=bs,
            inbound_metadata=inbound_metadata,
            build_source="order_flow_v2_reply",
        )
        previous_ctx = getattr(ctx, "known_previous_address", None)
        known_previous = _shipping_context_dict(previous_ctx)
        address_choices = [dict(c) for c in getattr(ctx, "known_address_candidates", ()) or ()]
        if known_previous:
            # This is the moment the address is put in front of the
            # customer; a later confirmation is checked against it.
            record_offered_address(
                db, tenant_id=int(tenant_id), conversation=conversation,
                previous=previous_ctx,
            )
        if address_choices:
            # Several candidates produce no reusable default on purpose.
            # Recording what was offered is what lets the customer choose
            # one explicitly instead of retyping the address.
            record_offered_address_set(
                db, tenant_id=int(tenant_id), conversation=conversation,
                candidates=address_choices,
            )
        _, engine_result = resolve_flow_missing_fields(
            prep,
            brain_state=bs,
            whatsapp_phone=customer_phone,
            db=db,
            tenant_id=tenant_id,
            conversation=conversation,
            inbound_metadata=inbound_metadata,
        )
        if engine_result is not None:
            missing = compute_v2_missing_fields(
                prep,
                brain_state=bs,
                whatsapp_phone=customer_phone,
                db=db,
                tenant_id=tenant_id,
                conversation=conversation,
                inbound_metadata=inbound_metadata,
            )
            field_modes = _engine_modes_to_v2(engine_result)
        elif known_previous:
            field_modes = _fallback_modes_from_known_previous(prep, known_previous)
    except Exception:  # noqa: BLE001
        if known_previous:
            field_modes = _fallback_modes_from_known_previous(prep, known_previous)

    first_name = _identity_first_name(ctx) if ctx is not None else ""
    return CheckoutReplyContext(
        missing_fields=list(missing),
        field_modes=field_modes,
        known_previous=known_previous,
        identity_first_name=first_name,
        address_choices=address_choices,
    )


def load_identity_first_name(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
) -> str:
    try:
        from core.order_context_builder import build_order_context  # noqa: PLC0415

        ctx = build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            phone=str(customer_phone or ""),
            build_source="order_flow_v2_greeting",
        )
        return _identity_first_name(ctx)
    except Exception:  # noqa: BLE001
        return ""


def apply_previous_address_confirmation(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> Dict[str, Any]:
    """Promote saved customer address when the customer confirms previous/on-file address."""
    from core.order_context_builder import build_order_context  # noqa: PLC0415
    from core.order_context_prefill import detect_edit_intent_facts  # noqa: PLC0415
    from modules.ai.brain.commerce.commerce_turn_contract import is_address_on_file_claim  # noqa: PLC0415

    text = str(message or "").strip()
    if not text:
        return {}
    edit = detect_edit_intent_facts(text, order_prep)
    if not (edit.previous_address_confirmed or is_address_on_file_claim(text)):
        return {}

    ctx = build_order_context(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        phone=str(customer_phone or ""),
        brain_state=brain_state,
        inbound_metadata=inbound_metadata,
        message=text,
        build_source="order_flow_v2_address_claim",
    )
    if ctx.known_previous_address is None:
        return {}

    if edit.previous_address_confirmed:
        from core.customer_address_candidates import (  # noqa: PLC0415
            SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        )
        from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

        if bool(getattr(ctx.shipping, "locked_by_merchant", False)):
            return {}
        if has_accepted_delivery_address(dict(order_prep or {})):
            return {}
        previous = ctx.known_previous_address
        # Consent is only consent about a specific offer. Without one, a
        # phrase that merely REFERS to an address on file — an inquiry, for
        # instance — cannot become a durable selection.
        offer = read_offered_address(tenant_id=int(tenant_id), conversation=conversation)
        if offer is None:
            return {}
        if int(offer["address_id"]) != int(getattr(previous, "address_id", 0) or 0):
            # A different address is current than the one offered.
            return {}
        if str(offer["fingerprint"]) != str(getattr(previous, "content_fingerprint", "") or ""):
            # The row changed between the offer and this reply: confirming
            # it would record approval of content the customer never saw.
            return {}

        # The customer confirming THAT address is the selection. Record it
        # durably, bound to the offered revision, so the choice survives a
        # state reset and a new conversation.
        recorded = _record_selection_for_confirmed_address(
            db,
            tenant_id=int(tenant_id),
            previous=previous,
            selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
            expected_fingerprint=str(offer["fingerprint"]),
            operation_ref=str(offer.get("offered_at") or ""),
        )
        if not recorded:
            # A failed revision check is never waved through because an
            # older projection happens to say the address was selected.
            return {}
        patch = _shipping_context_to_prep_patch(previous)
        patch["customer_confirmed_previous_address"] = True
        patch["shipping_source"] = "customer_confirmed_previous_address"
        return patch

    # On-file claim without explicit confirm phrase — reply layer confirms; do not auto-apply.
    return {}


def apply_delivery_continuation_address_patch(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Accept saved / evidenced address when customer asks delivery to their address."""
    from modules.ai.order_flow_v2.slot_ownership import promote_address_evidence_patch  # noqa: PLC0415

    prep = dict(order_prep or {})
    patch = promote_address_evidence_patch(prep)
    merged = {**prep, **patch}
    if has_accepted_delivery_address(merged):
        patch["customer_confirmed_previous_address"] = True
        patch["shipping_source"] = patch.get("shipping_source") or "delivery_continuation"
        return patch

    from core.order_context_builder import build_order_context  # noqa: PLC0415
    from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

    ctx = build_order_context(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        phone=str(customer_phone or ""),
        brain_state=brain_state,
        inbound_metadata=inbound_metadata,
        message="",
        build_source="order_flow_v2_delivery_continuation",
    )
    previous = ctx.known_previous_address
    if previous is None:
        return patch
    if bool(getattr(ctx.shipping, "locked_by_merchant", False)):
        return patch

    saved = _shipping_context_to_prep_patch(previous)
    if not bool(getattr(previous, "explicitly_selected", False)):
        # An imported candidate the customer has never selected is offered,
        # never adopted. Withholding only the confirmation flag is not
        # enough: copying the locating artefacts alone already makes
        # ``has_accepted_delivery_address`` true, so the order would be
        # treated as having an accepted delivery address the customer never
        # chose. Carry only the context fields, which re-ask nothing the
        # candidate already answers, and leave acceptance to an explicit
        # selection.
        saved = _candidate_context_only(saved)
        patch.update(saved)
        return patch

    saved["customer_confirmed_previous_address"] = True
    saved["shipping_source"] = "delivery_continuation_saved_address"
    patch.update(saved)
    return patch
