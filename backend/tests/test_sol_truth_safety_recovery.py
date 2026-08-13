"""Pre-Pack-C truth-safety recovery — fact-answer ownership + UNKNOWN contract.

Asserts owner, fact status, provenance, and catalog suppression.
Does not assert exact Arabic customer wording.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from modules.ai.brain.commerce.fact_answer import (
    KIND_BRANCH_EXISTENCE,
    KIND_CASH_ON_DELIVERY,
    KIND_CERTIFICATION,
    KIND_GIFT_RECOMMENDATION,
    KIND_PAYMENT_METHODS,
    KIND_RETURN_POLICY,
    KIND_SHIPPING_COMPANIES,
    KIND_SHIPPING_ETA,
    KIND_SHIPPING_FEE,
    KIND_WARRANTY,
    KIND_WORKING_HOURS,
    STATUS_KNOWN_VALUE,
    STATUS_UNKNOWN,
    build_fact_answer_contract,
    build_fact_answer_decision,
    catalog_must_yield_to_fact_owner,
    classify_fact_answer,
    fact_answer_owns_non_catalog_turn,
)
from modules.ai.brain.commerce.merchant_capability_faq import (
    is_merchant_payment_methods_question,
    is_merchant_shipping_companies_question,
)
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut
from modules.ai.brain.commerce.merchant_policy_intents import (
    classify_merchant_policy_topic,
    should_yield_catalog_for_merchant_policy,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_TRACK_ORDER
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.types import (
    INTENT_ASK_COD,
    INTENT_ASK_LOCATION,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_WORKING_HOURS,
    INTENT_COMPLAINT_REFUND,
    INTENT_PAY_NOW,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)


def _caps(*, payments: Optional[List[str]] = None, companies: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "source": "salla",
        "kind": "merchant_enabled",
        "payments": {
            "status": "known",
            "methods": [{"code": c, "label": c, "enabled": True} for c in (payments or [])],
        },
        "shipping": {
            "companies_status": "known" if companies is not None else "unknown",
            "companies": [{"id": i, "name": n, "enabled": True} for i, n in enumerate(companies or [], 1)],
            "zones_status": "known",
            "zones": [],
        },
    }


def _facts(**kwargs: Any) -> CommerceFacts:
    base = dict(
        store_name="متجر تجريبي عام",
        support_hours="",
        maps_url="",
        payment_methods=["cod", "bank"],
        shipping_methods=["Dev Company"],
        merchant_capabilities=_caps(payments=["cod", "bank"], companies=["Dev Company"]),
        merchant_policy={},
        has_products=True,
        product_count=3,
        top_products=[
            {"id": 1, "title": "فستان سهرة أسود", "price": 200},
            {"id": 2, "title": "تنورة كتان", "price": 90},
        ],
    )
    base.update(kwargs)
    return CommerceFacts(**base)


def _ctx(message: str, intent_name: str, facts: Optional[CommerceFacts] = None) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        history=[],
        profile={},
        intent=Intent(name=intent_name, confidence=0.9, slots={}, raw_message=message),
        state=MerchantConversationState(stage="browsing", greeted=True),
        facts=facts or _facts(),
    )


def _paid_ctx(message: str, intent_name: str, facts: Optional[CommerceFacts] = None) -> BrainContext:
    ctx = _ctx(message, intent_name, facts=facts)
    ctx.state.order_prep.payment_receipt_received = True
    return ctx


class TestSemanticFactKind:
    def test_hours_and_open_now(self) -> None:
        hours = classify_fact_answer("كم ساعات دوام الفرع في جدة؟", intent_name=INTENT_ASK_WORKING_HOURS)
        assert hours is not None
        assert hours.fact_kind == KIND_WORKING_HOURS
        assert hours.catalog_allowed is False
        open_now = classify_fact_answer("المتجر شغال الآن؟", intent_name=INTENT_ASK_WORKING_HOURS)
        assert open_now is not None
        assert open_now.catalog_allowed is False

    def test_branch_existence(self) -> None:
        req = classify_fact_answer("عندكم فرع في لندن؟", intent_name=INTENT_ASK_LOCATION)
        assert req is not None
        assert req.fact_kind == KIND_BRANCH_EXISTENCE
        assert req.catalog_allowed is False

    def test_certification_is_fact_kind_not_phrase_ban(self) -> None:
        for msg in (
            "هل المنتج معتمد من هيئة الغذاء؟",
            "هذا عليه اعتماد؟",
            "فيه شهادة؟",
            "هل عليه اعتماد رسمي؟",
        ):
            req = classify_fact_answer(msg, intent_name=INTENT_ASK_PRODUCT)
            assert req is not None, msg
            assert req.fact_kind == KIND_CERTIFICATION

    def test_shipping_kinds_are_distinct(self) -> None:
        companies = classify_fact_answer("أي شركة توصلون معها؟")
        assert companies is not None
        assert companies.fact_kind == KIND_SHIPPING_COMPANIES
        fee = classify_fact_answer("كم تكلفة الشحن؟", intent_name=INTENT_ASK_PRICE)
        assert fee is not None
        assert fee.fact_kind == KIND_SHIPPING_FEE
        eta = classify_fact_answer("كم يستغرق الشحن؟", intent_name=INTENT_ASK_SHIPPING)
        assert eta is not None
        assert eta.fact_kind == KIND_SHIPPING_ETA
        paraphrase = classify_fact_answer("مين اللي يشحن الطلب؟")
        assert paraphrase is not None
        assert paraphrase.fact_kind == KIND_SHIPPING_COMPANIES

    def test_policy_existence_not_catalog(self) -> None:
        assert classify_merchant_policy_topic("عندكم ضمان؟") == "warranty"
        assert classify_merchant_policy_topic("عندكم إرجاع؟") == "return_policy"
        assert not should_yield_catalog_for_merchant_policy(message="عندكم ضمان؟")
        assert not should_yield_catalog_for_merchant_policy(message="عندكم إرجاع؟")
        w = classify_fact_answer("عندكم ضمان؟", intent_name=INTENT_ASK_PRODUCT)
        r = classify_fact_answer("عندكم إرجاع؟", intent_name=INTENT_ASK_PRODUCT)
        assert w is not None and w.fact_kind == KIND_WARRANTY
        assert r is not None and r.fact_kind == KIND_RETURN_POLICY

    def test_product_warranty_still_not_merchant_wide(self) -> None:
        assert classify_merchant_policy_topic("هل هذا المنتج عليه ضمان؟") is None

    def test_payment_followup_and_paraphrase(self) -> None:
        assert is_merchant_payment_methods_question("إذا بطلب الآن وش أقدر أستخدم؟")
        req = classify_fact_answer("إذا بطلب الآن وش أقدر أستخدم؟", intent_name=INTENT_ASK_PRODUCT)
        assert req is not None
        assert req.fact_kind == KIND_PAYMENT_METHODS
        para = classify_fact_answer("وش عندكم طريقة أدفع فيها؟")
        assert para is not None
        assert para.fact_kind == KIND_PAYMENT_METHODS

    def test_catalog_browse_not_stolen(self) -> None:
        assert classify_fact_answer("وش المنتجات عندكم؟") is None
        assert catalog_must_yield_to_fact_owner(message="وش المنتجات عندكم؟") is False

    def test_carrier_paraphrase_detector(self) -> None:
        assert is_merchant_shipping_companies_question("أي شركة توصلون معها؟")
        req = classify_fact_answer("والشحن مين ماسكه؟")
        assert req is not None
        assert req.fact_kind == KIND_SHIPPING_COMPANIES


class TestUnknownContract:
    def test_hours_unknown_without_evidence(self) -> None:
        req = classify_fact_answer("كم ساعات دوام الفرع في جدة؟", intent_name=INTENT_ASK_WORKING_HOURS)
        assert req is not None
        contract = build_fact_answer_contract(req, facts=_facts(support_hours=""))
        assert contract.status == STATUS_UNKNOWN
        assert "invent_city_hours" in contract.forbidden_inferences

    def test_branch_unknown_without_maps(self) -> None:
        req = classify_fact_answer("عندكم فرع في لندن؟", intent_name=INTENT_ASK_LOCATION)
        assert req is not None
        contract = build_fact_answer_contract(req, facts=_facts(maps_url=""))
        assert contract.status == STATUS_UNKNOWN
        assert "imply_branch_network" in contract.forbidden_inferences
        assert "branch_selectable" in contract.forbidden_inferences
        assert "branch_address_exists" in contract.forbidden_inferences

    def test_branch_existence_maps_url_is_not_network_evidence(self) -> None:
        req = classify_fact_answer("عندكم فرع في لندن؟", intent_name=INTENT_ASK_LOCATION)
        assert req is not None
        contract = build_fact_answer_contract(
            req, facts=_facts(maps_url="https://maps.app.goo.gl/x"),
        )
        assert contract.status == STATUS_UNKNOWN
        assert contract.claimable_values == []
        assert "maps_url_proves_named_city" in contract.forbidden_inferences

    def test_certification_unknown_without_product_evidence(self) -> None:
        req = classify_fact_answer("هل المنتج معتمد من هيئة الغذاء؟")
        assert req is not None
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN
        assert "product_existence_implies_certification" in contract.forbidden_inferences

    def test_certification_pronoun_and_paraphrase_same_unknown_owner(self) -> None:
        for message in ("هل فستان معتمد؟", "هذا عليه اعتماد؟", "عليه شهادة؟"):
            req = classify_fact_answer(message, intent_name=INTENT_ASK_PRODUCT)
            assert req is not None, message
            assert req.fact_kind == KIND_CERTIFICATION, message
            contract = build_fact_answer_contract(req, facts=_facts())
            assert contract.status == STATUS_UNKNOWN, message
            d = DefaultDecisionEngine().decide(_ctx(message, INTENT_ASK_PRODUCT))
            assert d.action == ACTION_LLM_REPLY, message
            assert d.args.get("question_kind") == KIND_CERTIFICATION, message
            assert (d.args.get("response_goal") or "").strip(), message
            assert d.args.get("answer_contract", {}).get("status") == STATUS_UNKNOWN, message

    def test_fee_and_eta_not_inferred_from_carrier(self) -> None:
        facts = _facts()
        fee_req = classify_fact_answer("كم تكلفة الشحن؟")
        eta_req = classify_fact_answer("كم يستغرق الشحن؟")
        assert fee_req is not None and eta_req is not None
        fee = build_fact_answer_contract(fee_req, facts=facts)
        eta = build_fact_answer_contract(eta_req, facts=facts)
        assert fee.status == STATUS_UNKNOWN
        assert eta.status == STATUS_UNKNOWN
        assert "carrier_implies_fee" in fee.forbidden_inferences
        assert "carrier_implies_eta" in eta.forbidden_inferences

    def test_shipping_companies_known_from_pack_b(self) -> None:
        req = classify_fact_answer("أي شركة توصلون معها؟")
        assert req is not None
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_KNOWN_VALUE
        assert "Dev Company" in contract.claimable_values

    def test_gift_recommendations_constrained_to_catalog(self) -> None:
        req = classify_fact_answer("أبي هدية", intent_name=INTENT_START_ORDER)
        assert req is not None
        assert req.fact_kind == KIND_GIFT_RECOMMENDATION
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_KNOWN_VALUE
        assert "فستان سهرة أسود" in contract.claimable_values
        assert "invent_catalog_category" in contract.forbidden_inferences


class TestDecisionOwnership:
    def test_engine_hours_not_catalog(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("كم ساعات دوام الفرع في جدة؟", INTENT_ASK_WORKING_HOURS))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("answer_contract", {}).get("status") == STATUS_UNKNOWN
        assert decision.args.get("block_catalog_navigation") is True

    def test_engine_open_now_not_catalog(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("المتجر شغال الآن؟", INTENT_ASK_WORKING_HOURS))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("question_kind") in {KIND_WORKING_HOURS, "open_now"}

    def test_engine_warranty_policy_owner(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("عندكم ضمان؟", INTENT_ASK_PRODUCT))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("question_kind") == "warranty"
        assert decision.args.get("block_catalog_navigation") is True

    def test_engine_return_policy_owner(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("عندكم إرجاع؟", INTENT_ASK_PRODUCT))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("question_kind") == "return_policy"

    def test_engine_payment_followup(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("إذا بطلب الآن وش أقدر أستخدم؟", INTENT_ASK_PRODUCT))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "merchant_payment_methods"
        assert "cod" in (decision.args.get("answer_contract") or {}).get("claimable_values", [])

    def test_engine_shipping_company_paraphrase_not_empty_owner(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("أي شركة توصلون معها؟", ""))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("question_kind") == "shipping_companies"
        assert "Dev Company" in (decision.args.get("answer_contract") or {}).get("claimable_values", [])

    def test_engine_certification_unknown(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("هل المنتج معتمد من هيئة الغذاء؟", INTENT_ASK_PRODUCT))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("question_kind") == KIND_CERTIFICATION
        assert decision.args.get("answer_contract", {}).get("status") == STATUS_UNKNOWN

    def test_eta_followup_does_not_inherit_carrier_as_duration(self) -> None:
        req = classify_fact_answer(
            "طيب كم ياخذ عادة؟",
            intent_name=INTENT_ASK_SHIPPING,
            history=[{"content": "أي شركة توصلون معها؟"}],
        )
        assert req is not None
        assert req.fact_kind == KIND_SHIPPING_ETA
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN


class TestDualTenantFactIsolation:
    def test_hours_and_carriers_do_not_leak(self) -> None:
        a = _facts(
            support_hours="9-5",
            merchant_capabilities=_caps(payments=["cod"], companies=["Carrier A"]),
            shipping_methods=["Carrier A"],
        )
        b = _facts(
            support_hours="",
            merchant_capabilities=_caps(payments=["bank"], companies=["Carrier B"]),
            shipping_methods=["Carrier B"],
            payment_methods=["bank"],
        )
        hours_req = classify_fact_answer("كم ساعات دوام الفرع في جدة؟")
        ship_req = classify_fact_answer("أي شركة توصلون معها؟")
        assert hours_req is not None and ship_req is not None
        hours_a = build_fact_answer_contract(hours_req, facts=a)
        hours_b = build_fact_answer_contract(hours_req, facts=b)
        ship_a = build_fact_answer_contract(ship_req, facts=a)
        ship_b = build_fact_answer_contract(ship_req, facts=b)
        assert hours_a.status == STATUS_KNOWN_VALUE
        assert hours_b.status == STATUS_UNKNOWN
        assert hours_a.claimable_values != hours_b.claimable_values
        assert "Carrier A" in ship_a.claimable_values
        assert "Carrier B" in ship_b.claimable_values
        assert "Carrier A" not in ship_b.claimable_values


class TestPackRegressionSurface:
    def test_pack_b_canonical_phrases_still_owned(self) -> None:
        engine = DefaultDecisionEngine()
        pay = engine.decide(_ctx("وش طرق الدفع عندكم؟", INTENT_ASK_PAYMENT_INFO))
        ship = engine.decide(_ctx("وش شركات الشحن عندكم؟", INTENT_ASK_SHIPPING))
        assert pay.args.get("topic") == "merchant_payment_methods"
        assert ship.args.get("question_kind") == "shipping_companies"

    def test_explicit_return_policy_still_a3(self) -> None:
        engine = DefaultDecisionEngine()
        decision = engine.decide(_ctx("وش سياسة الاسترجاع؟", ""))
        assert decision.args.get("question_kind") == "return_policy"
        assert decision.args.get("merchant_policy_status") == "UNKNOWN"

    def test_build_decision_exposes_contract(self) -> None:
        dec = build_fact_answer_decision(
            message="كم تكلفة الشحن؟",
            intent_name=INTENT_ASK_PRICE,
            facts=_facts(),
        )
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args["answer_contract"]["status"] == STATUS_UNKNOWN
        assert dec.args["answer_contract"]["fact_kind"] == KIND_SHIPPING_FEE


class TestTransactionalOutranksGenericFact:
    """Customer-specific order/shipment truth outranks generic merchant facts."""

    def test_paid_order_branch_origin_is_shipping_post_order(self) -> None:
        engine = DefaultDecisionEngine()
        for message in (
            "اي فرع ارسلتو طلبي في سمسا",
            "طلبي انرسل من أي فرع؟",
            "الشحنة هذي طلعت من وين؟",
        ):
            d = engine.decide(_paid_ctx(message, INTENT_ASK_SHIPPING))
            assert d.action == ACTION_LLM_REPLY, message
            assert d.args.get("topic") == "shipping_post_order", message
            assert (d.args.get("answer_contract") or {}).get("fact_kind") not in {
                KIND_BRANCH_EXISTENCE,
                "location",
            }

    def test_actual_order_carrier_not_merchant_capability(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_paid_ctx("طلبي مع أي شركة شحن؟", INTENT_ASK_SHIPPING))
        assert d.args.get("topic") == "shipping_post_order"
        assert d.args.get("question_kind") != "shipping_companies"

    def test_generic_carrier_stays_pack_b(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_ctx("أي شركة توصلون معها؟", INTENT_ASK_SHIPPING))
        assert d.args.get("question_kind") == "shipping_companies"
        assert d.args.get("topic") != "shipping_post_order"

    def test_generic_branch_stays_location_fact(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_ctx("عندكم فرع في جدة؟", INTENT_ASK_LOCATION))
        assert d.args.get("question_kind") == KIND_BRANCH_EXISTENCE
        assert d.args.get("topic") == "location_delivery"
        assert d.args.get("topic") != "shipping_post_order"

    def test_generic_eta_stays_merchant_fact(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_ctx("كم يستغرق الشحن؟", INTENT_ASK_SHIPPING))
        assert d.args.get("question_kind") == KIND_SHIPPING_ETA
        assert d.args.get("answer_contract", {}).get("status") == STATUS_UNKNOWN
        assert d.args.get("topic") != "shipping_post_order"

    def test_actual_order_eta_not_generic_merchant_eta(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_paid_ctx("طلبي متى يوصل؟", INTENT_ASK_SHIPPING))
        assert d.args.get("topic") == "shipping_post_order"
        assert (d.args.get("answer_contract") or {}).get("fact_kind") != KIND_SHIPPING_ETA

    def test_warranty_and_return_boundaries_preserved(self) -> None:
        engine = DefaultDecisionEngine()
        warranty = engine.decide(_paid_ctx("عندكم ضمان؟", INTENT_ASK_PRODUCT))
        assert warranty.args.get("question_kind") == KIND_WARRANTY
        informational = engine.decide(_ctx("عندكم إرجاع؟", INTENT_ASK_PRODUCT))
        assert informational.args.get("question_kind") == KIND_RETURN_POLICY
        operational = engine.decide(_paid_ctx("أبي أرجع طلبي", INTENT_COMPLAINT_REFUND))
        assert operational.args.get("topic") == "support_complaint_refund"

    def test_certification_not_suppressed_by_paid_order(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_paid_ctx("هل المنتج معتمد من هيئة الغذاء؟", INTENT_ASK_PRODUCT))
        assert d.args.get("question_kind") == KIND_CERTIFICATION
        assert d.args.get("answer_contract", {}).get("status") == STATUS_UNKNOWN
        assert d.args.get("topic") != "shipping_post_order"

    def test_generic_branch_still_owned_during_paid_order(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_paid_ctx("عندكم فرع في جدة؟", INTENT_ASK_LOCATION))
        assert d.args.get("question_kind") == KIND_BRANCH_EXISTENCE
        assert d.args.get("topic") != "shipping_post_order"

    def test_explicit_where_is_my_order_stays_track_order(self) -> None:
        engine = DefaultDecisionEngine()
        d = engine.decide(_paid_ctx("وين طلبي؟", INTENT_TRACK_ORDER))
        assert d.action == ACTION_TRACK_ORDER
        assert d.args.get("topic") != "shipping_post_order"


class TestSemanticConvergence:
    """Canonical vs paraphrase ownership — no phrase-only routing."""

    def test_payment_paraphrases_are_method_discovery(self) -> None:
        engine = DefaultDecisionEngine()
        for message, intent_name in (
            ("وش طرق الدفع عندكم؟", INTENT_ASK_PAYMENT_INFO),
            ("وش عندكم طريقة أدفع فيها؟", INTENT_PAY_NOW),
            ("وش أقدر أدفع فيه؟", INTENT_PAY_NOW),
        ):
            req = classify_fact_answer(message, intent_name=intent_name)
            assert req is not None, message
            assert req.fact_kind == KIND_PAYMENT_METHODS, message
            d = engine.decide(_ctx(message, intent_name))
            assert d.action == ACTION_LLM_REPLY, message
            assert d.args.get("topic") == "merchant_payment_methods", message
            assert "cod" in (d.args.get("answer_contract") or {}).get("claimable_values", []), message

    def test_cod_capability_not_method_list(self) -> None:
        req = classify_fact_answer("هل أقدر أدفع عند الاستلام؟", intent_name=INTENT_PAY_NOW)
        assert req is not None
        assert req.fact_kind == KIND_CASH_ON_DELIVERY
        d = DefaultDecisionEngine().decide(
            _ctx("هل أقدر أدفع عند الاستلام؟", INTENT_ASK_COD),
        )
        assert d.args.get("topic") == "cash_on_delivery"

    def test_bank_detail_ask_is_not_method_list(self) -> None:
        req = classify_fact_answer("أرسل رقم الحساب البنكي", intent_name=INTENT_ASK_PAYMENT_INFO)
        assert req is None or req.fact_kind != KIND_PAYMENT_METHODS
        d = DefaultDecisionEngine().decide(
            _ctx("أرسل رقم الحساب البنكي", INTENT_ASK_PAYMENT_INFO),
        )
        assert d.args.get("topic") != "merchant_payment_methods"
        assert d.args.get("topic") == "payment_info"

    def test_payment_followup_preserves_methods_context(self) -> None:
        req = classify_fact_answer(
            "طيب وش أقدر أستخدم؟",
            history=[{"content": "وش طرق الدفع عندكم؟"}],
        )
        assert req is not None
        assert req.fact_kind == KIND_PAYMENT_METHODS

    def test_carrier_paraphrases_use_merchant_capability(self) -> None:
        engine = DefaultDecisionEngine()
        for message, intent_name in (
            ("أي شركة توصلون معها؟", INTENT_ASK_SHIPPING),
            ("والشحن مين ماسكه؟", INTENT_SOCIAL),
            ("مين شركة التوصيل؟", INTENT_SOCIAL),
            ("مين يتولى التوصيل؟", INTENT_SOCIAL),
        ):
            req = classify_fact_answer(message, intent_name=intent_name)
            assert req is not None, message
            assert req.fact_kind == KIND_SHIPPING_COMPANIES, message
            d = engine.decide(_ctx(message, intent_name))
            assert d.args.get("question_kind") == KIND_SHIPPING_COMPANIES, message
            assert "Dev Company" in (d.args.get("answer_contract") or {}).get(
                "claimable_values", [],
            ), message
            assert d.args.get("topic") != "shipping_post_order", message

    def test_carrier_followup_stays_merchant_capability(self) -> None:
        req = classify_fact_answer(
            "والشحن مين ماسكه؟",
            intent_name=INTENT_SOCIAL,
            history=[{"content": "وش شركات الشحن عندكم؟"}],
        )
        assert req is not None
        assert req.fact_kind == KIND_SHIPPING_COMPANIES

    def test_minimal_facts_lose_carrier_until_pack_b_surface_loads(self) -> None:
        req = classify_fact_answer("والشحن مين ماسكه؟", intent_name=INTENT_SOCIAL)
        assert req is not None
        empty = build_fact_answer_contract(req, facts=CommerceFacts())
        known = build_fact_answer_contract(req, facts=_facts())
        assert empty.status == STATUS_UNKNOWN
        assert empty.claimable_values == []
        assert known.status == STATUS_KNOWN_VALUE
        assert "Dev Company" in known.claimable_values

    def test_actual_order_who_holds_shipment_outranks_capability(self) -> None:
        d = DefaultDecisionEngine().decide(_paid_ctx("طلبي مع مين؟", INTENT_ASK_SHIPPING))
        assert d.args.get("topic") == "shipping_post_order"
        assert d.args.get("question_kind") != KIND_SHIPPING_COMPANIES

    def test_branch_existence_unknown_contract_forbids_network(self) -> None:
        engine = DefaultDecisionEngine()
        for message in (
            "عندكم فرع في لندن؟",
            "فيه فرع بالرياض؟",
            "طيب عندكم فروع؟",
        ):
            req = classify_fact_answer(message, intent_name=INTENT_ASK_LOCATION)
            assert req is not None, message
            assert req.fact_kind == KIND_BRANCH_EXISTENCE, message
            contract = build_fact_answer_contract(req, facts=_facts(maps_url=""))
            assert contract.status == STATUS_UNKNOWN, message
            assert not contract.claimable_values, message
            assert "branch_selectable" in contract.forbidden_inferences
            d = engine.decide(_ctx(message, INTENT_ASK_LOCATION))
            assert d.args.get("question_kind") == KIND_BRANCH_EXISTENCE, message
            assert d.args.get("answer_contract", {}).get("status") == STATUS_UNKNOWN, message
            goal = str(d.args.get("response_goal") or "")
            assert "branch_selectable" in goal or "address can be sent" in goal, message

    def test_generic_branches_question_does_not_invent_place_token(self) -> None:
        req = classify_fact_answer("طيب عندكم فروع؟", intent_name=INTENT_ASK_LOCATION)
        assert req is not None
        assert req.fact_kind == KIND_BRANCH_EXISTENCE
        contract = build_fact_answer_contract(
            req,
            facts=_facts(),
            merchant_context={
                "merchant_profile": {
                    "location": "الرياض",
                    "location.status": STATUS_KNOWN_VALUE,
                }
            },
            message="طيب عندكم فروع؟",
        )
        assert contract.status == STATUS_KNOWN_VALUE
        assert "الرياض" in [str(v) for v in contract.claimable_values]
        req = classify_fact_answer(
            "طيب في لندن؟",
            history=[{"content": "عندكم فروع؟"}],
        )
        assert req is not None
        assert req.fact_kind == KIND_BRANCH_EXISTENCE
        contract = build_fact_answer_contract(req, facts=_facts(maps_url=""))
        assert contract.status == STATUS_UNKNOWN

    def test_certification_followup_preserves_product_unknown(self) -> None:
        req = classify_fact_answer(
            "هذا عليه اعتماد؟",
            history=[{"content": "اخترت هذا المنتج"}],
        )
        assert req is not None
        assert req.fact_kind == KIND_CERTIFICATION
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN

    def test_carrier_then_eta_stays_unknown(self) -> None:
        req = classify_fact_answer(
            "طيب كم ياخذ عادة؟",
            intent_name=INTENT_ASK_SHIPPING,
            history=[{"content": "وش شركة الشحن عندكم؟"}],
        )
        assert req is not None
        assert req.fact_kind == KIND_SHIPPING_ETA
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN
        assert "carrier_implies_eta" in contract.forbidden_inferences


class TestLiveInprocessFactContractParity:
    def test_social_carrier_paraphrase_does_not_take_pre_commerce_shortcut(self) -> None:
        intent = Intent(
            name=INTENT_SOCIAL,
            confidence=0.95,
            slots={"social_category": "general_courtesy"},
            raw_message="والشحن مين ماسكه؟",
        )
        assert fact_answer_owns_non_catalog_turn(
            "والشحن مين ماسكه؟", intent_name=INTENT_SOCIAL,
        )
        assert should_pre_commerce_shortcut(
            intent, None, message="والشحن مين ماسكه؟",
        ) is False
        assert should_pre_commerce_shortcut(
            intent, None, message="مين شركة التوصيل؟",
        ) is False

    def test_pure_thanks_still_takes_pre_commerce_shortcut(self) -> None:
        intent = Intent(
            name=INTENT_SOCIAL,
            confidence=0.95,
            slots={"social_category": "thanks"},
            raw_message="شكرا",
        )
        assert not fact_answer_owns_non_catalog_turn("شكرا", intent_name=INTENT_SOCIAL)
        assert should_pre_commerce_shortcut(intent, None, message="شكرا") is True
