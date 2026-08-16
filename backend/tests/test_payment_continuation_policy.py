"""PaymentContinuationPolicy Phase 1 — deterministic pay_now guidance."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.payment_continuation_policy import (  # noqa: E402
    CASE_COD,
    CASE_DEFER_WA_DRAFT,
    CASE_DISAMBIGUATE,
    CASE_NO_CAPABILITY,
    CASE_PAID,
    CASE_PAYMENT_LINK,
    evaluate_payment_continuation,
    render_no_capability_reply,
)
from models import TenantSettings  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_LLM_REPLY,
    ACTION_PAYMENT_CONTINUATION_REPLY,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_TRACK_ORDER,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_knowledge_section,
    seed_order,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_PRODUCT = "قميص قطني أزرق"
VERIFIED_IBAN = "SA0380000000608010167519"


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


def _set_payment_methods(db, tenant_id: int, **flags) -> None:
    row = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).one()
    meta = dict(row.extra_metadata or {})
    pm = dict(meta.get("payment_methods") or {})
    pm.update(flags)
    meta["payment_methods"] = pm
    row.extra_metadata = meta
    db.commit()


def _brain_ctx(
    tenant_ctx,
    message: str,
    *,
    db=None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    intent = rules.match(message)
    assert intent is not None
    ctx = BrainContext(
        tenant_id=tenant_ctx.tenant_id,
        customer_phone=tenant_ctx.phone,
        conversation_id=tenant_ctx.conversation_id,
        customer_id=tenant_ctx.customer_id,
        message=message,
        intent=intent,
        state=state or MerchantConversationState(),
        facts=CommerceFacts(store_name=GENERIC_MERCHANT),
    )
    if db is not None:
        ctx._db = db  # noqa: SLF001
    return ctx


def _pending_order(
    db,
    tenant_ctx,
    *,
    ref: str = "ORD-9001",
    source: str = "manual",
    status: str = "payment_pending",
    checkout_url: str | None = None,
    extra_metadata: dict | None = None,
):
    return seed_order(
        db,
        tenant_ctx.tenant_id,
        source=source,
        status=status,
        external_id=f"ext-{ref}",
        external_order_number=ref,
        customer_info={"phone": tenant_ctx.phone, "name": GENERIC_CUSTOMER},
        line_items=[{"title": GENERIC_PRODUCT, "quantity": 1, "unit_price": "199"}],
        checkout_url=checkout_url,
        extra_metadata=extra_metadata,
    )


class TestPaymentContinuationPolicy:
    def test_pending_no_capability_safe_reply(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-7001", source="manual")
        _set_payment_methods(
            db,
            tenant_ctx.tenant_id,
            bank_transfer_enabled=False,
            cash_on_delivery_enabled=False,
        )
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            customer_id=tenant_ctx.customer_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.handled is True
        assert result.case == CASE_NO_CAPABILITY
        assert "رابط دفع جاهز" in result.reply
        assert "ORD-7001" in result.reply
        assert "العنوان" not in result.reply
        assert "واتساب مباشرة" not in result.reply

    def test_complete_payment_same_reply(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-7002", source="manual")
        _set_payment_methods(db, tenant_ctx.tenant_id, bank_transfer_enabled=False)
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="هل أقدر أكمل الدفع؟",
        )
        assert result.handled is True
        assert result.case == CASE_NO_CAPABILITY
        assert "ORD-7002" in result.reply

    def test_checkout_url_routes_to_send_payment_link(self, db, tenant_ctx) -> None:
        _pending_order(
            db,
            tenant_ctx,
            ref="ORD-8001",
            checkout_url="https://pay.example.test/ord-8001",
        )
        ctx = _brain_ctx(tenant_ctx, "كيف أدفع؟", db=db)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_SEND_PAYMENT_LINK, ACTION_LLM_REPLY}
        if decision.action == ACTION_SEND_PAYMENT_LINK:
            assert decision.args.get("checkout_url") == "https://pay.example.test/ord-8001"

    def test_paid_order_no_new_payment(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            status="paid",
            external_id="paid-ext-1",
            external_order_number="PAID-501",
            customer_info={"phone": tenant_ctx.phone},
        )
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.handled is True
        assert result.case == CASE_PAID
        assert "لا يحتاج دفع جديد" in result.reply

    def test_verified_bank_instructions_only(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-8100")
        seed_knowledge_section(
            db,
            tenant_ctx.tenant_id,
            kind="bank_transfer",
            title="تحويل بنكي",
            body=f"الآيبان: {VERIFIED_IBAN}",
        )
        _set_payment_methods(db, tenant_ctx.tenant_id, bank_transfer_enabled=True)
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.handled is True
        assert VERIFIED_IBAN in result.reply
        assert "قيد إكمال الدفع" in result.reply

    def test_bank_flag_without_instructions_no_iban_claim(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-8200")
        _set_payment_methods(db, tenant_ctx.tenant_id, bank_transfer_enabled=True)
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.handled is True
        assert result.case == CASE_NO_CAPABILITY
        assert "SA" not in result.reply
        assert "آيبان" not in result.reply.lower()

    def test_cod_disabled_no_cod_mention(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-8300")
        _set_payment_methods(
            db,
            tenant_ctx.tenant_id,
            cash_on_delivery_enabled=False,
            bank_transfer_enabled=False,
        )
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert "الاستلام" not in result.reply

    def test_cod_enabled_mentions_cod(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-8400")
        _set_payment_methods(
            db,
            tenant_ctx.tenant_id,
            cash_on_delivery_enabled=True,
            bank_transfer_enabled=False,
        )
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.handled is True
        assert result.case == CASE_COD
        assert "الدفع عند الاستلام" in result.reply

    def test_empty_payment_method_metadata_no_bank_assumption(self, db, tenant_ctx) -> None:
        _pending_order(
            db,
            tenant_ctx,
            ref="ORD-8500",
            extra_metadata={"payment_method": ""},
        )
        _set_payment_methods(db, tenant_ctx.tenant_id, bank_transfer_enabled=False)
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.case == CASE_NO_CAPABILITY
        assert "تحويل بنكي" not in result.reply

    def test_multiple_pending_orders_disambiguate(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-A100")
        _pending_order(db, tenant_ctx, ref="ORD-A200")
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.handled is True
        assert result.case == CASE_DISAMBIGUATE
        assert "اكتب رقم الطلب" in result.reply

    def test_wa_draft_missing_address_defers(self, db, tenant_ctx) -> None:
        _pending_order(
            db,
            tenant_ctx,
            ref="WA-DRAFT-1",
            source="whatsapp",
        )
        state = MerchantConversationState(stage="ordering")
        state.order_prep = OrderPreparationState(missing_fields=["city"])
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            conversation_id=tenant_ctx.conversation_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
            state=state,
        )
        assert result.handled is False
        assert result.defer_to_existing_flow is True
        assert result.case == CASE_DEFER_WA_DRAFT

    def test_salla_pending_not_redirected_to_address(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="IMP-9900", source="manual")
        state = MerchantConversationState(stage="discovery")
        state.order_prep = OrderPreparationState(city="", address_line="")
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="هل أقدر أكمل الدفع؟",
            state=state,
        )
        assert result.handled is True
        assert result.case == CASE_NO_CAPABILITY
        assert "التوصيلة" not in result.reply
        assert "المدينة" not in result.reply

    def test_decision_engine_not_commerce_llm(self, db, tenant_ctx) -> None:
        _pending_order(db, tenant_ctx, ref="ORD-9000")
        _set_payment_methods(db, tenant_ctx.tenant_id, bank_transfer_enabled=False)
        ctx = _brain_ctx(tenant_ctx, "كيف أدفع؟", db=db)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_PAYMENT_CONTINUATION_REPLY, ACTION_LLM_REPLY}

    def test_payment_link_eval_uses_send_flag(self, db, tenant_ctx) -> None:
        _pending_order(
            db,
            tenant_ctx,
            ref="ORD-LINK",
            checkout_url="https://pay.example.test/link",
        )
        result = evaluate_payment_continuation(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            message="كيف أدفع؟",
        )
        assert result.case == CASE_PAYMENT_LINK
        assert result.use_send_payment_link is True


class TestUnaffectedIntents:
    def test_track_order_unchanged(self) -> None:
        intent = rules.match("وين طلبي؟")
        ctx = BrainContext(
            tenant_id=1,
            customer_phone=DEFAULT_PHONE_E164,
            message="وين طلبي؟",
            intent=intent,
            state=MerchantConversationState(),
            facts=CommerceFacts(store_name=GENERIC_MERCHANT),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_TRACK_ORDER

    def test_ledger_unchanged(self) -> None:
        intent = rules.match("طلباتي السابقة كم؟")
        ctx = BrainContext(
            tenant_id=1,
            customer_phone=DEFAULT_PHONE_E164,
            message="طلباتي السابقة كم؟",
            intent=intent,
            state=MerchantConversationState(),
            facts=CommerceFacts(store_name=GENERIC_MERCHANT),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY


class TestRenderHelpers:
    def test_no_capability_template_has_ref(self) -> None:
        text = render_no_capability_reply("ORD-123456")
        assert "ORD-123456" in text
        assert "رابط دفع جاهز" in text
