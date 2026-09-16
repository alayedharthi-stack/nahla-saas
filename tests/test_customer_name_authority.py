"""Canonical customer-name authority — precedence, classification, evidence,
provenance, and transaction isolation.

Every write-path assertion here goes through ``apply_customer_name`` so
the tests prove the resolver is the REAL authority gate, not just that
the pure function behaves in isolation.
"""
from __future__ import annotations

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.customer_identity_resolver import (
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
    STATUS_VERIFIED,
    apply_customer_name,
    can_use_name_for_operations,
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
    EVIDENCE_DIRECT_ANSWER,
    EVIDENCE_EXPLICIT_STATEMENT,
    MERCHANT_OVERRIDE_LABEL,
    NOT_PERSON_NAME,
    PERSON_NAME,
    NameAuthority,
    NameAuthorityDecision,
    authority_for_source,
    classify_whatsapp_profile_name,
    evaluate_self_report_evidence,
    is_bare_name_answer,
    resolve_canonical_customer_name,
)
from core.customer_name_provenance import read_name_authority, record_name_decision
from models import Base, Customer, CustomerNameProvenance, Tenant

VERIFIED_NAME = "محمد أحمد الحارثي"


# ══════════════════════════════════════════════════════════════════════
# Harness
# ══════════════════════════════════════════════════════════════════════

