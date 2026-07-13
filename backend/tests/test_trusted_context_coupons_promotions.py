"""Trusted Context coupon/promotion shadow loader tests."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
)
from modules.ai.brain.truth_surface.coupon_offer_loader import (  # noqa: E402
    build_coupon_eligibility_record,
    build_promotion_eligibility_record,
    load_coupon_promotion_facts,
    mask_coupon_code,
    should_load_coupon_promotion_facts,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    build_trusted_context_snapshot,
    run_trusted_context_shadow,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coupon(**kwargs):
    defaults = dict(
        id=1,
        tenant_id=1,
        code="SAVE10",
        source_type="manual",
        expires_at=_now() + timedelta(days=7),
        extra_metadata={},
        rules=[],
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _promo(**kwargs):
    defaults = dict(
        id=10,
        tenant_id=1,
        status="active",
        promotion_type="percentage",
        discount_value=10,
        conditions={},
        starts_at=_now() - timedelta(days=1),
        ends_at=_now() + timedelta(days=7),
        usage_count=0,
        usage_limit=None,
        extra_metadata={},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _db_with_coupons_promotions(coupons, promotions):
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Coupon":
            q.filter.return_value.limit.return_value.all.return_value = coupons
        elif name == "Promotion":
            q.filter.return_value.limit.return_value.all.return_value = promotions
        elif name == "CustomerProfile":
            q.filter.return_value.first.return_value = None
        return q

    db.query.side_effect = _query
    return db


def test_coupon_active_eligible() -> None:
    record = build_coupon_eligibility_record(
        _coupon(),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is True
    assert record["verified"] is True
    assert record["available_state"] is True


def test_coupon_expired() -> None:
    record = build_coupon_eligibility_record(
        _coupon(expires_at=_now() - timedelta(hours=1)),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "expired"


def test_coupon_disabled() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"active": False}),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "disabled"


def test_coupon_usage_limit_reached() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"usage_count": 5, "usage_limit": 5}),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["usage_limit_reached"] is True
    assert record["reason_when_unavailable"] == "usage_limit_reached"


def test_coupon_personal_for_other_customer() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"customer_id": 99}),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["personalised"] is True
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "customer_restriction"


def test_coupon_minimum_basket_not_met() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"min_order_amount": 300}),
        tenant_id=1,
        customer_id=42,
        basket_total=199.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "minimum_basket_not_met"


def test_coupon_minimum_basket_cannot_be_evaluated() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"min_order_amount": 300}),
        tenant_id=1,
        customer_id=42,
        basket_total=None,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is None
    assert record["verified"] is False
    assert record["reason_when_unavailable"] == "minimum_basket_unverified"


def test_coupon_product_restriction_not_met() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"product_ids": ["shoe-1"]}),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
        line_product_ids={"shirt-9"},
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "product_restriction_not_met"


def test_coupon_product_category_advisory_not_falsely_verified() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"applicable_categories": [3, 7]}),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is None
    assert record["verified"] is False
    assert record["product_category_eligibility_state"] == "advisory"


def test_coupon_applied_vs_available() -> None:
    available = build_coupon_eligibility_record(
        _coupon(code="OPEN20"),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    applied = build_coupon_eligibility_record(
        _coupon(code="OPEN20"),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes={"OPEN20"},
        observed_at=_now().isoformat(),
    )
    assert available["available_state"] is True
    assert applied["applied_state"] is True
    assert applied["eligible"] is False
    assert applied["reason_when_unavailable"] == "already_applied"


def test_coupon_missing_data_unavailable_fact() -> None:
    db = _db_with_coupons_promotions([], [])
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader._resolve_customer_id",
        return_value=42,
    ):
        facts, obs = load_coupon_promotion_facts(
            db=db,
            tenant_id=1,
            customer_phone="966500000001",
        )
    unavailable = next(f for f in facts if f.key == "coupon:unavailable")
    assert unavailable.value["reason_when_unavailable"] == "no_coupon_data"
    assert obs["coupon_count"] == 0


def test_coupon_cross_tenant_isolation() -> None:
    record = build_coupon_eligibility_record(
        _coupon(tenant_id=2),
        tenant_id=1,
        customer_id=42,
        basket_total=250.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "tenant_mismatch"


def test_promotion_active_eligible() -> None:
    record = build_promotion_eligibility_record(
        _promo(),
        tenant_id=1,
        customer_profile=None,
        basket_total=200.0,
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is True
    assert record["active_window_result"] == "active"


def test_promotion_outside_window() -> None:
    record = build_promotion_eligibility_record(
        _promo(ends_at=_now() - timedelta(hours=2)),
        tenant_id=1,
        customer_profile=None,
        basket_total=200.0,
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "outside_active_window"


def test_promotion_usage_limit_reached() -> None:
    record = build_promotion_eligibility_record(
        _promo(usage_count=10, usage_limit=10),
        tenant_id=1,
        customer_profile=None,
        basket_total=200.0,
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["usage_available"] is False


def test_promotion_segment_mismatch() -> None:
    profile = SimpleNamespace(segment="new")
    record = build_promotion_eligibility_record(
        _promo(conditions={"customer_segments": ["vip"]}),
        tenant_id=1,
        customer_profile=profile,
        basket_total=200.0,
        observed_at=_now().isoformat(),
    )
    assert record["segment_result"] == "fail"
    assert record["eligible"] is False


def test_promotion_minimum_order_not_met() -> None:
    record = build_promotion_eligibility_record(
        _promo(conditions={"min_order_amount": 500}),
        tenant_id=1,
        customer_profile=None,
        basket_total=120.0,
        observed_at=_now().isoformat(),
    )
    assert record["basket_result"] == "fail"
    assert record["eligible"] is False


def test_promotion_missing_cart_total_does_not_pass_minimum() -> None:
    record = build_promotion_eligibility_record(
        _promo(conditions={"min_order_amount": 100}),
        tenant_id=1,
        customer_profile=None,
        basket_total=None,
        observed_at=_now().isoformat(),
    )
    assert record["basket_result"] == "unknown"
    assert record["eligible"] is None
    assert record["verified"] is False


def test_promotion_product_category_advisory_not_verified() -> None:
    record = build_promotion_eligibility_record(
        _promo(conditions={"applicable_products": ["sku-1"], "applicable_categories": [2]}),
        tenant_id=1,
        customer_profile=None,
        basket_total=200.0,
        observed_at=_now().isoformat(),
    )
    assert record["product_result"] == "advisory"
    assert record["category_result"] == "advisory"
    assert record["eligible"] is None
    assert record["verified"] is False


def test_promotion_buy_x_get_y_unknown_eligibility() -> None:
    record = build_promotion_eligibility_record(
        _promo(
            promotion_type="buy_x_get_y",
            conditions={"x_quantity": 2, "y_quantity": 1},
        ),
        tenant_id=1,
        customer_profile=None,
        basket_total=200.0,
        observed_at=_now().isoformat(),
    )
    assert record["buy_x_get_y_result"] == "unknown"
    assert record["eligible"] is None


def test_promotion_multiple_active_no_fabricated_winner() -> None:
    promos = [_promo(id=1), _promo(id=2)]
    db = _db_with_coupons_promotions([], promos)
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader._resolve_customer_id",
        return_value=42,
    ):
        facts, _obs = load_coupon_promotion_facts(
            db=db,
            tenant_id=1,
            customer_phone="966500000001",
        )
    promo_facts = [f for f in facts if f.domain == TrustedDomain.PROMOTIONS]
    assert len(promo_facts) == 2
    assert all(f.value.get("conflict_state") == "multiple_active_unresolved" for f in promo_facts)
    assert all(f.value.get("eligible") is None for f in promo_facts)


def test_promotion_cross_tenant_isolation() -> None:
    record = build_promotion_eligibility_record(
        _promo(tenant_id=9),
        tenant_id=1,
        customer_profile=None,
        basket_total=200.0,
        observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "tenant_mismatch"


def test_lazy_loader_runs_for_coupon_question() -> None:
    assert should_load_coupon_promotion_facts(message="عندكم كوبون خصم؟") is True
    assert should_load_coupon_promotion_facts(message="any promotion today?") is True


def test_lazy_loader_runs_for_arabic_plural_offers() -> None:
    assert should_load_coupon_promotion_facts(message="في عروض حالياً؟") is True


def test_lazy_loader_skips_unrelated_social_turn() -> None:
    assert should_load_coupon_promotion_facts(message="السلام عليكم كيف حالك") is False


def test_facts_enter_snapshot_and_projection() -> None:
    coupon = _coupon(id=5, code="GEN15")
    db = _db_with_coupons_promotions([coupon], [])
    with patch(
        "modules.ai.brain.truth_surface.trusted_context._load_customer_order_facts",
        return_value=[],
    ), patch(
        "core.active_order_context.load_commerce_bundle_from_db",
        return_value={},
    ), patch(
        "modules.ai.commerce_agent.capability_resolver.resolve_tenant_capabilities",
        return_value=SimpleNamespace(
            whatsapp_order=True,
            online_store=True,
            pickup=False,
            native_catalog=True,
            showroom_enabled=False,
            cod_enabled=True,
            store_url="https://example.test",
            available_tools=(),
        ),
    ), patch(
        "modules.ai.brain.facts.commerce_facts.DefaultFactsLoader.load",
        return_value=SimpleNamespace(store_name="متجر", has_coupons=True),
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader._resolve_customer_id",
        return_value=1,
    ):
        snapshot = build_trusted_context_snapshot(
            db=db,
            tenant_id=1,
            customer_phone="966500000010",
            message="هل يوجد كوبون؟",
        )
    assert TrustedDomain.COUPONS.value in snapshot.loaded_domains
    projection = snapshot.projection(domains=[TrustedDomain.COUPONS])
    assert "coupons" in projection["facts"]
    assert "coupon:5" in projection["facts"]["coupons"]


def test_shadow_observability_masks_coupon_codes() -> None:
    masked = mask_coupon_code("SECRET99")
    assert "SECRET99" not in masked
    assert masked.startswith("***")
    record = build_coupon_eligibility_record(
        _coupon(code="SECRET99"),
        tenant_id=1,
        customer_id=1,
        basket_total=100.0,
        applied_codes=set(),
        observed_at=_now().isoformat(),
    )
    assert record["code_masked"] == masked
    assert "code" not in record


def test_no_decision_plan_action_changes() -> None:
    import inspect

    from modules.ai.commerce_agent import decision_plan as dp_module

    source = inspect.getsource(dp_module)
    assert "coupon_offer_loader" not in source
    assert "COUPONS" not in source
    assert "apply_coupon" not in source


def test_no_customer_visible_response_changes() -> None:
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=TrustedContextSnapshot(
            tenant_id=1,
            customer_phone="966500000011",
            facts=[],
            loaded_domains=[TrustedDomain.COUPONS.value],
            sources=["coupon_offer_loader"],
        ),
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ):
        snapshot = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000011",
            message="كوبون",
        )
    assert snapshot is not None
    assert snapshot.facts == []


def test_loader_read_only_no_mutations() -> None:
    coupon = _coupon()
    promo = _promo()
    db = _db_with_coupons_promotions([coupon], [promo])
    before_coupon_meta = dict(coupon.extra_metadata)
    before_promo_count = promo.usage_count
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader._resolve_customer_id",
        return_value=1,
    ):
        load_coupon_promotion_facts(
            db=db,
            tenant_id=1,
            customer_phone="966500000012",
            message="كوبون",
        )
    assert coupon.extra_metadata == before_coupon_meta
    assert promo.usage_count == before_promo_count
    db.commit.assert_not_called()


def test_disabling_shadow_flag_skips_loader() -> None:
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=False,
    ):
        result = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000013",
            message="كوبون",
        )
    assert result is None


def test_constitution_compliance_stays_green() -> None:
    import subprocess

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/test_constitution_compliance.py",
            "-q",
            "--tb=no",
        ],
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
