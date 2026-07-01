"""Active checkout slot ownership regressions."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (ROOT, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.slot_ownership import apply_slot_ownership  # noqa: E402
from modules.ai.order_flow_v2.triggers import is_catalog_selection_acknowledgment  # noqa: E402
from modules.ai.order_flow_v2.replies import build_next_field_reply  # noqa: E402


@pytest.fixture(autouse=True)
def _v2_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "true")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)


class TestActiveCheckoutSlotOwnership:
    def test_brain_replay_consumes_name_like_text_as_name_during_checkout(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "catalog_line_items_authoritative": True,
            "line_items": [{"product_name": "حذاء رياضي أبيض", "quantity": 1}],
        }
        patch, reason = apply_slot_ownership(
            message="نورة عبدالله",
            order_prep=prep,
            missing_fields=["customer_name", "city", "delivery_address", "payment_method"],
        )
        assert reason == "customer_name_owned"
        assert patch.get("customer_first_name") == "نورة"
        assert patch.get("customer_last_name") == "عبدالله"

    def test_name_like_text_during_checkout_does_not_route_to_product_keyword(self) -> None:
        from modules.ai.order_flow_v2.triggers import is_short_product_keyword_in_order_flow  # noqa: PLC0415

        assert is_short_product_keyword_in_order_flow("نورة عبدالله") is False

    def test_customer_says_i_selected_products_preserves_checkout_state(self) -> None:
        assert is_catalog_selection_acknowledgment("انا اخترت المنتجات") is True
        prep = {
            "order_flow_v2_active": True,
            "catalog_line_items_authoritative": True,
            "line_items": [{"product_name": "قميص قطني أزرق", "quantity": 1}],
        }
        with patch("modules.ai.order_flow_v2.owner._load_brain_state") as _load:
            _load.return_value = (None, {"order_prep": prep})
            result = try_handle_order_flow_v2(
                MagicMock(),
                tenant_id=1,
                customer_phone="966500000001",
                message="انا اخترت المنتجات",
            )
        assert result.handled
        assert result.reason == "catalog_selection_acknowledged"
        assert "اختياراتك" in result.reply

    def test_after_valid_address_delivery_method_defaults_to_delivery_or_confirms_delivery(self) -> None:
        prep = {
            "customer_first_name": "أحمد",
            "customer_last_name": "سالم",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
            "delivery_address_status": "accepted",
            "line_items": [{"product_name": "عطر ورد 100ml", "quantity": 1}],
        }
        reply = build_next_field_reply(
            order_prep=prep,
            brain_state={},
            missing_fields=["payment_method"],
        )
        assert "أعتمد التوصيل" in reply
        assert "استلام من المتجر" not in reply