def _make_db():
    """In-memory DB with working SAVEPOINTs.

    pysqlite silently breaks SAVEPOINT unless we take over transaction
    control; this is the documented SQLAlchemy workaround.
    """
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _no_autobegin(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin(conn):
        conn.exec_driver_sql("BEGIN")

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


def _seed_customer(db, name=None, meta=None, phone="+966500000000"):
    tenant = Tenant(name="T", is_active=True)
    db.add(tenant)
    db.flush()
    cust = Customer(
        tenant_id=tenant.id,
        phone=phone,
        normalized_phone=phone,
        name=name,
        extra_metadata=dict(meta or {}),
        acquisition_channel="whatsapp_inbound",
    )
    db.add(cust)
    db.flush()
    return cust


def _prov(db, cust) -> CustomerNameProvenance:
    return (
        db.query(CustomerNameProvenance)
        .filter(CustomerNameProvenance.customer_id == cust.id)
        .one()
    )


class _Detached:
    """Detached stand-in — exercises the JSONB-mirror fallback path."""

    def __init__(self, name=None, meta=None):
        self.id = None
        self.tenant_id = None
        self.name = name
        self.extra_metadata = dict(meta or {})
        self.acquisition_channel = "whatsapp_inbound"


def _self_report(cust, name, message):
    return apply_customer_name(
        cust, name, source="ai_detected_name", explicit_customer_entry=True,
        message_context={"message": message},
    )


# ══════════════════════════════════════════════════════════════════════
# 1. Authority ladder (pure)
# ══════════════════════════════════════════════════════════════════════

def test_authority_ladder_is_strictly_ordered():
    assert (
        NameAuthority.VERIFIED_ECOMMERCE
        > NameAuthority.CUSTOMER_SELF_REPORTED
        > NameAuthority.WHATSAPP_PROFILE
        > NameAuthority.UNKNOWN
    )


def test_lower_authority_never_overwrites_higher():
    d = resolve_canonical_customer_name(
        incoming_name="أبو خالد", incoming_authority=NameAuthority.WHATSAPP_PROFILE,
        current_name=VERIFIED_NAME, current_authority=NameAuthority.VERIFIED_ECOMMERCE,
        classification=PERSON_NAME,
    )
    assert d.decision == DECISION_BLOCKED_LOWER and d.canonical_name == VERIFIED_NAME


def test_blank_incoming_never_erases_at_any_authority():
    for authority in NameAuthority:
        d = resolve_canonical_customer_name(
            incoming_name="  ", incoming_authority=authority,
            current_name=VERIFIED_NAME, current_authority=NameAuthority.VERIFIED_ECOMMERCE,
        )
        assert d.decision == DECISION_BLOCKED_BLANK and d.canonical_name == VERIFIED_NAME


def test_merchant_lock_outranks_the_whole_ladder():
    d = resolve_canonical_customer_name(
        incoming_name=VERIFIED_NAME, incoming_authority=NameAuthority.VERIFIED_ECOMMERCE,
        current_name="أبو خالد", current_authority=NameAuthority.WHATSAPP_PROFILE,
        merchant_locked=True,
    )
    assert d.decision == DECISION_BLOCKED_LOCK and d.canonical_name == "أبو خالد"


def test_merchant_cleared_name_is_refilled_only_by_self_report_or_store():
    wa = resolve_canonical_customer_name(
        incoming_name="محمد أحمد", incoming_authority=NameAuthority.WHATSAPP_PROFILE,
        current_name="", current_authority=NameAuthority.UNKNOWN,
        merchant_cleared=True, classification=PERSON_NAME,
    )
    assert wa.decision == DECISION_BLOCKED_LOCK
    sr = resolve_canonical_customer_name(
        incoming_name="محمد أحمد", incoming_authority=NameAuthority.CUSTOMER_SELF_REPORTED,
        current_name="", current_authority=NameAuthority.UNKNOWN,
        merchant_cleared=True, evidence_kind=EVIDENCE_EXPLICIT_STATEMENT,
    )
    assert sr.decision == DECISION_APPLIED


def test_unknown_provenance_name_is_protected_from_profile_but_not_from_store():
    wa = resolve_canonical_customer_name(
        incoming_name="محمد أحمد", incoming_authority=NameAuthority.WHATSAPP_PROFILE,
        current_name="اسم مستورد", current_authority=NameAuthority.UNKNOWN,
        classification=PERSON_NAME,
    )
    assert wa.decision == DECISION_BLOCKED_LOWER
    store = resolve_canonical_customer_name(
        incoming_name=VERIFIED_NAME, incoming_authority=NameAuthority.VERIFIED_ECOMMERCE,
        current_name="اسم مستورد", current_authority=NameAuthority.UNKNOWN,
    )
    assert store.decision == DECISION_APPLIED


def test_self_report_without_evidence_is_blocked():
    d = resolve_canonical_customer_name(
        incoming_name="محمد", incoming_authority=NameAuthority.CUSTOMER_SELF_REPORTED,
        current_name="", current_authority=NameAuthority.UNKNOWN, evidence_kind="",
    )
    assert d.decision == DECISION_BLOCKED_EVIDENCE


# ══════════════════════════════════════════════════════════════════════
# 2. Source alias authority audit (finding 7)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "source,expected",
    [
        # Canonical store sources.
        ("salla_sync", NameAuthority.VERIFIED_ECOMMERCE),
        ("salla_order", NameAuthority.VERIFIED_ECOMMERCE),
        ("zid_sync", NameAuthority.VERIFIED_ECOMMERCE),
        ("shopify_order", NameAuthority.VERIFIED_ECOMMERCE),
        # Retained generic aliases — each traced to a store handler in
        # backend/services/store_sync.py (handle_customer_webhook,
        # order webhook branch, _update_customer_profile_from_order,
        # upsert_customer_from_order default).
        ("customer_webhook", NameAuthority.VERIFIED_ECOMMERCE),
        ("order_webhook", NameAuthority.VERIFIED_ECOMMERCE),
        ("order_sync", NameAuthority.VERIFIED_ECOMMERCE),
        ("order_incremental", NameAuthority.VERIFIED_ECOMMERCE),
        # Dropped: no production caller writes these; a commercial-
        # sounding string does not earn store authority.
        ("order", NameAuthority.UNKNOWN),
        ("sales_channel", NameAuthority.UNKNOWN),
        ("commerce_platform", NameAuthority.UNKNOWN),
        ("platform_verified", NameAuthority.UNKNOWN),
        # Self-report / profile / unknown.
        ("ai_detected_name", NameAuthority.CUSTOMER_SELF_REPORTED),
        ("customer_message", NameAuthority.CUSTOMER_SELF_REPORTED),
        ("whatsapp_inbound", NameAuthority.WHATSAPP_PROFILE),
        ("whatsapp_lead", NameAuthority.WHATSAPP_PROFILE),
        ("whatsapp_profile", NameAuthority.WHATSAPP_PROFILE),
        ("manual_import", NameAuthority.UNKNOWN),
        ("widget", NameAuthority.UNKNOWN),
        ("", NameAuthority.UNKNOWN),
        ("some_unknown_thing", NameAuthority.UNKNOWN),
    ],
)
def test_authority_for_source_audit(source, expected):
    assert authority_for_source(source) is expected


