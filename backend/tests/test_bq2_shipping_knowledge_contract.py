"""BQ-2 — shipping knowledge contract regressions (merchant-agnostic)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (ROOT, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from commerce_scenario_fixtures import make_scenario_db, seed_knowledge_section, seed_tenant  # noqa: E402
from core.checkout_shipping_policy import (  # noqa: E402
    SHIPPING_KB_KINDS,
    build_shipping_knowledge_facts,
    clear_pending_shipping_city,
    get_pending_shipping_city,
    pin_pending_shipping_city,
    resolve_city_shipping_policy,
    resolve_verified_shipping_fee,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.state.store import DefaultStateStore  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    Decision,
    INTENT_ASK_SHIPPING,
    Intent,
    MerchantConversationState,
)
from modules.ai.brain.postprocess.shipping_cost_truth_guard import (  # noqa: E402
    apply_shipping_cost_truth_guard,
)


def _seed_shipping_kb(db, tenant_id: int, body: str, *, kind: str = "shipping") -> None:
    seed_knowledge_section(
        db,
        tenant_id,
        kind=kind,
        title="سياسة الشحن",
        body=body,
    )


@pytest.fixture
def db_tenant_a():
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي أ")
    _seed_shipping_kb(
        db,
        tenant.id,
        "الشحن للرياض 2-3 أيام عمل — 25 ريال.\nشحن جدة — 35 ريال خلال 4 أيام.",
    )
    return db, tenant


@pytest.fixture
def db_tenant_b():
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي ب")
    _seed_shipping_kb(
        db,
        tenant.id,
        "توصيل الدمام — 40 ريال خلال 5 أيام.",
    )
    return db, tenant


class TestShippingKbKindContract:
    def test_dual_read_accepts_legacy_shipping_kind(self, db_tenant_a) -> None:
        assert "shipping" in SHIPPING_KB_KINDS
        assert "shipping_zones" in SHIPPING_KB_KINDS

    def test_shipping_zones_kind_also_resolves(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر عام")
        _seed_shipping_kb(
            db,
            tenant.id,
            "الشحن للطائف — 18 ريال خلال 3 أيام.",
            kind="shipping_zones",
        )
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="الطائف")
        assert res.shipping_fee_sar == 18.0
        assert res.eta


class TestCityShippingResolution:
    def test_resolves_riyadh_fee_from_kb(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="الرياض")
        assert res.shipping_fee_sar == 25.0
        assert "2-3" in res.eta or "أيام" in res.eta
        assert res.source == "kb_shipping_policy"

    def test_resolves_jeddah_fee_from_kb(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="جدة")
        assert res.shipping_fee_sar == 35.0

    def test_unknown_city_has_no_invented_fee(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="تبوك")
        assert res.city_not_covered is True
        assert res.shipping_fee_sar is None

    def test_missing_city_signals_need_city(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        facts = build_shipping_knowledge_facts(
            db,
            tenant_id=tenant.id,
            message="كم تكلفة الشحن؟",
        )
        assert facts["need_city"] is True
        assert facts["source"] == "kb"
        assert "fee_sar" not in facts

    def test_generalization_third_city_without_code_change(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر هدايا عام")
        _seed_shipping_kb(
            db,
            tenant.id,
            "شحن أبها — 22 ريال خلال 3 أيام.",
        )
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="أبها")
        assert res.shipping_fee_sar == 22.0

    def test_generalization_kb_city_outside_builtin_list(self) -> None:
        """City label authored only in tenant KB — not hardcoded in runtime."""
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر نيوم تجريبي")
        _seed_shipping_kb(
            db,
            tenant.id,
            "الشحن لنيوم — 55 ريال خلال 2 أيام.",
        )
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="نيوم")
        assert res.shipping_fee_sar == 55.0
        facts = build_shipping_knowledge_facts(
            db,
            tenant_id=tenant.id,
            message="كم الشحن لنيوم؟",
        )
        assert facts.get("fee_sar") == 55.0
        assert facts.get("need_city") is False

    def test_tenant_isolation_different_fees(self, db_tenant_a, db_tenant_b) -> None:
        db_a, tenant_a = db_tenant_a
        db_b, tenant_b = db_tenant_b
        riyadh_a = resolve_city_shipping_policy(db_a, tenant_id=tenant_a.id, city="الرياض")
        dammam_b = resolve_city_shipping_policy(db_b, tenant_id=tenant_b.id, city="الدمام")
        assert riyadh_a.shipping_fee_sar == 25.0
        assert dammam_b.shipping_fee_sar == 40.0
        missing_on_b = resolve_city_shipping_policy(db_b, tenant_id=tenant_b.id, city="الرياض")
        assert missing_on_b.city_not_covered is True

    def test_same_city_different_fee_across_tenants(self) -> None:
        db, _ = make_scenario_db()
        tenant_a = seed_tenant(db, name="متجر أ")
        tenant_b = seed_tenant(db, name="متجر ب")
        _seed_shipping_kb(db, tenant_a.id, "الشحن للرياض — 25 ريال.")
        _seed_shipping_kb(db, tenant_b.id, "الشحن للرياض — 40 ريال.")
        fee_a = resolve_city_shipping_policy(db, tenant_id=tenant_a.id, city="الرياض")
        fee_b = resolve_city_shipping_policy(db, tenant_id=tenant_b.id, city="الرياض")
        assert fee_a.shipping_fee_sar == 25.0
        assert fee_b.shipping_fee_sar == 40.0

    def test_eta_without_fee_from_kb(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر مدة فقط")
        _seed_shipping_kb(db, tenant.id, "الشحن للرياض خلال 2-3 أيام عمل.")
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="الرياض")
        assert res.shipping_fee_sar is None
        assert res.eta
        assert res.city_not_covered is False
        facts = build_shipping_knowledge_facts(
            db,
            tenant_id=tenant.id,
            message="كم مدة الشحن للرياض؟",
        )
        assert "fee_sar" not in facts
        assert facts.get("eta")
        assert facts.get("city") == "الرياض"
        assert facts.get("need_city") is False

    def test_fee_without_eta_from_kb(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر رسوم فقط")
        _seed_shipping_kb(db, tenant.id, "الشحن لجدة — 35 ريال.")
        res = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="جدة")
        assert res.shipping_fee_sar == 35.0
        assert res.eta == ""

    def test_does_not_treat_fee_prose_as_destination(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر عام")
        _seed_shipping_kb(db, tenant.id, "الشحن مجاني داخل المدينة.")
        # Must not invent a destination city named after free-shipping prose.
        rules_city = resolve_city_shipping_policy(db, tenant_id=tenant.id, city="مجاني")
        assert rules_city.city_not_covered is True
        assert rules_city.shipping_fee_sar is None


class TestShippingKnowledgeComposeFacts:
    def test_structured_facts_when_city_in_message(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        facts = build_shipping_knowledge_facts(
            db,
            tenant_id=tenant.id,
            message="كم الشحن للرياض؟",
        )
        assert facts["city"] == "الرياض"
        assert facts["fee_sar"] == 25.0
        assert facts["need_city"] is False
        assert facts["source"] == "kb"

    def test_follow_up_city_after_pending_shipping_question(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        state = SimpleNamespace(commerce_session={})
        pin_pending_shipping_city(state, source="ask_shipping")
        facts = build_shipping_knowledge_facts(
            db,
            tenant_id=tenant.id,
            message="الرياض",
            brain_state={"commerce_session": dict(state.commerce_session)},
        )
        assert facts["city"] == "الرياض"
        assert facts["fee_sar"] == 25.0
        clear_pending_shipping_city(state)
        assert state.commerce_session.get("pending_shipping_city_inquiry") is None


class TestPendingShippingCityStateTransition:
    def _transition_after_ask_shipping(
        self,
        state: MerchantConversationState,
    ) -> MerchantConversationState:
        pin_pending_shipping_city(state, source="ask_shipping")
        intent = Intent(
            name=INTENT_ASK_SHIPPING,
            confidence=0.9,
            raw_message="كم تكلفة الشحن؟",
        )
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic_hint": "shipping"},
            reason="test",
            confidence=0.9,
        )
        return DefaultStateStore().transition(state, intent, decision)

    def test_pending_marker_survives_transition_and_resolves_city_follow_up(
        self,
        db_tenant_a,
    ) -> None:
        db, tenant = db_tenant_a
        source = MerchantConversationState()
        transitioned = self._transition_after_ask_shipping(source)

        assert get_pending_shipping_city(transitioned) is not None
        facts = build_shipping_knowledge_facts(
            db,
            tenant_id=tenant.id,
            message="الرياض",
            brain_state=transitioned.to_dict(),
        )
        assert facts["city"] == "الرياض"
        assert facts["fee_sar"] == 25.0
        assert facts["need_city"] is False

    def test_transition_copies_commerce_session_without_aliasing(self) -> None:
        source = MerchantConversationState()
        source.commerce_session["probe"] = "before_transition"

        transitioned = self._transition_after_ask_shipping(source)

        assert transitioned.commerce_session is not source.commerce_session
        assert transitioned.commerce_session.get("probe") == "before_transition"
        assert get_pending_shipping_city(transitioned) is not None

    def test_tenant_isolation_after_transition_city_follow_up(
        self,
        db_tenant_a,
        db_tenant_b,
    ) -> None:
        db_a, tenant_a = db_tenant_a
        db_b, tenant_b = db_tenant_b

        state_a = self._transition_after_ask_shipping(MerchantConversationState())
        state_b = self._transition_after_ask_shipping(MerchantConversationState())

        facts_a = build_shipping_knowledge_facts(
            db_a,
            tenant_id=tenant_a.id,
            message="الرياض",
            brain_state=state_a.to_dict(),
        )
        facts_b = build_shipping_knowledge_facts(
            db_b,
            tenant_id=tenant_b.id,
            message="الدمام",
            brain_state=state_b.to_dict(),
        )

        assert facts_a["fee_sar"] == 25.0
        assert facts_b["fee_sar"] == 40.0

        missing_on_b = build_shipping_knowledge_facts(
            db_b,
            tenant_id=tenant_b.id,
            message="الرياض",
            brain_state=state_b.to_dict(),
        )
        assert missing_on_b.get("city_not_in_policy") is True
        assert "fee_sar" not in missing_on_b

    def test_clearing_pending_marker_does_not_mutate_source_state(self) -> None:
        source = MerchantConversationState()
        transitioned = self._transition_after_ask_shipping(source)

        assert get_pending_shipping_city(source) is not None
        assert get_pending_shipping_city(transitioned) is not None

        clear_pending_shipping_city(transitioned)

        assert get_pending_shipping_city(transitioned) is None
        assert get_pending_shipping_city(source) is not None


class TestShippingCostTruthGuard:
    def test_guard_preserves_verified_kb_fee(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        reply = "تكلفة الشحن للرياض 25 ريال خلال 2-3 أيام."
        result = apply_shipping_cost_truth_guard(
            reply,
            db=db,
            tenant_id=tenant.id,
            message="كم الشحن للرياض؟",
        )
        assert result.replaced is False
        assert "25" in result.reply

    def test_guard_blocks_invented_fee_without_kb(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        reply = "شحن توصيل 29 ريال للرياض"
        result = apply_shipping_cost_truth_guard(
            reply,
            db=db,
            tenant_id=tenant.id,
            message="كم الشحن للرياض؟",
        )
        assert result.replaced is True
        assert "29" not in result.reply

    def test_guard_blocks_wrong_fee_for_known_city(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        reply = "الشحن للرياض 30 ريال"
        result = apply_shipping_cost_truth_guard(
            reply,
            db=db,
            tenant_id=tenant.id,
            message="كم الشحن للرياض؟",
        )
        assert result.replaced is True
        assert result.reason == "shipping_fee_mismatch"

    def test_guard_fails_closed_when_db_is_none(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        reply = "تكلفة الشحن للرياض 25 ريال خلال 2-3 أيام."
        verified = apply_shipping_cost_truth_guard(
            reply,
            db=db,
            tenant_id=tenant.id,
            message="كم الشحن للرياض؟",
        )
        assert verified.replaced is False
        assert "25" in verified.reply

        without_db = apply_shipping_cost_truth_guard(
            reply,
            db=None,
            tenant_id=tenant.id,
            message="كم الشحن للرياض؟",
        )
        assert without_db.replaced is True

    def test_verified_fee_helper_matches_kb(self, db_tenant_a) -> None:
        db, tenant = db_tenant_a
        fee, _resolution = resolve_verified_shipping_fee(
            db,
            tenant_id=tenant.id,
            message="كم الشحن للرياض؟",
        )
        assert fee == 25.0
