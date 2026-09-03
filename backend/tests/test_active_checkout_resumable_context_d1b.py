"""LIVE-COMMERCE-FREEDOM-D1B — active checkout is resumable context, not turn owner.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
Customer wording appears in TEST INPUT only. Tests assert owner/action/facts.
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any, Dict, List

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    current_turn_continues_catalog_checkout,
    is_active_catalog_checkout,
    maybe_enforce_catalog_order_continue_checkout,
    try_active_catalog_checkout_continue_decision,
    try_catalog_order_continue_decision,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from core.merchant_payment_methods import MerchantPaymentMethods  # noqa: E402
from core.order_payment_policy import (  # noqa: E402
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_CASH_ON_DELIVERY,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _catalog_line_item(
    *,
    product_id: str,
    product_name: str,
    retailer_id: str,
    unit_price: float,
) -> Dict[str, Any]:
    return {
        "product_id": product_id,
        "product_retailer_id": retailer_id,
        "product_name": product_name,
        "quantity": 1,
        "unit_price": unit_price,
        "catalog_price": unit_price,
        "currency": "SAR",
        "from_native_catalog_order": True,
        "source": "whatsapp_native_catalog_order",
    }


def _active_prep(
    *,
    product_id: str = "143",
    product_name: str = "250 جرام عسل سمر الحجاز",
    retailer_id: str = "86bqzca62a",
    total: float = 126.0,
    missing: List[str] | None = None,
) -> OrderPreparationState:
    return OrderPreparationState(
        product_id=product_id,
        catalog_line_items_authoritative=True,
        catalog_checkout_total=total,
        catalog_checkout_currency="SAR",
        checkout_channel="whatsapp_catalog",
        missing_fields=list(missing if missing is not None else ["payment_method"]),
        line_items=[
            _catalog_line_item(
                product_id=product_id,
                product_name=product_name,
                retailer_id=retailer_id,
                unit_price=total,
            ),
        ],
    )


def _followup_ctx(
    message: str,
    *,
    intent_name: str = "general",
    prep: OrderPreparationState | None = None,
    inbound_metadata: Dict[str, Any] | None = None,
    product_focus: Dict[str, Any] | None = None,
) -> BrainContext:
    prep = prep or _active_prep()
    first = (prep.line_items or [{}])[0]
    focus = product_focus or {
        "id": first.get("product_id") or prep.product_id,
        "title": first.get("product_name"),
        "price": prep.catalog_checkout_total,
        "from_native_catalog_order": True,
        "product_retailer_id": first.get("product_retailer_id"),
    }
    state = MerchantConversationState(stage="ordering", turn=6, greeted=True)
    state.order_prep = prep
    state.current_product_focus = focus
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        intent=Intent(name=intent_name, confidence=0.85, raw_message=message),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": dict(inbound_metadata or {})},
    )


def _catalog_order_ctx() -> BrainContext:
    msg = (
        "[طلب كتالوج من العميل]\n"
        "عدد أسطر الطلب: 1\n"
        "إجمالي الكمية: 1\n"
        "الإجمالي: 126 SAR\n"
        "رمز المنتج (SKU): 86bqzca62a\n"
        "ملاحظة: العميل أرسل طلبًا من كتالوج واتساب."
    )
    meta = {
        "source_type": "catalog_order",
        "product_items": [
            {
                "product_retailer_id": "86bqzca62a",
                "quantity": 1,
                "item_price": 126.0,
                "currency": "SAR",
            },
        ],
        "product_names": ["250 جرام عسل سمر الحجاز"],
        "total_price": 126.0,
        "currency": "SAR",
    }
    state = MerchantConversationState(stage="ordering", turn=3)
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=msg,
        intent=Intent(name="start_order", confidence=0.9, raw_message=msg),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": meta},
    )


def _prep_snapshot(prep: OrderPreparationState) -> Dict[str, Any]:
    return {
        "product_id": prep.product_id,
        "checkout_channel": prep.checkout_channel,
        "catalog_checkout_total": prep.catalog_checkout_total,
        "catalog_line_items_authoritative": prep.catalog_line_items_authoritative,
        "missing_fields": list(prep.missing_fields or []),
        "line_items": copy.deepcopy(list(prep.line_items or [])),
    }


def _location_payload() -> Dict[str, Any]:
    return {
        "source_type": "location",
        "location": {"latitude": 24.7136, "longitude": 46.6753},
    }


def _tenant_bank_methods() -> MerchantPaymentMethods:
    return MerchantPaymentMethods(
        bank_transfer_enabled=True,
        cash_on_delivery_enabled=False,
        moyasar_enabled=False,
        moyasar_checkout_ready=False,
        manual_payment_enabled=False,
        available_methods=[PAYMENT_METHOD_BANK_TRANSFER],
        source="tenant_settings",
    )


def _tenant_cod_only_methods() -> MerchantPaymentMethods:
    return MerchantPaymentMethods(
        bank_transfer_enabled=False,
        cash_on_delivery_enabled=True,
        moyasar_enabled=False,
        moyasar_checkout_ready=False,
        manual_payment_enabled=False,
        available_methods=[PAYMENT_METHOD_CASH_ON_DELIVERY],
        source="tenant_settings",
    )


def _patch_tenant_payments(
    monkeypatch: pytest.MonkeyPatch,
    methods: MerchantPaymentMethods,
) -> None:
    monkeypatch.setattr(
        "core.merchant_payment_methods.load_merchant_payment_methods",
        lambda db, tenant_id: methods,
    )


def _with_tenant_db(ctx: BrainContext) -> BrainContext:
    ctx._db = object()
    return ctx


def _assert_checkout_owner_no_state_preserved(ctx: BrainContext, before: Dict[str, Any]) -> None:
    assert is_active_catalog_checkout(ctx) is True
    assert current_turn_continues_catalog_checkout(ctx) is False
    assert try_active_catalog_checkout_continue_decision(ctx) is None
    contract = build_commerce_turn_contract(ctx, db=None)
    assert contract.known_facts.get("active_checkout_context_available") is True
    assert contract.known_facts.get("current_turn_checkout_owner") is not True
    assert contract.known_facts.get("checkout_owner_active") is not True
    assert contract.action_to_execute != ACTION_PROPOSE_DRAFT_ORDER
    after = _prep_snapshot(ctx.state.order_prep)
    assert after == before
    assert after["catalog_line_items_authoritative"] is True
    assert after["line_items"]
    assert after["catalog_checkout_total"] is not None


class TestActiveCheckoutContextIsNotTurnOwnership:
    def test_followup_unrelated_owner_does_not_become_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("سؤال غير متعلق بالدفع", intent_name="ask_store_info")
        assert is_active_catalog_checkout(ctx) is True
        assert current_turn_continues_catalog_checkout(ctx) is False
        assert try_active_catalog_checkout_continue_decision(ctx) is None

        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("active_checkout_context_available") is True
        assert contract.known_facts.get("active_catalog_checkout") is True
        assert contract.known_facts.get("current_turn_checkout_owner") is not True
        assert contract.known_facts.get("checkout_owner_active") is not True
        assert contract.action_to_execute != ACTION_PROPOSE_DRAFT_ORDER
        assert ctx.state.order_prep.missing_fields == ["payment_method"]
        assert contract.next_goal != "collect_payment_method_for_whatsapp_order"

        raw = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "store_information"},
            reason="structural store-info owner",
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        backup = maybe_enforce_catalog_order_continue_checkout(ctx, raw)
        assert enforced.action == ACTION_LLM_REPLY
        assert backup.action == ACTION_LLM_REPLY

    def test_generic_catalog_checkout_also_stays_resumable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        prep = _active_prep(
            product_id="shoe-1",
            product_name="حذاء رياضي أبيض",
            retailer_id="sku-white-sneaker",
            total=189.0,
        )
        ctx = _followup_ctx("سؤال عن المتجر", prep=prep, intent_name="ask_store_info")
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("active_checkout_context_available") is True
        assert contract.known_facts.get("current_turn_checkout_owner") is not True
        raw = Decision(action=ACTION_LLM_REPLY, args={"topic": "store_information"})
        assert maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw).action == (
            ACTION_LLM_REPLY
        )


class TestCurrentCatalogOrderStartPreserved:
    def test_current_native_catalog_order_still_starts_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_order_ctx()
        decision = try_catalog_order_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("continue_checkout") is True
        assert decision.args.get("skip_product_discovery") is True
        product = decision.args.get("product") or {}
        assert product.get("product_retailer_id") == "86bqzca62a"
        assert product.get("price") == 126.0

        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"source": "top_products"})
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER
        assert contract.known_facts.get("catalog_order_current_turn") is True


class TestCheckoutResumesWhenCurrentTurnOwns:
    def test_payment_method_choice_resumes_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        _patch_tenant_payments(monkeypatch, _tenant_bank_methods())
        ctx = _with_tenant_db(_followup_ctx("تحويل بنكي", intent_name="general"))
        assert current_turn_continues_catalog_checkout(ctx) is True
        decision = try_active_catalog_checkout_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("current_turn_checkout_owner") is True
        assert contract.action_to_execute == ACTION_PROPOSE_DRAFT_ORDER

    def test_address_on_file_claim_resumes_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("المدينة والعنوان عندكم مسجل")
        assert current_turn_continues_catalog_checkout(ctx) is True
        decision = try_active_catalog_checkout_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_same_order_confirmation_resumes_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("نفس الطلب")
        assert current_turn_continues_catalog_checkout(ctx) is True
        decision = try_active_catalog_checkout_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_delivery_address_code_resumes_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        prep = _active_prep(missing=["delivery_address", "payment_method"])
        ctx = _followup_ctx("RRRD1234", prep=prep)
        assert current_turn_continues_catalog_checkout(ctx) is True
        decision = try_active_catalog_checkout_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


class TestLlmReplyNotOverriddenByStaleCheckout:
    def test_legitimate_llm_reply_survives_active_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("موضوع آخر", intent_name="ask_owner_contact")
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "staff_contact", "turn_owner": "support"},
            reason="support owner",
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        backup = maybe_enforce_catalog_order_continue_checkout(ctx, raw)
        assert enforced.action == ACTION_LLM_REPLY
        assert backup.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "staff_contact"


class TestArbiterSupportOwnerNotReplaced:
    def test_support_llm_reply_not_replaced_by_checkout_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("طلب تواصل مع الموظف", intent_name="ask_owner_contact")
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "existing_order_support",
                "turn_owner": "support",
                "response_goal": "acknowledge_issue_ask_for_evidence_do_not_continue_checkout",
            },
            reason="turn_arbiter support",
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_LLM_REPLY
        assert maybe_enforce_catalog_order_continue_checkout(ctx, raw).action == ACTION_LLM_REPLY


class TestLiveShapedCheckoutDoesNotSteal:
    @pytest.mark.parametrize(
        ("message", "intent_name", "pre_action"),
        [
            ("وين موقعكم ؟", "ask_store_info", ACTION_LLM_REPLY),
            ("طيب مالقيت احد", "general", ACTION_LLM_REPLY),
            ("ابي رقم العامل", "ask_owner_contact", ACTION_LLM_REPLY),
            ("رقم الموظف", "ask_owner_contact", ACTION_LLM_REPLY),
            ("الب المتجر الإلكتروني", "start_order", ACTION_LLM_REPLY),
        ],
    )
    def test_checkout_does_not_steal_unrelated_pre_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
        message: str,
        intent_name: str,
        pre_action: str,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx(message, intent_name=intent_name)
        before = _prep_snapshot(ctx.state.order_prep)
        pre = Decision(action=pre_action, args={"topic": "unrelated_current_turn"})
        contract = build_commerce_turn_contract(ctx, db=None)
        post_contract = maybe_enforce_commerce_turn_contract_decision(ctx, contract, pre)
        post_backup = maybe_enforce_catalog_order_continue_checkout(ctx, pre)
        assert try_active_catalog_checkout_continue_decision(ctx) is None
        assert post_contract.action == pre.action
        assert post_backup.action == pre.action
        after = _prep_snapshot(ctx.state.order_prep)
        assert after == before
        assert after["missing_fields"] == ["payment_method"]
        assert after["catalog_line_items_authoritative"] is True
        assert after["catalog_checkout_total"] == 126.0


class TestStatePreservedDuringInterruption:
    def test_catalog_state_survives_unrelated_then_resumes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        start = try_catalog_order_continue_decision(_catalog_order_ctx())
        assert start is not None
        assert start.action == ACTION_PROPOSE_DRAFT_ORDER

        prep = _active_prep()
        snapshot = _prep_snapshot(prep)
        interrupt_ctx = _followup_ctx("وين موقعكم ؟", intent_name="ask_store_info", prep=prep)
        raw = Decision(action=ACTION_LLM_REPLY, args={"topic": "store_information"})
        contract = build_commerce_turn_contract(interrupt_ctx, db=None)
        post = maybe_enforce_commerce_turn_contract_decision(interrupt_ctx, contract, raw)
        assert post.action == ACTION_LLM_REPLY
        assert _prep_snapshot(prep) == snapshot
        assert prep.missing_fields == ["payment_method"]
        assert prep.catalog_checkout_total == 126.0
        assert prep.line_items[0]["product_retailer_id"] == "86bqzca62a"

        resume_ctx = _with_tenant_db(_followup_ctx("تحويل بنكي", prep=prep))
        _patch_tenant_payments(monkeypatch, _tenant_bank_methods())
        resume = try_active_catalog_checkout_continue_decision(resume_ctx)
        assert resume is not None
        assert resume.action == ACTION_PROPOSE_DRAFT_ORDER
        product = resume.args.get("product") or {}
        assert product.get("product_retailer_id") == "86bqzca62a"
        assert product.get("price") == 126.0
        assert _prep_snapshot(prep)["line_items"] == snapshot["line_items"]


class TestOwnerRemediationNarrowCheckoutOwnership:
    def test_a_awaiting_receipt_unrelated_general_is_not_owner(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        prep = _active_prep(missing=["payment_method"])
        prep.awaiting_payment_receipt = True
        ctx = _followup_ctx("سؤال عام عن المتجر", intent_name="general", prep=prep)
        before = _prep_snapshot(prep)
        _assert_checkout_owner_no_state_preserved(ctx, before)

    def test_b_awaiting_option_unrelated_support_is_not_owner(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        prep = _active_prep(missing=["payment_method"])
        prep.awaiting_option_confirmation = True
        ctx = _followup_ctx("ابي رقم العامل", intent_name="ask_owner_contact", prep=prep)
        before = _prep_snapshot(prep)
        _assert_checkout_owner_no_state_preserved(ctx, before)
        raw = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "staff_contact", "turn_owner": "support"},
            reason="support owner",
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        assert maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw).action == (
            ACTION_LLM_REPLY
        )
        assert maybe_enforce_catalog_order_continue_checkout(ctx, raw).action == ACTION_LLM_REPLY

    def test_c_tenant_enabled_bank_choice_owns_payment_slot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        _patch_tenant_payments(monkeypatch, _tenant_bank_methods())
        ctx = _with_tenant_db(_followup_ctx("تحويل بنكي", intent_name="general"))
        assert is_active_catalog_checkout(ctx) is True
        assert current_turn_continues_catalog_checkout(ctx) is True
        decision = try_active_catalog_checkout_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("current_turn_checkout_owner") is True

    def test_d_disabled_or_fallback_payment_does_not_own(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        fallback_ctx = _with_tenant_db(_followup_ctx("تحويل بنكي"))
        before = _prep_snapshot(fallback_ctx.state.order_prep)
        _assert_checkout_owner_no_state_preserved(fallback_ctx, before)

        _patch_tenant_payments(monkeypatch, _tenant_cod_only_methods())
        disabled_ctx = _with_tenant_db(_followup_ctx("تحويل بنكي"))
        before_disabled = _prep_snapshot(disabled_ctx.state.order_prep)
        _assert_checkout_owner_no_state_preserved(disabled_ctx, before_disabled)

    def test_e_payment_only_plus_location_payload_is_not_owner(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        prep = _active_prep(missing=["payment_method"])
        ctx = _followup_ctx("", prep=prep, inbound_metadata=_location_payload())
        before = _prep_snapshot(prep)
        _assert_checkout_owner_no_state_preserved(ctx, before)

    def test_f_delivery_address_plus_location_payload_owns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        prep = _active_prep(missing=["delivery_address"])
        ctx = _followup_ctx("", prep=prep, inbound_metadata=_location_payload())
        assert current_turn_continues_catalog_checkout(ctx) is True
        decision = try_active_catalog_checkout_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("current_turn_checkout_owner") is True

    def test_g_current_native_catalog_order_still_owns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_order_ctx()
        assert current_turn_continues_catalog_checkout(ctx) is True
        decision = try_catalog_order_continue_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("catalog_order_current_turn") is True
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"source": "top_products"})
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_h_order_state_survives_all_no_owner_cases(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        cases: List[BrainContext] = []

        receipt_prep = _active_prep()
        receipt_prep.awaiting_payment_receipt = True
        cases.append(_followup_ctx("سؤال عام عن المتجر", intent_name="general", prep=receipt_prep))

        option_prep = _active_prep()
        option_prep.awaiting_option_confirmation = True
        cases.append(
            _followup_ctx("ابي رقم العامل", intent_name="ask_owner_contact", prep=option_prep)
        )

        cases.append(_with_tenant_db(_followup_ctx("تحويل بنكي")))
        cases.append(
            _followup_ctx(
                "",
                prep=_active_prep(missing=["payment_method"]),
                inbound_metadata=_location_payload(),
            )
        )

        for ctx in cases:
            before = _prep_snapshot(ctx.state.order_prep)
            _assert_checkout_owner_no_state_preserved(ctx, before)
            assert before["line_items"][0]["product_retailer_id"] == "86bqzca62a"
            assert before["catalog_checkout_total"] == 126.0
            assert before["missing_fields"] == ["payment_method"]
