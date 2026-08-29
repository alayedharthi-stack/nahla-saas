"""
tests/test_entitlements.py
──────────────────────────
Final validation suite for the Nahla Entitlements System.

Run:
    cd backend
    python -m pytest tests/test_entitlements.py -v
    # or directly:
    python tests/test_entitlements.py

Tests:
  1. Plan feature definitions (authoritative mapping)
  2. Starter — allowed vs blocked features
  3. Growth — allowed features
  4. Scale  — allowed features
  5. Unknown slug → "none" (safety rule)
  6. Billing state: failed/cancelled blocks all
  7. Billing state: active/trial allows plan features
  8. Downgrade scenario: Growth → Starter stops advanced automations
  9. API error shape: upgrade_required, limit_exceeded, billing_blocked
 10. Limits: Starter campaign cap, Growth/Scale unlimited
"""
from __future__ import annotations

import sys
import os

# Allow running from repo root or backend/
_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from core.plan_entitlements import (
    PLAN_DEFINITIONS,
    PlanEntitlements,
    PlanFeatures,
    PlanLimits,
    EntitlementError,
    get_entitlements,
    require_feature,
    require_limit_not_exceeded,
    _UNLIMITED,
    _FEATURE_MIN_PLAN,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ent(plan_slug: str, billing_status: str = "active") -> PlanEntitlements:
    """Build a PlanEntitlements directly from PLAN_DEFINITIONS (no DB)."""
    # blocked states
    if billing_status == "cancelled":
        effective = "none"
    elif billing_status == "failed":
        effective = "failed"
    else:
        effective = plan_slug if plan_slug in PLAN_DEFINITIONS else "none"

    plan_def   = PLAN_DEFINITIONS[effective]
    is_active  = billing_status in ("active", "trial")
    is_blocked = not is_active and billing_status not in ("none",)

    return PlanEntitlements(
        plan_slug      = effective,
        plan_name_ar   = plan_def.name_ar,
        billing_status = billing_status,
        is_active      = is_active,
        is_blocked     = is_blocked,
        features       = plan_def.features,
        limits         = plan_def.limits,
        raw_plan       = plan_def,
    )


def _should_allow(ent: PlanEntitlements, feature: str) -> bool:
    try:
        require_feature(ent, feature)
        return True
    except EntitlementError:
        return False


def _error_for(ent: PlanEntitlements, feature: str) -> EntitlementError | None:
    try:
        require_feature(ent, feature)
        return None
    except EntitlementError as e:
        return e


# ─────────────────────────────────────────────────────────────────────────────
# 1. Plan definitions integrity
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanDefinitions:

    def test_all_slugs_present(self):
        for slug in ("starter", "growth", "scale", "none", "failed"):
            assert slug in PLAN_DEFINITIONS, f"Missing plan slug: {slug}"

    def test_feature_keys_consistent(self):
        """All plans must define the same feature keys."""
        real_plans = [PLAN_DEFINITIONS[s] for s in ("starter", "growth", "scale")]
        keys = set(PlanFeatures.__dataclass_fields__.keys())
        for plan in real_plans:
            actual = set(plan.features.__dataclass_fields__.keys())
            assert actual == keys, f"Plan {plan.slug} missing keys: {keys - actual}"

    def test_none_plan_has_no_features(self):
        none_plan = PLAN_DEFINITIONS["none"]
        for k in PlanFeatures.__dataclass_fields__:
            assert getattr(none_plan.features, k) is False, \
                f"none plan should have {k}=False"

    def test_failed_plan_has_no_features(self):
        failed_plan = PLAN_DEFINITIONS["failed"]
        for k in PlanFeatures.__dataclass_fields__:
            assert getattr(failed_plan.features, k) is False, \
                f"failed plan should have {k}=False"

    def test_scale_has_all_features(self):
        """Scale plan must have every single feature enabled."""
        scale = PLAN_DEFINITIONS["scale"]
        for k in PlanFeatures.__dataclass_fields__:
            assert getattr(scale.features, k) is True, \
                f"Scale plan should have {k}=True"

    def test_growth_is_superset_of_starter(self):
        """Every feature in Starter must also be in Growth."""
        starter = PLAN_DEFINITIONS["starter"].features
        growth  = PLAN_DEFINITIONS["growth"].features
        for k in PlanFeatures.__dataclass_fields__:
            if getattr(starter, k):
                assert getattr(growth, k), \
                    f"Growth should include Starter feature: {k}"

    def test_scale_is_superset_of_growth(self):
        """Every feature in Growth must also be in Scale."""
        growth = PLAN_DEFINITIONS["growth"].features
        scale  = PLAN_DEFINITIONS["scale"].features
        for k in PlanFeatures.__dataclass_fields__:
            if getattr(growth, k):
                assert getattr(scale, k), \
                    f"Scale should include Growth feature: {k}"

    def test_feature_min_plan_covers_all_features(self):
        """Every feature that any plan enables must appear in _FEATURE_MIN_PLAN."""
        all_feature_keys = set(PlanFeatures.__dataclass_fields__.keys())
        for slug in ("starter", "growth", "scale"):
            plan = PLAN_DEFINITIONS[slug]
            for k in all_feature_keys:
                if getattr(plan.features, k):
                    assert k in _FEATURE_MIN_PLAN, \
                        f"Feature {k} enabled in {slug} but missing from _FEATURE_MIN_PLAN"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Starter — authoritative allowed / blocked list
# ─────────────────────────────────────────────────────────────────────────────

class TestStarterPlan:

    ALLOWED = [
        "nahla_template_library",
        "meta_template_sync",
        "autopilot_order_confirmation",
        "autopilot_order_notifications",
        "autopilot_shipping_tracking",
        "cart_recovery_stage_2",
        "abandoned_cart_basic_coupon",
        "campaign_customer_segments",
    ]

    BLOCKED = [
        "cart_recovery_stage_3",
        "cart_recovery_advanced_coupon",
        "advanced_coupon_types",
        "autopilot_full",
        "autopilot_customer_recovery",
        "autopilot_cod_confirmation",
        "predictive_reorder",
        "vip_rewards",
        "back_in_stock_alerts",
        "new_products_alerts",
        "seasonal_smart_offers",
        "salary_offers",
        "seasonal_calendar",
        "smart_discount_popup",
        "meta_catalog_sync",
        "campaign_ai_optimization",
        "ai_performance_dashboard",
        "conversion_funnel",
        "advanced_ai_analytics",
        "revenue_breakdown",
        "top_products_analytics",
        "order_sources_analytics",
        "store_brain_advanced",
        "full_ai_customization",
        "advanced_discount_rules",
        "escalation_rules",
        "team_handoff_queue",
        "zid_integration",
        "future_integrations",
    ]

    def test_starter_allowed_features(self):
        ent = _make_ent("starter", "active")
        for feature in self.ALLOWED:
            assert _should_allow(ent, feature), \
                f"Starter should be ALLOWED: {feature}"

    def test_starter_blocked_features(self):
        ent = _make_ent("starter", "active")
        for feature in self.BLOCKED:
            assert not _should_allow(ent, feature), \
                f"Starter should be BLOCKED: {feature}"

    def test_starter_trial_same_as_active(self):
        ent = _make_ent("starter", "trial")
        for feature in self.ALLOWED:
            assert _should_allow(ent, feature), \
                f"Starter trial should be ALLOWED: {feature}"
        for feature in self.BLOCKED:
            assert not _should_allow(ent, feature), \
                f"Starter trial should be BLOCKED: {feature}"

    def test_starter_campaign_limit(self):
        ent = _make_ent("starter", "active")
        assert ent.get_limit("campaigns_per_month") == 5

    def test_starter_conversations_limit(self):
        ent = _make_ent("starter", "active")
        assert ent.get_limit("monthly_conversations") == 2_000

    def test_starter_campaign_limit_at_cap(self):
        ent = _make_ent("starter", "active")
        # At cap (5): should raise
        with pytest.raises(EntitlementError) as exc_info:
            require_limit_not_exceeded(ent, "campaigns_per_month", current=5)
        assert exc_info.value.error_code == "limit_exceeded"
        assert exc_info.value.required_plan == "growth"

    def test_starter_campaign_limit_below_cap(self):
        ent = _make_ent("starter", "active")
        # Below cap (4): should pass
        require_limit_not_exceeded(ent, "campaigns_per_month", current=4)

    def test_starter_blocked_returns_upgrade_required(self):
        ent = _make_ent("starter", "active")
        err = _error_for(ent, "meta_catalog_sync")
        assert err is not None
        assert err.error_code == "upgrade_required"
        assert err.feature_key == "meta_catalog_sync"
        assert err.required_plan == "growth"
        assert "النمو" in err.message_ar

    def test_starter_blocked_returns_upgrade_for_scale_feature(self):
        ent = _make_ent("starter", "active")
        err = _error_for(ent, "store_brain_advanced")
        assert err is not None
        assert err.required_plan == "scale"
        assert "التوسع" in err.message_ar


# ─────────────────────────────────────────────────────────────────────────────
# 3. Growth plan
# ─────────────────────────────────────────────────────────────────────────────

class TestGrowthPlan:

    GROWTH_ONLY = [
        "cart_recovery_stage_3",
        "cart_recovery_advanced_coupon",
        "advanced_coupon_types",
        "autopilot_full",
        "autopilot_customer_recovery",
        "autopilot_cod_confirmation",
        "predictive_reorder",
        "vip_rewards",
        "back_in_stock_alerts",
        "new_products_alerts",
        "seasonal_smart_offers",
        "salary_offers",
        "seasonal_calendar",
        "smart_discount_popup",
        "meta_catalog_sync",
        "campaign_ai_optimization",
        "ai_performance_dashboard",
        "conversion_funnel",
    ]

    SCALE_LOCKED_FOR_GROWTH = [
        "store_brain_advanced",
        "full_ai_customization",
        "advanced_discount_rules",
        "escalation_rules",
        "team_handoff_queue",
        "zid_integration",
        "future_integrations",
        "advanced_ai_analytics",
        "revenue_breakdown",
        "top_products_analytics",
        "order_sources_analytics",
    ]

    def test_growth_allows_all_starter_features(self):
        ent = _make_ent("growth", "active")
        for f in TestStarterPlan.ALLOWED:
            assert _should_allow(ent, f), f"Growth should allow Starter feature: {f}"

    def test_growth_allows_growth_only_features(self):
        ent = _make_ent("growth", "active")
        for f in self.GROWTH_ONLY:
            assert _should_allow(ent, f), f"Growth should be ALLOWED: {f}"

    def test_growth_blocks_scale_features(self):
        ent = _make_ent("growth", "active")
        for f in self.SCALE_LOCKED_FOR_GROWTH:
            assert not _should_allow(ent, f), f"Growth should be BLOCKED from Scale feature: {f}"

    def test_growth_conversations_limit(self):
        ent = _make_ent("growth", "active")
        assert ent.get_limit("monthly_conversations") == 10_000

    def test_growth_campaigns_unlimited(self):
        ent = _make_ent("growth", "active")
        assert ent.get_limit("campaigns_per_month") >= _UNLIMITED


# ─────────────────────────────────────────────────────────────────────────────
# 4. Scale plan
# ─────────────────────────────────────────────────────────────────────────────

class TestScalePlan:

    def test_scale_allows_all_features(self):
        ent = _make_ent("scale", "active")
        for k in PlanFeatures.__dataclass_fields__:
            assert _should_allow(ent, k), f"Scale should allow ALL features. Blocked: {k}"

    def test_scale_conversations_unlimited(self):
        ent = _make_ent("scale", "active")
        assert ent.get_limit("monthly_conversations") >= _UNLIMITED

    def test_scale_campaigns_unlimited(self):
        ent = _make_ent("scale", "active")
        assert ent.get_limit("campaigns_per_month") >= _UNLIMITED

    def test_scale_limit_never_exceeded(self):
        ent = _make_ent("scale", "active")
        # Even at 1 million — should never raise
        require_limit_not_exceeded(ent, "campaigns_per_month", current=1_000_000)
        require_limit_not_exceeded(ent, "monthly_conversations", current=1_000_000)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Unknown / missing plan slug
# ─────────────────────────────────────────────────────────────────────────────

class TestUnknownPlan:

    UNKNOWN_SLUGS = ["enterprise", "pro", "gold", "premium", "", "STARTER", "GROWTH"]

    def test_unknown_slug_resolves_to_none(self):
        for slug in self.UNKNOWN_SLUGS:
            ent = _make_ent(slug, "active")
            assert ent.plan_slug == "none", \
                f"Unknown slug '{slug}' should resolve to 'none', got '{ent.plan_slug}'"

    def test_unknown_slug_blocks_all_features(self):
        for slug in self.UNKNOWN_SLUGS:
            ent = _make_ent(slug, "active")
            for k in PlanFeatures.__dataclass_fields__:
                assert not ent.has_feature(k), \
                    f"Unknown slug '{slug}' should have no feature {k}"

    def test_unknown_slug_returns_upgrade_required(self):
        # Unknown slug → resolves to "none" plan.
        # billing_status="active" but no recognised plan → feature locked.
        # Expected: upgrade_required (subscribe to Starter), NOT no_active_subscription.
        ent = _make_ent("enterprise", "active")
        err = _error_for(ent, "campaign_customer_segments")
        assert err is not None
        # The merchant has billing "active" but no valid plan mapping →
        # they need to select a plan (upgrade_required with required_plan=starter).
        assert err.error_code == "upgrade_required"
        assert err.required_plan == "starter"


# ─────────────────────────────────────────────────────────────────────────────
# 6 + 7. Billing state tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBillingStates:

    BASIC_FEATURE = "nahla_template_library"    # Starter+
    GROWTH_FEATURE = "meta_catalog_sync"         # Growth+
    SCALE_FEATURE  = "store_brain_advanced"      # Scale+

    def test_active_starter_allows_basic(self):
        ent = _make_ent("starter", "active")
        assert _should_allow(ent, self.BASIC_FEATURE)

    def test_trial_starter_allows_basic(self):
        ent = _make_ent("starter", "trial")
        assert _should_allow(ent, self.BASIC_FEATURE)

    def test_trial_growth_allows_growth_features(self):
        ent = _make_ent("growth", "trial")
        assert _should_allow(ent, self.GROWTH_FEATURE)
        assert _should_allow(ent, "cart_recovery_stage_3")

    def test_failed_billing_blocks_everything(self):
        for slug in ("starter", "growth", "scale"):
            ent = _make_ent(slug, "failed")
            assert ent.is_blocked, f"failed billing for {slug} should be blocked"
            err = _error_for(ent, self.BASIC_FEATURE)
            assert err is not None
            assert err.error_code == "billing_blocked", \
                f"Expected billing_blocked for {slug}/failed"
            assert "تعليق" in err.message_ar or "الدفع" in err.message_ar

    def test_cancelled_billing_blocks_everything(self):
        for slug in ("starter", "growth", "scale"):
            ent = _make_ent(slug, "cancelled")
            # cancelled → resolved to "none" → no_active_subscription
            err = _error_for(ent, self.BASIC_FEATURE)
            assert err is not None
            assert err.error_code in ("no_active_subscription", "billing_blocked"), \
                f"Cancelled should block. Got: {err.error_code}"

    def test_none_billing_returns_no_active_subscription(self):
        ent = _make_ent("starter", "none")
        err = _error_for(ent, self.BASIC_FEATURE)
        assert err is not None
        assert err.error_code == "no_active_subscription"

    def test_active_is_true_for_active(self):
        ent = _make_ent("starter", "active")
        assert ent.is_active is True
        assert ent.is_blocked is False

    def test_active_is_true_for_trial(self):
        ent = _make_ent("growth", "trial")
        assert ent.is_active is True
        assert ent.is_blocked is False

    def test_is_blocked_true_for_failed(self):
        ent = _make_ent("growth", "failed")
        assert ent.is_blocked is True
        assert ent.is_active is False


# ─────────────────────────────────────────────────────────────────────────────
# 8. Downgrade scenario: Growth → Starter
# ─────────────────────────────────────────────────────────────────────────────

class TestDowngrade:
    """
    When a merchant downgrades from Growth to Starter, any Growth-only
    feature must be denied at enforcement time — not just hidden in UI.
    """

    GROWTH_ONLY_AUTOMATION_FEATURES = [
        "cart_recovery_stage_3",
        "autopilot_customer_recovery",
        "predictive_reorder",
        "vip_rewards",
        "seasonal_smart_offers",
        "salary_offers",
        "smart_discount_popup",
        "meta_catalog_sync",
    ]

    def test_downgraded_to_starter_blocks_growth_automations(self):
        # Simulate: was Growth, now billing resolves to Starter
        ent = _make_ent("starter", "active")
        for feature in self.GROWTH_ONLY_AUTOMATION_FEATURES:
            assert not _should_allow(ent, feature), \
                f"Downgraded Starter should block Growth feature: {feature}"

    def test_downgraded_error_code_is_upgrade_required(self):
        ent = _make_ent("starter", "active")
        for feature in self.GROWTH_ONLY_AUTOMATION_FEATURES:
            err = _error_for(ent, feature)
            assert err is not None
            assert err.error_code == "upgrade_required", \
                f"Expected upgrade_required for {feature}, got {err.error_code}"
            assert err.required_plan == "growth", \
                f"Expected required_plan=growth for {feature}, got {err.required_plan}"

    def test_downgrade_does_not_affect_starter_features(self):
        ent = _make_ent("starter", "active")
        for feature in TestStarterPlan.ALLOWED:
            assert _should_allow(ent, feature), \
                f"Downgraded Starter should still allow: {feature}"


# ─────────────────────────────────────────────────────────────────────────────
# 9. API error shape
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorShape:

    def test_upgrade_required_shape(self):
        ent = _make_ent("starter", "active")
        err = _error_for(ent, "meta_catalog_sync")
        assert err is not None
        d = err.to_dict()
        assert d["error"]         == "upgrade_required"
        assert d["feature"]       == "meta_catalog_sync"
        assert d["required_plan"] == "growth"
        assert isinstance(d["message"], str) and len(d["message"]) > 10

    def test_limit_exceeded_shape(self):
        ent = _make_ent("starter", "active")
        try:
            require_limit_not_exceeded(ent, "campaigns_per_month", current=10)
            assert False, "Should have raised"
        except EntitlementError as e:
            d = e.to_dict()
            assert d["error"]         == "limit_exceeded"
            assert d["feature"]       == "campaigns_per_month"
            assert d["required_plan"] == "growth"
            assert "5" in d["message"]   # limit value appears in message

    def test_billing_blocked_shape(self):
        ent = _make_ent("growth", "failed")
        err = _error_for(ent, "meta_catalog_sync")
        assert err is not None
        d = err.to_dict()
        assert d["error"] == "billing_blocked"
        assert "الدفع" in d["message"] or "تعليق" in d["message"]

    def test_no_active_subscription_shape(self):
        ent = _make_ent("starter", "none")
        err = _error_for(ent, "campaign_customer_segments")
        assert err is not None
        d = err.to_dict()
        assert d["error"] == "no_active_subscription"

    def test_error_message_is_arabic(self):
        """All error messages must contain Arabic text."""
        cases = [
            (_make_ent("starter", "active"), "meta_catalog_sync"),
            (_make_ent("growth",  "failed"), "cart_recovery_stage_3"),
            (_make_ent("starter", "none"),   "campaign_customer_segments"),
        ]
        for ent, feature in cases:
            err = _error_for(ent, feature)
            assert err is not None
            # Arabic Unicode range: \u0600-\u06FF
            has_arabic = any('\u0600' <= c <= '\u06FF' for c in err.message_ar)
            assert has_arabic, \
                f"Error message not Arabic for {feature}/{ent.billing_status}: {err.message_ar!r}"

    def test_scale_feature_error_says_scale(self):
        ent = _make_ent("growth", "active")
        err = _error_for(ent, "store_brain_advanced")
        assert err is not None
        assert err.required_plan == "scale"
        assert "التوسع" in err.message_ar


# ─────────────────────────────────────────────────────────────────────────────
# 10. to_dict() / JSON serialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestToDictSerialisation:

    def test_to_dict_has_required_keys(self):
        ent = _make_ent("growth", "active")
        d = ent.to_dict()
        for key in ("plan", "plan_name_ar", "billing_status", "is_active",
                    "is_blocked", "features", "limits"):
            assert key in d, f"to_dict() missing key: {key}"

    def test_to_dict_unlimited_limit_is_none(self):
        ent = _make_ent("growth", "active")
        d = ent.to_dict()
        assert d["limits"]["campaigns_per_month"] is None

    def test_to_dict_starter_campaigns_limit_is_5(self):
        ent = _make_ent("starter", "active")
        d = ent.to_dict()
        assert d["limits"]["campaigns_per_month"] == 5

    def test_to_dict_all_features_are_bool(self):
        for slug in ("starter", "growth", "scale"):
            ent = _make_ent(slug, "active")
            d = ent.to_dict()
            for k, v in d["features"].items():
                assert isinstance(v, bool), \
                    f"feature {k} in {slug} should be bool, got {type(v)}"

    def test_to_dict_feature_count_matches_dataclass(self):
        ent = _make_ent("scale", "active")
        d = ent.to_dict()
        expected_count = len(PlanFeatures.__dataclass_fields__)
        assert len(d["features"]) == expected_count, \
            f"to_dict features count mismatch: {len(d['features'])} != {expected_count}"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (no pytest required)
# ─────────────────────────────────────────────────────────────────────────────

class TestEntitlementLookupFailures:
    """Catalog sync must not treat a lookup outage as plan=none. Default
    get_entitlements keeps the historical fallback for other consumers."""

    def _db_salla_lookup_down(self):
        from sqlalchemy.exc import OperationalError

        db = MagicMock()

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", str(model))
            if name == "Integration":
                q.filter.return_value.first.side_effect = OperationalError(
                    "SELECT integrations", {}, Exception("catalog entitlement store timeout")
                )
                return q
            q.filter.return_value.first.return_value = None
            return q

        db.query.side_effect = _query
        return db

    def test_default_lookup_still_falls_back_to_none(self):
        db = self._db_salla_lookup_down()
        with patch("core.billing.get_tenant_subscription", return_value=None), patch(
            "core.manual_billing_grant.is_manual_gift_grant_active",
            return_value=False,
        ):
            ent = get_entitlements(db, 9)
        assert ent.plan_slug == "none"
        assert ent.has_feature("meta_catalog_sync") is False

    def test_strict_lookup_does_not_collapse_outage_to_none(self):
        from core.plan_entitlements import EntitlementLookupUnavailable

        db = self._db_salla_lookup_down()
        with patch("core.billing.get_tenant_subscription", return_value=None), patch(
            "core.manual_billing_grant.is_manual_gift_grant_active",
            return_value=False,
        ):
            with pytest.raises(EntitlementLookupUnavailable):
                get_entitlements(db, 9, strict_lookup=True)

    def test_strict_lookup_wraps_active_gift_slug_read(self):
        from sqlalchemy.exc import OperationalError
        from core.plan_entitlements import EntitlementLookupUnavailable

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        slug_error = OperationalError("SELECT gift slug", {}, Exception("gift metadata timeout"))
        with patch("core.billing.get_tenant_subscription", return_value=None), patch(
            "core.manual_billing_grant.is_manual_gift_grant_active",
            return_value=True,
        ), patch(
            "core.manual_billing_grant.get_manual_gift_grant_plan_slug",
            side_effect=slug_error,
        ):
            with pytest.raises(OperationalError):
                get_entitlements(db, 9)
            with pytest.raises(EntitlementLookupUnavailable) as caught:
                get_entitlements(db, 9, strict_lookup=True)
        assert caught.value.source == "gift_slug"

    def test_strict_lookup_wraps_active_override_slug_read(self):
        from sqlalchemy.exc import OperationalError
        from core.plan_entitlements import EntitlementLookupUnavailable

        db = MagicMock()
        slug_error = OperationalError("SELECT override slug", {}, Exception("override metadata timeout"))
        with patch("core.billing_override.is_partner_testing_override_active", return_value=True), patch(
            "core.billing_override.get_partner_testing_override_plan_slug",
            side_effect=slug_error,
        ):
            with pytest.raises(OperationalError):
                get_entitlements(db, 1)
            with pytest.raises(EntitlementLookupUnavailable) as caught:
                get_entitlements(db, 1, strict_lookup=True)
        assert caught.value.source == "partner_override_slug"


if __name__ == "__main__":
    import traceback

    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    RESET  = "\033[0m"
    BOLD   = "\033[1m"

    test_classes = [
        TestPlanDefinitions,
        TestStarterPlan,
        TestGrowthPlan,
        TestScalePlan,
        TestUnknownPlan,
        TestBillingStates,
        TestDowngrade,
        TestErrorShape,
        TestToDictSerialisation,
    ]

    passed = failed = 0
    failures = []

    print(f"\n{BOLD}=== Nahla Entitlements - Final Validation Suite ==={RESET}\n")

    for cls in test_classes:
        instance = cls()
        methods  = [m for m in dir(cls) if m.startswith("test_")]
        print(f"{BOLD}▸ {cls.__name__}{RESET}  ({len(methods)} tests)")

        for method_name in sorted(methods):
            method = getattr(instance, method_name)
            label  = method_name.replace("test_", "").replace("_", " ")
            try:
                method()
                print(f"  {GREEN}✓{RESET}  {label}")
                passed += 1
            except AssertionError as e:
                print(f"  {RED}✗{RESET}  {label}")
                print(f"     {RED}{e}{RESET}")
                failures.append((cls.__name__, method_name, str(e)))
                failed += 1
            except Exception as e:
                print(f"  {RED}✗{RESET}  {label}")
                print(f"     {RED}EXCEPTION: {e}{RESET}")
                failures.append((cls.__name__, method_name, traceback.format_exc()))
                failed += 1

        print()

    total = passed + failed
    print(f"{BOLD}=== Results ==={RESET}")
    print(f"  Total:  {total}")
    print(f"  {GREEN}Passed: {passed}{RESET}")
    if failed:
        print(f"  {RED}Failed: {failed}{RESET}")
        print(f"\n{BOLD}Failures:{RESET}")
        for cls_name, method, msg in failures:
            print(f"  {RED}{cls_name}::{method}{RESET}")
            for line in msg.splitlines()[:3]:
                print(f"    {line}")
    else:
        print(f"\n{GREEN}{BOLD}All {passed} tests passed - Entitlements system is correct!{RESET}")

    sys.exit(0 if failed == 0 else 1)
