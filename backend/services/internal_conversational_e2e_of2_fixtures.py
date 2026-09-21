"""Sandbox fixtures for OrderFlowV2 address scenarios, and why a turn did not happen.

The OrderFlowV2 address branch is reached only when a long chain of real
preconditions holds. Running a scenario without them does not fail — it
succeeds at doing nothing, because the owner returns ``handled=False`` and
the turn looks like an ordinary quiet turn. That is the worst possible
outcome for a validation harness: an absent proof wearing the shape of a
passing one.

Two things live here.

**Preparation.** ``prepare_of2_scenario_fixture`` puts one scenario's
customer into exactly the state that scenario describes — no saved
address, one, several, or an accepted address still missing its city.
The manifest's states are mutually exclusive: a customer cannot
simultaneously have no address and three, so carrying one prepared state
through every scenario cannot work. Isolation is therefore per scenario,
and it is done by RESETTING the conversation the runtime itself
resolves: its addresses, its checkout and any recorded address offer.
Opening a second conversation beside it would not do — the webhook
resolves its own, so a fresh row is simply invisible to the owner, and
every artifact lookup comes back empty while the turn looks healthy.

**Diagnosis.** ``of2_fixture_preflight`` walks the same preconditions the
runtime walks, in the runtime's own order, and stops at the FIRST one
that does not hold. It is a read-only reader of real state: it asks the
runtime's own helpers (billing access, the operational gate, the
permission loader, the pre-Brain ownership rule) rather than
re-implementing their opinions. What it returns is a first divergence,
in the sense of the root-cause-first policy — the layer to look at, not
a guess about the layer above it.

One finding from building this is worth stating plainly, because it
invalidates a whole class of scenario: **OrderFlowV2 may only own a turn
before Brain when the inbound is structurally explicit** — an interactive
reply, a location pin, a catalog order, or a bare national short code.
Ordinary Arabic prose is Brain's to interpret, by design
(``unstructured_turn_ownership``). A scenario whose turn is free text can
never reach this path, however well its fixture is prepared, and the
preflight says so rather than letting the run look quiet and healthy.

Everything here writes only synthetic sandbox rows and is intended for a
disposable database. It enforces no policy and relaxes none: billing,
store mode, pause/handoff, permissions and the ownership rule are read,
never bypassed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Customer address states the manifest distinguishes. They are mutually
# exclusive by construction, which is exactly why each scenario needs the
# conversation reset before it runs.
STATE_NO_SAVED = "no_saved_addresses"
STATE_ONE_SAVED = "one_saved_address"
STATE_SEVERAL_SAVED = "several_saved_addresses"
STATE_ACCEPTED_PENDING_CITY = "accepted_address_pending_city"
FIXTURE_STATES: Tuple[str, ...] = (
    STATE_NO_SAVED,
    STATE_ONE_SAVED,
    STATE_SEVERAL_SAVED,
    STATE_ACCEPTED_PENDING_CITY,
)

# The layers, in the order the runtime traverses them. A divergence is
# reported at the first one that does not hold, and nothing below it is
# evaluated — a lower layer's opinion is meaningless while an upper one
# is broken.
LAYER_TENANT = "tenant"
LAYER_BILLING = "billing"
LAYER_CHANNEL = "channel"
LAYER_STORE_MODE = "store_mode"
LAYER_OPERATIONAL = "order_flow_v2_operational"
LAYER_PERMISSIONS = "commerce_permissions"
LAYER_CUSTOMER = "customer"
LAYER_CONVERSATION = "conversation"
LAYER_CHECKOUT = "checkout_state"
LAYER_INBOUND = "inbound_shape"
FIXTURE_LAYERS: Tuple[str, ...] = (
    LAYER_TENANT,
    LAYER_BILLING,
    LAYER_CHANNEL,
    LAYER_STORE_MODE,
    LAYER_OPERATIONAL,
    LAYER_PERMISSIONS,
    LAYER_CUSTOMER,
    LAYER_CONVERSATION,
    LAYER_CHECKOUT,
    LAYER_INBOUND,
)

# A generic catalog line, rotated across categories per AGENTS.md rather
# than anchored to one merchant's products.
GENERIC_LINE_ITEMS: Tuple[Dict[str, Any], ...] = (
    {
        "product_id": "SANDBOX-SKU-1",
        "title": "حذاء رياضي أبيض",
        "quantity": 1,
        "price": 149.0,
        "currency": "SAR",
    },
)


@dataclass(frozen=True)
class FixtureDivergence:
    """The first layer that does not hold, and what was observed there."""

    layer: str
    reason: str
    detail: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"layer": self.layer, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class FixtureResult:
    """What the fixture established, for the scenario that asked for it."""

    state: str
    conversation: Any
    customer: Any
    address_ids: Tuple[str, ...] = ()
    accepted_address_id: str = ""
    mutations: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "conversation_id": int(getattr(self.conversation, "id", 0) or 0),
            "customer_id": int(getattr(self.customer, "id", 0) or 0),
            "address_ids": list(self.address_ids),
            "accepted_address_id": self.accepted_address_id,
            "mutations": list(self.mutations),
            "notes": list(self.notes),
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Diagnosis ────────────────────────────────────────────────────────────

def of2_fixture_preflight(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Any,
    message: str,
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    inbound_normalized_type: str = "text",
    require_address_turn: bool = True,
) -> List[FixtureDivergence]:
    """The first precondition that does not hold, or an empty list.

    Read-only. Every judgement is delegated to the runtime helper that
    owns it, so this cannot drift into a second opinion about whether a
    tenant may send.
    """
    meta = dict(inbound_metadata or {})

    tenant = _tenant_row(db, tenant_id)
    if tenant is None:
        return [FixtureDivergence(LAYER_TENANT, "tenant_missing", str(tenant_id))]
    if not bool(getattr(tenant, "is_active", False)):
        return [FixtureDivergence(LAYER_TENANT, "tenant_inactive", str(tenant_id))]

    divergence = _billing_divergence(db, tenant_id)
    if divergence is not None:
        return [divergence]

    divergence = _channel_divergence(db, tenant_id)
    if divergence is not None:
        return [divergence]

    divergence = _store_mode_divergence(db, tenant_id, customer_phone)
    if divergence is not None:
        return [divergence]

    divergence = _operational_divergence(
        db, tenant_id=tenant_id, customer_phone=customer_phone,
        conversation=conversation,
    )
    if divergence is not None:
        return [divergence]

    divergence = _permission_divergence(db, tenant_id)
    if divergence is not None:
        return [divergence]

    customer = _customer_row(db, tenant_id, customer_phone)
    if customer is None:
        return [
            FixtureDivergence(LAYER_CUSTOMER, "customer_missing", "no row for phone")
        ]

    if conversation is None:
        return [FixtureDivergence(LAYER_CONVERSATION, "conversation_missing", "")]
    if int(getattr(conversation, "tenant_id", 0) or 0) != int(tenant_id):
        return [
            FixtureDivergence(
                LAYER_CONVERSATION, "conversation_tenant_mismatch",
                str(getattr(conversation, "tenant_id", "")),
            )
        ]

    if require_address_turn:
        divergence = _checkout_divergence(conversation)
        if divergence is not None:
            return [divergence]

        divergence = _inbound_divergence(
            meta, message=message, normalized_type=inbound_normalized_type,
        )
        if divergence is not None:
            return [divergence]

    return []


def _tenant_row(db: Any, tenant_id: int) -> Any:
    try:
        from models import Tenant  # noqa: PLC0415

        return db.query(Tenant).filter(Tenant.id == int(tenant_id)).first()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — absence is the diagnosis
        return None


def _phone_forms(phone: str) -> Tuple[str, ...]:
    """Every spelling of one number the runtime may have stored.

    The webhook normalises an inbound number to E.164 before it creates a
    customer. A fixture that stored the raw form created a SECOND
    customer for the same person, and then prepared one customer's
    address state while the runtime read the other's — a turn that looked
    prepared and was not.
    """
    raw = str(phone or "").strip()
    forms = {raw}
    try:
        from core.order_flow import _normalize_e164  # noqa: PLC0415

        normalized = str(_normalize_e164(raw) or "").strip()
        if normalized:
            forms.add(normalized)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — the raw form still stands
        pass
    return tuple(sorted(f for f in forms if f))


def _canonical_phone(phone: str) -> str:
    """The spelling the runtime itself would store."""
    try:
        from core.order_flow import _normalize_e164  # noqa: PLC0415

        return str(_normalize_e164(phone) or "").strip() or str(phone or "").strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return str(phone or "").strip()


def _customer_row(db: Any, tenant_id: int, phone: str) -> Any:
    try:
        from models import Customer  # noqa: PLC0415

        forms = list(_phone_forms(phone))
        if not forms:
            return None
        return (
            db.query(Customer)
            .filter(
                Customer.tenant_id == int(tenant_id),
                (
                    Customer.normalized_phone.in_(forms)
                    | Customer.phone.in_(forms)
                ),
            )
            .order_by(Customer.id.asc())
            .first()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — absence is the diagnosis
        return None


def _billing_divergence(db: Any, tenant_id: int) -> Optional[FixtureDivergence]:
    try:
        from core.billing import has_billing_access  # noqa: PLC0415

        if not has_billing_access(db, int(tenant_id)):
            return FixtureDivergence(
                LAYER_BILLING,
                "billing_access_denied",
                "no subscription, Salla entitlement, trial window or grant",
            )
    except Exception as exc:  # noqa: BLE001
        return FixtureDivergence(LAYER_BILLING, "billing_check_failed", type(exc).__name__)
    return None


def _channel_divergence(db: Any, tenant_id: int) -> Optional[FixtureDivergence]:
    try:
        from models import WhatsAppConnection  # noqa: PLC0415

        row = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == int(tenant_id))
            .order_by(WhatsAppConnection.id.desc())
            .first()
        )
        if row is None:
            return FixtureDivergence(
                LAYER_CHANNEL, "whatsapp_connection_missing", "",
            )
        if not str(getattr(row, "phone_number_id", "") or "").strip():
            return FixtureDivergence(
                LAYER_CHANNEL, "whatsapp_phone_number_id_missing", "",
            )
    except Exception as exc:  # noqa: BLE001
        return FixtureDivergence(LAYER_CHANNEL, "channel_check_failed", type(exc).__name__)
    return None


def _store_mode_divergence(
    db: Any, tenant_id: int, customer_phone: str,
) -> Optional[FixtureDivergence]:
    try:
        from core.ai_disabled_gate import is_ai_allowed_by_store_mode  # noqa: PLC0415

        decision = is_ai_allowed_by_store_mode(db, int(tenant_id), str(customer_phone or ""))
        if not decision.allowed:
            return FixtureDivergence(
                LAYER_STORE_MODE,
                str(getattr(decision, "reason", "") or "store_ai_mode_not_allowed"),
                "store_ai_mode / ai_test_allowed_numbers",
            )
    except Exception as exc:  # noqa: BLE001
        return FixtureDivergence(
            LAYER_STORE_MODE, "store_mode_check_failed", type(exc).__name__,
        )
    return None


def _operational_divergence(
    db: Any, *, tenant_id: int, customer_phone: str, conversation: Any,
) -> Optional[FixtureDivergence]:
    """Is OrderFlowV2 allowed to SEND this turn, per its own gate?"""
    try:
        from modules.ai.order_flow_v2.enforcement import (  # noqa: PLC0415
            resolve_order_flow_v2_operational,
        )

        decision = resolve_order_flow_v2_operational(
            db,
            tenant_id=int(tenant_id),
            customer_phone=str(customer_phone or ""),
            conversation=conversation,
        )
        if not decision.live:
            # Shadow evaluation observes; it never sends, so an address
            # reply cannot be delivered and no capture can occur.
            return FixtureDivergence(
                LAYER_OPERATIONAL,
                str(decision.reason or "order_flow_v2_not_live"),
                "shadow_log" if decision.shadow_log else "disabled",
            )
    except Exception as exc:  # noqa: BLE001
        return FixtureDivergence(
            LAYER_OPERATIONAL, "operational_gate_failed", type(exc).__name__,
        )
    return None


def _permission_divergence(db: Any, tenant_id: int) -> Optional[FixtureDivergence]:
    try:
        from modules.ai.commerce.permission_loader import (  # noqa: PLC0415
            load_tenant_commerce_permissions,
        )

        load = load_tenant_commerce_permissions(db, int(tenant_id))
        if not load.ok:
            return FixtureDivergence(
                LAYER_PERMISSIONS, "permission_load_failed", str(load.source),
            )
        if not load.permissions.can_create_orders:
            # A durable address write needs the same authorization an
            # order write needs, so without it the owner observes only.
            return FixtureDivergence(
                LAYER_PERMISSIONS, "can_create_orders_denied", str(load.source),
            )
    except Exception as exc:  # noqa: BLE001
        return FixtureDivergence(
            LAYER_PERMISSIONS, "permission_check_failed", type(exc).__name__,
        )
    return None


def _checkout_divergence(conversation: Any) -> Optional[FixtureDivergence]:
    """Is there an active checkout with product evidence to collect for?"""
    try:
        from modules.ai.checkout_authority import (  # noqa: PLC0415
            active_whatsapp_checkout,
            checkout_has_items,
        )

        meta = dict(getattr(conversation, "extra_metadata", None) or {})
        brain_state = meta.get("brain_state")
        brain_state = dict(brain_state) if isinstance(brain_state, Mapping) else {}
        order_prep = brain_state.get("order_prep")
        order_prep = dict(order_prep) if isinstance(order_prep, Mapping) else {}
        if not active_whatsapp_checkout(order_prep, brain_state):
            return FixtureDivergence(
                LAYER_CHECKOUT,
                "checkout_not_active",
                "brain_state.order_prep has no active OrderFlowV2 checkout",
            )
        if not checkout_has_items(order_prep, brain_state):
            return FixtureDivergence(
                LAYER_CHECKOUT,
                "checkout_has_no_items",
                "no line items and no product focus",
            )
    except Exception as exc:  # noqa: BLE001
        return FixtureDivergence(
            LAYER_CHECKOUT, "checkout_check_failed", type(exc).__name__,
        )
    return None


def _inbound_divergence(
    meta: Mapping[str, Any], *, message: str, normalized_type: str,
) -> Optional[FixtureDivergence]:
    """Can OrderFlowV2 own this inbound before Brain at all?"""
    try:
        from modules.ai.brain.commerce.unstructured_turn_ownership import (  # noqa: PLC0415
            ofv2_may_own_prebrain,
        )

        if not ofv2_may_own_prebrain(
            dict(meta or {}),
            normalized_type=str(normalized_type or "text"),
            message=str(message or ""),
        ):
            return FixtureDivergence(
                LAYER_INBOUND,
                "unstructured_requires_brain_semantic_ownership",
                "free text is Brain's to interpret; OrderFlowV2 owns a turn "
                "pre-Brain only for an interactive reply, a location pin, a "
                "catalog order or a bare national short code",
            )
    except Exception as exc:  # noqa: BLE001
        return FixtureDivergence(
            LAYER_INBOUND, "inbound_shape_check_failed", type(exc).__name__,
        )
    return None


# ── Preparation ──────────────────────────────────────────────────────────

def prepare_of2_scenario_fixture(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    scenario_id: str,
    session_id: str,
    state: str,
    line_items: Sequence[Mapping[str, Any]] = GENERIC_LINE_ITEMS,
    customer_name: str = "أحمد سالم",
) -> FixtureResult:
    """Put this scenario's customer in its declared state, in its own conversation.

    The caller owns the transaction. Raises rather than degrading: a
    fixture that half-applied would produce a turn nobody can interpret.
    """
    if state not in FIXTURE_STATES:
        raise ValueError("fixture_state_invalid")

    customer = _customer_row(db, tenant_id, customer_phone)
    if customer is None:
        customer = _create_customer(db, tenant_id, customer_phone, customer_name)

    mutations: List[str] = []
    notes: List[str] = []

    removed = _clear_sandbox_addresses(db, tenant_id=tenant_id, customer=customer)
    if removed:
        mutations.append(f"sandbox_addresses_cleared:{removed}")

    address_ids, accepted_id = _apply_address_state(
        db, tenant_id=tenant_id, customer=customer, state=state,
    )
    if address_ids:
        mutations.append(f"sandbox_addresses_created:{len(address_ids)}")
    if accepted_id:
        mutations.append("sandbox_address_selected")

    conversation, created = _reset_conversation(
        db,
        tenant_id=tenant_id,
        customer=customer,
        scenario_id=scenario_id,
        session_id=session_id,
        state=state,
        line_items=list(line_items),
        customer_name=customer_name,
    )
    mutations.append(
        "sandbox_conversation_created" if created else "sandbox_conversation_reset"
    )
    db.commit()
    db.refresh(conversation)

    if state == STATE_ACCEPTED_PENDING_CITY and not accepted_id:
        notes.append("accepted_address_not_established")

    return FixtureResult(
        state=state,
        conversation=conversation,
        customer=customer,
        address_ids=tuple(address_ids),
        accepted_address_id=accepted_id,
        mutations=tuple(mutations),
        notes=tuple(notes),
    )


def _create_customer(db: Any, tenant_id: int, phone: str, name: str) -> Any:
    from models import Customer  # noqa: PLC0415

    canonical = _canonical_phone(phone)
    customer = Customer(
        tenant_id=int(tenant_id),
        phone=canonical,
        normalized_phone=canonical,
        name=name,
        acquisition_channel="whatsapp",
    )
    db.add(customer)
    db.flush()
    return customer


def _clear_sandbox_addresses(db: Any, *, tenant_id: int, customer: Any) -> int:
    """Remove this sandbox customer's addresses and their provenance."""
    from models import CustomerAddress, CustomerAddressProvenance  # noqa: PLC0415

    customer_id = int(getattr(customer, "id", 0) or 0)
    removed = (
        db.query(CustomerAddressProvenance)
        .filter(
            CustomerAddressProvenance.tenant_id == int(tenant_id),
            CustomerAddressProvenance.customer_id == customer_id,
        )
        .delete(synchronize_session=False)
    )
    removed += (
        db.query(CustomerAddress)
        .filter(
            CustomerAddress.tenant_id == int(tenant_id),
            CustomerAddress.customer_id == customer_id,
        )
        .delete(synchronize_session=False)
    )
    db.flush()
    return int(removed or 0)


