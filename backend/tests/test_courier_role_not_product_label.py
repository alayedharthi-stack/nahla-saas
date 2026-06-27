"""Regression: courier/logistics role phrases must never become product labels."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.customer_name_adoption_guard import can_ai_update_customer_name  # noqa: E402
from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: E402
    is_negative_logistics_or_contact_context,
    is_non_product_label,
    sanitize_product_label,
)
from modules.ai.brain.discovery.entry import resolve_discovery_entry  # noqa: E402
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    inbound_exempt_from_availability_rewrite,
    should_block_availability_rewrite,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _label_from_inbound_availability_ask,
    apply_product_availability_truth_guard,
    build_operational_availability_conflict_reply,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    has_explicit_product_browse_intent,
    product_discovery_block_reason,
)
from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState  # noqa: E402


def _ctx(message: str, *, intent: str = "general") -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        raw_message=message,
        intent=Intent(name=intent, confidence=0.8, raw_message=message),
        state=MerchantConversationState(stage="discovery", greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True, product_count=8, in_stock_count=8),
        profile={"inbound_metadata": {}},
    )


COURIER_MESSAGES = (
    "معك مندوب سمسا",
    "I am SMSA courier",
    "أنا مندوب SMSA",
    "مندوب الشحن معك",
)


class TestCourierRoleNotProduct:
    @pytest.mark.parametrize("message", COURIER_MESSAGES)
    def test_courier_role_not_product_label(self, message: str) -> None:
        assert is_negative_logistics_or_contact_context(message) is True
        assert is_non_product_label(message) is True
        assert _label_from_inbound_availability_ask(message) == ""
        assert sanitize_product_label(message) == ""

        ctx = _ctx(message)
        assert has_explicit_product_browse_intent(ctx) is False
        assert product_discovery_block_reason(ctx) == "logistics_context"
        assert resolve_discovery_entry(ctx).matched is False
        assert inbound_exempt_from_availability_rewrite(message) is True

        ev = MagicMock()
        ev.entity.product_id = None
        ev.entity.family_key = "inbound:unknown"
        reply = build_operational_availability_conflict_reply(
            ev,
            availability_context={"catalog_skus": [], "focus_product": {}},
            inbound_text=message,
        )
        assert message not in reply
        assert reply == "متوفر بعدة خيارات."

    @pytest.mark.parametrize("message", COURIER_MESSAGES)
    def test_availability_guard_does_not_rewrite_courier_turn(self, message: str) -> None:
        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            bad_reply = f"متوفر {message} بعدة خيارات. وش الكمية تبغى؟"
            ctx = {
                "catalog_skus": [],
                "focus_product": None,
                "recommended_product_ids": [],
                "kb_signals": [],
                "kb_links": [],
            }
            result = apply_product_availability_truth_guard(
                reply=bad_reply,
                availability_context=ctx,
                inbound_text=message,
                tenant_id=1,
            )
            assert result.replaced is False
            assert result.reply == bad_reply
            assert should_block_availability_rewrite(
                inbound_text=message,
                evidence_state="unknown",
                guard_action="rewrite_unknown",
            ) is True
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev


def test_forced_product_label_rejected_by_gate() -> None:
    label = "معك مندوب سمسا"
    assert sanitize_product_label(label) == ""
    assert is_non_product_label(label) is True


def test_explicit_product_availability_still_works() -> None:
    message = "هل عسل السمر متوفر؟"
    assert is_negative_logistics_or_contact_context(message) is False
    assert is_non_product_label(message) is False
    assert inbound_exempt_from_availability_rewrite(message) is False


def test_explicit_browse_still_works() -> None:
    message = "وش الأنواع المتوفرة؟"
    ctx = _ctx(message)
    assert is_negative_logistics_or_contact_context(message) is False
    assert has_explicit_product_browse_intent(ctx) is True
    assert product_discovery_block_reason(ctx) is None


def test_name_protection_unchanged_for_courier_intro() -> None:
    customer = SimpleNamespace(
        name="Verified Customer",
        name_source="merchant_manual",
    )
    message = "معك مندوب سمسا"
    assert can_ai_update_customer_name(
        customer,
        "معك مندوب سمسا",
        {"message": message, "source": "whatsapp_inbound"},
    ) is False
    assert customer.name == "Verified Customer"
