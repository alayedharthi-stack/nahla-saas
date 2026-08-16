"""
Fourth canary regression — payment IBAN grounding, Saudi dialect, saved profile,
address routing, product image, dedup observability.

Platform-wide generic commerce only. See AGENTS.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.inbound_dedup import is_duplicate_inbound, reset_cache  # noqa: E402
from core.wa_address_ingestion import (  # noqa: E402
    is_address_like_delivery_text,
    resolve_address_state_patch,
)
from models import MerchantKnowledgeSection  # noqa: E402
from modules.ai.brain.commerce.commerce_turn_contract import is_address_on_file_claim  # noqa: E402
from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: E402
    apply_payment_credential_guard,
    compose_verified_bank_transfer_block,
    reply_contains_unverified_payment_credentials,
)
from modules.ai.brain.postprocess.saudi_dialect_guard import apply_saudi_dialect_guard  # noqa: E402
from modules.ai.order_flow_v2.outbound_guards import apply_order_flow_v2_outbound_guards  # noqa: E402
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.payment import build_payment_instruction_reply  # noqa: E402
from modules.ai.order_flow_v2.replies import (  # noqa: E402
    build_next_field_reply,
    build_product_image_request_reply,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE,
    DEFAULT_PHONE_E164,
    attach_brain_state,
    make_scenario_db,
    seed_customer,
    seed_customer_address,
    seed_conversation,
    seed_product,
    seed_tenant,
)

_PLACEHOLDER_IBAN = "SA1234567890123456789012"
_VERIFIED_IBAN = "SA0380000000608010167519"
_EGYPTIAN_MARKERS = ("كام", "بتاعنا", "بتاع", "لسه")


@pytest.fixture(autouse=True)
def _reset_inbound_dedup_cache() -> None:
    reset_cache()


@pytest.fixture(autouse=True)
def _enable_order_flow_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "true")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "false")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)


@pytest.fixture
def v2_enabled(_enable_order_flow_v2: None) -> None:
    """Alias for tests that declare explicit V2 dependency."""
    return None


def _seed_bank_kb(db, tenant_id: int, iban: str) -> None:
    row = MerchantKnowledgeSection(
        tenant_id=tenant_id,
        kind="bank_transfer",
        title="حساب التحويل",
        body=f"الآيبان: {iban}",
        is_active=True,
    )
    db.add(row)
    db.commit()


def _catalog_meta(*, retailer_id: str, price: float) -> dict:
    return {
        "source_type": "catalog_order",
        "product_items": [{"product_retailer_id": retailer_id, "quantity": 1, "item_price": price}],
        "order": {"product_items": [{"product_retailer_id": retailer_id, "quantity": 1, "item_price": price}]},
    }


class TestPaymentGroundingP0:
    def test_bank_transfer_does_not_invent_iban_when_store_payment_settings_missing(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر إلكترونيات تجريبي")
        reply = build_payment_instruction_reply(
            db,
            tenant_id=tenant.id,
            order_prep={"payment_method": "bank_transfer", "line_items": [{"product_name": "سماعة", "quantity": 1}]},
            brain_state={},
            payment_method="bank_transfer",
        )
        assert _PLACEHOLDER_IBAN not in reply
        assert "غير مضبوطة" in reply or "تواصل مع المتجر" in reply

    def test_bank_transfer_uses_only_verified_store_iban_when_configured(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر عطور تجريبي")
        _seed_bank_kb(db, tenant.id, _VERIFIED_IBAN)
        reply = build_payment_instruction_reply(
            db,
            tenant_id=tenant.id,
            order_prep={"payment_method": "bank_transfer"},
            brain_state={},
            payment_method="bank_transfer",
        )
        assert _VERIFIED_IBAN in reply
        assert _PLACEHOLDER_IBAN not in reply

    def test_payment_receipt_does_not_mark_paid_from_pdf_extraction_alone(self) -> None:
        from modules.ai.brain.postprocess.payment_reply_guard import apply_payment_reply_guard  # noqa: E402

        result = apply_payment_reply_guard(
            reply="تم تأكيد الدفع وتم استلام المبلغ ✅",
            inbound_text="",
            inbound_metadata={
                "payment_evidence_status": "amount_only_insufficient",
                "pdf_kind": "payment_pending_evidence",
            },
            payment_receipt_received=False,
        )
        assert result.replaced
        assert "تم تأكيد الدفع" not in result.reply

    def test_payment_receipt_amount_mismatch_requires_merchant_review(self) -> None:
        from modules.ai.brain.postprocess.payment_evidence import evaluate_payment_evidence  # noqa: E402

        evidence = evaluate_payment_evidence(
            inbound_metadata={"payment_evidence_status": "amount_mismatch"},
            inbound_text="",
            payment_receipt_received=False,
        )
        assert not evidence.evidence_ok

    def test_payment_reply_has_no_placeholder_iban(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر ملابس تجريبي")
        raw = f"رقم الآيبان بتاعنا: {_PLACEHOLDER_IBAN}"
        guarded = apply_payment_credential_guard(
            raw,
            db=db,
            tenant_id=tenant.id,
        )
        assert guarded.replaced
        assert _PLACEHOLDER_IBAN not in guarded.reply

    def test_compose_verified_block_never_emits_placeholder(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        text = compose_verified_bank_transfer_block(db, tenant_id=tenant.id)
        assert _PLACEHOLDER_IBAN not in text


class TestSaudiDialectGuard:
    def test_orderflow_replies_do_not_use_egyptian_terms(self) -> None:
        raw = "الكمية كام؟ باقي ما صدر رقم طلب"
        guarded = apply_order_flow_v2_outbound_guards(
            raw,
            db=None,
            tenant_id=1,
        )
        for marker in _EGYPTIAN_MARKERS:
            assert marker not in guarded

    def test_payment_replies_do_not_use_btaana_or_kam(self) -> None:
        raw = "رقم الحساب البنكي أو رقم الآيبان بتاعنا: SA000"
        guarded = apply_saudi_dialect_guard(raw, locale="ar")
        assert "بتاعنا" not in guarded.reply
        assert "كام" not in guarded.reply

    def test_composer_output_passes_saudi_dialect_guard_in_checkout(self) -> None:
        result = apply_saudi_dialect_guard("الكمية كام وبتاعنا", locale="ar")
        assert result.replaced
        assert "كام" not in result.reply
        assert "بتاعنا" not in result.reply


class TestSavedProfileAddress:
    @pytest.mark.usefixtures("v2_enabled")
    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    def test_catalog_checkout_does_not_ask_name_when_reliable_name_exists(
        self,
        mock_items,
    ) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر أحذية تجريبي")
        customer = seed_customer(
            db,
            tenant.id,
            phone=DEFAULT_PHONE_E164,
            name="أحمد سالم",
            extra_metadata={"customer_name_source": "merchant", "customer_name_status": "customer_entered"},
        )
        product = seed_product(db, tenant.id, title="حذاء رياضي", external_id="shoe-01", price=199.0)
        convo = seed_conversation(db, tenant.id, customer.id)
        seed_customer_address(
            db,
            tenant.id,
            customer.id,
            city="الرياض",
            saudi_national_address="RRRD1234",
        )
        mock_items.return_value = SimpleNamespace(
            line_items=[{"product_name": product.title, "quantity": 1, "catalog_price": 199.0}],
            unmatched_count=0,
        )
        attach_brain_state(
            convo,
            {
                "line_items": [{"product_name": product.title, "quantity": 1}],
                "order_flow_v2_trusted_price": True,
                "catalog_line_items_authoritative": True,
                "order_flow_v2_active": True,
            },
        )
        db.add(convo)
        db.commit()
        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="",
            inbound_metadata=_catalog_meta(retailer_id=product.meta_retailer_id or "shoe-01", price=199.0),
        )
        assert result.handled
        assert "وش اسمك" not in result.reply

    def test_on_file_all_my_data_claim_matches_broad_profile_phrase(self) -> None:
        assert is_address_on_file_claim("عنواني عندكم واسمي وكل بياناتي عندكم")

    def test_on_file_all_my_data_claim_confirms_saved_profile_address_when_available(self) -> None:
        assert is_address_on_file_claim("عنواني عندكم واسمي وكل بياناتي عندكم")
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        customer = seed_customer(db, tenant.id, phone=DEFAULT_PHONE_E164, name="سارة محمد")
        seed_customer_address(
            db,
            tenant.id,
            customer.id,
            city="الدمام",
            saudi_national_address="DMMD4321",
        )
        reply = build_next_field_reply(
            order_prep={"customer_first_name": "سارة", "customer_last_name": "محمد"},
            brain_state={},
            missing_fields=["city"],
            field_modes={"city": "confirm"},
            known_previous={"city": "الدمام", "short_address": "DMMD4321"},
        )
        assert "وش المدينة" not in reply
        assert "الدمام" in reply

    def test_on_file_all_my_data_claim_without_saved_data_asks_honestly(self) -> None:
        reply = build_next_field_reply(
            order_prep={},
            brain_state={},
            missing_fields=["city"],
            field_modes={},
            known_previous={},
        )
        from modules.ai.order_flow_v2.replies import build_address_on_file_collect_reply  # noqa: E402

        honest = build_address_on_file_collect_reply(
            order_prep={},
            brain_state={},
            missing_fields=["city"],
            field_modes={},
            known_previous={},
        )
        assert "ما ظهر لي عنوان محفوظ" in honest


class TestAddressRouting:
    def test_active_checkout_consumes_national_short_address_message(self) -> None:
        text = "RRRD1234"
        assert is_address_like_delivery_text(text)
        patch = resolve_address_state_patch(inbound_normalized_type="text", inbound_text=text)
        assert patch is not None
        assert patch.get("short_address_code") == "RRRD1234"

    def test_active_checkout_consumes_address_near_message(self) -> None:
        text = "عنوان قريب: RRRD1234، 123 شارع الملك، حي النخيل، الرياض 11564"
        assert is_address_like_delivery_text(text)
        patch = resolve_address_state_patch(inbound_normalized_type="text", inbound_text=text)
        assert patch is not None
        assert patch.get("short_address_code") == "RRRD1234"
        assert patch.get("address_line")

    @pytest.mark.usefixtures("v2_enabled")
    def test_address_like_text_does_not_route_to_catalog_browse(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر إكسسوارات تجريبي")
        customer = seed_customer(db, tenant.id, phone=DEFAULT_PHONE_E164, name="خالد فهد")
        convo = seed_conversation(db, tenant.id, customer.id)
        attach_brain_state(
            convo,
            {
                "line_items": [{"product_name": "كفر جوال", "quantity": 1, "catalog_price": 59.0}],
                "order_flow_v2_trusted_price": True,
                "catalog_line_items_authoritative": True,
                "order_flow_v2_active": True,
                "city": "الرياض",
                "customer_first_name": "خالد",
                "customer_last_name": "فهد",
            },
        )
        db.add(convo)
        db.commit()
        text = "عنوان قريب: RRRD1234، 123 شارع الملك، حي النخيل، الرياض 11564"
        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message=text,
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"


class TestProductImageRequest:
    @pytest.mark.usefixtures("v2_enabled")
    def test_product_image_request_after_product_keyword_keeps_product_context(self) -> None:
        prep = {
            "checkout_channel": "whatsapp_quick_order",
            "line_items": [{"product_name": "حذاء رياضي", "quantity": 1}],
        }
        reply = build_product_image_request_reply(order_prep=prep, brain_state={"order_prep": prep})
        assert "حذاء رياضي" in reply
        assert "الكتالوج" in reply

    @pytest.mark.usefixtures("v2_enabled")
    def test_product_image_request_in_quick_order_does_not_reset_to_generic_social(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        customer = seed_customer(db, tenant.id, phone=DEFAULT_PHONE_E164)
        convo = seed_conversation(db, tenant.id, customer.id)
        attach_brain_state(
            convo,
            {"checkout_channel": "whatsapp_quick_order", "catalog_sent": True},
        )
        db.add(convo)
        db.commit()
        result = try_handle_order_flow_v2(
            db,
            tenant_id=tenant.id,
            customer_phone=DEFAULT_PHONE,
            message="ابي أشوف صورته",
            inbound_metadata={"native_catalog_sent": True},
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"


class TestDedupObservability:
    def test_duplicate_inbound_same_provider_msg_id_processed_once(self) -> None:
        first = is_duplicate_inbound(phone_number_id="PH1", msg_id="wamid.TEST123")
        second = is_duplicate_inbound(phone_number_id="PH1", msg_id="wamid.TEST123")
        assert first is False
        assert second is True

    def test_duplicate_distinct_provider_msg_ids_allowed(self) -> None:
        a = is_duplicate_inbound(phone_number_id="PH1", msg_id="wamid.A")
        b = is_duplicate_inbound(phone_number_id="PH1", msg_id="wamid.B")
        assert a is False
        assert b is False


class TestPaymentCredentialDetection:
    def test_unverified_iban_detection(self) -> None:
        blocked, ibans = reply_contains_unverified_payment_credentials(
            f"IBAN: {_PLACEHOLDER_IBAN}",
            verified_ibans=(_VERIFIED_IBAN,),
        )
        assert blocked
        assert _PLACEHOLDER_IBAN in ibans

    def test_verified_iban_allowed(self) -> None:
        blocked, _ = reply_contains_unverified_payment_credentials(
            f"الآيبان: {_VERIFIED_IBAN}",
            verified_ibans=(_VERIFIED_IBAN,),
        )
        assert not blocked
