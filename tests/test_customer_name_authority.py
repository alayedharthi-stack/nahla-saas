"""Canonical customer-name authority — precedence, classification, evidence.

Covers the four requirements of the identity spec:

  1. One centralized authority with a strict precedence ladder
     VERIFIED_ECOMMERCE > CUSTOMER_SELF_REPORTED > WHATSAPP_PROFILE > UNKNOWN.
  2. Conservative WhatsApp profile classification that does NOT reject
     ordinary Arabic given names (دعاء / نور / جود) and DOES reject
     devotional/status phrases (الحمد لله / استغفر الله).
  3. Self-reported names only with explicit evidence.
  4. Blank ecommerce names never erase a stored name; lower authority
     never overwrites higher.
"""
from __future__ import annotations

import pytest

from core.customer_identity_resolver import (
    apply_customer_name,
    display_name_for_customer,
    read_customer_identity,
)
from core.customer_name_authority import (
    AMBIGUOUS,
    DECISION_APPLIED,
    DECISION_BLOCKED_BLANK,
    DECISION_BLOCKED_CLASS,
    DECISION_BLOCKED_EVIDENCE,
    DECISION_BLOCKED_LOCK,
    DECISION_BLOCKED_LOWER,
    DECISION_HINT_ONLY,
    NOT_PERSON_NAME,
    PERSON_NAME,
    NameAuthority,
    authority_for_source,
    classify_whatsapp_profile_name,
    evaluate_self_report_evidence,
    resolve_canonical_customer_name,
)


class _Customer:
    """Minimal detached stand-in for the Customer ORM row."""

    def __init__(self, name=None, meta=None, channel="whatsapp_inbound"):
        self.id = None
        self.tenant_id = None
        self.name = name
        self.extra_metadata = dict(meta or {})
        self.acquisition_channel = channel


# ══════════════════════════════════════════════════════════════════════
# 1. Authority ladder
# ══════════════════════════════════════════════════════════════════════

def test_authority_ladder_is_strictly_ordered():
    assert (
        NameAuthority.VERIFIED_ECOMMERCE
        > NameAuthority.CUSTOMER_SELF_REPORTED
        > NameAuthority.WHATSAPP_PROFILE
        > NameAuthority.UNKNOWN
    )


@pytest.mark.parametrize(
    "source,expected",
    [
        ("salla_sync", NameAuthority.VERIFIED_ECOMMERCE),
        ("salla_order", NameAuthority.VERIFIED_ECOMMERCE),
        ("zid_sync", NameAuthority.VERIFIED_ECOMMERCE),
        ("shopify_order", NameAuthority.VERIFIED_ECOMMERCE),
        ("order_webhook", NameAuthority.VERIFIED_ECOMMERCE),
        ("ai_detected_name", NameAuthority.CUSTOMER_SELF_REPORTED),
        ("customer_message", NameAuthority.CUSTOMER_SELF_REPORTED),
        ("whatsapp_inbound", NameAuthority.WHATSAPP_PROFILE),
        ("whatsapp_profile", NameAuthority.WHATSAPP_PROFILE),
        ("", NameAuthority.UNKNOWN),
        ("some_unknown_thing", NameAuthority.UNKNOWN),
    ],
)
def test_authority_for_source(source, expected):
    assert authority_for_source(source) is expected


def test_lower_authority_never_overwrites_higher():
    decision = resolve_canonical_customer_name(
        incoming_name="نور",
        incoming_authority=NameAuthority.WHATSAPP_PROFILE,
        current_name="محمد العتيبي",
        current_authority=NameAuthority.VERIFIED_ECOMMERCE,
        classification=PERSON_NAME,
    )
    assert decision.decision == DECISION_BLOCKED_LOWER
    assert decision.canonical_name == "محمد العتيبي"


def test_higher_authority_overwrites_lower():
    decision = resolve_canonical_customer_name(
        incoming_name="محمد العتيبي",
        incoming_authority=NameAuthority.VERIFIED_ECOMMERCE,
        current_name="نور",
        current_authority=NameAuthority.WHATSAPP_PROFILE,
    )
    assert decision.decision == DECISION_APPLIED
    assert decision.canonical_name == "محمد العتيبي"
    assert decision.authority is NameAuthority.VERIFIED_ECOMMERCE


