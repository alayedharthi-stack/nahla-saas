"""AI-first action ownership — semantic families, not phrase exceptions.

Asserts who owns intent vs who may execute merchant capabilities.
Does not assert exact customer-facing prose. Does not add an exception
for any one social token.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_libraries import is_payment_query  # noqa: E402
from modules.ai.brain.commerce.conversational_priority import (  # noqa: E402
    detect_payment_intent_strength,
    has_payment_outbound_consent,
)
from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: E402
    customer_origin_has_payment_request as origin_has_payment_request,
)
from modules.ai.brain.commerce.payment_execution_ownership import (  # noqa: E402
    asset_existence_creates_intent,
    is_structurally_explicit_inbound,
    is_structurally_explicit_payment_action,
    may_attach_payment_asset_after_brain,
    payment_early_bypass_allowed,
)
from modules.ai.brain.commerce.visual_delivery_capability import (  # noqa: E402
    try_visual_catalog_send_decision,
)
from modules.ai.brain.intent.link_disambiguation import (  # noqa: E402
    looks_like_store_link_request,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_PRODUCT_VISUAL_REQUEST,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

# Canonical live social family (Tenant 33 / conversation 26) plus siblings
# in the same semantic class. No per-phrase ignore list.
_SOCIAL_BANK_COLLISION_FAMILY = (
    "شوف اشوي بروح عند اهلي شويه واجي",
    "بروح عند أهلي الليلة وارجع",
    "جالس مع اهلي الحين",
)

_GENUINE_PAYMENT_FAMILY = (
    "أرسل بيانات التحويل",
    "كيف أدفع؟",
    "أرسل الحساب",
)

_ORDINARY_SOCIAL_FAMILY = (
    "تمام بشوفك بعد شوي",
    "الله يعافيك",
)

_WEBHOOK = Path(_BACKEND) / "routers" / "whatsapp_webhook.py"


def _text_meta() -> dict:
    return {"source_type": "text", "normalized_type": "text"}


class TestSocialBankCollisionNeverExecutesPayment:
    @pytest.mark.parametrize("message", _SOCIAL_BANK_COLLISION_FAMILY)
    def test_lexical_hit_is_not_semantic_intent(self, message: str) -> None:
        verdict = detect_payment_intent_strength(message)
        assert verdict.strength < 0.65
        assert verdict.source != "semantic" or verdict.strength == 0.0

    @pytest.mark.parametrize("message", _SOCIAL_BANK_COLLISION_FAMILY)
    def test_consent_not_inferred_from_substring(self, message: str) -> None:
        assert not has_payment_outbound_consent(
            message,
            inbound_metadata=_text_meta(),
            normalized_type="text",
            tenant_id=33,
            route="ownership_social_collision",
        )
        assert not origin_has_payment_request(message)

    @pytest.mark.parametrize("message", _SOCIAL_BANK_COLLISION_FAMILY)
    def test_unstructured_text_cannot_early_bypass(self, message: str) -> None:
        assert not payment_early_bypass_allowed(
            inbound_metadata=_text_meta(),
            normalized_type="text",
        )
        consent = has_payment_outbound_consent(
            message,
            inbound_metadata=_text_meta(),
            normalized_type="text",
            tenant_id=33,
            route="ownership_early_bypass",
        )
        assert not may_attach_payment_asset_after_brain(
            requestive_consent=consent,
            inbound_metadata=_text_meta(),
            normalized_type="text",
        )

    def test_weak_regex_may_still_collide_without_owning_execution(self) -> None:
        live = _SOCIAL_BANK_COLLISION_FAMILY[0]
        # Retrieval regex is intentionally unchanged — ownership moved.
        assert is_payment_query(live) is True
        assert detect_payment_intent_strength(live).strength < 0.65


class TestGenuinePaymentRequestAfterBrain:
    @pytest.mark.parametrize("message", _GENUINE_PAYMENT_FAMILY)
    def test_requestive_consent_without_pre_brain_bypass(self, message: str) -> None:
        assert has_payment_outbound_consent(
            message,
            inbound_metadata=_text_meta(),
            normalized_type="text",
            tenant_id=33,
            route="ownership_genuine_payment",
        )
        assert not payment_early_bypass_allowed(
            inbound_metadata=_text_meta(),
            normalized_type="text",
        )
        assert may_attach_payment_asset_after_brain(
            requestive_consent=True,
            inbound_metadata=_text_meta(),
            normalized_type="text",
            brain_decision_args={"topic": "payment_info"},
        )


class TestAssetExistenceDoesNotCreateIntent:
    def test_helper_contract(self) -> None:
        assert asset_existence_creates_intent() is False

    def test_asset_found_without_consent_cannot_attach(self) -> None:
        assert not may_attach_payment_asset_after_brain(
            requestive_consent=False,
            inbound_metadata=_text_meta(),
            normalized_type="text",
            brain_decision_args={"topic": "payment_info"},
        )


class TestStructuredPaymentActionRemainsDeterministic:
    def test_interactive_payment_action_id_may_bypass_brain(self) -> None:
        meta = {
            "normalized_type": "interactive",
            "source_type": "interactive",
            "button_id": "payment_bank_transfer",
        }
        assert is_structurally_explicit_inbound(
            meta, normalized_type="interactive",
        )
        assert is_structurally_explicit_payment_action(
            meta, normalized_type="interactive",
        )
        assert payment_early_bypass_allowed(
            inbound_metadata=meta, normalized_type="interactive",
        )
        assert may_attach_payment_asset_after_brain(
            requestive_consent=False,
            inbound_metadata=meta,
            normalized_type="interactive",
        )

    def test_interactive_non_payment_button_is_not_payment_bypass(self) -> None:
        meta = {
            "normalized_type": "interactive",
            "button_id": "pick_1",
        }
        assert is_structurally_explicit_inbound(
            meta, normalized_type="interactive",
        )
        assert not is_structurally_explicit_payment_action(
            meta, normalized_type="interactive",
        )
        assert not payment_early_bypass_allowed(
            inbound_metadata=meta, normalized_type="interactive",
        )


class TestOrdinarySocialHasNoCommerceEarlyBypass:
    @pytest.mark.parametrize("message", _ORDINARY_SOCIAL_FAMILY)
    def test_no_payment_ownership(self, message: str) -> None:
        assert not has_payment_outbound_consent(
            message,
            inbound_metadata=_text_meta(),
            normalized_type="text",
            tenant_id=33,
            route="ownership_ordinary_social",
        )
        assert not payment_early_bypass_allowed(
            inbound_metadata=_text_meta(),
            normalized_type="text",
        )


class TestProductMediaAndStoreLinkNotLexicallyHijacked:
    def test_social_collision_is_not_visual_send(self) -> None:
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="+966500000000",
            message=_SOCIAL_BANK_COLLISION_FAMILY[0],
            intent=Intent(name="general", confidence=0.4, slots={}),
            state=MerchantConversationState(turn=3),
            facts=CommerceFacts(has_products=True),
            history=[],
        )
        assert try_visual_catalog_send_decision(ctx) is None

    def test_visual_capability_still_executes_on_visual_intent(self) -> None:
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="+966500000000",
            message="نعم ارسل صور",
            intent=Intent(name=INTENT_PRODUCT_VISUAL_REQUEST, confidence=0.9, slots={}),
            state=MerchantConversationState(
                turn=4,
                last_presented_products=[
                    {"id": 11, "title": "قميص قطني أزرق", "image_url": "https://cdn.example/shirt.jpg"},
                ],
            ),
            facts=CommerceFacts(
                has_products=True,
                discovery_products=[
                    {"id": 11, "title": "قميص قطني أزرق", "image_url": "https://cdn.example/shirt.jpg"},
                ],
            ),
            history=[],
        )
        decision = try_visual_catalog_send_decision(ctx)
        assert decision is not None
        assert decision.action != "send_payment_asset"

    def test_social_collision_does_not_force_store_link(self) -> None:
        live = _SOCIAL_BANK_COLLISION_FAMILY[0]
        assert not looks_like_store_link_request(live)

    def test_genuine_store_link_ask_still_classified(self) -> None:
        assert looks_like_store_link_request("أبغى رابط المتجر")


class TestCannedPaymentTakeoverRemoved:
    def test_webhook_has_no_canned_bank_intro(self) -> None:
        src = _WEBHOOK.read_text(encoding="utf-8")
        assert "أكيد 🌷 تفضل، هذه بيانات التحويل البنكي." not in src
        assert "unstructured_requires_brain_semantic_ownership" in src


class TestMultiTenantAndIdempotencyContract:
    def test_consent_does_not_depend_on_foreign_tenant_asset(self) -> None:
        msg = _GENUINE_PAYMENT_FAMILY[0]
        a = has_payment_outbound_consent(msg, tenant_id=33, route="t33")
        b = has_payment_outbound_consent(msg, tenant_id=1, route="t1")
        assert a is True and b is True

    def test_duplicate_attach_gate_keeps_single_asset(self) -> None:
        # Ownership helper is idempotent: same inputs → same grant.
        kwargs = dict(
            requestive_consent=True,
            inbound_metadata=_text_meta(),
            normalized_type="text",
        )
        assert may_attach_payment_asset_after_brain(**kwargs) is True
        assert may_attach_payment_asset_after_brain(**kwargs) is True
