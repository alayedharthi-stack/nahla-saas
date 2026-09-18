"""Provider labels are admin identity, never evidence for checkout identity."""
from types import SimpleNamespace
from pathlib import Path
import asyncio
import sys
from unittest.mock import patch

import pytest

from core.customer_identity_resolver import (
    apply_customer_name, can_use_name_for_operations, display_name_for_customer,
)
from core.customer_display import DEFAULT_FALLBACK_NAME, personalization_customer_name_or_fallback
from core.customer_name_extractor import extract_high_confidence_name
from modules.ai.brain.commerce.catalog_checkout_customer_identity import (
    _resolve_operational_name, resolve_catalog_checkout_customer_identity,
)


def customer(**metadata):
    return SimpleNamespace(id=1, tenant_id=1, name=None, extra_metadata=metadata)


@pytest.mark.parametrize("label", [
    "مشاعل", "أحمد", "نورة", "المالكي", "الدوسري", "José", "John", "Ирина",
    "李雷", "李娜", "山田太郎", "María José", "Jean-Luc", "O’Connor", "D’Angelo",
    "Jürgen", "İpek", "مُحَمَّد", "Acme Studio",
])
def test_provider_label_display_without_checkout_identity(label):
    c = customer()
    apply_customer_name(c, label, source="whatsapp_profile")
    assert display_name_for_customer(c, phone_fallback="[PHONE]") == label
    assert not can_use_name_for_operations(c)
    resolved = resolve_catalog_checkout_customer_identity(customer=c, profile={"display_name": label})
    assert not resolved.customer_name_known
    assert not resolved.prep_patch


@pytest.mark.parametrize("label", [
    "", " ", "Unknown", "+15550000123", "123456", "https://example.test",
    "person@example.test", "😀", "!!!", "a\u202eb", "a\nb", "الموقع",
    "الشحن", "نور", "الحمد لله", "سبحان الله", "مشغول", "متجر الملابس",
])
def test_non_identity_profile_has_only_phone_fallback(label):
    c = customer()
    apply_customer_name(c, label, source="whatsapp_profile")
    assert display_name_for_customer(c, phone_fallback="[PHONE]") == "[PHONE]"
    assert not can_use_name_for_operations(c)


@pytest.mark.parametrize("label", [
    "الطلب", "المتجر", "الحساب", "المتوفر", "الجديد", "السعودي",
    "العالمي", "الهلالي", "المجاني", "شمس",
])
def test_removed_incident_lexicon_values_are_display_only_when_structurally_safe(label):
    """These values were rejected only by the removed PR-local token list.

    Without a product-approved worldwide lexicon, safe provider text may be
    discoverable to the merchant while remaining non-operational.
    """
    c = customer()
    apply_customer_name(c, label, source="whatsapp_profile")
    assert c.name is None
    assert display_name_for_customer(c, phone_fallback="[PHONE]") == label
    assert not can_use_name_for_operations(c)


@pytest.mark.parametrize("label", ["Acme Studio", "Example LLC"])
def test_inherited_multitoken_profile_policy_remains_proposed_but_non_operational(label):
    """List removal must not silently rewrite the inherited profile classifier."""
    c = customer()
    apply_customer_name(c, label, source="whatsapp_profile")
    assert c.name == label
    assert c.extra_metadata["proposed_name"] == label
    assert display_name_for_customer(c) == label
    assert not can_use_name_for_operations(c)


@pytest.mark.parametrize("profile", [{"name": "أحمد سالم"}, {"display_name": "Acme Studio"},
                                     {"customer": {"full_name": "أحمد سالم"}}])
def test_denied_customer_cannot_fall_back_to_raw_profile(profile):
    c = customer(customer_name_status="proposed", customer_name_source="whatsapp_profile")
    c.name = "أحمد سالم"
    assert not can_use_name_for_operations(c)
    assert _resolve_operational_name(customer=c, profile=profile) == ("", "")


def test_raw_profile_without_customer_has_no_operational_provenance():
    assert _resolve_operational_name(customer=None, profile={"display_name": "Acme Studio"}) == ("", "")


def _assert_absent_from_personalization_and_prompt_boundary(label):
    from modules.ai.prompts.builder import build_system_prompt

    c = customer()
    apply_customer_name(c, label, source="whatsapp_profile")
    approved = personalization_customer_name_or_fallback(c.name)
    prompt = build_system_prompt({"store_name": "Test Store", "customer_name": c.name or ""})
    assert (
        approved,
        label in prompt,
        f"Name: {label}" in prompt,
    ) == (DEFAULT_FALLBACK_NAME, False, False)


def test_single_token_display_label_is_absent_from_personalization_and_prompt_boundary():
    _assert_absent_from_personalization_and_prompt_boundary("مشاعل")


def test_business_display_label_is_absent_from_personalization_and_prompt_boundary():
    _assert_absent_from_personalization_and_prompt_boundary("Acme Studio")


def test_trusted_name_reaches_actual_personalization_and_prompt_boundary():
    from modules.ai.prompts.builder import build_system_prompt

    c = customer()
    apply_customer_name(
        c, "أحمد سالم", source="customer_message",
        message_context={"message": "اسمي أحمد سالم", "message_id": "salutation-proof"},
    )
    approved = personalization_customer_name_or_fallback(c.name)
    prompt = build_system_prompt({"store_name": "Test Store", "customer_name": c.name or ""})
    assert approved == "أحمد سالم"
    assert "Name: أحمد سالم" in prompt