def test_blank_incoming_never_erases_at_any_authority():
    for authority in NameAuthority:
        decision = resolve_canonical_customer_name(
            incoming_name="",
            incoming_authority=authority,
            current_name="محمد العتيبي",
            current_authority=NameAuthority.VERIFIED_ECOMMERCE,
        )
        assert decision.decision == DECISION_BLOCKED_BLANK
        assert decision.canonical_name == "محمد العتيبي"


def test_merchant_lock_outranks_the_whole_ladder():
    decision = resolve_canonical_customer_name(
        incoming_name="محمد العتيبي",
        incoming_authority=NameAuthority.VERIFIED_ECOMMERCE,
        current_name="أبو خالد",
        current_authority=NameAuthority.WHATSAPP_PROFILE,
        merchant_locked=True,
    )
    assert decision.decision == DECISION_BLOCKED_LOCK
    assert decision.canonical_name == "أبو خالد"


def test_equal_authority_may_refresh():
    decision = resolve_canonical_customer_name(
        incoming_name="محمد الغامدي",
        incoming_authority=NameAuthority.VERIFIED_ECOMMERCE,
        current_name="محمد العتيبي",
        current_authority=NameAuthority.VERIFIED_ECOMMERCE,
    )
    assert decision.decision == DECISION_APPLIED
    assert decision.canonical_name == "محمد الغامدي"


def test_ambiguous_profile_is_hint_only_never_canonical():
    decision = resolve_canonical_customer_name(
        incoming_name="شمس",
        incoming_authority=NameAuthority.WHATSAPP_PROFILE,
        current_name="",
        current_authority=NameAuthority.UNKNOWN,
        classification=AMBIGUOUS,
    )
    assert decision.decision == DECISION_HINT_ONLY
    assert decision.canonical_name == ""


def test_not_person_name_profile_is_blocked_outright():
    decision = resolve_canonical_customer_name(
        incoming_name="الحمد لله",
        incoming_authority=NameAuthority.WHATSAPP_PROFILE,
        current_name="",
        current_authority=NameAuthority.UNKNOWN,
        classification=NOT_PERSON_NAME,
    )
    assert decision.decision == DECISION_BLOCKED_CLASS


def test_self_report_without_evidence_is_blocked():
    decision = resolve_canonical_customer_name(
        incoming_name="محمد",
        incoming_authority=NameAuthority.CUSTOMER_SELF_REPORTED,
        current_name="",
        current_authority=NameAuthority.UNKNOWN,
        evidence_kind="",
    )
    assert decision.decision == DECISION_BLOCKED_EVIDENCE


# ══════════════════════════════════════════════════════════════════════
# 2. WhatsApp profile classification
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "profile",
    [
        # Explicitly required by the spec — common nouns that are also
        # ordinary given names. Must NOT be blanket-rejected.
        "دعاء", "نور", "جود",
        "محمد", "سارة", "خالد العتيبي",
        # الله-containing PERSONAL names must survive the devotional rule.
        "عبدالله", "عبد الله", "عبدالرحمن", "أبو عبدالله",
        "Ahmed Ali",
    ],
)
def test_legitimate_names_classify_as_person_name(profile):
    assert classify_whatsapp_profile_name(profile).classification == PERSON_NAME


@pytest.mark.parametrize(
    "profile",
    [
        # Devotional phrases — the production leak this work fixes.
        "الحمد لله", "الحمدلله", "استغفر الله", "سبحان الله",
        "ما شاء الله", "بسم الله", "إن شاء الله", "توكلت على الله",
        "يا رب", "اللهم صل",
        # Status / storefront profile strings.
        "متوفر", "مشغول", "للبيع", "مبيعات", "شركة",
        # Already covered by the platform validator.
        "الرياض", "عميل", "ابغى عسل", "هذا انت", "طيب",
        # Structural rejects.
        "0501234567", "",
    ],
)
def test_non_names_classify_as_not_person_name(profile):
    assert classify_whatsapp_profile_name(profile).classification == NOT_PERSON_NAME


def test_devotional_rule_is_structural_not_token_matching():
    """عبد الله is a name; الحمد لله is not. Same trailing token."""
    assert classify_whatsapp_profile_name("عبد الله").classification == PERSON_NAME
    assert classify_whatsapp_profile_name("الحمد لله").classification == NOT_PERSON_NAME