def _apply_address_state(
    db: Any, *, tenant_id: int, customer: Any, state: str,
) -> Tuple[List[str], str]:
    """Create the addresses this state describes, through the real writer."""
    if state == STATE_NO_SAVED:
        return [], ""

    from core.customer_address_candidates import (  # noqa: PLC0415
        AddressComponents,
        SOURCE_SALLA_CUSTOMER_PROFILE,
        upsert_imported_address_candidate,
    )

    # Rotated generic destinations, never one real merchant's city list.
    specs: Tuple[Tuple[str, AddressComponents], ...]
    if state == STATE_ONE_SAVED:
        specs = (
            (
                "sandbox-addr-1",
                AddressComponents(
                    city="الرياض", district="حي النرجس",
                    address_line="شارع 10", country="SA",
                ),
            ),
        )
    elif state == STATE_SEVERAL_SAVED:
        specs = (
            (
                "sandbox-addr-1",
                AddressComponents(
                    city="الرياض", district="حي النرجس",
                    address_line="شارع 10", country="SA",
                ),
            ),
            (
                "sandbox-addr-2",
                AddressComponents(
                    city="جدة", district="حي الروضة",
                    address_line="شارع 21", country="SA",
                ),
            ),
            (
                "sandbox-addr-3",
                AddressComponents(
                    city="الدمام", district="حي الشاطئ",
                    address_line="شارع 4", country="SA",
                ),
            ),
        )
    else:  # STATE_ACCEPTED_PENDING_CITY
        # An address the customer has accepted, whose city is still the
        # open question — the state the city-collection scenario needs.
        specs = (
            (
                "sandbox-addr-accepted",
                AddressComponents(
                    address_line="حي النرجس، شارع 10",
                    country="SA",
                    maps_url="https://maps.google.com/?q=24.77,46.63",
                ),
            ),
        )

    created: List[str] = []
    fingerprints: Dict[str, str] = {}
    observed = _now() - timedelta(days=2)
    for source_ref, components in specs:
        result = upsert_imported_address_candidate(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(getattr(customer, "id", 0) or 0),
            components=components,
            source=SOURCE_SALLA_CUSTOMER_PROFILE,
            source_ref=source_ref,
            source_updated_at=observed,
        )
        address_id = str(getattr(result, "address_id", "") or "")
        if address_id:
            created.append(address_id)
            fingerprints[address_id] = str(getattr(result, "fingerprint", "") or "")
    db.flush()

    accepted_id = ""
    if state == STATE_ACCEPTED_PENDING_CITY and created:
        from core.customer_address_candidates import (  # noqa: PLC0415
            SELECTION_SOURCE_CUSTOMER_CONFIRMED,
            record_explicit_address_selection,
        )

        accepted_id = created[0]
        record_explicit_address_selection(
            db,
            tenant_id=int(tenant_id),
            customer_id=int(getattr(customer, "id", 0) or 0),
            address_id=accepted_id,
            selection_source=SELECTION_SOURCE_CUSTOMER_CONFIRMED,
            expected_fingerprint=fingerprints.get(accepted_id, ""),
        )
        db.flush()

    return created, accepted_id


