"""Bridge OrderContext saved-address truth into OrderFlowV2 deterministic replies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.order_context_prefill import MODE_CONFIRM
from core.wa_order_lifecycle import has_accepted_delivery_address

from .missing_fields import compute_v2_missing_fields


@dataclass(frozen=True)
class AddressPresentation:
    """What a reply is ABOUT TO put in front of the customer.

    Reading context is not showing anything to anyone. This carries the
    revision a reply would describe, and only the delivery boundary turns
    it into a recorded offer.
    """

    address_id: int = 0
    fingerprint: str = ""
    choices: Tuple[Dict[str, Any], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.address_id and self.fingerprint) and not self.choices


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
    # Prepared, NOT recorded. See ``record_presented_address_offer``.
    presentation: AddressPresentation = field(default_factory=AddressPresentation)


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
_TURN_OPERATION_KEY = "address_operation"

# Namespace for the structured reply id that carries consent. A customer
# tapping this is an unambiguous, machine-checkable act: it names the exact
# address, it cannot be a question, and it cannot be produced by phrasing.
CONSENT_ACTION_PREFIX = "nahla_addr_select"


def _conversation_metadata(conversation: Any) -> Dict[str, Any]:
    raw = getattr(conversation, "extra_metadata", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _write_conversation_metadata(db: Any, conversation: Any, meta: Dict[str, Any]) -> bool:
    try:
        conversation.extra_metadata = meta
        db.add(conversation)
        return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — every caller treats a failed write as "not recorded", which refuses rather than allows
        return False


def consent_action_id(address_id: Any) -> str:
    """The structured reply id that selects this address."""
    try:
        return f"{CONSENT_ACTION_PREFIX}:{int(address_id)}"
    except (TypeError, ValueError):
        return ""


def structured_consent_address_id(inbound_metadata: Any) -> Optional[int]:
    """The address a STRUCTURED customer action named, if any.

    Only an interactive reply id counts. Free text — however it is phrased,
    and whatever an intent detector makes of it — never reaches here, which
    is why a question can no longer become a durable selection.
    """
    if not isinstance(inbound_metadata, dict):
        return None
    for key in ("button_id", "list_reply_id", "interactive_reply_id", "button_provenance"):
        raw = str(inbound_metadata.get(key) or "").strip()
        if not raw.startswith(f"{CONSENT_ACTION_PREFIX}:"):
            continue
        try:
            address_id = int(raw.split(":", 1)[1])
        except (TypeError, ValueError):
            continue
        if address_id:
            return address_id
    return None


def record_turn_address_operation(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    attempt: Any,
    turn_ref: str,
) -> bool:
    """Let the WRITER publish what it just did, for this turn only.

    The guard boundary cannot be trusted to assemble this: it would then be
    the claimant vouching for itself. The selection owner records the
    operation it performed — address, revision and operation reference —
    stamped with the inbound turn it belongs to, and the boundary reads it
    back and verifies it against committed state.
    """
    if conversation is None or attempt is None or not getattr(attempt, "is_actionable", False):
        return False
    meta = _conversation_metadata(conversation)
    payload = dict(attempt.as_dict())
    payload["turn_ref"] = str(turn_ref or "")
    payload["conversation_id"] = int(getattr(conversation, "id", 0) or 0)
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    meta[_TURN_OPERATION_KEY] = payload
    return _write_conversation_metadata(db, conversation, meta)


def read_turn_address_operation(conversation: Any, *, turn_ref: str = "") -> Any:
    """The operation THIS turn performed, or nothing.

    Scoped three ways: same conversation, same inbound turn, and recent.
    An operation from an earlier turn is not this turn's, so it can never
    support this turn's claim.
    """
    from core.customer_address_persistence_evidence import (  # noqa: PLC0415
        NO_OPERATION,
        AddressOperationAttempt,
    )

    if conversation is None:
        return NO_OPERATION
    payload = _conversation_metadata(conversation).get(_TURN_OPERATION_KEY)
    if not isinstance(payload, dict):
        return NO_OPERATION
    conversation_id = int(getattr(conversation, "id", 0) or 0)
    if conversation_id and int(payload.get("conversation_id") or 0) != conversation_id:
        return NO_OPERATION
    if str(payload.get("turn_ref") or "") != str(turn_ref or ""):
        return NO_OPERATION
    recorded = _parse_iso(payload.get("recorded_at"))
    if recorded is None:
        return NO_OPERATION
    if (datetime.now(timezone.utc) - recorded).total_seconds() > _OPERATION_TTL_SECONDS:
        return NO_OPERATION
    return AddressOperationAttempt.from_dict(payload)


def read_turn_address_operation_for_conversation(
    db: Any,
    *,
    conversation_id: Any,
    turn_ref: str = "",
) -> Any:
    """Same read, from a boundary that holds only the conversation id."""
    from core.customer_address_persistence_evidence import NO_OPERATION  # noqa: PLC0415

    try:
        from models import Conversation  # noqa: PLC0415

        row = db.query(Conversation).filter_by(id=int(conversation_id)).first()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unreadable conversation means no operation, which refuses the claim
        return NO_OPERATION
    return read_turn_address_operation(row, turn_ref=turn_ref)


_OPERATION_TTL_SECONDS = 600


def _parse_iso(raw: Any) -> Optional[datetime]:
    try:
        value = datetime.fromisoformat(str(raw or ""))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def turn_reference(inbound_metadata: Any) -> str:
    """The inbound message this turn is answering — its turn identity."""
    if not isinstance(inbound_metadata, dict):
        return ""
    for key in ("wa_message_id", "message_id", "inbound_message_id", "wamid"):
        value = str(inbound_metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def record_offered_address(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    previous: Any,
    delivery_ref: str = "",
) -> None:
    """Remember which address revision was put in front of the customer.

    Called from the DELIVERY boundary, never from a context read. Reading
    the customer's addresses is not presentation: it happens on turns that
    send nothing about an address, and a later reply would then be checked
    against an "offer" nobody ever saw.

    An existing offer is never silently advanced either. A refresh that
    lands between the presentation and the customer's answer must make the
    answer refuse — replacing the offer with the newer revision would
    instead record approval of content that was never shown.
    """
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
        "delivery_ref": str(delivery_ref or ""),
    }
    if isinstance(current, dict) and all(
        current.get(k) == offer[k] for k in ("address_id", "fingerprint", "customer_id", "tenant_id")
    ):
        return
    meta[_OFFER_KEY] = offer
    _write_conversation_metadata(db, conversation, meta)


def record_offered_address_set(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    candidates: Any,
    delivery_ref: str = "",
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
        "delivery_ref": str(delivery_ref or ""),
        "addresses": offered,
    }
    existing = meta.get(_OFFER_SET_KEY)
    if isinstance(existing, dict) and existing.get("addresses") == offered:
        return
    meta[_OFFER_SET_KEY] = payload
    _write_conversation_metadata(db, conversation, meta)


def record_presented_address_offer(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    presentation: Any,
    delivery_ref: str = "",
) -> bool:
    """Record what a reply ACTUALLY presented, at the delivery boundary.

    This is the lifecycle event a later consent is checked against: it
    exists because a reply was produced and handed to delivery, not
    because some code read the customer's addresses.
    """
    if conversation is None or presentation is None or presentation.is_empty:
        return False
    recorded = False
    if presentation.address_id and presentation.fingerprint:
        record_offered_address(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            previous=_PresentedAddress(
                address_id=int(presentation.address_id),
                content_fingerprint=str(presentation.fingerprint),
            ),
            delivery_ref=delivery_ref,
        )
        recorded = True
    if presentation.choices:
        record_offered_address_set(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            candidates=[dict(c) for c in presentation.choices],
            delivery_ref=delivery_ref,
        )
        recorded = True
    return recorded


@dataclass(frozen=True)
class _PresentedAddress:
    """Minimal shape ``record_offered_address`` reads an offer from."""

    address_id: int
    content_fingerprint: str


def apply_explicit_address_selection(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    address_id: int,
    order_prep: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
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

    offered, operation_ref = _offered_revisions(
        tenant_id=int(tenant_id), conversation=conversation,
    )
    if int(address_id) not in offered:
        return {}
    customer_id = int(getattr(conversation, "customer_id", 0) or 0)
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
    attempt = _record_selection_for_confirmed_address(
        db,
        tenant_id=int(tenant_id),
        previous=previous,
        selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
        expected_fingerprint=chosen.fingerprint,
        operation_ref=operation_ref,
    )
    if not getattr(attempt, "is_actionable", False):
        return {}
    # The writer publishes what it did, for this turn only. Nothing
    # downstream asserts on its own behalf that a save happened.
    record_turn_address_operation(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        attempt=attempt,
        turn_ref=turn_reference(inbound_metadata),
    )
    patch = _shipping_context_to_prep_patch(previous)
    patch["customer_confirmed_previous_address"] = True
    patch["shipping_source"] = "customer_selected_address"
    return patch


def _offered_revisions(
    *,
    tenant_id: int,
    conversation: Any,
) -> Tuple[Dict[int, str], str]:
    """Every revision this conversation actually presented, and its ref.

    Both offer shapes count: a single address put forward for confirmation
    and a set put forward for a choice. A customer who was shown one
    address must be able to accept it structurally, not only choose from a
    list.
    """
    meta = _conversation_metadata(conversation)
    customer_id = int(getattr(conversation, "customer_id", 0) or 0)
    offered: Dict[int, str] = {}
    operation_ref = ""

    def _scoped(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            if int(payload.get("tenant_id") or 0) != int(tenant_id):
                return False
            if customer_id and int(payload.get("customer_id") or 0) != customer_id:
                return False
        except (TypeError, ValueError):
            return False
        return True

    offer_set = meta.get(_OFFER_SET_KEY)
    if _scoped(offer_set):
        for entry in offer_set.get("addresses") or []:
            try:
                offered[int(entry["address_id"])] = str(entry["fingerprint"])
            except (KeyError, TypeError, ValueError):
                continue
        operation_ref = str(offer_set.get("offered_at") or "")

    single = meta.get(_OFFER_KEY)
    if _scoped(single):
        try:
            offered.setdefault(
                int(single.get("address_id") or 0), str(single.get("fingerprint") or "")
            )
        except (TypeError, ValueError):
            pass
        operation_ref = operation_ref or str(single.get("offered_at") or "")
    offered.pop(0, None)
    return offered, operation_ref


def apply_structured_address_consent(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    order_prep: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Durable selection from a STRUCTURED customer action, and only that.

    Consent about an address is a lifecycle event, not a reading of words.
    A structured reply id names the exact address the customer tapped: it
    cannot be a question, and no phrasing produces it. Free text still
    drives the turn's checkout exactly as before — it simply never writes
    a durable selection, which is the thing a "saved / adopted" claim
    would rest on.

    Idempotent: tapping the same choice twice selects once.
    """
    address_id = structured_consent_address_id(inbound_metadata)
    if not address_id:
        return {}
    return apply_explicit_address_selection(
        db,
        tenant_id=int(tenant_id),
        conversation=conversation,
        address_id=int(address_id),
        order_prep=order_prep,
        inbound_metadata=inbound_metadata,
    )


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
) -> Any:
    """Persist the customer's explicit choice of THIS address revision.

    Bound to the exact revision that was just reviewed: if the stored
    content changed since it was read, nothing is written and the caller
    treats the address as unconfirmed. Idempotent — confirming the same
    revision twice writes once.

    Returns the ``AddressOperationAttempt`` describing what was written, or
    ``NO_OPERATION``. The WRITER produces it, so nothing downstream has to
    assert on its own behalf that an operation happened.
    """
    from core.customer_address_persistence_evidence import (  # noqa: PLC0415
        NO_OPERATION,
        AddressOperation,
        AddressOperationAttempt,
    )

    address_id = getattr(previous, "address_id", None)
    if not address_id:
        return NO_OPERATION
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
            return NO_OPERATION
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
        if not result.selected:
            return NO_OPERATION
        return AddressOperationAttempt(
            operation=AddressOperation.ADOPT_SELECTION,
            tenant_id=int(tenant_id),
            customer_id=int(row.customer_id),
            address_id=int(address_id),
            fingerprint=str(result.fingerprint or ""),
            operation_ref=str(operation_ref or ""),
        )
    except Exception:  # noqa: BLE001
        return NO_OPERATION


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
    presentation = AddressPresentation()
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
        # PREPARED, not recorded. Reading context is not presentation: this
        # runs on turns that never mention an address, and recording an
        # offer here made a later reply answerable to something nobody saw.
        # ``record_presented_address_offer`` records it when a reply that
        # actually presents it is delivered.
        presentation = AddressPresentation(
            address_id=int(getattr(previous_ctx, "address_id", 0) or 0),
            fingerprint=str(getattr(previous_ctx, "content_fingerprint", "") or ""),
            choices=tuple(dict(c) for c in address_choices),
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
        presentation=presentation,
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
        from core.order_context_prefill import _shipping_context_to_prep_patch  # noqa: PLC0415

        if bool(getattr(ctx.shipping, "locked_by_merchant", False)):
            return {}
        if has_accepted_delivery_address(dict(order_prep or {})):
            return {}
        previous = ctx.known_previous_address
        # Free text continues this turn's checkout exactly as it always
        # has. What it deliberately does NOT do is write a durable
        # selection.
        #
        # A phrase detector cannot tell consent from an inquiry: "هل عنواني
        # محفوظ عندكم؟" and "نفس العنوان السابق" both read as
        # ``previous_address_confirmed``. Widening or narrowing that
        # detector would be customer-intent regex repair, which the
        # intelligence policy forbids and which would fail on the next
        # phrasing anyway. So the durable act needs a durable signal:
        # ``apply_structured_address_consent`` writes the selection when
        # the customer takes a structured action naming the address they
        # were shown. Until then this turn holds the address in order
        # state, and no reply may claim it was saved or adopted.
        patch = _shipping_context_to_prep_patch(previous)
        patch["customer_confirmed_previous_address"] = True
        patch["shipping_source"] = "customer_confirmed_previous_address"
        patch["address_selection_durable"] = False
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
