"""Direct-answer name capture on the REAL checkout runtime path.

Nahlah's order funnel asks "ما اسمك الأول؟" by placing a customer-name
slot at ``prep.missing_fields[0]``. On the next turn
``_expects_checkout_name_answer`` reads that durable state and
``_persist_expected_checkout_name_answer`` persists the grounded answer.
These tests drive those real functions — no evidence flag is supplied
by hand; the runtime sets it.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.customer_name_authority import EVIDENCE_DIRECT_ANSWER, NameAuthority
from core.customer_name_provenance import read_name_authority
from models import Base, Customer, CustomerNameProvenance, Tenant
from modules.ai.brain.execution.orders import (
    _expects_checkout_name_answer,
    _merge_message_details,
    _persist_expected_checkout_name_answer,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

PHONE = "+966500000000"


def _db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    db = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي", is_active=True)
    db.add(tenant)
    db.flush()
    cust = Customer(tenant_id=tenant.id, phone=PHONE, normalized_phone=PHONE,
                    name=None, extra_metadata={}, acquisition_channel="whatsapp_inbound")
    db.add(cust)
    db.commit()
    return db, tenant, cust


def _state(missing_fields: list[str]) -> MerchantConversationState:
    prep = OrderPreparationState(
        quantity=1,
        product_id="generic-1",
        order_status="awaiting_address",
        missing_fields=list(missing_fields),
        line_items=[{"product_id": "generic-1", "product_name": "منتج", "quantity": 1, "match_status": "matched"}],
        product_options_loaded=True,
        checkout_channel="whatsapp",
    )
    return MerchantConversationState(
        stage="ordering", turn=2, order_prep=prep,
        current_product_focus={"id": "generic-1", "external_id": "generic-1", "title": "منتج",
                               "price": 10.0, "orderable": True, "can_checkout": True, "in_stock": True},
    )


def _ctx(db, tenant_id: int, message: str, slots: dict, state: MerchantConversationState) -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id, customer_phone=PHONE, message=message, raw_message=message,
        intent=Intent(name="checkout_continuation", confidence=0.9, slots=dict(slots),
                      raw_message=message, extraction_method="hybrid"),
        state=state, facts=CommerceFacts(), history=[], profile={}, conversation_id=99,
    )
    ctx._db = db
    return ctx


def _name_slots(name: str) -> dict:
    parts = name.split()
    return {"customer_name": name, "customer_first_name": parts[0], "customer_last_name": " ".join(parts[1:])}


def _run(db, tenant_id: int, message: str, slots: dict, missing: list[str]) -> bool:
    state = _state(missing)
    ctx = _ctx(db, tenant_id, message, slots, state)
    prep = state.order_prep
    expected = _expects_checkout_name_answer(prep)
    _merge_message_details(prep, ctx.intent.slots, ctx.raw_message)
    return _persist_expected_checkout_name_answer(ctx, prep, expected=expected, previous_name="")


def test_bare_answer_to_nahla_name_question_is_captured_with_durable_evidence():
    db, tenant, cust = _db()
    assert _run(db, tenant.id, "محمد أحمد", _name_slots("محمد أحمد"),
                ["customer_first_name", "city", "delivery_address"]) is True
    db.commit()
    db.expire_all()
    cust = db.get(Customer, cust.id)
    assert cust.name == "محمد أحمد"
    assert cust.extra_metadata["customer_name_authority"] == "CUSTOMER_SELF_REPORTED"
    assert cust.extra_metadata["customer_name_evidence_kind"] == EVIDENCE_DIRECT_ANSWER
    assert read_name_authority(cust) is NameAuthority.CUSTOMER_SELF_REPORTED
    row = db.query(CustomerNameProvenance).filter_by(customer_id=cust.id).one()
    assert row.evidence_kind == EVIDENCE_DIRECT_ANSWER
    assert row.evidence_ref["conversation_id"] == 99
    assert row.evidence_ref["asked_slot"] == "customer_first_name"
    assert row.evidence_ref["message_excerpt"] == "محمد أحمد"


def test_sentence_about_someone_else_is_not_the_customers_name_even_when_asked():
    db, tenant, cust = _db()
    for message, name in (("كلمت خالد", "خالد"), ("أرسلها لمحمد", "محمد"), ("الهدية لسارة", "سارة")):
        assert _run(db, tenant.id, message, _name_slots(name),
                    ["customer_first_name", "city"]) is False
    db.commit()
    db.expire_all()
    assert db.get(Customer, cust.id).name is None


def test_name_shaped_message_is_not_captured_when_nahla_did_not_ask():
    db, tenant, cust = _db()
    assert _run(db, tenant.id, "محمد أحمد", _name_slots("محمد أحمد"), ["city", "delivery_address"]) is False
    assert db.get(Customer, cust.id).name is None


def test_recipient_name_slot_is_not_the_customers_own_name():
    """Collecting a gift recipient is not collecting the customer's identity."""
    prep = _state(["recipient_name", "city"]).order_prep
    assert _expects_checkout_name_answer(prep) is False


def test_direct_answer_does_not_beat_a_verified_store_name():
    from core.customer_identity_resolver import apply_customer_name

    db, tenant, cust = _db()
    apply_customer_name(cust, "محمد أحمد الحارثي الكامل", source="salla_sync")
    db.commit()
    assert _run(db, tenant.id, "أبو خالد", _name_slots("أبو خالد"), ["customer_first_name"]) is False
    db.expire_all()
    assert db.get(Customer, cust.id).name == "محمد أحمد الحارثي الكامل"