@pytest.mark.parametrize("source", ["shopify_sync", "salla_sync", "customer_message", "manual_admin"])
def test_trusted_identity_stays_operational_and_profile_cannot_replace_it(source):
    c = customer()
    apply_customer_name(c, "أحمد سالم", source=source, force_merchant=source == "manual_admin",
                        message_context={"inbound_text": "اسمي أحمد سالم", "message_id": "synthetic-self-report"})
    assert can_use_name_for_operations(c)
    apply_customer_name(c, "مشاعل", source="whatsapp_profile")
    assert display_name_for_customer(c) == "أحمد سالم"
    assert resolve_catalog_checkout_customer_identity(customer=c).customer_name_known


def test_865_invalid_proposed_name_is_not_operational():
    c = customer(proposed_name="هذا انت", customer_name_status="proposed")
    assert not can_use_name_for_operations(c)
    assert _resolve_operational_name(customer=c, profile={}) == ("", "")


@pytest.mark.parametrize("message", ["اسمي مشاعل", "معك مشاعل"])
def test_explicit_self_report_promotes_provider_label(message):
    c = customer()
    apply_customer_name(c, "مشاعل", source="whatsapp_profile")
    assert not can_use_name_for_operations(c)
    apply_customer_name(c, "مشاعل", source="customer_message", message_context={"message": message})
    assert can_use_name_for_operations(c)


def test_bare_ana_single_token_remains_deferred_extractor_behavior():
    """Characterization only: this is not self-identification acceptance.

    Owner decision A explicitly defers support for ``أنا + single token``.
    Keeping the exact reproducer here prevents this scope choice from being
    mistaken for a fix or silently disappearing from the regression record.
    """
    message = "أنا مشاعل"
    c = customer()
    apply_customer_name(c, "مشاعل", source="whatsapp_profile")
    assert display_name_for_customer(c) == "مشاعل"
    assert extract_high_confidence_name(message) is None
    assert not can_use_name_for_operations(c)
    assert not resolve_catalog_checkout_customer_identity(customer=c).customer_name_known


@pytest.mark.parametrize("message", ["يا مشاعل", "كلم مشاعل", "أرسلها لمشاعل"])
def test_addressee_does_not_promote_provider_label(message):
    c = customer()
    apply_customer_name(c, "مشاعل", source="whatsapp_profile")
    assert not apply_customer_name(c, "مشاعل", source="customer_message", message_context={"message": message})
    assert not resolve_catalog_checkout_customer_identity(customer=c).customer_name_known


@pytest.fixture
def identity_db():
    # Reuse the prior incident's schema fixture; no new identity persistence model.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from test_customer_name_authority import _make_db

    db = _make_db()
    engine = db.get_bind()
    yield db
    db.close()
    engine.dispose()


@pytest.mark.parametrize("label", ["مشاعل", "李雷", "Acme Studio", "Unknown"])
def test_admin_surfaces_agree_without_checkout_promotion(identity_db, label):
    from test_customer_name_incident import _customer, PHONE
    from models import Conversation, User
    from routers.customers import _serialize_customer
    from routers.conversations import list_conversations
    from services.merchant_first_contact import maybe_notify_first_customer

    db = identity_db
    c = _customer(db)
    apply_customer_name(c, label, source="whatsapp_profile")
    db.add(User(tenant_id=c.tenant_id, username="Merchant", role="merchant",
                email="merchant@example.test", password_hash="test-only"))
    db.add(Conversation(tenant_id=c.tenant_id, customer_id=c.id, extra_metadata={"phone": PHONE}, status="active"))
    db.commit()
    expected = PHONE if label == "Unknown" else label
    request = SimpleNamespace(state=SimpleNamespace(tenant_id=c.tenant_id))
    with patch("routers.conversations.resolve_tenant_id", return_value=c.tenant_id):
        conversations = asyncio.run(list_conversations(request, db=db))
    with patch("services.email_service.enqueue_email") as email:
        maybe_notify_first_customer(db=db, tenant_id=c.tenant_id, customer=c,
                                    customer_phone=PHONE, customer_name="UNTRUSTED RAW VALUE")
    assert email.call_args.kwargs["variables"]["customer_name"] == expected
    assert _serialize_customer(c, None)["display_name"] == expected
    assert conversations["conversations"][0]["customer"] == expected
    assert not resolve_catalog_checkout_customer_identity(customer=c).customer_name_known


def test_disabled_gate_display_capture_replay_and_tenant_isolation(identity_db, monkeypatch):
    from test_customer_name_incident import _customer, _dispatch_disabled
    from models import CustomerNameProvenance, MessageEvent
    import routers.whatsapp_webhook as webhook

    db = identity_db
    customers = [_customer(db), _customer(db)]
    original_dispatch = webhook._dispatch_message

    async def dispatch(pid, message, value):
        value["contacts"][0]["profile"]["name"] = "مشاعل"
        return await original_dispatch(pid, message, value)

    monkeypatch.setattr(webhook, "_dispatch_message", dispatch)
    # The shared harness asserts zero Brain, Commerce runner and provider calls.
    _dispatch_disabled(db, monkeypatch, customers=customers, message="مرحبا", replay=True)
    db.expire_all()
    for c in customers:
        assert display_name_for_customer(c) == "مشاعل"
        assert not resolve_catalog_checkout_customer_identity(customer=c).customer_name_known
        assert db.query(CustomerNameProvenance).filter_by(tenant_id=c.tenant_id, customer_id=c.id).count() == 1
        assert db.query(MessageEvent).filter_by(tenant_id=c.tenant_id).count() == 1