def test_platform_hint_resolves_generic_order_alias_to_that_store():
    assert authority_for_source("order_webhook", platform="zid") is NameAuthority.VERIFIED_ECOMMERCE


# ══════════════════════════════════════════════════════════════════════
# 3. WhatsApp profile classification (finding 5)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "profile",
    [
        "محمد", "خالد", "فهد", "سارة", "فاطمة",          # name-only lexicon
        "عبدالله", "عبدالرحمن",                           # theophoric compound
        "عبد الله", "أبو عبدالله", "أبو خالد",             # onomastic prefix
        "محمد أحمد", "خالد العتيبي", "دعاء محمد العتيبي",  # multi-token shape
        "Ahmed Ali",
    ],
)
def test_person_names(profile):
    assert classify_whatsapp_profile_name(profile).classification == PERSON_NAME


@pytest.mark.parametrize("profile", ["دعاء", "نور", "جود", "أمل", "وعد", "شهد", "ريم"])
def test_polysemous_single_tokens_are_ambiguous_not_rejected(profile):
    verdict = classify_whatsapp_profile_name(profile)
    assert verdict.classification == AMBIGUOUS
    assert verdict.reason == "polysemous_given_name"


def test_unknown_single_token_is_ambiguous():
    assert classify_whatsapp_profile_name("شمس").classification == AMBIGUOUS


@pytest.mark.parametrize(
    "profile",
    [
        "الحمد لله", "الحمدلله", "استغفر الله", "سبحان الله", "ما شاء الله",
        "بسم الله", "إن شاء الله", "توكلت على الله", "يا رب", "اللهم صل",
        "متوفر", "مشغول", "للبيع", "مبيعات", "شركة",
        "الرياض", "عميل", "ابغى عسل", "هذا انت", "طيب", "0501234567", "",
    ],
)
def test_non_names_are_rejected(profile):
    assert classify_whatsapp_profile_name(profile).classification == NOT_PERSON_NAME


def test_devotional_rule_is_structural():
    assert classify_whatsapp_profile_name("عبد الله").classification == PERSON_NAME
    assert classify_whatsapp_profile_name("الحمد لله").classification == NOT_PERSON_NAME


# ══════════════════════════════════════════════════════════════════════
# 4. Self-reported evidence (finding 6, pure part)
# ══════════════════════════════════════════════════════════════════════

def test_explicit_self_statement_accepted():
    ev = evaluate_self_report_evidence("محمد", {"message": "اسمي محمد"})
    assert ev.accepted and ev.kind == EVIDENCE_EXPLICIT_STATEMENT


def test_direct_bare_answer_accepted_only_when_nahla_asked():
    asked = evaluate_self_report_evidence(
        "محمد أحمد",
        {"message": "محمد أحمد", "awaiting_name_answer": True, "conversation_id": 42},
    )
    assert asked.accepted and asked.kind == EVIDENCE_DIRECT_ANSWER
    assert asked.detail["conversation_id"] == 42
    not_asked = evaluate_self_report_evidence("محمد أحمد", {"message": "محمد أحمد"})
    assert not not_asked.accepted


