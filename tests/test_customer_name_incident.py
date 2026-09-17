"""Name ingestion contract: persisted identity, independent of AI replies.

All phones/events are synthetic. The incident's ambiguous wording is retained
as a negative case, never as evidence of the sender's identity.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.customer_identity_resolver import (
    apply_customer_name, can_use_name_for_operations, display_name_for_customer,
)
from core.customer_name_authority import PERSON_NAME, classify_whatsapp_profile_name
from core.customer_name_extractor import extract_high_confidence_name
from models import (
    Conversation, Customer, CustomerNameProvenance, MessageEvent, Tenant, User,
    WhatsAppConnection,
)
from test_customer_name_authority import _make_db

PHONE = "+966500000123"
FAMILIES = ("الغامدي", "الحارثي", "العتيبي", "القحطاني", "الزهراني")
NON_NAMES = (
    "الموقع", "الطلب", "الشحن", "المتجر", "الحساب", "المتوفر", "الجديد",
    "السعودي", "العالمي", "الهلالي", "المجاني", "الحمد لله", "سبحان الله",
    "مشغول", "متجر الملابس", "Unknown", "", "نور", "شمس",
)
EXPLICIT = (
    "اسمي أبو سعد", "أنا اسمي أبو سعد", "معك أبو سعد", "أنا أبو سعد",
    "هذا رقمي، أنا أبو سعد", "هذا رقمي, انا ابو سعد", "هذا رقمي، أنا ابوسعد",
    "هذا رقمي، انا ابوسعد.", "هذا رقمي، أَنا أبوسعد", "اسمي ابوسعد",
)
AMBIGUOUS = (
    "ذا رقمي ابوسعد", "هذا رقمي يا أبو سعد", "أرسله لأبو سعد",
    "كلم أبو سعد على هذا الرقم", "هذا رقمي، يا أبو سعد", "هذا رقمي أبو سعد",
)


@pytest.fixture
def db():
    session = _make_db()
    engine = session.get_bind()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _customer(db, *, name=None, meta=None, tenant=None):
    if tenant is None:
        tenant = Tenant(name=f"متجر تجريبي عام {db.query(Tenant).count() + 1}", is_active=True)
        db.add(tenant)
        db.flush()
    customer = Customer(
        tenant_id=tenant.id, name=name, phone=PHONE, normalized_phone=PHONE,
        extra_metadata=dict(meta or {}), acquisition_channel="whatsapp_inbound",
        first_seen_at=datetime.now(timezone.utc),
    )
    db.add(customer)
    db.flush()
    return customer


def _provenance(db, customer):
    return db.query(CustomerNameProvenance).filter_by(
        tenant_id=customer.tenant_id, customer_id=customer.id,
    ).one()


@pytest.mark.parametrize("name", FAMILIES)
def test_family_profile_persists_canonical_and_provenance(db, name):
    customer = _customer(db)
    apply_customer_name(customer, name, source="whatsapp_inbound")
    db.commit()
    db.expire_all()
    assert customer.name == name
    assert display_name_for_customer(customer, phone_fallback=PHONE) == name
    assert classify_whatsapp_profile_name(name).classification == PERSON_NAME
    assert not can_use_name_for_operations(customer)
    row = _provenance(db, customer)
    assert row.canonical_name == row.profile_hint == name
    assert row.authority == "WHATSAPP_PROFILE"
    assert row.source == "whatsapp_inbound"
    assert row.last_decision == "applied"


@pytest.mark.parametrize("name", NON_NAMES)
def test_untrusted_profile_word_never_becomes_canonical(db, name):
    customer = _customer(db)
    apply_customer_name(customer, name, source="whatsapp_inbound")
    db.commit()
    assert not customer.name
    assert display_name_for_customer(customer, phone_fallback=PHONE) == PHONE


@pytest.mark.parametrize("message", EXPLICIT)
def test_explicit_self_report_is_normalized_and_promoted(db, message):
    customer = _customer(db)
    apply_customer_name(customer, "الغامدي", source="whatsapp_inbound")
    hit = extract_high_confidence_name(message)
    assert hit is not None
    assert hit.value == "أبو سعد"
    assert apply_customer_name(
        customer, hit.value, source="ai_detected_name", explicit_customer_entry=True,
        message_context={"message": message, "wa_message_id": "synthetic:self-report"},
    )
    db.commit()
    db.expire_all()
    row = _provenance(db, customer)
    assert customer.name == row.canonical_name == "أبو سعد"
    assert row.authority == "CUSTOMER_SELF_REPORTED"
    assert row.previous_name == row.profile_hint == "الغامدي"
    assert row.previous_authority == "WHATSAPP_PROFILE"
    assert row.source == "ai_detected_name"
    assert row.evidence_ref["wa_message_id"] == "synthetic:self-report"


@pytest.mark.parametrize("message", AMBIGUOUS)
def test_addressee_or_ambiguous_text_does_not_overwrite_identity(db, message):
    customer = _customer(db, name="الغامدي", meta={
        "customer_name_authority": "WHATSAPP_PROFILE",
        "customer_name_source": "whatsapp_profile", "customer_name_status": "proposed",
    })
    assert extract_high_confidence_name(message) is None
    assert not apply_customer_name(
        customer, "أبو سعد", source="ai_detected_name", explicit_customer_entry=True,
        message_context={"message": message},
    )
    db.commit()
    assert customer.name == "الغامدي"


@pytest.mark.parametrize("protected", ["verified", "manual"])
def test_stronger_names_protected_from_profile_and_self_report(db, protected):
    customer = _customer(db)
    apply_customer_name(
        customer, "أحمد سالم", source="salla_sync" if protected == "verified" else "manual_admin",
        force_merchant=protected == "manual",
    )
    for name, source, context in (
        ("الغامدي", "whatsapp_inbound", {}),
        ("أبو سعد", "ai_detected_name", {"message": "اسمي أبو سعد"}),
    ):
        apply_customer_name(customer, name, source=source,
                            explicit_customer_entry=source == "ai_detected_name",
                            message_context=context)
    db.commit()
    assert customer.name == _provenance(db, customer).canonical_name == "أحمد سالم"


@pytest.mark.parametrize("profile,canonical", [
    ("الغامدي", "الغامدي"), ("الحمد لله", None), ("شمس", None),
    ("الغامدي", "أحمد سالم"),
])
def test_email_customers_and_conversations_share_resolved_identity(db, profile, canonical):
    from routers.customers import _serialize_customer
    from routers.conversations import list_conversations
    from services.merchant_first_contact import maybe_notify_first_customer

    customer = _customer(db)
    if canonical == "أحمد سالم":
        apply_customer_name(customer, canonical, source="salla_sync")
    apply_customer_name(customer, profile, source="whatsapp_inbound")
    db.add(User(tenant_id=customer.tenant_id, username="Merchant", role="merchant",
                email="merchant@example.com", password_hash="test-only"))
    db.add(Conversation(tenant_id=customer.tenant_id, customer_id=customer.id,
                        extra_metadata={"phone": PHONE}, status="active"))
    db.commit()
    expected = canonical or PHONE
    request = SimpleNamespace(state=SimpleNamespace(tenant_id=customer.tenant_id))
    with patch("routers.conversations.resolve_tenant_id", return_value=customer.tenant_id):
        conversations = asyncio.run(list_conversations(request, db=db))
    customers = _serialize_customer(customer, None)
    with patch("services.email_service.enqueue_email") as enqueue:
        result = maybe_notify_first_customer(
            db=db, tenant_id=customer.tenant_id, customer=customer,
            customer_phone=PHONE, customer_name=profile,
        )
    assert result["send"]
    assert enqueue.call_args.kwargs["variables"]["customer_name"] == expected
    assert customers["display_name"] == expected
    assert conversations["conversations"][0]["customer"] == expected


def _connection(db, customer, suffix):
    connection = WhatsAppConnection(
        tenant_id=customer.tenant_id, phone_number_id=f"PID_{suffix}",
        whatsapp_business_account_id=f"WABA_{suffix}", provider="meta",
        connection_type="embedded", status="connected", sending_enabled=True,
        webhook_verified=True,
    )
    db.add(connection)
    db.commit()
    return connection.phone_number_id


def _dispatch_disabled(db, monkeypatch, *, customers, message="اسمي أبو سعد", replay=False):
    from core.ai_disabled_gate import AIDisabledDecision
    from core.inbound_dedup import reset_cache
    import core.customer_identity_resolver as identity
    import routers.whatsapp_webhook as webhook

    pid_by_tenant = {c.tenant_id: _connection(db, c, str(c.tenant_id)) for c in customers}
    monkeypatch.setattr(webhook, "get_db", lambda: iter([db]))
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(webhook, "_is_platform_tenant", lambda *_a, **_k: False)
    monkeypatch.setattr("services.merchant_first_contact.maybe_notify_first_customer", lambda **_k: {})
    monkeypatch.setattr("core.ai_disabled_gate.is_ai_disabled_for_conversation",
                        lambda *_a, **_k: AIDisabledDecision(
                            disabled=True, reason="store_ai_test_mode_not_allowed"))
    reset_cache()
    with (
        patch.object(identity, "apply_customer_name", wraps=identity.apply_customer_name) as apply,
        patch.object(webhook, "_post_wa", new=AsyncMock()) as outbound,
        patch("modules.ai.brain.pipeline.get_brain") as brain,
        patch.object(webhook, "_run_and_deliver_commerce_v2_owner", new=AsyncMock()) as commerce,
    ):
        for customer in customers:
            msg = {"from": PHONE.lstrip("+"), "id": "synthetic:same-event",
                   "type": "text", "text": {"body": message}}
            value = {"contacts": [{"wa_id": PHONE.lstrip("+"),
                                    "profile": {"name": "الغامدي"}}]}
            asyncio.run(webhook._dispatch_message(pid_by_tenant[customer.tenant_id], msg, value))
            if replay:
                # Exercise BOTH worker-local and durable dedup on the real dispatcher.
                asyncio.run(webhook._dispatch_message(pid_by_tenant[customer.tenant_id], msg, value))
                reset_cache()
                asyncio.run(webhook._dispatch_message(pid_by_tenant[customer.tenant_id], msg, value))
        outbound.assert_not_called()
        brain.assert_not_called()
        commerce.assert_not_called()
    return apply


def test_disabled_gate_captures_identity_once_without_ai_or_outbound(db, monkeypatch):
    customer = _customer(db)
    apply = _dispatch_disabled(db, monkeypatch, customers=[customer], replay=True)
    db.expire_all()
    assert customer.name == "أبو سعد"
    rows = db.query(MessageEvent).filter_by(tenant_id=customer.tenant_id).all()
    assert len(rows) == 1 and rows[0].direction == "inbound"
    assert rows[0].extra_metadata["ai_disabled_gate"] is True
    assert db.query(CustomerNameProvenance).count() == 1
    row = _provenance(db, customer)
    assert row.previous_name == row.profile_hint == "الغامدي"
    assert row.last_decision == "applied"
    assert row.evidence_ref["wa_message_id"] == "synthetic:same-event"
    assert len([c for c in apply.call_args_list if c.kwargs.get("source") == "ai_detected_name"]) == 1


def test_disabled_gate_same_phone_and_event_are_tenant_isolated(db, monkeypatch):
    customers = [_customer(db), _customer(db)]
    _dispatch_disabled(db, monkeypatch, customers=customers, replay=True)
    db.expire_all()
    for customer in customers:
        assert customer.name == "أبو سعد"
        row = _provenance(db, customer)
        assert row.canonical_name == "أبو سعد"
        assert db.query(MessageEvent).filter_by(tenant_id=customer.tenant_id).count() == 1
    assert db.query(CustomerNameProvenance).count() == 2
