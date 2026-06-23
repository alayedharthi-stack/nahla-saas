"""P0 — catalog checkout address must not trigger active_order_quantity hijack."""
from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.config import MARKETING_EMOJI_POLICY_ENABLED  # noqa: E402
from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: E402
from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: E402
    message_has_bare_quantity_or_variant_signal,
    message_looks_like_address_delivery,
    resolve_active_order_quantity_reply,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    select_arabic_commerce_fallback,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402
from modules.ai.commerce_agent.contracts import AgentInputContext  # noqa: E402
from modules.ai.commerce_agent.policies.shipping_readiness import (  # noqa: E402
    evaluate_shipping_readiness,
)
from modules.ai.postprocess.marketing_emoji_policy import (  # noqa: E402
    apply_marketing_emoji_policy,
    build_marketing_emoji_context,
)
from services.address_resolution import extract_address_signals  # noqa: E402

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_LONG_ADDRESS = (
    "عنوان قريب: 5061 MDQA5061 محمد الكناني، 6684، حي بطحاء قريش، مكة 24352"
)
_MAPS_URL = "https://maps.app.goo.gl/abc123xyz"
_SHORT_CODE = "MDQA5061"


def _catalog_checkout_state(*, city: str = "") -> SimpleNamespace:
    item = {
        "product_id": "cat_123",
        "product_name": "عسل سدر",
        "quantity": 1,
        "retailer_id": "cat_123",
    }
    missing = ["customer_first_name", "short_address_code"]
    if not city:
        missing.append("city")
    prep = OrderPreparationState(
        line_items=[dict(item)],
        order_status="collecting_address",
        missing_fields=missing,
        city=city or "",
    )
    return SimpleNamespace(
        cart_items=[dict(item)],
        current_product_focus={"title": item["product_name"], "product_id": item["product_id"]},
        order_prep=prep,
    )


class TestAddressNotBareQuantity:
    def test_long_national_address_not_bare_quantity(self) -> None:
        assert message_looks_like_address_delivery(_LONG_ADDRESS)
        assert not message_has_bare_quantity_or_variant_signal(_LONG_ADDRESS)

    def test_short_address_code_not_bare_quantity(self) -> None:
        assert not message_has_bare_quantity_or_variant_signal(_SHORT_CODE)

    def test_maps_url_not_bare_quantity(self) -> None:
        assert not message_has_bare_quantity_or_variant_signal(_MAPS_URL)

    @pytest.mark.parametrize(
        "fragment",
        ["5061", "6684", "24352"],
    )
    def test_postal_fragments_in_address_context_not_bare_quantity(self, fragment: str) -> None:
        text = f"عنوان قريب: {fragment} MDQA5061 حي بطحاء قريش، مكة {fragment}"
        assert not message_has_bare_quantity_or_variant_signal(text)

    def test_real_half_kilo_still_detected(self) -> None:
        assert message_has_bare_quantity_or_variant_signal("نص كيلo")


class TestCatalogCheckoutAddressFlow:
    def test_city_then_address_saves_short_code_no_size_ask(self) -> None:
        state = _catalog_checkout_state()
        state.order_prep.city = "مكة المكرمة"
        state.order_prep.missing_fields = [
            f for f in state.order_prep.missing_fields if f != "city"
        ]

        signals = extract_address_signals(_LONG_ADDRESS)
        assert signals.get("short_address_code") == "MDQA5061"

        reply = resolve_active_order_quantity_reply(
            _LONG_ADDRESS,
            state=state,
            active_commerce=True,
        )
        assert reply is None

        fallback, reason = select_arabic_commerce_fallback(
            inbound_text=_LONG_ADDRESS,
            state=state,
        )
        assert reason != "active_order_quantity"
        assert fallback != "تمام، أكمل معك الطلب — وش الحجم أو التفاصيل اللي تبغاها؟"
        if fallback:
            assert "الحجم" not in fallback

    def test_catalog_sku_locked_skips_size_fallback_on_empty_intents(self) -> None:
        state = _catalog_checkout_state(city="مكة")
        state.order_prep.missing_fields = ["customer_first_name", "short_address_code"]
        reply = resolve_active_order_quantity_reply(
            "MDQA5061",
            state=state,
            active_commerce=True,
        )
        assert reply is None


class TestMarketingEmojiConfigAndCheckout:
    def test_marketing_emoji_policy_enabled_by_default(self) -> None:
        assert MARKETING_EMOJI_POLICY_ENABLED is True

    def test_order_slot_prompt_gets_checkout_emoji(self) -> None:
        body = "تمام، وش اسمك الكامل عشان نكمل الطلب؟"
        ctx = build_marketing_emoji_context(
            reply_instruction_path="order_slot_prompt",
            decision_action="propose_draft_order",
            policy_enabled=True,
            audit_only=False,
            inbound_text="مكة",
        )
        result = apply_marketing_emoji_policy(body, ctx)
        assert result.changed
        assert _EMOJI_RE.search(result.reply)
        assert result.reply != body

    def test_polished_body_is_final_outbound_text(self) -> None:
        body = "تمام، وش اسمك الكامل عشان نكمل الطلب؟"
        ctx = build_marketing_emoji_context(
            reply_instruction_path="order_slot_prompt",
            decision_action="propose_draft_order",
            policy_enabled=True,
            audit_only=False,
            inbound_text="مكة",
        )
        result = apply_marketing_emoji_policy(body, ctx)
        outbound = result.reply if result.changed else body
        assert outbound.startswith(body.rstrip())
        assert _EMOJI_RE.search(outbound)


class TestOperationalGuardsUnchanged:
    def test_missing_fields_never_include_phone(self) -> None:
        prep = {
            "line_items": [{"product_id": "p1", "quantity": 1}],
            "city": "مكة",
        }
        missing = compute_wa_missing_fields(
            prep,
            whatsapp_phone="+966501234567",
        )
        assert "phone" not in missing
        assert "recipient_phone" not in missing
        assert "customer_phone" not in missing

    def test_shipping_not_ready_without_name_city_and_address(self) -> None:
        prep = {"short_address_code": "MDQA5061"}
        verdict = evaluate_shipping_readiness(
            AgentInputContext(
                tenant_id=1,
                customer_phone="966501234567",
                message="",
                order_prep=prep,
            ),
        )
        assert not verdict.allowed

    def test_shipping_ready_with_name_city_and_short_code(self) -> None:
        prep = {
            "customer_first_name": "محمد",
            "customer_last_name": "الكناني",
            "city": "مكة",
            "short_address_code": "MDQA5061",
            "line_items": [{"product_id": "p1", "quantity": 1}],
        }
        verdict = evaluate_shipping_readiness(
            AgentInputContext(
                tenant_id=1,
                customer_phone="966501234567",
                message="",
                order_prep=prep,
                line_items=prep["line_items"],
            ),
        )
        assert verdict.allowed