@pytest.mark.parametrize("text", ["أنا محمد أحمد", "اسمي: محمد أحمد", "محمد أحمد 🙏", "طيب محمد أحمد شكرا"])
def test_bare_answer_tolerates_particles(text):
    assert is_bare_name_answer(text, "محمد أحمد")


@pytest.mark.parametrize(
    "text,candidate",
    [("أرسلها لمحمد", "محمد"), ("الهدية لسارة", "سارة"), ("كلمت خالد", "خالد"), ("أرسلها لأخوي سعد", "سعد")],
)
def test_third_party_mentions_rejected_even_when_nahla_asked(text, candidate):
    assert not is_bare_name_answer(text, candidate)
    ev = evaluate_self_report_evidence(candidate, {"message": text, "awaiting_name_answer": True})
    assert not ev.accepted
    assert not evaluate_self_report_evidence(candidate, {"message": text}).accepted


def test_role_context_rejected():
    assert not evaluate_self_report_evidence("مندوب", {"message": "أنا مندوب SMSA", "awaiting_name_answer": True}).accepted


# ══════════════════════════════════════════════════════════════════════
# 5. apply_customer_name follows the centralized resolver (finding 2)
# ══════════════════════════════════════════════════════════════════════

def test_apply_customer_name_uses_the_resolver(monkeypatch):
    """A fake resolver decision is what gets applied — nothing else decides."""
    import core.customer_name_authority as authority_mod

    captured = {}

    def fake_resolver(**kwargs):
        captured.update(kwargs)
        return NameAuthorityDecision(
            DECISION_APPLIED, canonical_name="اسم من المحلل",
            authority=NameAuthority.VERIFIED_ECOMMERCE, reason="fake",
        )

    monkeypatch.setattr(authority_mod, "resolve_canonical_customer_name", fake_resolver)
    cust = _Detached()
    assert apply_customer_name(cust, "محمد العتيبي", source="salla_sync") is True
    assert cust.name == "اسم من المحلل"
    assert captured["incoming_authority"] is NameAuthority.VERIFIED_ECOMMERCE
    assert captured["current_authority"] is NameAuthority.UNKNOWN


def test_apply_customer_name_obeys_a_blocking_resolver(monkeypatch):
    import core.customer_name_authority as authority_mod

    monkeypatch.setattr(
        authority_mod, "resolve_canonical_customer_name",
        lambda **kw: NameAuthorityDecision(DECISION_BLOCKED_LOWER, canonical_name=kw["current_name"], reason="fake"),
    )
    cust = _Detached(name="ثابت", meta={"customer_name_authority": "WHATSAPP_PROFILE"})
    assert apply_customer_name(cust, "محمد العتيبي", source="salla_sync") is False
    assert cust.name == "ثابت"


def test_legacy_trust_engine_is_gone():
    import core.customer_identity_resolver as resolver

    assert not hasattr(resolver, "_should_overwrite")
    assert not hasattr(resolver, "_protected_stored_name")


# ══════════════════════════════════════════════════════════════════════
# 6. WhatsApp canonical/display semantics (finding 4)
# ══════════════════════════════════════════════════════════════════════

def test_accepted_whatsapp_name_becomes_canonical_but_not_operational():
    db = _make_db()
    cust = _seed_customer(db)
    assert apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound") is True

    assert cust.name == "محمد أحمد"
    assert display_name_for_customer(cust, phone_fallback="+9665") == "محمد أحمد"
    assert cust.extra_metadata["customer_name_authority"] == "WHATSAPP_PROFILE"
    assert read_customer_identity(cust).customer_name_status == STATUS_PROPOSED
    assert can_use_name_for_operations(cust) is False
    assert read_name_authority(cust) is NameAuthority.WHATSAPP_PROFILE
    row = _prov(db, cust)
    assert row.canonical_name == "محمد أحمد" and row.authority == "WHATSAPP_PROFILE"