def test_unknown_single_token_is_ambiguous_not_rejected():
    verdict = classify_whatsapp_profile_name("شمس")
    assert verdict.classification == AMBIGUOUS
    assert verdict.cleaned == "شمس"


# ══════════════════════════════════════════════════════════════════════
# 3. Self-reported name evidence
# ══════════════════════════════════════════════════════════════════════

def test_explicit_self_statement_is_accepted():
    ev = evaluate_self_report_evidence("محمد", {"message": "اسمي محمد"})
    assert ev.accepted
    assert ev.kind == "explicit_self_statement"


def test_direct_answer_to_nahla_name_question_is_accepted():
    ev = evaluate_self_report_evidence(
        "محمد", {"message": "محمد", "awaiting_name_answer": True},
    )
    assert ev.accepted
    assert ev.kind == "direct_answer_to_name_question"


def test_explicit_correction_is_accepted():
    ev = evaluate_self_report_evidence("محمد", {"message": "صحح اسمي محمد"})
    assert ev.accepted


def test_third_party_mention_is_never_self_reported():
    """'send it to my brother Saad' must not make the customer Saad."""
    ev = evaluate_self_report_evidence("سعد", {"message": "أرسلها لأخوي سعد"})
    assert not ev.accepted


def test_bare_name_shaped_message_is_not_evidence():
    ev = evaluate_self_report_evidence("محمد", {"message": "محمد"})
    assert not ev.accepted
    assert ev.reason == "no_explicit_evidence"


def test_empty_candidate_is_rejected():
    assert not evaluate_self_report_evidence("", {"message": "اسمي محمد"}).accepted


# ══════════════════════════════════════════════════════════════════════
# 4. End-to-end through apply_customer_name
# ══════════════════════════════════════════════════════════════════════

def test_devotional_whatsapp_profile_never_becomes_display_name():
    """Regression: the proven production root cause.

    Before this fix "الحمد لله" was stored as ``proposed_name`` and
    surfaced by ``display_name_for_customer`` as the customer's name.
    """
    cust = _Customer()
    changed = apply_customer_name(cust, "الحمد لله", source="whatsapp_inbound")

    assert changed is False
    assert cust.name is None
    assert read_customer_identity(cust).proposed_name == ""
    assert display_name_for_customer(cust, phone_fallback="+966500000000") == (
        "+966500000000"
    )


def test_legitimate_arabic_profile_name_still_flows_through():
    cust = _Customer()
    assert apply_customer_name(cust, "نور", source="whatsapp_inbound") is True

    snap = read_customer_identity(cust)
    assert snap.proposed_name == "نور"
    assert cust.extra_metadata["proposed_name_classification"] == PERSON_NAME
    assert display_name_for_customer(cust, phone_fallback="+966500000000") == "نور"


def test_ambiguous_profile_hint_is_retained_but_not_displayed():
    cust = _Customer()
    apply_customer_name(cust, "شمس", source="whatsapp_inbound")

    assert cust.extra_metadata.get("proposed_name") == "شمس"
    assert cust.extra_metadata.get("proposed_name_classification") == AMBIGUOUS
    # Retained for merchant review, never shown as the customer's name.
    assert display_name_for_customer(cust, phone_fallback="+966500000000") == (
        "+966500000000"
    )


def test_salla_name_overrides_whatsapp_profile_hint():
    cust = _Customer()
    apply_customer_name(cust, "نور", source="whatsapp_inbound")
    assert apply_customer_name(cust, "محمد العتيبي", source="salla_sync") is True

    assert cust.name == "محمد العتيبي"
    assert cust.extra_metadata["customer_name_authority"] == "VERIFIED_ECOMMERCE"


def test_blank_salla_resync_never_erases_a_verified_name():
    cust = _Customer()
    apply_customer_name(cust, "محمد العتيبي", source="salla_sync")

    for blank in ("", "   ", None):
        assert apply_customer_name(cust, blank, source="salla_sync") is False
        assert cust.name == "محمد العتيبي"


def test_whatsapp_profile_never_overwrites_verified_name():
    cust = _Customer()
    apply_customer_name(cust, "محمد العتيبي", source="salla_sync")
    assert apply_customer_name(cust, "نور", source="whatsapp_inbound") is False
    assert cust.name == "محمد العتيبي"


