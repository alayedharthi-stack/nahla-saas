"""Brain-owned checkout shipping grounding regressions."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (ROOT, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from commerce_scenario_fixtures import make_scenario_db, seed_knowledge_section, seed_tenant  # noqa: E402
from core.checkout_shipping_policy import resolve_checkout_shipping_policy  # noqa: E402
from modules.ai.brain.postprocess.shipping_cost_truth_guard import (  # noqa: E402
    apply_shipping_cost_truth_guard,
)


def _seed_shipping_kb(db, tenant_id: int, body: str) -> None:
    seed_knowledge_section(
        db,
        tenant_id,
        kind="shipping_zones",
        title="سياسة الشحن",
        body=body,
    )


@pytest.fixture
def db_tenant():
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    return db, tenant


class TestBrainCheckoutShippingGrounding:
    def test_brain_checkout_summary_does_not_invent_shipping_fee(self, db_tenant) -> None:
        db, tenant = db_tenant
        invented = "المجموع 394.50 ريال (شامل شحن توصيل 29 ريال)"
        result = apply_shipping_cost_truth_guard(
            invented,
            db=db,
            tenant_id=tenant.id,
            order_prep={"line_items": [{"product_name": "حذاء رياضي أبيض", "quantity": 1}]},
        )
        assert result.replaced
        assert "29" not in result.reply

    def test_brain_checkout_summary_respects_free_shipping_policy(self, db_tenant) -> None:
        db, tenant = db_tenant
        _seed_shipping_kb(
            db,
            tenant.id,
            "food: شحن مجاني.\nclothing: شحن توصيل 29 ريال.",
        )
        resolution = resolve_checkout_shipping_policy(
            db,
            tenant_id=tenant.id,
            order_prep={
                "line_items": [{"product_name": "عصير برتقال طازج", "quantity": 1}],
            },
        )
        assert resolution.free_shipping is True
        patch = resolution.to_state_patch()
        assert patch.get("free_shipping") is True

    def test_brain_checkout_summary_uses_configured_paid_shipping_fee(self, db_tenant) -> None:
        db, tenant = db_tenant
        _seed_shipping_kb(
            db,
            tenant.id,
            "clothing: شحن توصيل 35 ريال.",
        )
        resolution = resolve_checkout_shipping_policy(
            db,
            tenant_id=tenant.id,
            order_prep={
                "line_items": [{"product_name": "قميص قطني أزرق", "quantity": 1}],
            },
        )
        assert resolution.shipping_fee_sar == 35.0
        assert resolution.free_shipping is False

    def test_brain_checkout_summary_handles_mixed_cart_deterministically(self, db_tenant) -> None:
        db, tenant = db_tenant
        _seed_shipping_kb(
            db,
            tenant.id,
            "food: شحن مجاني.\naccessories: شحن توصيل 29 ريال.",
        )
        resolution = resolve_checkout_shipping_policy(
            db,
            tenant_id=tenant.id,
            order_prep={
                "line_items": [
                    {"product_name": "عطر ورد 100ml", "quantity": 1},
                    {"product_name": "مشروب طاقة", "quantity": 1},
                ],
            },
        )
        assert resolution.merchant_review_required is True

    def test_brain_checkout_summary_unknown_shipping_policy_does_not_invent_fee(self, db_tenant) -> None:
        db, tenant = db_tenant
        result = apply_shipping_cost_truth_guard(
            "شحن توصيل 29 ريال",
            db=db,
            tenant_id=tenant.id,
            order_prep={"line_items": [{"product_name": "منتج عام", "quantity": 1}]},
        )
        assert result.replaced
        assert "29" not in result.reply