def test_full_upgrade_sequence_wa_then_self_report_then_salla_then_wa_cannot_downgrade():
    db = _make_db()
    cust = _seed_customer(db)

    assert apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound") is True
    assert cust.name == "محمد أحمد"

    assert _self_report(cust, "محمد أحمد الحارثي", "اسمي محمد أحمد الحارثي") is True
    assert cust.name == "محمد أحمد الحارثي"
    assert cust.extra_metadata["customer_name_authority"] == "CUSTOMER_SELF_REPORTED"
    assert cust.extra_metadata["customer_name_evidence_kind"] == EVIDENCE_EXPLICIT_STATEMENT
    assert read_customer_identity(cust).customer_name_status == STATUS_CUSTOMER_ENTERED

    assert apply_customer_name(cust, "محمد أحمد الحارثي الكامل", source="salla_sync") is True
    assert cust.name == "محمد أحمد الحارثي الكامل"
    assert cust.extra_metadata["customer_name_authority"] == "VERIFIED_ECOMMERCE"
    assert read_customer_identity(cust).customer_name_status == STATUS_VERIFIED
    assert can_use_name_for_operations(cust) is True

    assert apply_customer_name(cust, "أبو خالد", source="whatsapp_inbound") is False
    assert cust.name == "محمد أحمد الحارثي الكامل"
    row = _prov(db, cust)
    assert row.canonical_name == "محمد أحمد الحارثي الكامل"
    assert row.authority == "VERIFIED_ECOMMERCE"
    assert row.last_decision == DECISION_BLOCKED_LOWER
    assert row.last_attempt_name == "أبو خالد"


def test_self_reported_overrides_whatsapp_but_not_salla():
    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, VERIFIED_NAME, source="salla_sync")
    assert _self_report(cust, "أبو خالد", "اسمي أبو خالد") is False
    assert cust.name == VERIFIED_NAME


def test_whatsapp_profile_refresh_at_equal_authority():
    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound")
    assert apply_customer_name(cust, "محمد العتيبي", source="whatsapp_inbound") is True
    assert cust.name == "محمد العتيبي"


def test_validated_self_report_is_replaced_only_by_correction_or_direct_answer():
    db = _make_db()
    cust = _seed_customer(db)
    assert _self_report(cust, "عبدالله علي", "اسمي عبدالله علي") is True

    # Bare restatement in passing — not enough to flip a validated name.
    assert _self_report(cust, "هشام تركي", "معك هشام تركي") is False
    assert cust.name == "عبدالله علي"
    assert _prov(db, cust).last_decision == DECISION_BLOCKED_EVIDENCE

    # Explicit correction — replaces it.
    assert _self_report(cust, "هشام تركي", "اسمي الصحيح هشام تركي") is True
    assert cust.name == "هشام تركي"

    # Direct answer to Nahlah's own question — replaces it.
    assert apply_customer_name(
        cust, "سعد الغامدي", source="ai_detected_name", explicit_customer_entry=True,
        message_context={"message": "سعد الغامدي", "awaiting_name_answer": True},
    ) is True
    assert cust.name == "سعد الغامدي"


def test_dua_from_whatsapp_is_hint_only_but_self_reported_dua_is_canonical():
    db = _make_db()
    cust = _seed_customer(db)
    assert apply_customer_name(cust, "دعاء", source="whatsapp_inbound") is True  # metadata changed
    assert cust.name is None
    assert cust.extra_metadata["proposed_name"] == "دعاء"
    assert cust.extra_metadata["proposed_name_classification"] == AMBIGUOUS
    assert display_name_for_customer(cust, phone_fallback="+9665") == "+9665"
    assert _prov(db, cust).last_decision == DECISION_HINT_ONLY
    assert _prov(db, cust).canonical_name is None

    assert _self_report(cust, "دعاء", "اسمي دعاء") is True
    assert cust.name == "دعاء"
    assert cust.extra_metadata["customer_name_authority"] == "CUSTOMER_SELF_REPORTED"

    assert apply_customer_name(cust, "دعاء محمد العتيبي", source="salla_sync") is True
    assert cust.name == "دعاء محمد العتيبي"