def _reset_conversation(
    db: Any,
    *,
    tenant_id: int,
    customer: Any,
    scenario_id: str,
    session_id: str,
    state: str,
    line_items: List[Mapping[str, Any]],
    customer_name: str,
) -> Tuple[Any, bool]:
    """Give this scenario a clean conversation — the one the runtime resolves.

    Isolation here is by RESET, not by creating a fresh row. The webhook
    resolves a customer's conversation itself; a second conversation
    created beside it is simply invisible to the runtime, so a fixture
    that opened one per scenario would hand the runner a row the owner
    never writes to, and every artifact lookup would come back empty
    while the turn itself looked healthy.

    Resetting the row the runtime does use is also the stronger
    isolation: it clears the previous scenario's checkout, its recorded
    address offer and its collection state, so no scenario can inherit
    an offer or a field from the one before it.
    """
    from models import Conversation  # noqa: PLC0415

    parts = str(customer_name or "").split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    order_prep: Dict[str, Any] = {
        "order_flow_v2_active": True,
        "order_flow_v2_pending": False,
        "order_status": "collecting_customer_info",
        "catalog_line_items_authoritative": True,
        "order_flow_v2_trusted_price": True,
        "line_items": [dict(item) for item in line_items],
        "customer_first_name": first,
        "customer_last_name": last,
    }
    metadata: Dict[str, Any] = {
        "internal_e2e_session_id": session_id,
        "internal_e2e_scenario_id": scenario_id,
        "internal_e2e_fixture_state": state,
        "synthetic": True,
        "brain_state": {"order_prep": order_prep},
    }

    rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == int(tenant_id),
            Conversation.customer_id == int(getattr(customer, "id", 0) or 0),
        )
        .order_by(Conversation.id.asc())
        .all()
    )
    if not rows:
        convo = Conversation(
            tenant_id=int(tenant_id),
            customer_id=int(getattr(customer, "id", 0) or 0),
            status="active",
            extra_metadata=metadata,
        )
        db.add(convo)
        db.flush()
        return convo, True

    # EVERY synthetic conversation for this customer is reset, not just
    # one. The runtime resolves a customer's conversation through its own
    # path, and a webhook turn can open a second row beside the one the
    # fixture prepared; resetting only the row this function happened to
    # pick left the runtime reading a stale state from the other one.
    for row in rows:
        existing = dict(getattr(row, "extra_metadata", None) or {})
        if not existing.get("synthetic"):
            # Never reset a conversation this harness did not create,
            # even in a disposable database.
            raise ValueError("sandbox_conversation_not_synthetic")
        row.status = "active"
        row.extra_metadata = dict(metadata)
    db.flush()
    return rows[0], False


__all__ = [
    "FIXTURE_LAYERS",
    "FIXTURE_STATES",
    "GENERIC_LINE_ITEMS",
    "FixtureDivergence",
    "FixtureResult",
    "LAYER_BILLING",
    "LAYER_CHANNEL",
    "LAYER_CHECKOUT",
    "LAYER_CONVERSATION",
    "LAYER_CUSTOMER",
    "LAYER_INBOUND",
    "LAYER_OPERATIONAL",
    "LAYER_PERMISSIONS",
    "LAYER_STORE_MODE",
    "LAYER_TENANT",
    "STATE_ACCEPTED_PENDING_CITY",
    "STATE_NO_SAVED",
    "STATE_ONE_SAVED",
    "STATE_SEVERAL_SAVED",
    "of2_fixture_preflight",
    "prepare_of2_scenario_fixture",
]
