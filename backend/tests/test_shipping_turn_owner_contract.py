"""Pre-order ask_shipping turn-owner contract — product-claim guard survival."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.turn_owner_contract import (  # noqa: E402
    TOPIC_SHIPPING,
    build_turn_owner_contract,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_ASK_SHIPPING,
    MerchantConversationState,
    OrderPreparationState,
)


def _pre_order_shipping_ctx(
    message: str = "بكم الشحن للرياض؟",
    *,
    city: str = "الرياض",
) -> BrainContext:
    state = MerchantConversationState()
    state.order_prep = OrderPreparationState()
    if city:
        state.order_prep.city = city
    return BrainContext(
        tenant_id=42,
        customer_phone="+966500000099",
        message=message,
        intent=Intent(
            name=INTENT_ASK_SHIPPING,
            confidence=0.92,
            slots={"city": city} if city else {},
            raw_message=message,
            extraction_method="rules",
        ),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={},
    )


def _ungrounded_shipping_fee_evidence() -> ProductClaimGroundingEvidence:
    return ProductClaimGroundingEvidence(
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


def test_pre_order_ask_shipping_decision_establishes_shipping_turn_owner() -> None:
    decision = DefaultDecisionEngine().decide(_pre_order_shipping_ctx())
    contract = build_turn_owner_contract(decision)

    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == TOPIC_SHIPPING
    assert decision.args.get("topic_hint") == "shipping"
    assert decision.args.get("topic") != "shipping"

    assert contract.owner == "commerce_order_channel"
    assert contract.topic == TOPIC_SHIPPING
    assert contract.protected_final_reply is True
    assert contract.block_product_benefit_rewrite is True
    assert contract.block_medical_claim_rewrite is True


def test_shipping_contract_preserves_ungrounded_fee_reply_in_product_claim_guard(
    monkeypatch,
) -> None:
    def _fake_evidence(*_args, **_kwargs):
        return _ungrounded_shipping_fee_evidence()

    monkeypatch.setattr(
        "modules.ai.brain.postprocess.product_claim_grounding_guard."
        "build_product_claim_grounding_evidence",
        _fake_evidence,
    )

    decision = DefaultDecisionEngine().decide(_pre_order_shipping_ctx())
    contract = build_turn_owner_contract(decision)
    composed = "تكلفة الشحن للرياض 25 ريال."

    result = apply_product_claim_grounding_guard(
        reply=composed,
        inbound_metadata={"turn_owner_contract": contract.to_metadata()},
    )

    assert result.replaced is False
    assert result.reply == composed
    assert result.action == "blocked_protected_final_reply"
    assert result.would_rewrite is True
    assert "ungrounded_price" in result.blocked_claims
    assert "ما ظهر عندي سعر مؤكد من الكتالوج" not in result.reply


def test_ungrounded_fee_reply_without_shipping_contract_still_rewrites(
    monkeypatch,
) -> None:
    def _fake_evidence(*_args, **_kwargs):
        return _ungrounded_shipping_fee_evidence()

    monkeypatch.setattr(
        "modules.ai.brain.postprocess.product_claim_grounding_guard."
        "build_product_claim_grounding_evidence",
        _fake_evidence,
    )

    composed = "تكلفة الشحن للرياض 25 ريال."

    result = apply_product_claim_grounding_guard(reply=composed)

    assert result.replaced is True
    assert result.reply != composed
    assert "ungrounded_price" in result.blocked_claims
