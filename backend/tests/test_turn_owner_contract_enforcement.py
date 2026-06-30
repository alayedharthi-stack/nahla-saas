"""PR-D7 — current-turn owner contract enforcement."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: E402
    CatalogDeliveryKind,
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.commerce.commerce_order_channel_owner import (  # noqa: E402
    TOPIC_COLD_SHIPPING_INQUIRY,
    TOPIC_STOREFRONT_SELF_CHECKOUT,
)
from modules.ai.brain.commerce.health_advisory_product_safety import (  # noqa: E402
    TOPIC_HEALTH_ADVISORY,
)
from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    TOPIC_PRODUCT_KNOWLEDGE_FACTS,
)
from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_product_ordering_prompt,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.turn_owner_contract import (  # noqa: E402
    attach_turn_owner_contract,
    build_turn_owner_contract,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


HEALTH_MSG = (
    "عندي أطفال عندهم تأخر نطق واحتمال طيف توحد ومشاكل أمعاء "
    "وش تنصحني من منتجاتكم؟"
)


def _intent_for(message: str) -> Intent:
    return intent_rules.match(message) or Intent(
        name="general",
        confidence=0.50,
        slots={},
        raw_message=message,
        extraction_method="fallback",
    )


def _ctx(
    message: str,
    *,
    state: MerchantConversationState | None = None,
    profile: dict | None = None,
    facts: CommerceFacts | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="+966542980511",
        message=message,
        intent=_intent_for(message),
        state=state or MerchantConversationState(),
        facts=facts or CommerceFacts(has_products=True, orderable=True),
        profile=profile or {},
    )


def _claim_evidence(**overrides) -> ProductClaimGroundingEvidence:
    base = dict(
        grounded_prices=frozenset({120}),
        grounded_text_corpus="",
        available_products=(),
        unavailable_products=(),
        catalog_products_this_turn=False,
        catalog_miss_this_turn=False,
        recent_catalog_miss=False,
        recent_no_synced=False,
        has_checkout_catalog=True,
        executor_product_ids=frozenset(),
        kb_section_ids=frozenset(),
    )
    base.update(overrides)
    return ProductClaimGroundingEvidence(**base)


def test_health_contract_blocks_catalog_grounding_rewrite() -> None:
    state = MerchantConversationState()
    decision = DefaultDecisionEngine().decide(_ctx(HEALTH_MSG, state=state))
    contract = build_turn_owner_contract(decision)

    assert decision.args.get("topic") == TOPIC_HEALTH_ADVISORY
    assert contract.block_catalog_push is True

    result = apply_catalog_product_grounding_guard(
        reply="الله يشفيهم. لا أقدر أوصي بعسل القطف كعلاج طبي.",
        inbound_text=HEALTH_MSG,
        executor_products=[{"title": "عسل سدر بلدي"}],
        order_state=state,
        inbound_metadata={"turn_owner_contract": contract.to_metadata()},
    )

    assert result.replaced is False
    assert "أقدر أعرض لك الخيارات المؤكدة" not in result.reply
    assert "الكتالوج" not in result.reply


def test_cold_shipping_contract_blocks_product_benefit_fallback_after_health() -> None:
    state = MerchantConversationState()
    health_decision = DefaultDecisionEngine().decide(_ctx(HEALTH_MSG, state=state))
    assert health_decision.args.get("topic") == TOPIC_HEALTH_ADVISORY

    shipping_decision = DefaultDecisionEngine().decide(_ctx("مبرد التوصيل؟", state=state))
    contract = build_turn_owner_contract(shipping_decision)

    assert contract.topic == TOPIC_COLD_SHIPPING_INQUIRY
    assert contract.block_product_benefit_rewrite is True
    assert contract.block_medical_claim_rewrite is True

    result = apply_product_claim_grounding_guard(
        reply="التوصيل المبرد للأطفال يعتمد على المدينة وسياسة شركة الشحن.",
        order_state=state,
        inbound_metadata={"turn_owner_contract": contract.to_metadata()},
    )

    assert result.replaced is False
    assert "ما عندي وصف موثق من المتجر عن فوائد صحية" not in result.reply


def test_storefront_contract_blocks_whatsapp_quick_order_prompt() -> None:
    ctx = _ctx("أنا بدخل السلة")
    decision = DefaultDecisionEngine().decide(ctx)
    contract = build_turn_owner_contract(decision)
    attach_turn_owner_contract(ctx, contract)

    assert decision.args.get("topic") == TOPIC_STOREFRONT_SELF_CHECKOUT
    assert contract.block_product_ordering_prompt is True

    prompt = build_product_ordering_prompt(ctx)
    assert prompt == ""
    assert "وش المنتج اللي تبي أجهزه لك" not in prompt
    assert "قبل ما أكمل طلبك" not in prompt


def test_payment_evidence_contract_blocks_catalog_grounding() -> None:
    profile = {
        "inbound_metadata": {
            "normalized_type": "document",
            "has_attached_media": True,
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "receipt_data": {"amount": 120},
        },
    }
    decision = DefaultDecisionEngine().decide(_ctx("", profile=profile))
    contract = build_turn_owner_contract(decision)

    assert contract.topic in {
        "payment_receipt_received",
        "payment_evidence_pending_review",
    }
    assert contract.block_catalog_push is True

    result = apply_catalog_product_grounding_guard(
        reply="عندنا عسل القطف متوفر.",
        inbound_text="",
        executor_products=[{"title": "عسل سدر بلدي"}],
        inbound_metadata={"turn_owner_contract": contract.to_metadata()},
    )
    assert result.replaced is False
    assert "أقدر أعرض لك الخيارات المؤكدة" not in result.reply


def test_catalog_request_contract_does_not_block_catalog_send() -> None:
    decision = try_commerce_entry_catalog_decision(_ctx("أرسل الكتالوج"))
    assert decision is not None

    contract = build_turn_owner_contract(decision)
    assert decision.action == ACTION_CATALOG_NAVIGATE
    assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value
    assert contract.owner == "commerce_entry_catalog"
    assert contract.block_catalog_push is False
    assert contract.block_medical_claim_rewrite is True


def test_ce4_knowledge_contract_blocks_catalog_fallback() -> None:
    state = MerchantConversationState()
    state.current_product_focus = {
        "id": 9,
        "title": "عسل سدر بلدي",
        "price": 120,
    }
    decision = DefaultDecisionEngine().decide(
        _ctx("وش الفرق عن السدر العادي؟", state=state),
    )
    contract = build_turn_owner_contract(decision)

    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS
    assert contract.block_catalog_push is True

    result = apply_catalog_product_grounding_guard(
        reply="السدر البلدي يختلف عن عسل القطف في المرعى والموسم.",
        inbound_text="وش الفرق عن السدر العادي؟",
        executor_products=[{"title": "عسل سدر بلدي"}],
        inbound_metadata={"turn_owner_contract": contract.to_metadata()},
    )
    assert result.replaced is False
    assert "أقدر أعرض لك الخيارات المؤكدة" not in result.reply


def test_order_buy_flow_still_works_with_product_focus() -> None:
    state = MerchantConversationState()
    state.current_product_focus = {
        "id": 9,
        "title": "عسل سدر بلدي",
        "price": 120,
    }
    ctx = _ctx("نبغى كيلوين", state=state)
    ctx.intent = Intent(
        name="start_order",
        confidence=0.95,
        slots={"quantity": 2},
        raw_message="نبغى كيلوين",
        extraction_method="test",
    )
    decision = DefaultDecisionEngine().decide(ctx)
    contract = build_turn_owner_contract(decision)

    assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
    assert contract.owner in {None, "ordering"}


def test_protected_payment_reply_blocks_product_claim_fallback(monkeypatch) -> None:
    def _fake_evidence(*_args, **_kwargs):
        return _claim_evidence(grounded_prices=frozenset({120}))

    monkeypatch.setattr(
        "modules.ai.brain.postprocess.product_claim_grounding_guard."
        "build_product_claim_grounding_evidence",
        _fake_evidence,
    )
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={"topic": "payment_receipt_received"},
    )
    contract = build_turn_owner_contract(decision)

    result = apply_product_claim_grounding_guard(
        reply="تم استلام الإيصال، وسعر المنتج 999 ريال.",
        inbound_metadata={"turn_owner_contract": contract.to_metadata()},
    )

    assert result.replaced is False
    assert result.action == "blocked_protected_final_reply"
    assert result.would_rewrite is True
    assert "ungrounded_price" in result.blocked_claims
    assert "ما ظهر عندي سعر مؤكد من الكتالوج" not in result.reply


def test_protected_reply_quality_does_not_resume_staff_route() -> None:
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={"topic": "health_advisory_product_safety"},
    )
    contract = build_turn_owner_contract(decision)
    original = "تمام 🌷 وصلت رسالتك."

    result = apply_commerce_reply_quality_guard(
        original,
        inbound_text="ما أبغى أمين أنا أبغى أشتري عسل",
        state={
            "order_prep": {
                "city": "الطائف",
                "quantity_label": "نصف كيلو",
                "product_name": "عسل",
            },
        },
        inbound_metadata={"turn_owner_contract": contract.to_metadata()},
    )

    assert result.replaced is False
    assert result.reply == original
    assert result.fallback_kind == ""
