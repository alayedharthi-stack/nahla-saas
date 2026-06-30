"""Health advisory final-route enforcement after PR #365."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.commerce_order_channel_owner import (  # noqa: E402
    TOPIC_COLD_SHIPPING_INQUIRY,
    try_commerce_order_channel_decision,
)
from modules.ai.brain.commerce.health_advisory_product_safety import (  # noqa: E402
    TOPIC_HEALTH_ADVISORY,
)
from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: E402
    CatalogDeliveryKind,
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE, ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)


HEALTH_MSG = (
    "عندي أطفال عندهم تأخر نطق واحتمال طيف توحد ومشاكل أمعاء "
    "وش تنصحني من منتجاتكم؟"
)

CATALOG_FALLBACK_MARKERS = (
    "أقدر أعرض لك الخيارات المؤكدة",
    "الكتالوج",
    "تبغاني أرسل",
    "وش المنتج",
    "كم الكمية",
    "وش المدينة",
)


def _intent_for(message: str) -> Intent:
    return intent_rules.match(message) or Intent(
        name="general",
        confidence=0.50,
        slots={},
        raw_message=message,
        extraction_method="fallback",
    )


def _ctx(message: str, *, state: MerchantConversationState | None = None) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="+966542980511",
        message=message,
        intent=_intent_for(message),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(has_products=True),
    )


def _decision_meta(decision) -> dict:
    args = dict(decision.args or {})
    meta = {"decision_topic": str(args.get("topic") or "")}
    for flag in (
        "block_catalog_push",
        "block_staff_contact",
        "block_showroom_location",
        "pause_order_slot_collection",
    ):
        if flag in args:
            meta[flag] = bool(args.get(flag))
    return meta


def test_health_turn_does_not_become_catalog_reply() -> None:
    state = MerchantConversationState()
    decision = DefaultDecisionEngine().decide(_ctx(HEALTH_MSG, state=state))

    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == TOPIC_HEALTH_ADVISORY
    assert decision.args.get("block_catalog_push") is True
    assert decision.args.get("pause_order_slot_collection") is True
    assert "treats_autism" in (decision.args.get("forbidden_claims") or [])

    reply = (
        "الله يشفيهم ويطمنكم عليهم. ما أقدر أوصي بمنتج كعلاج أو بديل "
        "لاستشارة الطبيب، وأقدر أوضح المنتجات كغذاء فقط إذا حبيت."
    )
    pcgg = apply_product_claim_grounding_guard(
        reply=reply,
        order_state=state,
        inbound_metadata=_decision_meta(decision),
    )
    cpgg = apply_catalog_product_grounding_guard(
        reply=pcgg.reply,
        inbound_text=HEALTH_MSG,
        executor_products=[{"title": "عسل سدر بلدي"}],
        order_state=state,
        inbound_metadata=_decision_meta(decision),
    )

    assert pcgg.replaced is False
    assert cpgg.replaced is False
    assert all(marker not in cpgg.reply for marker in CATALOG_FALLBACK_MARKERS)


def test_block_catalog_push_respected_by_catalog_grounding_guard() -> None:
    original = (
        "الله يشفيهم. لا أقدر أوصي بعسل القطف كعلاج، والمتابعة مع المختص أفضل."
    )
    result = apply_catalog_product_grounding_guard(
        reply=original,
        inbound_text=HEALTH_MSG,
        executor_products=[{"title": "عسل سدر بلدي"}],
        inbound_metadata={
            "decision_topic": TOPIC_HEALTH_ADVISORY,
            "block_catalog_push": True,
        },
    )

    assert result.replaced is False
    assert result.action == "allowed_catalog_push_blocked"
    assert result.reply == original
    assert "أقدر أعرض لك الخيارات المؤكدة" not in result.reply


def test_shipping_after_health_context_remains_shipping() -> None:
    state = MerchantConversationState()
    health_decision = DefaultDecisionEngine().decide(_ctx(HEALTH_MSG, state=state))
    assert health_decision.args.get("topic") == TOPIC_HEALTH_ADVISORY

    shipping_decision = DefaultDecisionEngine().decide(_ctx("مبرد التوصيل؟", state=state))
    assert shipping_decision.args.get("topic") == TOPIC_COLD_SHIPPING_INQUIRY

    reply = "بالنسبة للتوصيل المبرد للأطفال، أحتاج المدينة عشان أتأكد لك من توفره."
    result = apply_product_claim_grounding_guard(
        reply=reply,
        order_state=state,
        inbound_metadata=_decision_meta(shipping_decision),
    )

    assert result.replaced is False
    assert "ما عندي وصف موثق من المتجر عن فوائد صحية" not in result.reply
    assert "المدينة" in result.reply


def test_product_benefit_guard_ignores_shipping_current_turn_health_context() -> None:
    state = MerchantConversationState()
    DefaultDecisionEngine().decide(_ctx(HEALTH_MSG, state=state))

    result = apply_product_claim_grounding_guard(
        reply="التوصيل المبرد للأطفال يعتمد على المدينة وسياسة شركة الشحن.",
        order_state=state,
        inbound_metadata={
            "decision_topic": TOPIC_COLD_SHIPPING_INQUIRY,
            "inbound_text": "التوصيل مبرد؟",
        },
    )

    assert result.replaced is False
    assert "فوائد صحية محددة" not in result.reply


def test_ce2_catalog_send_unaffected_without_health_turn() -> None:
    decision = try_commerce_entry_catalog_decision(_ctx("أرسل الكتالوج"))

    assert decision is not None
    assert decision.action == ACTION_CATALOG_NAVIGATE
    assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value


def test_ce3_cold_shipping_unaffected_fresh_conversation() -> None:
    decision = try_commerce_order_channel_decision(_ctx("مبرد التوصيل؟"))

    assert decision is not None
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == TOPIC_COLD_SHIPPING_INQUIRY


def test_health_followup_still_health_not_catalog() -> None:
    state = MerchantConversationState()
    DefaultDecisionEngine().decide(_ctx(HEALTH_MSG, state=state))

    decision = DefaultDecisionEngine().decide(
        _ctx("أبغى شي من عندك مع غذاء ملكات", state=state),
    )

    assert decision.args.get("topic") == TOPIC_HEALTH_ADVISORY
    assert decision.args.get("block_catalog_push") is True
    assert "therapy_mix_instruction" in (decision.args.get("forbidden_claims") or [])

    result = apply_catalog_product_grounding_guard(
        reply="ما أقدر أوصي بخلطة علاجية للأطفال، والأفضل مراجعة المختص.",
        inbound_text="أبغى شي من عندك مع غذاء ملكات",
        executor_products=[{"title": "غذاء ملكات"}],
        inbound_metadata=_decision_meta(decision),
    )
    assert result.replaced is False
    assert "الكتالوج" not in result.reply
