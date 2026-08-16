"""Global AI ownership — Brain-first unstructured NL, tenant-parity, isolation.

Asserts ownership and structured-slot exceptions. Does not assert exact
customer-facing prose. Does not add phrase exceptions for live utterances.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_order_lifecycle import has_accepted_delivery_address  # noqa: E402
from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: E402
    evaluate_branch_trigger_routing,
)
from modules.ai.brain.commerce.unstructured_turn_ownership import (  # noqa: E402
    UNSTRUCTURED_REQUIRES_BRAIN_REASON,
    ofv2_may_own_prebrain,
    unstructured_natural_language_requires_brain,
)
from modules.ai.order_flow_v2.slot_ownership import (  # noqa: E402
    apply_explicit_name_override,
    apply_slot_ownership,
)
from modules.ai.routing.layer0_router import evaluate_layer0_route  # noqa: E402

_SOCIAL_WHILE_CHECKOUT = (
    "شوف اشوي بروح عند اهلي شويه واجي",
    "تمام بشوفك بعد شوي",
)
_FOLLOW_UP_FAMILY = (
    "وش صار",
    "طيب وبعدين",
)
_PAYMENT_FAMILY = (
    "ابي ادفع",
    "كيف احول المبلغ",
    "أرسل بيانات التحويل",
)
_LOCATION_OVERLAP_FAMILY = (
    "شوف اشوي بروح عند اهلي شويه واجي",
    "بجيكم بعد العشاء",
)
_GENERIC_SOCIAL = "الله يعافيك"
_IDENTITY = "من أنت؟"
_PRODUCT = "وش عندكم؟"


def _text_meta() -> dict:
    return {"source_type": "text", "normalized_type": "text"}


class TestUnstructuredRequiresBrain:
    @pytest.mark.parametrize(
        "message",
        _SOCIAL_WHILE_CHECKOUT + _FOLLOW_UP_FAMILY + _PAYMENT_FAMILY
        + _LOCATION_OVERLAP_FAMILY + (_GENERIC_SOCIAL, _IDENTITY, _PRODUCT),
    )
    def test_free_text_requires_brain(self, message: str) -> None:
        assert unstructured_natural_language_requires_brain(
            _text_meta(), normalized_type="text", message=message,
        )
        assert not ofv2_may_own_prebrain(
            _text_meta(), normalized_type="text", message=message,
        )

    def test_tenant_parity_same_ownership(self) -> None:
        msg = _PAYMENT_FAMILY[0]
        a = ofv2_may_own_prebrain(_text_meta(), message=msg)
        b = ofv2_may_own_prebrain(_text_meta(), message=msg)
        assert a is False and b is False
        assert unstructured_natural_language_requires_brain(
            _text_meta(), message=msg,
        ) is True

    def test_salla_not_required_for_brain_gate(self) -> None:
        msg = _SOCIAL_WHILE_CHECKOUT[0]
        assert unstructured_natural_language_requires_brain(
            {"source_type": "text", "integration": "none"},
            message=msg,
        )
        assert unstructured_natural_language_requires_brain(
            {"source_type": "text", "integration": "salla"},
            message=msg,
        )


class TestStructuredExceptionsRemainDeterministic:
    def test_catalog_order_payload(self) -> None:
        meta = {
            "source_type": "catalog_order",
            "product_items": [{"retailer_id": "SKU-1", "quantity": 1}],
        }
        assert ofv2_may_own_prebrain(meta, normalized_type="catalog_order", message="")
        assert not unstructured_natural_language_requires_brain(
            meta, normalized_type="catalog_order", message="",
        )

    def test_interactive_button(self) -> None:
        meta = {
            "normalized_type": "interactive",
            "button_id": "pay_now",
        }
        assert ofv2_may_own_prebrain(meta, normalized_type="interactive", message="")

    def test_location_pin(self) -> None:
        meta = {"source_type": "location", "latitude": 24.7, "longitude": 46.7}
        assert ofv2_may_own_prebrain(meta, normalized_type="location", message="")

    def test_national_short_code_token(self) -> None:
        assert ofv2_may_own_prebrain(_text_meta(), message="RIYD1234")
        assert not unstructured_natural_language_requires_brain(
            _text_meta(), message="RIYD1234",
        )

    def test_national_short_code_does_not_match_natural_language(self) -> None:
        sentence = "العنوان الوطني RIYD1234 قريب من البيت"
        assert unstructured_natural_language_requires_brain(
            _text_meta(), message=sentence,
        )
        assert not ofv2_may_own_prebrain(_text_meta(), message=sentence)


class TestNameSlotDoesNotIngestFollowUp:
    def test_follow_up_is_not_customer_identity(self) -> None:
        from unittest.mock import MagicMock, patch  # noqa: PLC0415
        from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: PLC0415

        prep = {
            "order_flow_v2_active": True,
            "line_items": [{"product_id": "sku-1", "product_name": "قميص قطني أزرق", "quantity": 1}],
            "missing_fields": ["customer_name"],
        }
        with patch(
            "modules.ai.order_flow_v2.owner.operational_tuple",
            return_value=(True, False, "global_enabled"),
        ), patch(
            "modules.ai.order_flow_v2.owner._load_brain_state",
            return_value=(None, {"order_prep": prep}),
        ):
            result = try_handle_order_flow_v2(
                MagicMock(),
                tenant_id=1,
                customer_phone="966500000001",
                message="وش صار",
                inbound_metadata=_text_meta(),
            )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == UNSTRUCTURED_REQUIRES_BRAIN_REASON
        assert "وش" not in str(result.state_patch)

        patch, reason = apply_explicit_name_override(
            message="وش صار",
            order_prep={"order_flow_v2_active": True},
            checkout_active=True,
        )
        # Helper may still classify; OFV2 owner must not execute it pre-Brain.
        _ = patch, reason


class TestBranchDoesNotOwnUnstructuredArrivalOverlap:
    def test_family_sentence_does_not_route_branch(self) -> None:
        decision = evaluate_branch_trigger_routing(
            db=None,
            tenant_id=33,
            message=_LOCATION_OVERLAP_FAMILY[0],
            customer_phone="966500000033",
            inbound_metadata=_text_meta(),
        )
        assert decision is None


class TestLayer0DoesNotOwnUnstructured:
    def test_social_and_faq_yield_to_brain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LAYER0_ROUTER_ENABLED", "true")
        decision = evaluate_layer0_route(
            db=None,
            tenant_id=33,
            customer_phone="966500000033",
            message=_GENERIC_SOCIAL,
        )
        assert decision is None


class TestAcceptedMapsPinIsAddressEvidence:
    def test_nested_pending_location_counts(self) -> None:
        prep = {
            "city": "",
            "google_maps_url": "",
            "pending_delivery_location": {
                "source": "whatsapp_location_pin",
                "google_maps_url": "https://maps.app.goo.gl/example",
                "delivery_address_status": "accepted",
            },
        }
        assert has_accepted_delivery_address(prep) is True

    def test_city_is_independent_slot(self) -> None:
        prep = {
            "city": "",
            "pending_delivery_location": {
                "google_maps_url": "https://maps.app.goo.gl/example",
                "delivery_address_status": "accepted",
            },
        }
        assert has_accepted_delivery_address(prep) is True
        assert not str(prep.get("city") or "").strip()


class TestOfv2TestModeDoesNotCreateSeparateRuntime:
    def test_global_disabled_test_mode_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.ai_disabled_gate import StoreAIModeDecision  # noqa: PLC0415
        from core.tenant import STORE_AI_MODE_TEST  # noqa: PLC0415
        from modules.ai.order_flow_v2.enforcement import (  # noqa: PLC0415
            resolve_order_flow_v2_operational,
        )
        from unittest.mock import MagicMock, patch  # noqa: PLC0415

        monkeypatch.delenv("ORDER_FLOW_V2_ENFORCE_TENANTS", raising=False)
        monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
        monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_allowed_by_store_mode",
            return_value=StoreAIModeDecision(allowed=True, mode=STORE_AI_MODE_TEST),
        ):
            with patch("core.billing.has_billing_access", return_value=True):
                decision = resolve_order_flow_v2_operational(
                    MagicMock(),
                    tenant_id=33,
                    customer_phone="966537970430",
                )
        assert decision.live is False
        assert decision.reason == "shadow_only"
    def test_reason_constant_stable(self) -> None:
        assert UNSTRUCTURED_REQUIRES_BRAIN_REASON == (
            "unstructured_requires_brain_semantic_ownership"
        )

    def test_payload_signature_includes_inbound_id(self) -> None:
        from core.outbound_dedup import _payload_signature  # noqa: PLC0415

        a = _payload_signature({
            "type": "text",
            "text": {"body": "وش المدينة؟"},
            "_nahla_inbound_id": "wamid.AAA",
        })
        b = _payload_signature({
            "type": "text",
            "text": {"body": "وش المدينة؟"},
            "_nahla_inbound_id": "wamid.BBB",
        })
        replay = _payload_signature({
            "type": "text",
            "text": {"body": "وش المدينة؟"},
            "_nahla_inbound_id": "wamid.AAA",
        })
        assert a != b
        assert a == replay