def test_devotional_whatsapp_profile_never_names_the_customer():
    db = _make_db()
    cust = _seed_customer(db)
    assert apply_customer_name(cust, "الحمد لله", source="whatsapp_inbound") is False
    assert cust.name is None
    assert read_customer_identity(cust).proposed_name == ""
    assert display_name_for_customer(cust, phone_fallback="+9665") == "+9665"
    row = _prov(db, cust)
    assert row.canonical_name is None and row.last_decision == DECISION_BLOCKED_CLASS


def test_blank_salla_resync_never_erases():
    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, VERIFIED_NAME, source="salla_sync")
    for blank in ("", "   ", None):
        assert apply_customer_name(cust, blank, source="salla_sync") is False
        assert cust.name == VERIFIED_NAME


def test_merchant_manual_override_semantics_preserved():
    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, "حارث ضيف الله", source="merchant_manual", force_merchant=True)
    assert cust.name == "حارث ضيف الله"
    assert cust.extra_metadata["manual_name_override"] is True
    assert _prov(db, cust).authority == MERCHANT_OVERRIDE_LABEL
    assert _prov(db, cust).merchant_locked is True
    for attempt in (
        lambda: apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound"),
        lambda: _self_report(cust, "محمد أحمد", "اسمي محمد أحمد"),
        lambda: apply_customer_name(cust, VERIFIED_NAME, source="salla_sync"),
    ):
        assert attempt() is False
        assert cust.name == "حارث ضيف الله"


# ══════════════════════════════════════════════════════════════════════
# 7. Provenance preservation (finding 3)
# ══════════════════════════════════════════════════════════════════════

def test_blocked_whatsapp_attempt_preserves_verified_canonical_provenance():
    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, VERIFIED_NAME, source="salla_sync")
    before = _prov(db, cust).canonical_updated_at

    assert apply_customer_name(cust, "الحمد لله", source="whatsapp_inbound") is False

    row = _prov(db, cust)
    assert row.canonical_name == VERIFIED_NAME
    assert row.authority == "VERIFIED_ECOMMERCE"
    assert row.source == "salla_sync"
    assert row.canonical_updated_at == before
    assert row.last_decision == DECISION_BLOCKED_CLASS
    assert row.last_attempt_name == "الحمد لله"
    assert row.last_attempt_classification == NOT_PERSON_NAME
    assert cust.name == VERIFIED_NAME
    assert cust.extra_metadata["customer_name_authority"] == "VERIFIED_ECOMMERCE"


def test_ambiguous_whatsapp_hint_does_not_downgrade_verified_canonical_provenance():
    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, VERIFIED_NAME, source="salla_sync")

    assert apply_customer_name(cust, "نور", source="whatsapp_inbound") is True  # hint recorded

    row = _prov(db, cust)
    assert row.canonical_name == VERIFIED_NAME and row.authority == "VERIFIED_ECOMMERCE"
    assert row.source == "salla_sync"
    assert row.profile_hint == "نور" and row.profile_hint_classification == AMBIGUOUS
    assert row.last_decision == DECISION_HINT_ONLY
    assert cust.name == VERIFIED_NAME
    assert cust.extra_metadata["customer_name_authority"] == "VERIFIED_ECOMMERCE"
    assert cust.extra_metadata["customer_name_status"] == STATUS_VERIFIED
    assert display_name_for_customer(cust) == VERIFIED_NAME


