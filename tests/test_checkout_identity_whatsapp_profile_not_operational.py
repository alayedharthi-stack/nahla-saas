"""WhatsApp profile identity is display-only: it never auto-fills the
checkout / shipping name.

Drives the REAL checkout functions (``_seed_checkout_state``,
``_missing_checkout_fields``, ``_persist_expected_checkout_name_answer``)
against a real customer row so the five review cases are proven on the
runtime path, not on the pure resolver.

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

from core.customer_identity_resolver import (
    STATUS_PROPOSED,
    apply_customer_name,
    can_use_name_for_operations,
    display_name_for_customer,
    read_customer_identity,
)
from core.customer_name_authority import AMBIGUOUS, EVIDENCE_DIRECT_ANSWER, NameAuthority
from core.customer_name_provenance import read_name_authority
from models import Base, Customer, CustomerNameProvenance, Tenant
from modules.ai.brain.execution.orders import (
    _expects_checkout_name_answer,
    _merge_message_details,
    _missing_checkout_fields,
    _persist_expected_checkout_name_answer,
    _seed_checkout_state,
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


def _prep(missing=None) -> OrderPreparationState:
    return OrderPreparationState(
        quantity=1, product_id="generic-1", order_status="awaiting_address",
        missing_fields=list(missing or []),
        line_items=[{"product_id": "generic-1", "product_name": "منتج", "quantity": 1, "match_status": "matched"}],
        product_options_loaded=True, checkout_channel="whatsapp",
    )


def _ctx(db, tenant_id, *, message="", slots=None, prep=None, profile_name=""):
    state = MerchantConversationState(
        stage="ordering", turn=2, order_prep=prep or _prep(),
        current_product_focus={"id": "generic-1", "external_id": "generic-1", "title": "منتج",
                               "price": 10.0, "orderable": True, "can_checkout": True, "in_stock": True},
    )
    ctx = BrainContext(
        tenant_id=tenant_id, customer_phone=PHONE, message=message, raw_message=message,
        intent=Intent(name="checkout_continuation", confidence=0.9, slots=dict(slots or {}),
                      raw_message=message, extraction_method="hybrid"),
        state=state, facts=CommerceFacts(), history=[],
        profile={"name": profile_name} if profile_name else {}, conversation_id=99,
    )
    ctx._db = db
    return ctx


def _seed_and_missing(db, tenant_id, profile_name):
    prep = _prep()
    ctx = _ctx(db, tenant_id, prep=prep, profile_name=profile_name)
    _seed_checkout_state(prep, ctx)
    return prep, _missing_checkout_fields(prep)


def _answer_name_question(db, tenant_id, answer):
    prep = _prep(missing=["customer_first_name", "customer_last_name", "city"])
    parts = answer.split()
    slots = {"customer_name": answer, "customer_first_name": parts[0], "customer_last_name": " ".join(parts[1:])}
    ctx = _ctx(db, tenant_id, message=answer, slots=slots, prep=prep)
    expected = _expects_checkout_name_answer(prep)
    _merge_message_details(prep, ctx.intent.slots, ctx.raw_message)
    return _persist_expected_checkout_name_answer(ctx, prep, expected=expected, previous_name="")


# ── CASE 1: PERSON_NAME WhatsApp profile, nothing stronger ───────────

def test_case1_whatsapp_profile_displays_but_checkout_still_asks():
    db, tenant, cust = _db()
    assert apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound") is True
    db.commit()

    # CRM / conversation display may show it …
    assert cust.name == "محمد أحمد"
    assert display_name_for_customer(cust, phone_fallback=PHONE) == "محمد أحمد"
    assert read_name_authority(cust) is NameAuthority.WHATSAPP_PROFILE
    assert read_customer_identity(cust).customer_name_status == STATUS_PROPOSED
    # … but it is not operational …
    assert can_use_name_for_operations(cust) is False

    # … so checkout does not prefill and the name slot stays missing.
    prep, missing = _seed_and_missing(db, tenant.id, profile_name="محمد أحمد")
    assert prep.customer_first_name == ""
    assert prep.customer_last_name == ""
    assert "customer_first_name" in missing


# ── CASE 2: direct answer to Nahlah's question upgrades identity ─────

def test_case2_direct_answer_upgrades_and_then_prefills_checkout():
    db, tenant, cust = _db()
    apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound")
    db.commit()
    assert _seed_and_missing(db, tenant.id, "محمد أحمد")[0].customer_first_name == ""

    assert _answer_name_question(db, tenant.id, "محمد أحمد") is True
    db.commit()
    db.expire_all()
    cust = db.get(Customer, cust.id)
    assert cust.extra_metadata["customer_name_authority"] == "CUSTOMER_SELF_REPORTED"
    assert can_use_name_for_operations(cust) is True
    row = db.query(CustomerNameProvenance).filter_by(customer_id=cust.id).one()
    assert row.evidence_kind == EVIDENCE_DIRECT_ANSWER
    assert row.evidence_ref["conversation_id"] == 99

    prep, missing = _seed_and_missing(db, tenant.id, profile_name="محمد أحمد")
    assert prep.customer_first_name == "محمد"
    assert prep.customer_last_name == "أحمد"
    assert "customer_first_name" not in missing


# ── CASE 3: verified ecommerce name prefills ─────────────────────────

def test_case3_verified_store_name_prefills_checkout():
    db, tenant, cust = _db()
    apply_customer_name(cust, "محمد أحمد الحارثي", source="salla_sync")
    db.commit()
    assert can_use_name_for_operations(cust) is True
    prep, missing = _seed_and_missing(db, tenant.id, profile_name="أبو خالد")
    assert prep.customer_first_name == "محمد"
    assert prep.customer_last_name == "أحمد الحارثي"
    assert "customer_first_name" not in missing


# ── CASE 4: ambiguous profile ────────────────────────────────────────

def test_case4_ambiguous_profile_is_neither_canonical_nor_prefilled():
    db, tenant, cust = _db()
    apply_customer_name(cust, "دعاء", source="whatsapp_inbound")
    db.commit()
    assert cust.name is None
    assert cust.extra_metadata["proposed_name_classification"] == AMBIGUOUS
    prep, missing = _seed_and_missing(db, tenant.id, profile_name="دعاء")
    assert prep.customer_first_name == ""
    assert "customer_first_name" in missing


# ── CASE 5: PERSON_NAME profile never populates shipping name ─────────

def test_case5_person_name_profile_displays_only():
    db, tenant, cust = _db()
    apply_customer_name(cust, "خالد محمد", source="whatsapp_inbound")
    db.commit()
    assert display_name_for_customer(cust, phone_fallback=PHONE) == "خالد محمد"
    prep, missing = _seed_and_missing(db, tenant.id, profile_name="خالد محمد")
    assert prep.customer_first_name == "" and prep.customer_last_name == ""
    assert "customer_first_name" in missing


def test_profile_without_any_customer_row_never_prefills():
    """Even with no row at all, ctx.profile['name'] is not shipping identity."""
    db, tenant, _ = _db()
    prep = _prep()
    ctx = _ctx(db, tenant.id, prep=prep, profile_name="خالد محمد")
    ctx.customer_phone = "+966599999999"  # no row for this phone
    _seed_checkout_state(prep, ctx)
    assert prep.customer_first_name == ""
    assert "customer_first_name" in _missing_checkout_fields(prep)
