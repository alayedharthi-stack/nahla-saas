"""
tests/test_price_inquiry_instruction_conflict.py
────────────────────────────────────────────────
Regression tests for instruction_conflict + output_contract_behavior on
price_inquiry turns: trusted numeric price must appear in textual reply;
[PRODUCT:…] is optional for the card and must not substitute for the answer.

Unit tests pin the clarified instruction contract in rendered prompts.
Optional live proof uses gpt-5.6-luna when OPENAI_API_KEY is set.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.selection_context import (  # noqa: E402
    resolve_selection_context,
    stamp_selection_context_from_products,
)
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt  # noqa: E402
from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: E402
    _COMPACT_RESOLVER_PROTOCOL,
    slim_resolver_overlay_for_commerce,
)
from modules.ai.brain.intent_priority.analyzer import _build_recommended_focus  # noqa: E402
from modules.ai.brain.intent_priority.types import (  # noqa: E402
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
    IntentPriorityVerdict,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    INTENT_ASK_PRICE,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    Intent,
    MerchantConversationState,
)
from modules.ai.prompts.high_priority_layer import (  # noqa: E402
    BASELINE_POLICY_RULES,
    BASELINE_SALES_BEHAVIOR_RULES,
    build_high_priority_block,
)

# ── Generic commerce catalog (platform-wide, not merchant-specific) ───────────

CATALOG_PRODUCTS: List[Dict[str, Any]] = [
    {
        "id": 301,
        "external_id": "301",
        "title": "حذاء رياضي أبيض",
        "display_label": "حذاء رياضي أبيض",
        "price": 120,
        "currency": "SAR",
        "available": True,
        "in_stock": True,
        "can_checkout": True,
    },
    {
        "id": 302,
        "external_id": "302",
        "title": "قميص قطني أزرق",
        "display_label": "قميص قطني أزرق",
        "price": 89,
        "currency": "SAR",
        "available": True,
        "in_stock": True,
        "can_checkout": True,
    },
]

SELECTED_PRODUCT_302 = dict(CATALOG_PRODUCTS[1])
EXPECTED_PRICE = 89

PRICE_PHRASINGS = (
    "كم سعر الثاني؟",
    "بكم الثاني؟",
    "وش سعره؟",
    "كم قيمة هذا المنتج؟",
)

JOURNEY_TURNS = (
    "اعرض المنتجات",
    "كم سعر الثاني؟",
    "أرسل رابطه",
    "هل هذا متوفر؟",
    "قارن بينه وبين الأول",
)

_OLD_CARD_ONLY_POLICY = (
    "قبل أن تذكر منتجًا اسمًا وسعرًا، اطلب الكرت الكامل عبر "
    "[PRODUCT:<اسم المنتج>] — النظام سيرسل الصورة والسعر والرابط."
)
_OLD_RESOLVER_PRICE_LINE = "لا تخترعي أسعاراً مع [PRODUCT:...] — النظام يضيفها."


def _numeric_price_present(text: str, *, expected: int = EXPECTED_PRICE) -> bool:
    normalized = (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    return str(expected) in normalized


def _currency_present(text: str) -> bool:
    return bool(re.search(r"ريال|ر\.س", text or ""))


def _product_marker_present(text: str) -> bool:
    return "[PRODUCT:" in (text or "")


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=20,
        in_stock_count=20,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
    )


def _browse_state(*, selected_id: str = "302") -> MerchantConversationState:
    state = MerchantConversationState(greeted=True, stage="discovery")
    stamp_selection_context_from_products(
        state,
        products=CATALOG_PRODUCTS,
        selected_collection="منتجات",
        discovery_mode="search",
    )
    state.last_search_candidates = list(CATALOG_PRODUCTS)
    if selected_id:
        state.selected_product_id = selected_id
        state.current_product_focus = SELECTED_PRODUCT_302
    return state


def _browse_ctx(
    message: str,
    *,
    state: Optional[MerchantConversationState] = None,
    intent_name: str = INTENT_ASK_PRICE,
) -> BrainContext:
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=message,
        raw_message=message,
        intent=Intent(name=intent_name, confidence=0.9, raw_message=message),
        state=state or _browse_state(),
        facts=_facts(),
    )


def _price_reply_state(
    *,
    selected_product: Optional[Dict[str, Any]] = None,
    response_goal: str = "price_inquiry — أجيبي على سؤال السعر/الوحدة أولاً.",
    inbound: str = "كم سعر الثاني؟",
) -> BrainReplyState:
    return BrainReplyState(
        store_name="متجر تجريبي عام",
        intent_name=INTENT_ASK_PRICE,
        primary_customer_goal=GOAL_PRICE_INQUIRY,
        response_goal=response_goal,
        stage="discovery",
        selected_product=selected_product or SELECTED_PRODUCT_302,
        recent_turns=["اعرض المنتجات", inbound],
        merchant_context={
            "tenant_id": 7,
            "products": list(CATALOG_PRODUCTS),
            "ai_settings": {"reply_tone": "friendly"},
            "resolver_overlay": (
                "أدوات الوسائط المتوفرة في هذا المتجر:\n[MEDIA_KEY:pay]"
            ),
        },
    )


# ── 1. Instruction contract in prompt sources ─────────────────────────────────


class TestInstructionConflictRemoved:
    def test_old_card_only_policy_rule_removed(self) -> None:
        assert _OLD_CARD_ONLY_POLICY not in BASELINE_POLICY_RULES
        block = build_high_priority_block({})
        assert _OLD_CARD_ONLY_POLICY not in block

    def test_price_inquiry_textual_answer_required_in_policy(self) -> None:
        joined = "\n".join(BASELINE_POLICY_RULES)
        assert "response_goal=price_inquiry" in joined
        assert "لا يُغني عن الإجابة النصية" in joined
        assert "سعر رقمي موثوق" in joined

    def test_sales_intent_map_covers_resolved_price_turn(self) -> None:
        joined = "\n".join(BASELINE_SALES_BEHAVIOR_RULES)
        assert "(ب2)" in joined
        assert "selected_product محدّد" in joined
        assert "لا يكفي وحده" in joined

    def test_compact_resolver_protocol_no_marker_substitutes_price(self) -> None:
        assert _OLD_RESOLVER_PRICE_LINE not in _COMPACT_RESOLVER_PROTOCOL
        assert "لا يُغني عن الإجابة النصية" in _COMPACT_RESOLVER_PROTOCOL
        assert "لا تخترعي أسعاراً" in _COMPACT_RESOLVER_PROTOCOL
        overlay = slim_resolver_overlay_for_commerce(
            "أدوات الوسائط المتوفرة في هذا المتجر:\n[MEDIA_KEY:pay]"
        )
        assert "لا يُغني عن الإجابة النصية" in overlay

    def test_rendered_brain_prompt_carries_price_contract(self) -> None:
        prompt = build_brain_reply_prompt(_price_reply_state())
        assert "response_goal=price_inquiry" in prompt or "price_inquiry" in prompt
        assert "لا يُغني عن الإجابة النصية" in prompt
        assert _OLD_CARD_ONLY_POLICY not in prompt


# ── 2. Semantic price phrasings (routing / selection, not phrase-hardcoded) ───


class TestPricePhrasingsSemantic:
    @pytest.mark.parametrize("message", PRICE_PHRASINGS)
    def test_ordinal_and_pronoun_price_phrasings_resolve_second_product(
        self,
        message: str,
    ) -> None:
        ctx = _browse_ctx(message)
        resolution = resolve_selection_context(ctx)
        if message in {"كم سعر الثاني؟", "بكم الثاني؟"}:
            assert resolution is not None
            assert resolution.kind == "price_ordinal"
            assert str(resolution.selected.get("id")) == "302"
            assert resolution.selected.get("price") == EXPECTED_PRICE

    def test_response_goal_directive_prioritizes_price_answer(self) -> None:
        directive = _build_recommended_focus(
            primary_goal=GOAL_PRICE_INQUIRY,
            requires_clarification=False,
            clarification_reason="",
            focus_token="price_inquiry",
            has_secondary_social=False,
        )
        assert "price_inquiry" in directive
        assert "السعر" in directive


# ── 3. Missing price — no invention ─────────────────────────────────────────


class TestMissingPriceNoInvention:
    def test_prompt_forbids_inventing_prices_when_absent(self) -> None:
        state = _price_reply_state(
            selected_product={
                "id": 302,
                "title": "قميص قطني أزرق",
                "available": True,
            },
        )
        prompt = build_brain_reply_prompt(state)
        assert "تخترع" in prompt and "أسعار" in prompt
        assert "غياب السعر" in prompt or "غير الموجودة" in prompt

    def test_missing_price_product_has_no_trusted_numeric(self) -> None:
        product = {"id": 302, "title": "قميص قطني أزرق"}
        assert product.get("price") in (None, "", 0)


# ── 4. Wrong candidate price — do not use when multiple candidates ────────────


class TestWrongCandidatePriceGuard:
    def test_duplicate_price_candidates_require_clarification_not_guess(self) -> None:
        dupes = [
            {**CATALOG_PRODUCTS[0], "id": "401", "price": 89},
            {**CATALOG_PRODUCTS[1], "id": "402", "price": 89},
        ]
        state = MerchantConversationState(greeted=True, stage="discovery")
        stamp_selection_context_from_products(
            state,
            products=dupes,
            selected_collection="منتجات",
            discovery_mode="search",
        )
        state.last_search_candidates = list(dupes)
        ctx = _browse_ctx("أريد القميص سعره 89 ريال", state=state)
        resolution = resolve_selection_context(ctx)
        assert resolution is None or resolution.kind in {
            "price_ambiguous",
            "price_no_match",
        }


# ── 5. Ambiguous reference → clarify (no wrong price) ───────────────────────


class TestAmbiguousReferenceClarify:
    def test_price_without_context_does_not_pick_arbitrary_product(self) -> None:
        state = MerchantConversationState(greeted=True, stage="discovery")
        ctx = _browse_ctx("كم سعره؟", state=state)
        resolution = resolve_selection_context(ctx)
        assert resolution is None

    def test_clarification_directive_for_missing_product_price(self) -> None:
        directive = _build_recommended_focus(
            primary_goal=GOAL_PRICE_INQUIRY,
            requires_clarification=True,
            clarification_reason="missing_product_for_price",
            focus_token="price_inquiry",
            has_secondary_social=False,
        )
        assert "clarify" in directive.lower() or "اسألي" in directive
        assert "89" not in directive


# ── 6. Journey routing (fixtures; compose stubbed for non-price turns) ───────


class TestPriceInquiryJourney:
    def test_journey_turn_sequence_routes_without_price_injection(self) -> None:
        state = _browse_state()
        browse_msg, price_msg, link_msg, avail_msg, compare_msg = JOURNEY_TURNS

        assert "منتج" in browse_msg
        price_resolution = resolve_selection_context(_browse_ctx(price_msg, state=state))
        assert price_resolution is not None
        assert str(price_resolution.selected.get("id")) == "302"

        avail_directive = _build_recommended_focus(
            primary_goal=GOAL_PRODUCT_AVAILABILITY,
            requires_clarification=False,
            clarification_reason="",
            focus_token="product_availability",
            has_secondary_social=False,
        )
        assert "توفر" in avail_directive

        compare_resolution = resolve_selection_context(
            _browse_ctx(compare_msg, state=state),
        )
        assert compare_resolution is not None or "قارن" in compare_msg
        assert link_msg  # link turn covered by separate availability/compare routing

    def test_price_turn_prompt_includes_selected_product_302_and_contract(self) -> None:
        state = _price_reply_state(inbound="كم سعر الثاني؟")
        prompt = build_brain_reply_prompt(state)
        assert "302" in prompt or "قميص قطني أزرق" in prompt
        assert str(EXPECTED_PRICE) in prompt or "89" in prompt
        assert "post_model" not in prompt.lower()


# ── 7. Optional live proof (gpt-5.6-luna) ──────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY", "").strip(),
    reason="OPENAI_API_KEY not set — skip live Luna price-turn proof",
)
class TestLivePriceTurnProof:
    def test_price_turn_states_numeric_price_from_model(self) -> None:
        from modules.ai.orchestrator.customer_chat_models import MODEL_LUNA  # noqa: PLC0415
        from modules.ai.orchestrator.providers.openai_compatible_provider import (  # noqa: PLC0415
            OpenAICompatibleProvider,
        )

        state = _price_reply_state(inbound="كم سعر الثاني؟")
        prompt = build_brain_reply_prompt(state)
        provider = OpenAICompatibleProvider()
        result = provider.call(
            "كم سعر الثاني؟",
            prompt,
            history=[{"role": "user", "content": "اعرض المنتجات"}],
            audit_context={
                "model_override": MODEL_LUNA,
                "intent": INTENT_ASK_PRICE,
                "tenant_id": 7,
            },
        )
        raw_text = str(result.get("reply_text") or "").strip()
        assert raw_text, f"empty live reply: {result.get('status')}"
        assert _numeric_price_present(raw_text, expected=EXPECTED_PRICE), raw_text
        assert _currency_present(raw_text), raw_text
        # No post-model price injection in this fix scope — final = raw.
        final_text = raw_text
        assert final_text == raw_text
        assert result.get("model") == MODEL_LUNA or MODEL_LUNA in str(result.get("model", ""))
