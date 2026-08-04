"""BQ-3 — multi-turn commerce focus ownership regressions (merchant-agnostic)."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (ROOT, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    FOCUS_ORDER_TRACKING,
    FOCUS_PRODUCT,
    FOCUS_SHIPPING_POLICY,
    apply_commerce_focus_lifecycle,
    archive_current_product_focus,
    bind_variant_to_focus,
    get_effective_product_focus,
    product_focus_identity,
    revert_to_previous_product_focus,
    set_product_focus,
    suspend_product_focus,
    try_ordinal_correction_focus_swap,
)
from modules.ai.brain.commerce.selection_context import (  # noqa: E402
    stamp_selection_context_from_products,
)
from modules.ai.brain.interpret.semantic_routing import (  # noqa: E402
    try_semantic_interpretation_decision,
)
from modules.ai.brain.interpret.semantic_turn_interpreter import (  # noqa: E402
    interpret_semantic_turn,
)
from modules.ai.brain.state.product_correction import (  # noqa: E402
    clear_stale_product_state_for_correction,
)
from modules.ai.brain.state.store import DefaultStateStore  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _shoe_white() -> dict:
    return {
        "id": "shoe-white-1",
        "external_id": "SKU-WHITE",
        "title": "حذاء رياضي أبيض",
        "price": 199,
        "variant_label": "أبيض",
    }


def _shoe_black() -> dict:
    return {
        "id": "shoe-black-1",
        "external_id": "SKU-BLACK",
        "title": "حذاء رياضي أسود",
        "price": 209,
        "variant_label": "أسود",
    }


def _perfume() -> dict:
    return {
        "id": "perf-rose-1",
        "external_id": "SKU-PERF",
        "title": "عطر ورد 100ml",
        "price": 149,
    }


def _ctx(
    message: str,
    *,
    state: MerchantConversationState | None = None,
    tenant_id: int = 101,
    phone: str = "966500000101",
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=Intent(name="ask_price", confidence=0.9, raw_message=message),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(has_products=True, orderable=True),
        history=[],
    )


class TestProductSwitchAndReturn:
    def test_switch_chains_previous_product(self) -> None:
        state = MerchantConversationState(turn=3)
        set_product_focus(state, _shoe_white(), reason="browse", turn=3)
        set_product_focus(state, _shoe_black(), reason="color_switch", turn=4)

        assert product_focus_identity(state.current_product_focus) == "SKU-BLACK"
        assert product_focus_identity(state.previous_product_focus) == "SKU-WHITE"

    def test_revert_to_previous_restores_first_product(self) -> None:
        state = MerchantConversationState(turn=5)
        set_product_focus(state, _shoe_white(), reason="browse", turn=3)
        set_product_focus(state, _shoe_black(), reason="color_switch", turn=4)
        assert revert_to_previous_product_focus(state)

        assert product_focus_identity(state.current_product_focus) == "SKU-WHITE"
        assert product_focus_identity(state.previous_product_focus) == "SKU-BLACK"


class TestVariantBindingPreservesProduct:
    def test_variant_bind_updates_price_without_identity_swap(self) -> None:
        state = MerchantConversationState(turn=2)
        set_product_focus(state, _shoe_white(), reason="browse", turn=2)
        bind_variant_to_focus(
            state,
            {
                "variant_id": "var-large",
                "variant_label": "كبير",
                "price": 219,
                "unit": {"display_label": "كبير"},
            },
        )

        assert product_focus_identity(state.current_product_focus) == "SKU-WHITE"
        assert state.current_product_focus["variant_label"] == "كبير"
        assert state.current_product_focus["price"] == 219
        assert state.selected_variant["variant_label"] == "كبير"


class TestShortSizeFollowupUsesFocus:
    def test_size_followup_resolves_with_active_focus(self) -> None:
        state = MerchantConversationState(turn=4)
        set_product_focus(state, _shoe_white(), reason="browse", turn=3)
        stamp_selection_context_from_products(
            state,
            products=[_shoe_white(), _shoe_black()],
        )
        msg = "\u0643\u0645 \u0627\u0644\u0643\u0628\u064a\u0631\u061f"  # كم الكبير؟
        history = [{"direction": "outbound", "body": "\u0623\u064a \u062d\u062c\u0645 \u064a\u0646\u0627\u0633\u0628\u0643\u061f"}]
        interp = interpret_semantic_turn(
            raw_text=msg,
            state=state,
            history=history,
        )
        assert interp is not None
        assert interp.interpreted_intent == "ask_price_specific_variant"
        assert (interp.slots or {}).get("size_hint") == "large"
        assert product_focus_identity(get_effective_product_focus(state)) == "SKU-WHITE"


class TestUserCorrectionUpdatesFocus:
    def test_ordinal_correction_reverts_to_previous(self) -> None:
        state = MerchantConversationState(turn=6)
        set_product_focus(state, _shoe_white(), reason="first", turn=4)
        set_product_focus(state, _shoe_black(), reason="second_pick", turn=5)

        assert try_ordinal_correction_focus_swap(state, "لا أقصد الثاني")
        assert product_focus_identity(state.current_product_focus) == "SKU-WHITE"

    def test_clear_stale_respects_ordinal_revert(self) -> None:
        state = MerchantConversationState(turn=6)
        set_product_focus(state, _shoe_white(), reason="first", turn=4)
        set_product_focus(state, _shoe_black(), reason="second_pick", turn=5)

        clear_stale_product_state_for_correction(state, "لا أقصد الثاني")
        assert product_focus_identity(state.current_product_focus) == "SKU-WHITE"


class TestShippingDigressionThenReturn:
    def test_shipping_suspend_and_product_return_restore(self) -> None:
        state = MerchantConversationState(turn=5)
        set_product_focus(state, _shoe_white(), reason="browse", turn=4)

        apply_commerce_focus_lifecycle(
            state,
            intent_name="ask_shipping",
            action="llm_reply",
            message="كم الشحن؟",
            turn=5,
        )
        assert state.conversation_focus == FOCUS_SHIPPING_POLICY
        assert product_focus_identity(state.suspended_product_focus) == "SKU-WHITE"

        apply_commerce_focus_lifecycle(
            state,
            intent_name="ask_price",
            action="llm_reply",
            message="كم سعره؟",
            turn=6,
        )
        assert state.conversation_focus == FOCUS_PRODUCT
        assert product_focus_identity(state.current_product_focus) == "SKU-WHITE"
        assert state.suspended_product_focus is None


class TestPauseResumePreservesFocus:
    def test_roundtrip_state_dict_preserves_focus_stack(self) -> None:
        state = MerchantConversationState(turn=7)
        set_product_focus(state, _shoe_white(), reason="browse", turn=5)
        set_product_focus(state, _shoe_black(), reason="switch", turn=6)
        suspend_product_focus(state, digression=FOCUS_SHIPPING_POLICY)

        raw = state.to_dict()
        restored = MerchantConversationState.from_dict(raw)

        assert product_focus_identity(restored.suspended_product_focus) == "SKU-BLACK"
        assert product_focus_identity(restored.previous_product_focus) == "SKU-WHITE"
        assert restored.conversation_focus == FOCUS_SHIPPING_POLICY


class TestTenantIsolation:
    def test_focus_fields_do_not_share_between_state_instances(self) -> None:
        tenant_a = MerchantConversationState()
        tenant_b = MerchantConversationState()
        set_product_focus(tenant_a, _shoe_white(), reason="a", turn=1)
        set_product_focus(tenant_b, _perfume(), reason="b", turn=1)

        assert product_focus_identity(tenant_a.current_product_focus) == "SKU-WHITE"
        assert product_focus_identity(tenant_b.current_product_focus) == "SKU-PERF"
        assert tenant_a.previous_product_focus is None
        assert tenant_b.previous_product_focus is None


class TestTwoCustomersSameTenant:
    def test_independent_customer_state_objects(self) -> None:
        customer_one = MerchantConversationState()
        customer_two = MerchantConversationState()
        set_product_focus(customer_one, _shoe_white(), reason="c1", turn=2)
        set_product_focus(customer_two, _shoe_black(), reason="c2", turn=2)

        assert product_focus_identity(customer_one.current_product_focus) == "SKU-WHITE"
        assert product_focus_identity(customer_two.current_product_focus) == "SKU-BLACK"


class TestOrderTrackingDoesNotLeakIntoBrowse:
    def test_fresh_product_browse_clears_tracking_mode(self) -> None:
        state = MerchantConversationState(turn=4)
        set_product_focus(state, _shoe_white(), reason="browse", turn=3)
        state.conversation_focus = FOCUS_ORDER_TRACKING
        state.suspended_product_focus = copy.deepcopy(state.current_product_focus)
        state.current_product_focus = None

        apply_commerce_focus_lifecycle(
            state,
            intent_name="ask_product",
            action="search_products",
            message="أبغى عطر جديد",
            turn=4,
        )

        assert state.conversation_focus in {"", FOCUS_PRODUCT}
        assert state.conversation_focus != FOCUS_ORDER_TRACKING
        focus = get_effective_product_focus(state)
        assert focus is not None
        assert product_focus_identity(focus) == "SKU-WHITE"


class TestGeneralizationPerfumePronoun:
    def test_deictic_reference_resolves_via_suspended_focus(self) -> None:
        state = MerchantConversationState(turn=5)
        set_product_focus(state, _perfume(), reason="browse", turn=4)
        suspend_product_focus(state, digression=FOCUS_SHIPPING_POLICY)
        state.current_product_focus = None

        msg = "\u0647\u0630\u0627"  # هذا
        interp = interpret_semantic_turn(raw_text=msg, state=state, history=[])
        assert interp is not None
        assert interp.interpreted_intent == "refer_last_product"

        state.current_product_focus = get_effective_product_focus(state)
        ctx = _ctx(msg, state=state)
        ctx.semantic_interpretation = interp
        decision = try_semantic_interpretation_decision(ctx)
        assert decision is not None
        product = (decision.args or {}).get("product") or {}
        assert product_focus_identity(product) == "SKU-PERF"


class TestArchiveOnBrowseList:
    def test_archive_before_list_clears_current_keeps_previous(self) -> None:
        state = MerchantConversationState(turn=3)
        set_product_focus(state, _shoe_white(), reason="browse", turn=2)
        archive_current_product_focus(state, reason="list_display")
        state.current_product_focus = None

        assert state.current_product_focus is None
        assert product_focus_identity(state.previous_product_focus) == "SKU-WHITE"


class TestStateStoreTransitionCarriesFocus:
    def test_transition_preserves_focus_stack(self) -> None:
        store = DefaultStateStore()
        state = MerchantConversationState(turn=2)
        set_product_focus(state, _shoe_white(), reason="t", turn=2)
        set_product_focus(state, _shoe_black(), reason="t2", turn=2)
        state.conversation_focus = FOCUS_PRODUCT

        new_state = store.transition(
            state,
            Intent(name="ask_price", confidence=0.9),
            Decision(action="llm_reply", args={}, reason="test", confidence=0.9),
        )

        assert product_focus_identity(new_state.previous_product_focus) == "SKU-WHITE"
        assert product_focus_identity(new_state.current_product_focus) == "SKU-BLACK"
        assert new_state.conversation_focus == FOCUS_PRODUCT


def _shirt_cotton() -> dict:
    return {
        "id": "shirt-blue-9",
        "external_id": "SKU-SHIRT-BLUE",
        "title": "قميص قطني أزرق",
        "price": 89,
        "variant_label": "M",
    }


def _watch_silver() -> dict:
    return {
        "id": "watch-silver-3",
        "external_id": "SKU-WATCH",
        "title": "ساعة يد فضية",
        "price": 320,
        "variant_label": "كبير",
    }


class TestGeneralizationNewCategoryAndProducts:
    def test_new_products_not_from_layer3_fixtures(self) -> None:
        state = MerchantConversationState(turn=2)
        set_product_focus(state, _shirt_cotton(), reason="browse", turn=1)
        set_product_focus(state, _watch_silver(), reason="switch", turn=2)
        assert product_focus_identity(state.current_product_focus) == "SKU-WATCH"
        assert product_focus_identity(state.previous_product_focus) == "SKU-SHIRT-BLUE"

    def test_third_category_perfume_then_watch(self) -> None:
        state = MerchantConversationState(turn=3)
        set_product_focus(state, _perfume(), reason="cat3", turn=2)
        set_product_focus(state, _watch_silver(), reason="cat_accessories", turn=3)
        assert product_focus_identity(state.previous_product_focus) == "SKU-PERF"
        assert product_focus_identity(state.current_product_focus) == "SKU-WATCH"


class TestDifferentMessageOrder:
    def test_black_first_then_white_still_chains(self) -> None:
        state = MerchantConversationState(turn=2)
        set_product_focus(state, _shoe_black(), reason="first", turn=1)
        set_product_focus(state, _shoe_white(), reason="second", turn=2)
        assert product_focus_identity(state.current_product_focus) == "SKU-WHITE"
        assert product_focus_identity(state.previous_product_focus) == "SKU-BLACK"
        assert revert_to_previous_product_focus(state)
        assert product_focus_identity(state.current_product_focus) == "SKU-BLACK"


class TestAlternateFollowupPhrasing:
    def test_price_followup_keeps_focus_without_phrase_tree(self) -> None:
        state = MerchantConversationState(turn=3)
        set_product_focus(state, _watch_silver(), reason="browse", turn=2)
        apply_commerce_focus_lifecycle(
            state,
            intent_name="ask_price",
            action="llm_reply",
            message="وش سعر هالقطعة؟",
            turn=3,
        )
        assert product_focus_identity(state.current_product_focus) == "SKU-WATCH"
        assert state.conversation_focus == FOCUS_PRODUCT


class TestProductToOrderThenReturn:
    def test_tracking_digression_then_product_return(self) -> None:
        state = MerchantConversationState(turn=4)
        set_product_focus(state, _shirt_cotton(), reason="browse", turn=2)
        apply_commerce_focus_lifecycle(
            state,
            intent_name="track_order",
            action="track_order",
            message="وين طلبي؟",
            turn=3,
        )
        assert state.conversation_focus == FOCUS_ORDER_TRACKING
        assert product_focus_identity(state.suspended_product_focus) == "SKU-SHIRT-BLUE"

        apply_commerce_focus_lifecycle(
            state,
            intent_name="ask_product",
            action="search_products",
            message="أبغى أشوف القميص مرة ثانية",
            turn=4,
        )
        assert state.conversation_focus != FOCUS_ORDER_TRACKING
        assert product_focus_identity(get_effective_product_focus(state)) == "SKU-SHIRT-BLUE"


class TestKnowledgeToProduct:
    def test_return_from_shipping_policy_to_product_on_ask_product(self) -> None:
        state = MerchantConversationState(turn=5)
        set_product_focus(state, _perfume(), reason="browse", turn=3)
        apply_commerce_focus_lifecycle(
            state,
            intent_name="ask_shipping",
            action="llm_reply",
            message="كم مدة التوصيل؟",
            turn=4,
        )
        assert state.conversation_focus == FOCUS_SHIPPING_POLICY
        apply_commerce_focus_lifecycle(
            state,
            intent_name="ask_product",
            action="search_products",
            message="وريني العطر",
            turn=5,
        )
        assert product_focus_identity(get_effective_product_focus(state)) == "SKU-PERF"


class TestRepeatedUserCorrection:
    def test_double_ordinal_correction_stays_consistent(self) -> None:
        state = MerchantConversationState(turn=8)
        set_product_focus(state, _shirt_cotton(), reason="first", turn=4)
        set_product_focus(state, _watch_silver(), reason="second", turn=5)
        assert try_ordinal_correction_focus_swap(state, "لا أقصد الثاني")
        assert product_focus_identity(state.current_product_focus) == "SKU-SHIRT-BLUE"
        set_product_focus(state, _perfume(), reason="third", turn=7)
        assert try_ordinal_correction_focus_swap(state, "مو اقصد الثاني")
        assert product_focus_identity(state.current_product_focus) == "SKU-SHIRT-BLUE"


class TestSameVariantLabelAcrossProducts:
    def test_shared_variant_label_does_not_keep_wrong_product(self) -> None:
        state = MerchantConversationState(turn=3)
        set_product_focus(state, _shoe_white(), reason="p1", turn=1)
        bind_variant_to_focus(
            state,
            {"variant_id": "v-large-shoe", "variant_label": "كبير", "price": 219},
        )
        set_product_focus(state, _watch_silver(), reason="p2", turn=2)
        bind_variant_to_focus(
            state,
            {"variant_id": "v-large-watch", "variant_label": "كبير", "price": 340},
        )
        assert product_focus_identity(state.current_product_focus) == "SKU-WATCH"
        assert product_focus_identity(state.previous_product_focus) == "SKU-WHITE"
        assert state.current_product_focus["variant_label"] == "كبير"
        assert state.current_product_focus["price"] == 340
        assert state.selected_variant["variant_id"] == "v-large-watch"