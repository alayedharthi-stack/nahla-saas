"""Regression coverage for explicit checkout-name answers.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DATABASE = ROOT / "database"
for path in (ROOT, BACKEND, DATABASE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_customer,
    seed_tenant,
)
from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_CUSTOMER_MESSAGE,
    SOURCE_MERCHANT,
    STATUS_CUSTOMER_ENTERED,
)
from core.order_context_builder import build_order_context  # noqa: E402
from core.order_context_prefill import MODE_SKIP, build_checkout_compose_facts  # noqa: E402
from models import Customer  # noqa: E402
from modules.ai.commerce.permission_loader import PermissionLoadResult  # noqa: E402
from modules.ai.commerce.permissions import CommercePermissionSet  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER  # noqa: E402
from modules.ai.brain.execution.orders import (  # noqa: E402
    _expects_checkout_name_answer,
    _merge_message_details,
    _persist_expected_checkout_name_answer,
)
from modules.ai.brain.pipeline import get_brain  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


GENERIC_PRODUCT = "حذاء رياضي أبيض"


def _name_slots(name: str) -> dict:
    parts = name.split()
    return {
        "customer_name": name,
        "customer_first_name": parts[0],
        "customer_last_name": " ".join(parts[1:]),
    }


def _active_state(
    *,
    missing_fields: list[str] | None = None,
    product_title: str = GENERIC_PRODUCT,
) -> MerchantConversationState:
    prep = OrderPreparationState(
        quantity=2,
        product_id="generic-shoe-1",
        order_status="awaiting_address",
        missing_fields=list(
            missing_fields
            if missing_fields is not None
            else ["customer_name", "city", "delivery_address", "payment_method"]
        ),
        line_items=[
            {
                "product_id": "generic-shoe-1",
                "product_name": product_title,
                "quantity": 2,
                "match_status": "matched",
            }
        ],
        product_options_loaded=True,
        checkout_channel="whatsapp",
    )
    return MerchantConversationState(
        stage="ordering",
        turn=2,
        order_prep=prep,
        current_product_focus={
            "id": "generic-shoe-1",
            "external_id": "generic-shoe-1",
            "title": product_title,
            "price": 120.0,
            "orderable": True,
            "can_checkout": True,
            "in_stock": True,
        },
    )


def _ctx(
    *,
    tenant_id: int,
    message: str,
    slots: dict | None = None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=DEFAULT_PHONE_E164,
        message=message,
        raw_message=message,
        intent=Intent(
            name="checkout_continuation",
            confidence=0.92,
            slots=dict(slots or {}),
            raw_message=message,
            extraction_method="hybrid",
        ),
        state=state or _active_state(),
        facts=CommerceFacts(),
        history=[],
        profile={},
    )


def _full_name(prep: OrderPreparationState) -> str:
    return " ".join(
        part
        for part in (
            str(prep.customer_first_name or "").strip(),
            str(prep.customer_last_name or "").strip(),
        )
        if part
    ).strip()


def _consume_and_persist(db, ctx: BrainContext) -> bool:
    prep = ctx.state.order_prep
    assert prep is not None
    expected = _expects_checkout_name_answer(prep)
    previous_name = _full_name(prep)
    _merge_message_details(prep, ctx.intent.slots, ctx.raw_message)
    ctx._db = db
    return _persist_expected_checkout_name_answer(
        ctx,
        prep,
        expected=expected,
        previous_name=previous_name,
    )


def _reload_customer(db, *, tenant_id: int, customer_id: int) -> Customer:
    db.commit()
    db.expire_all()
    return (
        db.query(Customer)
        .filter(
            Customer.tenant_id == tenant_id,
            Customer.id == customer_id,
        )
        .one()
    )


def test_live_shape_explicit_single_name_answer_is_saved_in_db() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(
        db,
        tenant.id,
        extra_metadata={
            "customer_name_status": "rejected",
            "customer_name_rejected_reason": "pattern_mismatch",
            "manual_name_override": True,
            "manual_name_cleared": True,
        },
    )
    state = _active_state(product_title="عسل الطلح")
    ctx = _ctx(
        tenant_id=tenant.id,
        message="سعد",
        slots=_name_slots("سعد"),
        state=state,
    )

    assert _consume_and_persist(db, ctx) is True
    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)

    assert state.order_prep.customer_first_name == "سعد"
    assert reloaded.name == "سعد"
    assert reloaded.extra_metadata["customer_name_source"] == SOURCE_CUSTOMER_MESSAGE
    assert reloaded.extra_metadata["customer_name_status"] == STATUS_CUSTOMER_ENTERED
    assert reloaded.extra_metadata["manual_name_cleared"] is False


@pytest.mark.parametrize(
    ("message", "slot_name"),
    [
        ("سعد", "سعد"),
        ("اسمي سعد", "سعد"),
        ("أنا سعد", "سعد"),
    ],
)
def test_supported_explicit_name_answer_shapes(message: str, slot_name: str) -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)

    assert _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message=message, slots=_name_slots(slot_name)),
    )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert reloaded.name == slot_name


def test_spurious_carried_quantity_slot_does_not_block_grounded_name() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    slots = {**_name_slots("سعد"), "quantity": 2}

    assert _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message="سعد", slots=slots),
    )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert reloaded.name == "سعد"


def test_saved_name_reloads_and_is_not_requested_next_turn() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message="سعد", slots=_name_slots("سعد")),
    )
    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)

    order_context = build_order_context(
        db,
        tenant_id=tenant.id,
        customer=reloaded,
        phone=DEFAULT_PHONE_E164,
        brain_state={"order_prep": _active_state().order_prep.to_dict()},
    )
    facts = build_checkout_compose_facts(order_context, phone=DEFAULT_PHONE_E164)

    assert facts["name_mode"] == MODE_SKIP
    assert facts["known_name"] == "سعد"
    assert not {
        "name",
        "customer_name",
        "customer_first_name",
        "customer_last_name",
    }.intersection(facts["missing_fields"])


def test_explicit_full_name_is_saved_correctly() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    full_name = "سعد الحارثي"

    assert _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message=full_name, slots=_name_slots(full_name)),
    )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert reloaded.name == full_name


def test_first_name_then_last_name_promotes_completed_identity() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    first_name = "سعد"
    last_name = "الحارثي"

    assert _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message=first_name, slots=_name_slots(first_name)),
    )
    first = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert first.name == first_name

    state = _active_state(
        missing_fields=["customer_last_name", "city", "delivery_address"]
    )
    state.order_prep.customer_first_name = first_name
    assert _consume_and_persist(
        db,
        _ctx(
            tenant_id=tenant.id,
            message=last_name,
            slots={"customer_last_name": last_name},
            state=state,
        ),
    )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert reloaded.name == f"{first_name} {last_name}"


@pytest.mark.parametrize(
    ("message", "slots"),
    [
        ("تمام", {}),
        ("تمام", _name_slots("سعد")),
        ("الرياض", {"city": "الرياض"}),
    ],
)
def test_ambiguous_or_non_name_answer_is_not_saved(
    message: str,
    slots: dict,
) -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)

    assert not _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message=message, slots=slots),
    )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert not reloaded.name


def test_bare_name_is_not_saved_without_expected_name_state() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    state = _active_state(missing_fields=["city", "delivery_address"])

    assert not _consume_and_persist(
        db,
        _ctx(
            tenant_id=tenant.id,
            message="سعد",
            slots=_name_slots("سعد"),
            state=state,
        ),
    )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert not reloaded.name


def test_product_or_profile_metadata_is_not_used_as_customer_name() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    state = _active_state(product_title="سعد الحارثي")
    ctx = _ctx(tenant_id=tenant.id, message="تمام", state=state)
    ctx.profile = {"name": "سعد الحارثي", "customer_name": "سعد الحارثي"}

    assert not _consume_and_persist(db, ctx)

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert not reloaded.name


def test_existing_trusted_name_is_not_overwritten_without_correction() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(
        db,
        tenant.id,
        name="نورة عبدالله",
        extra_metadata={
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
            "customer_name_confidence": 0.95,
            "manual_name_override": True,
        },
    )

    assert not _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message="سعد", slots=_name_slots("سعد")),
    )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert reloaded.name == "نورة عبدالله"
    assert reloaded.extra_metadata["customer_name_source"] == SOURCE_MERCHANT


def test_name_write_is_tenant_scoped() -> None:
    db, _ = make_scenario_db()
    tenant_a = seed_tenant(db, name="متجر تجريبي عام")
    tenant_b = seed_tenant(db, name="متجر تجريبي آخر")
    customer_a = seed_customer(db, tenant_a.id)
    customer_b = seed_customer(
        db,
        tenant_b.id,
        name="نورة عبدالله",
        extra_metadata={
            "customer_name_source": SOURCE_MERCHANT,
            "customer_name_status": STATUS_CUSTOMER_ENTERED,
        },
    )

    assert _consume_and_persist(
        db,
        _ctx(tenant_id=tenant_a.id, message="سعد", slots=_name_slots("سعد")),
    )
    db.commit()
    db.expire_all()

    assert db.get(Customer, customer_a.id).name == "سعد"
    assert db.get(Customer, customer_b.id).name == "نورة عبدالله"


def test_reprocessed_answer_is_idempotent_and_does_not_duplicate_customer() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    assert _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message="سعد", slots=_name_slots("سعد")),
    )
    first = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    first_updated_at = first.extra_metadata["customer_name_updated_at"]

    assert not _consume_and_persist(
        db,
        _ctx(tenant_id=tenant.id, message="سعد", slots=_name_slots("سعد")),
    )
    db.commit()

    db.expire_all()
    rows = db.query(Customer).filter(Customer.tenant_id == tenant.id).all()
    assert len(rows) == 1
    assert rows[0].id == customer.id
    assert rows[0].name == "سعد"
    assert rows[0].extra_metadata["customer_name_updated_at"] == first_updated_at


def test_name_persistence_preserves_product_quantity_and_checkout_state() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    seed_customer(db, tenant.id)
    state = _active_state()
    prep = state.order_prep
    before = {
        "product_id": prep.product_id,
        "quantity": prep.quantity,
        "line_items": list(prep.line_items),
        "order_status": prep.order_status,
        "checkout_channel": prep.checkout_channel,
        "missing_fields": list(prep.missing_fields),
    }

    assert _consume_and_persist(
        db,
        _ctx(
            tenant_id=tenant.id,
            message="سعد",
            slots=_name_slots("سعد"),
            state=state,
        ),
    )

    assert prep.customer_first_name == "سعد"
    assert {
        "product_id": prep.product_id,
        "quantity": prep.quantity,
        "line_items": list(prep.line_items),
        "order_status": prep.order_status,
        "checkout_channel": prep.checkout_channel,
        "missing_fields": list(prep.missing_fields),
    } == before


def _brain_stack(brain, state: MerchantConversationState, intent: Intent) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True,
                used_total=0,
                limit=1000,
                reason="",
                pct=0,
            ),
        )
    )
    stack.enter_context(
        patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None),
        )
    )
    stack.enter_context(
        patch("core.store_knowledge.build_merchant_context", return_value={})
    )
    stack.enter_context(
        patch(
            "core.active_order_context.load_commerce_bundle_from_db",
            return_value={},
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.commerce.permission_loader.load_tenant_commerce_permissions",
            return_value=PermissionLoadResult(
                permissions=CommercePermissionSet(tenant_id=1),
                source="defaults_missing_row",
                ok=True,
            ),
        )
    )
    stack.enter_context(
        patch.object(
            brain._classifier,
            "classify",
            new=AsyncMock(return_value=intent),
        )
    )
    stack.enter_context(
        patch.object(
            brain._decision_engine,
            "decide",
            return_value=Decision(action=ACTION_PROPOSE_DRAFT_ORDER, args={}),
        )
    )
    stack.enter_context(
        patch.object(brain._policy_gate, "gate", side_effect=lambda decision, _ctx: decision)
    )
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(
        patch.object(
            brain._facts_loader,
            "load",
            return_value=CommerceFacts(
                store_name="متجر تجريبي عام",
                has_products=True,
                product_count=1,
                in_stock_count=1,
                orderable=True,
                snapshot_fresh=True,
            ),
        )
    )
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    stack.enter_context(
        patch.object(brain._composer, "compose", new=AsyncMock(return_value="حاضر"))
    )
    return stack


def test_merchant_brain_process_persists_expected_name_answer() -> None:
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id)
    state = _active_state()
    intent = Intent(
        name="checkout_continuation",
        confidence=0.92,
        slots=_name_slots("سعد"),
        raw_message="سعد",
        extraction_method="hybrid",
    )
    brain = get_brain()

    with _brain_stack(brain, state, intent):
        asyncio.run(
            brain.process(
                db=db,
                tenant_id=tenant.id,
                customer_phone=DEFAULT_PHONE_E164,
                message="سعد",
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=42,
            )
        )

    reloaded = _reload_customer(db, tenant_id=tenant.id, customer_id=customer.id)
    assert reloaded.name == "سعد"
    assert reloaded.extra_metadata["customer_name_status"] == STATUS_CUSTOMER_ENTERED
