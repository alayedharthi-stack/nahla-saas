"""AI-D01 — proposed/display identity is confirmation context, not operational truth.

Assert status, provenance, name_mode, operational eligibility, confirmation
candidate, missing fields, and next_goal ownership. Do not assert exact
customer-facing wording.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Tuple
from unittest.mock import patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_CUSTOMER_MESSAGE,
    SOURCE_MANUAL_ADMIN,
    SOURCE_MERCHANT,
    SOURCE_WHATSAPP_PROFILE,
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
    apply_customer_name,
    can_use_name_for_operations,
    read_customer_identity,
)
from core.order_context_builder import build_order_context  # noqa: E402
from core.order_context_prefill import (  # noqa: E402
    MODE_ASK,
    MODE_CONFIRM,
    MODE_EDIT_REQUESTED,
    MODE_SKIP,
    build_checkout_compose_facts,
    derive_checkout_next_goal,
)
from models import Base, Customer, Tenant  # noqa: E402
from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: E402
    resolve_catalog_checkout_customer_identity,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    _derive_active_checkout_next_goal,
    build_commerce_turn_contract,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_CUSTOMER_B = "نورة عبدالله"
GENERIC_PRODUCT = "حذاء رياضي أبيض"
GENERIC_PHONE_A = "+966511111111"
GENERIC_PHONE_B = "+966522222222"

_COLLECT_NAME_FROM_SCRATCH = {
    "collect_customer_name_for_whatsapp_order",
    "collect_customer_name_only",
    "collect_customer_first_name",
    "collect_customer_name_from_scratch",
}


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db, *, name: str = GENERIC_MERCHANT) -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _customer(
    db,
    tenant_id: int,
    *,
    phone: str = GENERIC_PHONE_A,
    name: str = "",
    proposed: str = "",
    status: str = "",
    source: str = "",
    confidence: float = 0.0,
    extra: dict | None = None,
) -> Customer:
    digits = phone.lstrip("+")
    meta = dict(extra or {})
    if source:
        meta["customer_name_source"] = source
    if status:
        meta["customer_name_status"] = status
    if proposed:
        meta["proposed_name"] = proposed
    if confidence:
        meta["customer_name_confidence"] = confidence
    row = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=digits,
        name=name,
        extra_metadata=meta,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _catalog_prep() -> dict:
    return {
        "catalog_line_items_authoritative": True,
        "catalog_checkout_total": 180.0,
        "city": "الرياض",
        "short_address_code": "RRRD1234",
        "line_items": [{
            "product_retailer_id": "sku-white-shoe",
            "quantity": 1,
            "from_native_catalog_order": True,
            "name": GENERIC_PRODUCT,
        }],
    }


def _facts() -> CommerceFacts:
    return CommerceFacts(
        store_name=GENERIC_MERCHANT,
        has_products=True,
        product_count=4,
        in_stock_count=4,
        orderable=True,
        snapshot_fresh=True,
    )


def _order_ctx(db, tenant, customer, *, message: str = "", prep: dict | None = None):
    return build_order_context(
        db,
        tenant_id=tenant.id,
        customer=customer,
        phone=customer.phone,
        message=message,
        brain_state={"order_prep": prep or _catalog_prep()},
    )


def test_control_a_verified_operational_name_skips_collection() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        name=GENERIC_CUSTOMER,
        status=STATUS_CUSTOMER_ENTERED,
        source=SOURCE_MERCHANT,
        confidence=0.95,
    )
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert ctx.identity.missing_mode == MODE_SKIP
    assert facts["name_mode"] == MODE_SKIP
    assert facts["known_name"] == GENERIC_CUSTOMER
    assert facts.get("name_operational") is True
    assert "name_confirmation_candidate" not in facts
    assert "customer_first_name" not in facts["missing_fields"]
    assert "customer_last_name" not in facts["missing_fields"]
    assert facts["next_goal"] not in _COLLECT_NAME_FROM_SCRATCH
    assert facts["next_goal"] != "confirm_customer_name_once"
    assert can_use_name_for_operations(customer) is True


def test_control_b_proposed_whatsapp_name_is_confirmation_not_collect_from_scratch() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        proposed=GENERIC_CUSTOMER,
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.4,
    )
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert ctx.identity.missing_mode == MODE_CONFIRM
    assert ctx.identity.has_proposed_name is True
    assert ctx.identity.has_verified_name is False
    assert facts["name_mode"] == MODE_CONFIRM
    assert facts["name_confirmation_candidate"] == GENERIC_CUSTOMER
    assert facts.get("known_name") != GENERIC_CUSTOMER
    assert facts.get("name_operational") is False
    assert facts["next_goal"] == "confirm_customer_name_once"
    assert facts["next_goal"] not in _COLLECT_NAME_FROM_SCRATCH
    assert derive_checkout_next_goal(ctx.missing_fields_result, ctx.prefill) == (
        "confirm_customer_name_once"
    )


def test_control_c_proposed_name_is_never_silently_operational() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        name=GENERIC_CUSTOMER,
        proposed=GENERIC_CUSTOMER,
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.4,
    )
    assert can_use_name_for_operations(customer) is False
    identity = resolve_catalog_checkout_customer_identity(
        customer=customer,
        phone=customer.phone,
        order_prep={},
        profile={"name": GENERIC_CUSTOMER},
    )
    assert identity.customer_name_known is False
    assert not identity.known_facts.get("customer_name")
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert facts.get("name_operational") is False
    assert "known_name" not in facts


def test_control_d_confirmation_candidate_carries_proposed_provenance() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        proposed=GENERIC_CUSTOMER,
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.4,
    )
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert facts["name_confirmation_candidate"] == GENERIC_CUSTOMER
    assert facts["name_source"] == SOURCE_WHATSAPP_PROFILE
    assert facts["name_status"] == STATUS_PROPOSED
    assert facts.get("name_confidence") == 0.4
    assert facts.get("name_operational") is False
    assert "known_name" not in facts
    name_state = ctx.missing_fields_result.field_states["name"]
    assert name_state.mode == MODE_CONFIRM
    assert name_state.evidence.get("name_confirmation_candidate") == GENERIC_CUSTOMER


def test_control_e_canonical_confirmation_persists_operational_name() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        proposed=GENERIC_CUSTOMER,
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.4,
    )
    changed = apply_customer_name(
        customer,
        GENERIC_CUSTOMER,
        source="ai_detected_name",
        explicit_customer_entry=True,
        message_context={
            "message": GENERIC_CUSTOMER,
            "explicit_customer_entry": True,
        },
    )
    assert changed is True
    db.add(customer)
    db.commit()
    db.refresh(customer)
    snap = read_customer_identity(customer)
    assert snap.customer_name == GENERIC_CUSTOMER
    assert snap.customer_name_status == STATUS_CUSTOMER_ENTERED
    assert snap.customer_name_source == SOURCE_CUSTOMER_MESSAGE
    assert can_use_name_for_operations(customer) is True
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert facts["name_mode"] == MODE_SKIP
    assert facts["known_name"] == GENERIC_CUSTOMER
    assert facts.get("name_operational") is True
    assert facts["next_goal"] not in _COLLECT_NAME_FROM_SCRATCH
    assert facts["next_goal"] != "confirm_customer_name_once"


def test_control_f_correction_does_not_keep_proposed_as_official() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        proposed=GENERIC_CUSTOMER,
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.4,
    )
    changed = apply_customer_name(
        customer,
        GENERIC_CUSTOMER_B,
        source="ai_detected_name",
        explicit_customer_entry=True,
        message_context={
            "message": GENERIC_CUSTOMER_B,
            "explicit_customer_entry": True,
        },
    )
    assert changed is True
    db.add(customer)
    db.commit()
    db.refresh(customer)
    snap = read_customer_identity(customer)
    assert snap.customer_name == GENERIC_CUSTOMER_B
    assert snap.customer_name_status == STATUS_CUSTOMER_ENTERED
    assert can_use_name_for_operations(customer) is True
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert facts["known_name"] == GENERIC_CUSTOMER_B
    assert facts["name_mode"] == MODE_SKIP
    assert facts.get("name_confirmation_candidate") != GENERIC_CUSTOMER


def test_control_g_unknown_identity_still_asks() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(db, tenant.id)
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert ctx.identity.missing_mode == MODE_ASK
    assert facts["name_mode"] == MODE_ASK
    assert not facts.get("name_confirmation_candidate")
    assert facts["next_goal"] == "collect_customer_name_only"
    assert facts["next_goal"] != "confirm_customer_name_once"


def test_control_h_invalid_proposed_display_is_not_confirmation_candidate() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        proposed="هذا انت",
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.2,
    )
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert ctx.identity.has_proposed_name is False
    assert not ctx.identity.confirmation_candidate
    assert ctx.identity.missing_mode == MODE_ASK
    assert facts["name_mode"] == MODE_ASK
    assert "name_confirmation_candidate" not in facts
    assert can_use_name_for_operations(customer) is False


def test_control_i_merchant_locked_name_skips_confirm() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        name=GENERIC_CUSTOMER_B,
        status=STATUS_CUSTOMER_ENTERED,
        source=SOURCE_MANUAL_ADMIN,
        confidence=0.98,
        extra={"manual_name_override": True, "manual_name_cleared": False},
    )
    ctx = _order_ctx(db, tenant, customer)
    facts = build_checkout_compose_facts(ctx, phone=customer.phone)
    assert ctx.identity.locked_by_merchant is True
    assert ctx.identity.missing_mode == MODE_SKIP
    assert facts["name_mode"] == MODE_SKIP
    assert facts["known_name"] == GENERIC_CUSTOMER_B
    assert facts["next_goal"] != "confirm_customer_name_once"
    assert facts["next_goal"] not in _COLLECT_NAME_FROM_SCRATCH


def test_control_j_explicit_name_edit_request_is_preserved() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        name=GENERIC_CUSTOMER,
        status=STATUS_CUSTOMER_ENTERED,
        source=SOURCE_MERCHANT,
        confidence=0.95,
    )
    ctx = _order_ctx(db, tenant, customer, message="مو اسمي")
    facts = build_checkout_compose_facts(
        ctx,
        message="مو اسمي",
        phone=customer.phone,
    )
    assert ctx.identity.missing_mode == MODE_EDIT_REQUESTED
    assert facts["name_mode"] == MODE_EDIT_REQUESTED
    assert facts["next_goal"] == "collect_customer_name_only"


def test_control_k_tenant_isolation_same_phone() -> None:
    db, _ = _make_db()
    tenant_a = _seed_tenant(db, name="متجر أ")
    tenant_b = _seed_tenant(db, name="متجر ب")
    customer_a = _customer(
        db,
        tenant_a.id,
        phone=GENERIC_PHONE_A,
        proposed=GENERIC_CUSTOMER,
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.4,
    )
    customer_b = _customer(
        db,
        tenant_b.id,
        phone=GENERIC_PHONE_A,
        name=GENERIC_CUSTOMER_B,
        status=STATUS_CUSTOMER_ENTERED,
        source=SOURCE_MERCHANT,
        confidence=0.95,
    )
    facts_a = build_checkout_compose_facts(
        _order_ctx(db, tenant_a, customer_a),
        phone=GENERIC_PHONE_A,
    )
    facts_b = build_checkout_compose_facts(
        _order_ctx(db, tenant_b, customer_b),
        phone=GENERIC_PHONE_A,
    )
    assert facts_a["name_mode"] == MODE_CONFIRM
    assert facts_a["name_confirmation_candidate"] == GENERIC_CUSTOMER
    assert facts_a.get("name_operational") is False
    assert facts_b["name_mode"] == MODE_SKIP
    assert facts_b["known_name"] == GENERIC_CUSTOMER_B
    assert facts_b.get("name_operational") is True
    assert facts_a["name_confirmation_candidate"] != facts_b["known_name"]


def test_control_l_generic_commerce_catalog_contract_preserves_confirm_goal() -> None:
    db, _ = _make_db()
    tenant = _seed_tenant(db)
    customer = _customer(
        db,
        tenant.id,
        proposed=GENERIC_CUSTOMER,
        status=STATUS_PROPOSED,
        source=SOURCE_WHATSAPP_PROFILE,
        confidence=0.4,
    )
    prep = OrderPreparationState(
        catalog_line_items_authoritative=True,
        city="الرياض",
        short_address_code="RRRD1234",
        line_items=[{
            "product_retailer_id": "sku-white-shoe",
            "quantity": 1,
            "from_native_catalog_order": True,
            "name": GENERIC_PRODUCT,
        }],
        catalog_checkout_total=180.0,
    )
    state = MerchantConversationState(stage="ordering", order_prep=prep)
    brain_ctx = BrainContext(
        tenant_id=tenant.id,
        customer_id=customer.id,
        customer_phone=customer.normalized_phone,
        message="أبغى أكمل الطلب",
        intent=Intent(name="start_order", confidence=0.9, raw_message="أبغى أكمل الطلب"),
        state=state,
        facts=_facts(),
        history=[],
        profile={"name": GENERIC_CUSTOMER},
    )
    with patch(
        "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
        return_value=False,
    ), patch(
        "modules.ai.brain.commerce.catalog_order_checkout.try_active_catalog_checkout_continue_decision",
        return_value=None,
    ):
        contract = build_commerce_turn_contract(brain_ctx, db=db)
    assert contract.known_facts.get("customer_name_known") is not True
    assert contract.known_facts.get("name_mode") == MODE_CONFIRM
    assert contract.known_facts.get("name_confirmation_candidate") == GENERIC_CUSTOMER
    assert contract.known_facts.get("name_operational") is False
    assert contract.known_facts.get("name_source") == SOURCE_WHATSAPP_PROFILE
    assert contract.next_goal == "confirm_customer_name_once"
    assert contract.next_goal not in _COLLECT_NAME_FROM_SCRATCH


def test_active_checkout_goal_uses_confirm_mode_not_collect_from_scratch() -> None:
    goal = _derive_active_checkout_next_goal(
        "أبغى أكمل الطلب",
        ["customer_first_name", "city"],
        identity_missing_mode=MODE_CONFIRM,
    )
    assert goal == "confirm_customer_name_once"
    ask_goal = _derive_active_checkout_next_goal(
        "أبغى أكمل الطلب",
        ["customer_first_name"],
        identity_missing_mode=MODE_ASK,
    )
    assert ask_goal == "collect_customer_name_for_whatsapp_order"
    edit_goal = _derive_active_checkout_next_goal(
        "مو اسمي",
        ["customer_first_name"],
        identity_missing_mode=MODE_EDIT_REQUESTED,
    )
    assert edit_goal == "collect_customer_name_only"
