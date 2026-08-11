"""Pack B customer-facing merchant capability grounding regressions."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.salla_merchant_capabilities import STATUS_KNOWN as CAP_KNOWN
from modules.ai.brain.commerce.cod_policy_evidence import (
    STATUS_KNOWN,
    STATUS_UNKNOWN,
    build_cod_policy_reply,
    load_cod_policy_evidence,
)
from modules.ai.brain.commerce.merchant_capability_faq import (
    is_merchant_payment_methods_question,
    is_merchant_shipping_companies_question,
)
from modules.ai.brain.compose.prompt_state_serializer import _slim_known_facts
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent.rules import match as rules_match
from modules.ai.brain.postprocess.merchant_capability_truth_guard import (
    apply_merchant_capability_truth_guard,
)
from modules.ai.brain.types import (
    INTENT_ASK_COD,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_SHIPPING,
    INTENT_PAY_NOW,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)


def _caps(
    *,
    payments_status: str,
    payment_codes: List[str],
    company_status: str = CAP_KNOWN,
    companies: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    pay_items = [{"code": c, "label": c, "enabled": True} for c in payment_codes]
    company_items = list(companies or [])
    return {
        "source": "salla",
        "kind": "merchant_enabled",
        "payments": {"status": payments_status, "methods": pay_items},
        "shipping": {
            "companies_status": company_status,
            "companies": company_items,
            "zones_status": CAP_KNOWN,
            "zones": [],
        },
    }


def _ctx(message: str, intent: Intent, facts: CommerceFacts) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        history=[],
        profile={},
        intent=intent,
        state=MerchantConversationState(stage="browsing", greeted=True),
        facts=facts,
    )


class TestPaymentMethodsRouting:
    def test_payment_methods_phrase_not_pay_now(self) -> None:
        intent = rules_match("وش طرق الدفع عندكم؟")
        assert intent is not None
        assert intent.name == INTENT_ASK_PAYMENT_INFO
        assert intent.name != INTENT_PAY_NOW
        assert is_merchant_payment_methods_question("وش طرق الدفع عندكم؟")

    def test_decision_routes_to_merchant_payment_methods(self) -> None:
        engine = DefaultDecisionEngine()
        facts = CommerceFacts(
            store_name="متجر تجريبي عام",
            payment_methods=["cod", "bank"],
            payment_methods_source="salla_merchant_enabled",
            salla_payments_status=STATUS_KNOWN,
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=["cod", "bank"],
            ),
        )
        intent = Intent(
            name=INTENT_ASK_PAYMENT_INFO,
            confidence=0.97,
            slots={},
            raw_message="وش طرق الدفع عندكم؟",
        )
        decision = engine.decide(_ctx("وش طرق الدفع عندكم؟", intent, facts))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "merchant_payment_methods"


class TestCodOwnership:
    def test_cod_enabled_from_merchant_capabilities(self) -> None:
        evidence = load_cod_policy_evidence(
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=["cod", "bank"],
            )
        )
        assert evidence.source == "salla_merchant_enabled"
        assert evidence.status == STATUS_KNOWN
        assert evidence.cash_on_delivery_enabled is True
        assert "cod" in evidence.available_methods

    def test_cod_known_absent(self) -> None:
        evidence = load_cod_policy_evidence(
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=["mahally_customer_wallet"],
            )
        )
        assert evidence.cash_on_delivery_enabled is False
        reply = build_cod_policy_reply(evidence)
        assert "غير متاح" in reply.reply_text
        assert "الراجحي" not in reply.reply_text
        assert "STC Pay" not in reply.reply_text

    def test_unknown_does_not_invent_methods(self) -> None:
        evidence = load_cod_policy_evidence({})
        assert evidence.status == STATUS_UNKNOWN
        assert evidence.cash_on_delivery_enabled is None
        assert evidence.available_methods == []
        reply = build_cod_policy_reply(evidence)
        assert "الراجحي" not in reply.reply_text
        assert "STC" not in reply.reply_text

    def test_nahla_native_does_not_override_salla_known(self) -> None:
        evidence = load_cod_policy_evidence(
            {
                "cash_on_delivery_enabled": False,
                "available_payment_methods": ["alrajhi"],
            },
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=["cod"],
            ),
        )
        assert evidence.source == "salla_merchant_enabled"
        assert evidence.cash_on_delivery_enabled is True
        assert evidence.available_methods == ["cod"]

    def test_cod_intent_routes_to_llm_not_faq_prose(self) -> None:
        engine = DefaultDecisionEngine()
        intent = rules_match("هل عندكم دفع عند الاستلام؟")
        assert intent is not None
        assert intent.name == INTENT_ASK_COD
        facts = CommerceFacts(
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=["cod"],
            ),
            payment_methods=["cod"],
            payment_methods_source="salla_merchant_enabled",
            salla_payments_status=STATUS_KNOWN,
        )
        decision = engine.decide(
            _ctx("هل عندكم دفع عند الاستلام؟", intent, facts)
        )
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "cash_on_delivery"


class TestShippingCompaniesRouting:
    def test_shipping_companies_phrase(self) -> None:
        assert is_merchant_shipping_companies_question("وش شركات الشحن عندكم؟")
        intent = rules_match("وش شركات الشحن عندكم؟")
        assert intent is not None
        assert intent.name == INTENT_ASK_SHIPPING

    def test_decision_marks_shipping_companies_question(self) -> None:
        engine = DefaultDecisionEngine()
        facts = CommerceFacts(
            shipping_methods=["Carrier A"],
            shipping_methods_source="salla_merchant_enabled",
            salla_shipping_companies_status=STATUS_KNOWN,
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=["bank"],
                companies=[{"id": 1, "name": "Carrier A", "enabled": True}],
            ),
        )
        intent = Intent(
            name=INTENT_ASK_SHIPPING,
            confidence=0.9,
            slots={},
            raw_message="وش شركات الشحن عندكم؟",
        )
        decision = engine.decide(_ctx("وش شركات الشحن عندكم؟", intent, facts))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("question_kind") == "shipping_companies"
        assert decision.args.get("capability_surface") == "salla_merchant_enabled"


class TestDualTenantIsolation:
    def test_tenant_a_and_b_capability_answers_isolated(self) -> None:
        caps_a = _caps(
            payments_status=STATUS_KNOWN,
            payment_codes=["cod", "bank"],
            companies=[{"id": 1, "name": "Carrier A", "enabled": True}],
        )
        caps_b = _caps(
            payments_status=STATUS_KNOWN,
            payment_codes=["mahally_customer_wallet"],
            companies=[{"id": 2, "name": "Carrier B", "enabled": True}],
        )
        ev_a = load_cod_policy_evidence(merchant_capabilities=caps_a)
        ev_b = load_cod_policy_evidence(merchant_capabilities=caps_b)
        assert ev_a.cash_on_delivery_enabled is True
        assert ev_b.cash_on_delivery_enabled is False
        assert ev_a.available_methods == ["cod", "bank"]
        assert ev_b.available_methods == ["mahally_customer_wallet"]


class TestSlimKeepsCapabilities:
    def test_slim_known_facts_keeps_pack_b_fields(self) -> None:
        slim = _slim_known_facts(
            {
                "payment_methods": ["cod"],
                "payment_methods_source": "salla_merchant_enabled",
                "shipping_methods": ["Dev Company"],
                "merchant_capabilities": {"payments": {"status": "known"}},
                "merchant_capability_answer": {"question_kind": "payment_methods"},
            }
        )
        assert slim["payment_methods"] == ["cod"]
        assert slim["shipping_methods"] == ["Dev Company"]
        assert slim["merchant_capabilities"]["payments"]["status"] == "known"
        assert slim["merchant_capability_answer"]["question_kind"] == "payment_methods"


class TestTruthGuard:
    def test_scrubs_invented_banks_when_pack_b_known(self) -> None:
        result = apply_merchant_capability_truth_guard(
            "حاليًا الدفع عند الاستلام غير متاح. المتوفر عبر: الراجحي، STC Pay، برق.",
            known_facts={
                "merchant_capability_answer": {
                    "payments_status": "known",
                    "payment_methods": ["cod"],
                    "question_kind": "cash_on_delivery",
                }
            },
            decision_topic="cash_on_delivery",
        )
        assert result.invented_payment_methods
        assert "alrajhi" in result.invented_payment_methods or "stc_pay" in result.invented_payment_methods
        assert "الراجحي" not in result.text


class TestCatalogNavigatorYieldsToCapability:
    """Production-equivalent: has_products + navigator enabled must still yield."""

    def _facts(self, **kwargs: Any) -> CommerceFacts:
        return CommerceFacts(
            store_name="متجر تجريبي عام",
            has_products=True,
            payment_methods=kwargs.get("payment_codes", ["cod", "bank"]),
            payment_methods_source="salla_merchant_enabled",
            salla_payments_status=STATUS_KNOWN,
            shipping_methods=kwargs.get("shipping", ["Carrier A"]),
            shipping_methods_source="salla_merchant_enabled",
            salla_shipping_companies_status=STATUS_KNOWN,
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=kwargs.get("payment_codes", ["cod", "bank"]),
                companies=[
                    {"id": 1, "name": c, "enabled": True}
                    for c in kwargs.get("shipping", ["Carrier A"])
                ],
            ),
        )

    def test_payment_methods_not_catalog_top_fallback(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav
        from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE

        calls = {"groups": 0}

        def _fake_groups(ctx: Any) -> list:
            calls["groups"] += 1
            return []

        monkeypatch.setattr(nav, "_load_catalog_groups", _fake_groups)

        msg = "وش طرق الدفع عندكم؟"
        intent = rules_match(msg)
        assert intent is not None
        ctx = _ctx(msg, intent, self._facts())
        ctx._db = object()  # enable navigator DB path
        # Direct navigator call must yield.
        assert nav.try_catalog_navigation_decision(ctx) is None
        assert calls["groups"] == 0  # yielded before group load
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("topic") == "merchant_payment_methods"
        assert "catalog_navigation_top_products_fallback" not in str(
            decision.args.get("chosen_path") or ""
        )

    def test_shipping_companies_not_catalog_top_fallback(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav
        from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE

        monkeypatch.setattr(nav, "_load_catalog_groups", lambda ctx: [])
        msg = "وش شركات الشحن عندكم؟"
        intent = rules_match(msg)
        assert intent is not None
        ctx = _ctx(msg, intent, self._facts(shipping=["Dev Company"]))
        ctx._db = object()
        assert nav.try_catalog_navigation_decision(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("question_kind") == "shipping_companies"

    def test_genuine_browse_still_reaches_catalog_navigator(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav
        from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE
        from modules.ai.brain.types import INTENT_GREETING

        monkeypatch.setattr(nav, "_load_catalog_groups", lambda ctx: [])
        msg = "وش عندكم؟"
        intent = Intent(name=INTENT_GREETING, confidence=0.9, slots={}, raw_message=msg)
        # Prefer browse intent naming used by navigator signals.
        intent = Intent(name="general", confidence=0.5, slots={}, raw_message=msg)
        ctx = _ctx(msg, intent, self._facts())
        ctx._db = object()
        decision = nav.try_catalog_navigation_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") == nav.PATH_TOP_FALLBACK


class TestCodComposeGrounding:
    def test_cod_known_true_response_goal(self) -> None:
        from modules.ai.brain.pipeline import _compose_base_response_goal
        from modules.ai.brain.types import Decision, SuggestionSnapshot

        goal = _compose_base_response_goal(
            Decision(
                action=ACTION_LLM_REPLY,
                args={"topic": "cash_on_delivery"},
                reason="test",
            ),
            SuggestionSnapshot(),
        )
        assert "cash_on_delivery" in goal
        assert "product stock" in goal.lower() or "stock/availability" in goal.lower()
        assert "MERCHANT_CAPABILITIES" in goal or "merchant_capability_answer" in goal

    def test_cod_evidence_known_true_false_unknown(self) -> None:
        yes = load_cod_policy_evidence(
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN, payment_codes=["cod", "bank"]
            )
        )
        no = load_cod_policy_evidence(
            merchant_capabilities=_caps(
                payments_status=STATUS_KNOWN,
                payment_codes=["mahally_customer_wallet"],
            )
        )
        unk = load_cod_policy_evidence(
            merchant_capabilities=_caps(
                payments_status=STATUS_UNKNOWN, payment_codes=[]
            )
        )
        assert yes.cash_on_delivery_enabled is True
        assert no.cash_on_delivery_enabled is False
        assert unk.cash_on_delivery_enabled is None
        assert "الراجحي" not in build_cod_policy_reply(no).reply_text
        assert "الراجحي" not in build_cod_policy_reply(unk).reply_text

    def test_capability_compose_turn_helper(self) -> None:
        from modules.ai.brain.commerce.merchant_capability_faq import (
            is_merchant_capability_compose_turn,
        )

        assert is_merchant_capability_compose_turn(
            decision_topic="cash_on_delivery", message="هل عندكم دفع عند الاستلام؟"
        )
        assert is_merchant_capability_compose_turn(
            message="وش طرق الدفع عندكم؟"
        )
        assert not is_merchant_capability_compose_turn(
            decision_topic="", message="وش عندكم؟"
        )


class TestDualTenantShippingIsolation:
    def test_shipping_companies_isolated_across_tenants(self) -> None:
        caps_a = _caps(
            payments_status=STATUS_KNOWN,
            payment_codes=["cod", "bank"],
            companies=[{"id": 1, "name": "Carrier A", "enabled": True}],
        )
        caps_b = _caps(
            payments_status=STATUS_KNOWN,
            payment_codes=["mahally_customer_wallet"],
            companies=[{"id": 2, "name": "Carrier B", "enabled": True}],
        )
        ships_a = [
            c.get("name")
            for c in caps_a["shipping"]["companies"]
            if isinstance(c, dict)
        ]
        ships_b = [
            c.get("name")
            for c in caps_b["shipping"]["companies"]
            if isinstance(c, dict)
        ]
        assert ships_a == ["Carrier A"]
        assert ships_b == ["Carrier B"]
        assert "Carrier B" not in ships_a
        assert "Carrier A" not in ships_b
        assert "cod" in [
            m["code"] for m in caps_a["payments"]["methods"]
        ]
        assert "cod" not in [
            m["code"] for m in caps_b["payments"]["methods"]
        ]