def test_provenance_survives_wholesale_metadata_merge():
    db = _make_db()
    cust = _seed_customer(db)
    apply_customer_name(cust, VERIFIED_NAME, source="salla_sync")
    cust.extra_metadata = {"channel": "whatsapp"}
    db.flush()
    assert read_name_authority(cust) is NameAuthority.VERIFIED_ECOMMERCE
    assert apply_customer_name(cust, "محمد أحمد", source="whatsapp_inbound") is False


def test_self_report_provenance_carries_evidence_reference():
    db = _make_db()
    cust = _seed_customer(db)
    assert apply_customer_name(
        cust, "محمد أحمد", source="ai_detected_name", explicit_customer_entry=True,
        message_context={"message": "محمد أحمد", "awaiting_name_answer": True,
                         "conversation_id": 7, "asked_slot": "customer_first_name"},
    ) is True
    row = _prov(db, cust)
    assert row.evidence_kind == EVIDENCE_DIRECT_ANSWER
    assert row.evidence_ref["conversation_id"] == 7
    assert row.evidence_ref["asked_slot"] == "customer_first_name"
    assert cust.extra_metadata["customer_name_evidence_ref"]["conversation_id"] == 7


# ══════════════════════════════════════════════════════════════════════
# 8. Transaction isolation (finding 1)
# ══════════════════════════════════════════════════════════════════════

def _duplicate_row_on_write(monkeypatch):
    """Make the provenance flush hit UNIQUE(tenant_id, customer_id).

    A genuine DB-level failure INSIDE the savepoint — the strongest
    proof that only the savepoint rolls back.
    """
    import core.customer_name_provenance as prov_mod

    real = prov_mod._get_or_create_row

    def _dup(session, model, *, tenant_id, customer_id):
        row = real(session, model, tenant_id=tenant_id, customer_id=customer_id)
        session.add(model(tenant_id=tenant_id, customer_id=customer_id))  # duplicate
        return row

    monkeypatch.setattr(prov_mod, "_get_or_create_row", _dup)


def test_provenance_failure_does_not_roll_back_outer_transaction(monkeypatch):
    db = _make_db()
    cust = _seed_customer(db)

    # Unrelated pending business mutation in the SAME session.
    sibling = Customer(tenant_id=cust.tenant_id, phone="+966511111111",
                       normalized_phone="+966511111111", name="عميل آخر")
    db.add(sibling)
    cust.email = "pending@example.com"

    _duplicate_row_on_write(monkeypatch)
    decision = NameAuthorityDecision(
        DECISION_APPLIED, canonical_name=VERIFIED_NAME, authority=NameAuthority.VERIFIED_ECOMMERCE,
        incoming_name=VERIFIED_NAME, incoming_authority=NameAuthority.VERIFIED_ECOMMERCE,
    )
    assert record_name_decision(cust, decision, source="salla_sync") is False

    # Outer transaction is intact, usable and committable; the unrelated
    # pending mutation survived; no provenance row leaked.
    assert sibling in db
    assert db.query(Customer).filter(Customer.normalized_phone == "+966511111111").count() == 1
    db.commit()
    db.expire_all()
    assert db.query(Customer).filter(Customer.normalized_phone == "+966511111111").one().name == "عميل آخر"
    assert db.get(Customer, cust.id).email == "pending@example.com"
    assert db.query(CustomerNameProvenance).count() == 0


def test_provenance_failure_inside_apply_customer_name_keeps_the_name_write(monkeypatch):
    db = _make_db()
    cust = _seed_customer(db)
    sibling = Customer(tenant_id=cust.tenant_id, phone="+966522222222",
                       normalized_phone="+966522222222", name="عميل آخر")
    db.add(sibling)

    _duplicate_row_on_write(monkeypatch)
    assert apply_customer_name(cust, VERIFIED_NAME, source="salla_sync") is True
    assert cust.name == VERIFIED_NAME
    db.commit()
    db.expire_all()
    assert db.get(Customer, cust.id).name == VERIFIED_NAME
    assert db.query(Customer).filter(Customer.normalized_phone == "+966522222222").count() == 1
