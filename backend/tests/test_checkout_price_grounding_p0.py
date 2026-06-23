"""P0 — checkout address must not trip product price grounding; slot fallbacks."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_pause_guard import (  # noqa: E402
    RECOVERY_TEXT_AR,
    evaluate_loop_pre_send,
)
from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: E402
    build_checkout_slot_fallback_reply,
    is_checkout_continue_inbound,
)
from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: E402
    message_has_bare_quantity_or_variant_signal,
    resolve_active_order_quantity_reply,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    select_arabic_commerce_fallback,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    build_product_claim_grounding_evidence,
    collect_whatsapp_catalog_grounded_prices,
    extract_reply_prices,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402

_ADDRESS_REPLY = (
    "تمام هشام، سجلت عنوانك MDQA5061، 5061 محمد الكارزيني، "
    "6684، حي بطحاء قريش، مكة 24352"
)
_LONG_ADDRESS = (
    "عنوان قريب: MDQA5061، 5061 محمد الكارزيني، 6684، "
    "حي بطحاء قريش، مكة 24352"
)
_BROKEN_GENERIC = "تمام، أكمل معك الطلب — وش تحتاج؟"
_PRICE_GUARD_SNIPPET = "ما ظهر عندي سعر مؤكد من الكتالوج"


def _catalog_checkout_state(*, missing: list[str] | None = None) -> SimpleNamespace:
    item = {
        "product_id": "140",
        "product_name": "عسل",
        "quantity": 2,
        "unit_price": 182.75,
        "currency": "SAR",
        "product_retailer_id": "geuiu4knwm",
        "source": "whatsapp_native_catalog_order",
        "from_catalog_order": True,
    }
    prep = OrderPreparationState(
        line_items=[dict(item)],
        order_status="collecting_address",
        missing_fields=missing or ["short_address_code"],
        customer_first_name="هشام",
        customer_last_name="الحارثي",
        city="",
    )
    return SimpleNamespace(
        cart_items=[dict(item)],
        order_prep=prep,
        stage="ordering",
    )


class TestAddressDigitsAreNotPrices:
    def test_extract_reply_prices_ignores_address_digits(self) -> None:
        prices = extract_reply_prices(_ADDRESS_REPLY)
        assert prices == set()

    def test_product_claim_guard_allows_address_confirmation(self) -> None:
        result = apply_product_claim_grounding_guard(
            reply=_ADDRESS_REPLY,
            tenant_id=33,
            chosen_path="order_slot_prompt",
        )
        assert not result.replaced
        assert _PRICE_GUARD_SNIPPET not in result.reply


class TestWaCatalogTotalTrusted:
    def test_catalog_metadata_prices_enter_grounded_evidence(self) -> None:
        meta = {
            "source_type": "catalog_order",
            "total_price": 365.50,
            "currency": "SAR",
            "product_items": [
                {"item_price": 182.75, "quantity": 2, "currency": "SAR"},
            ],
        }
        prices = collect_whatsapp_catalog_grounded_prices(
            inbound_metadata=meta,
        )
        assert 365 in prices or 182 in prices

    def test_catalog_total_in_reply_not_rewritten(self) -> None:
        meta = {
            "source_type": "catalog_order",
            "total_price": 365.50,
            "currency": "SAR",
            "product_items": [{"item_price": 182.75, "quantity": 2}],
        }
        state = _catalog_checkout_state()
        reply = "تمام، إجمالي طلبك 365.50 ريال — نكمل بيانات التوصيل؟"
        result = apply_product_claim_grounding_guard(
            reply=reply,
            tenant_id=33,
            chosen_path="propose_draft_order",
            order_state=state,
            inbound_metadata=meta,
        )
        assert not result.replaced
        assert _PRICE_GUARD_SNIPPET not in result.reply

    def test_evidence_builder_includes_wa_prices(self) -> None:
        ev = build_product_claim_grounding_evidence(
            None,
            33,
            inbound_metadata={
                "source_type": "catalog_order",
                "total_price": 365.50,
                "product_items": [{"item_price": 182.75, "quantity": 2}],
            },
            order_state=_catalog_checkout_state(),
        )
        assert ev.whatsapp_catalog_trusted
        assert 365 in ev.grounded_prices or 182 in ev.grounded_prices


class TestCheckoutAddressTurn:
    def test_no_quantity_hijack_on_address(self) -> None:
        state = _catalog_checkout_state(missing=["city", "short_address_code"])
        assert not message_has_bare_quantity_or_variant_signal(_LONG_ADDRESS)
        assert resolve_active_order_quantity_reply(
            _LONG_ADDRESS,
            state=state,
            active_commerce=True,
        ) is None

    def test_address_turn_not_price_guarded(self) -> None:
        meta = {
            "source_type": "catalog_order",
            "total_price": 365.50,
            "product_items": [{"item_price": 182.75, "quantity": 2}],
        }
        result = apply_product_claim_grounding_guard(
            reply=_ADDRESS_REPLY,
            tenant_id=33,
            chosen_path="order_slot_prompt",
            order_state=_catalog_checkout_state(missing=["city"]),
            inbound_metadata=meta,
        )
        assert _PRICE_GUARD_SNIPPET not in result.reply


class TestBrokenFallbackPrevention:
    def test_empty_compose_returns_slot_specific_not_generic(self) -> None:
        state = _catalog_checkout_state(missing=["city"])
        fallback, kind = select_arabic_commerce_fallback(
            inbound_text="مكة",
            state=state,
        )
        assert kind == "checkout_slot_prompt"
        assert fallback != _BROKEN_GENERIC
        assert "المدينة" in fallback

    def test_missing_address_prompt(self) -> None:
        state = _catalog_checkout_state(missing=["short_address_code"])
        fallback, kind = select_arabic_commerce_fallback(
            inbound_text="",
            state=state,
        )
        assert kind == "checkout_slot_prompt"
        assert "العنوان" in fallback or "خرائط" in fallback


class TestContinueWordsInCheckout:
    @pytest.mark.parametrize("word", ["كمل", "تابع"])
    def test_continue_words_detected(self, word: str) -> None:
        assert is_checkout_continue_inbound(word)

    @pytest.mark.parametrize("word", ["كمل", "تابع"])
    def test_continue_words_get_slot_fallback_not_generic(self, word: str) -> None:
        state = _catalog_checkout_state(missing=["city"])
        fallback, kind = select_arabic_commerce_fallback(
            inbound_text=word,
            state=state,
        )
        assert kind == "checkout_slot_prompt"
        assert _BROKEN_GENERIC not in fallback
        assert RECOVERY_TEXT_AR.split()[0] not in fallback

    def test_loop_guard_checkout_uses_slot_recovery_not_confused(self) -> None:
        state = _catalog_checkout_state(missing=["city"])
        slot = build_checkout_slot_fallback_reply(state=state, inbound_text="كمل")
        assert slot
        assert "المدينة" in slot
        assert "كرّرت" not in slot