def test_self_reported_requires_evidence_end_to_end():
    no_evidence = _Customer()
    assert (
        apply_customer_name(
            no_evidence, "محمد",
            source="ai_detected_name", explicit_customer_entry=True,
        )
        is False
    )
    assert no_evidence.name is None

    with_evidence = _Customer()
    assert (
        apply_customer_name(
            with_evidence, "محمد",
            source="ai_detected_name", explicit_customer_entry=True,
            message_context={"message": "اسمي محمد"},
        )
        is True
    )
    assert with_evidence.name == "محمد"
    assert with_evidence.extra_metadata["customer_name_authority"] == (
        "CUSTOMER_SELF_REPORTED"
    )
    assert with_evidence.extra_metadata["customer_name_evidence_kind"] == (
        "explicit_self_statement"
    )


def test_self_reported_does_not_beat_verified_ecommerce():
    cust = _Customer()
    apply_customer_name(cust, "محمد العتيبي", source="salla_sync")
    applied = apply_customer_name(
        cust, "أبو خالد",
        source="ai_detected_name", explicit_customer_entry=True,
        message_context={"message": "اسمي أبو خالد"},
    )
    assert applied is False
    assert cust.name == "محمد العتيبي"


def test_merchant_manual_override_semantics_are_preserved():
    cust = _Customer()
    apply_customer_name(
        cust, "حارث ضيف الله",
        source="merchant_manual", force_merchant=True,
    )
    assert cust.name == "حارث ضيف الله"
    assert cust.extra_metadata["manual_name_override"] is True

    # A WhatsApp profile hint must not disturb a merchant-typed name.
    assert apply_customer_name(cust, "نور", source="whatsapp_inbound") is False
    assert cust.name == "حارث ضيف الله"


# ══════════════════════════════════════════════════════════════════════
# 5. Durable provenance (real ORM session)
# ══════════════════════════════════════════════════════════════════════

def _make_db():
    """In-memory DB harness — mirrors tests/test_abandoned_cart_recovery.py."""
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker

    from models import Base

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
    return sessionmaker(bind=engine)()


def _seed_customer(db, name=None, meta=None):
    from models import Customer, Tenant

    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.flush()
    cust = Customer(
        tenant_id=tenant.id,
        phone="+966500000000",
        normalized_phone="+966500000000",
        name=name,
        extra_metadata=dict(meta or {}),
        acquisition_channel="whatsapp_inbound",
    )
    db.add(cust)
    db.flush()
    return cust


def test_provenance_row_is_written_for_a_verified_name():
    from core.customer_name_provenance import read_name_authority
    from models import CustomerNameProvenance

    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, "محمد العتيبي", source="salla_sync")
    db.flush()

    row = (
        db.query(CustomerNameProvenance)
        .filter(CustomerNameProvenance.customer_id == cust.id)
        .one()
    )
    assert row.canonical_name == "محمد العتيبي"
    assert row.authority == "VERIFIED_ECOMMERCE"
    assert row.source == "salla_sync"
    assert row.decision == DECISION_APPLIED
    assert read_name_authority(cust) is NameAuthority.VERIFIED_ECOMMERCE


def test_provenance_survives_a_metadata_merge():
    """CIS merges inbound metadata; the durable row is unaffected."""
    from models import CustomerNameProvenance

    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, "محمد العتيبي", source="salla_sync")
    db.flush()

    # Simulate a CIS upsert that clobbers extra_metadata wholesale.
    cust.extra_metadata = {"channel": "whatsapp", "source": "whatsapp_inbound"}
    db.flush()

    row = (
        db.query(CustomerNameProvenance)
        .filter(CustomerNameProvenance.customer_id == cust.id)
        .one()
    )
    assert row.canonical_name == "محمد العتيبي"
    assert row.authority == "VERIFIED_ECOMMERCE"


def test_rejected_profile_hint_records_provenance_without_naming_customer():
    from models import CustomerNameProvenance

    db = _make_db()
    cust = _seed_customer(db)
    assert apply_customer_name(cust, "الحمد لله", source="whatsapp_inbound") is False
    db.flush()

    assert cust.name is None
    row = (
        db.query(CustomerNameProvenance)
        .filter(CustomerNameProvenance.customer_id == cust.id)
        .one()
    )
    assert row.classification == NOT_PERSON_NAME
    assert row.decision == DECISION_BLOCKED_CLASS
    assert not row.canonical_name
