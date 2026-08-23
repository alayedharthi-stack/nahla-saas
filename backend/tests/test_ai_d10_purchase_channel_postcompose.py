"""AI-D10 post-compose — purchase_channel_selection owns catalog grounding.

Runtime path under test:
Decision → build_turn_owner_contract → attach/project → catalog grounding guard.

Do not assert one exact customer-facing Arabic sentence.
Assert ownership: Brain channel-choice text is preserved; product invention
is still rewritten when this topic does not own the turn.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.navigation import (  # noqa: E402
    PATH_GROUPS,
    STEP_SHOW_GROUPS,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    has_actionable_active_order_context,
)
from modules.ai.brain.commerce.discovery_strategy import DiscoveryMode  # noqa: E402
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    MerchantSalesChannels,
    SalesChannelSlot,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.turn_owner_contract import (  # noqa: E402
    POSTPROCESS_CATALOG_GROUNDING,
    TOPIC_PURCHASE_CHANNEL_SELECTION,
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

MSG_LIVE = "طيب ابي اطلب"
MSG_START = "ابي اطلب"
MSG_SOCIAL = "مرحبا كيف الحال"
_STORE = "https://shop.example"
_MAPS = "https://maps.example.com/showroom"

# Generic numbered channel-choice shape (not a frozen production sentence).
BRAIN_CHANNEL_CHOICE = (
    "تقدر تكمل الطلب بطريقتين:\n"
    "1. المتجر الإلكتروني\n"
    "2. واتساب للطلب السريع\n"
    "أي طريقة تناسبك؟"
)
BRAIN_THREE_CHANNELS = (
    "تقدر تختار طريقة الطلب:\n"
    "1. المتجر الإلكتروني\n"
    "2. واتساب للطلب السريع\n"
    "3. زيارة المعرض\n"
    "أي خيار تفضل؟"
)
INVENTED_PRODUCT_LIST = (
    "المتوفر عندنا:\n"
    "- عسل الطلح البلدي\n"
    "- عسل السدر\n"
    "- عسل القطف\n"
    "- عسل الشهد"
)
GENERIC_INVENTED_OPTIONS = (
    "أقترح عليك:\n"
    "1. قميص قطني أزرق\n"
    "2. حذاء رياضي أبيض"
)
_CATALOG_TITLES = ["عطر ورد 100ml", "عطر مسك 50ml"]
COLLECTIONS = [
    {"group_id": "shoes", "group_name": "الأحذية", "browse_rank": 1},
    {"group_id": "shirts", "group_name": "القمصان", "browse_rank": 2},
]


def _slot(*, enabled: bool, available: bool, evidence: str) -> SalesChannelSlot:
    return SalesChannelSlot(enabled=enabled, available=available, evidence=evidence)


def _sales(
    *,
    store: bool = False,
    whatsapp: bool = True,
    showroom: bool = False,
    store_url: str = "",
    maps_url: str = "",
) -> MerchantSalesChannels:
    return MerchantSalesChannels(
        store_url=store_url,
        store_url_source="structured_settings" if store else "none",
        maps_url=maps_url,
        online_store=_slot(
            enabled=store, available=store, evidence="store_url" if store else "none"
        ),
        whatsapp_quick_order=_slot(
            enabled=whatsapp,
            available=whatsapp,
            evidence="whatsapp_catalog_or_enabled",
        ),
        showroom_visit=_slot(
            enabled=showroom,
            available=showroom,
            evidence="maps_url" if showroom else "none",
        ),
    )


def _facts(*, store_url: str = "", maps_url: str = "") -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=24,
        in_stock_count=24,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
        store_url=store_url,
        store_url_source="structured_settings" if store_url else "none",
        maps_url=maps_url,
        top_products=[
            {"id": "501", "external_id": "sku-white-shoe", "title": "حذاء رياضي أبيض", "price": 249},
        ],
    )


def _ctx(
    msg: str,
    *,
    tenant_id: int = 11,
    intent_name: str | None = None,
    store_url: str = "",
    maps_url: str = "",
    sales: MerchantSalesChannels | None = None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent_name or intent is None:
        intent = Intent(
            name=intent_name or getattr(intent, "name", "general"),
            confidence=0.9 if intent_name else 0.5,
            raw_message=msg,
        )
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(store_url=store_url, maps_url=maps_url),
        profile={"inbound_metadata": {}},
    )
    ctx._db = MagicMock()  # type: ignore[attr-defined]
    if sales is not None:
        ctx.merchant_sales_channels = sales  # type: ignore[attr-defined]
    return ctx


def _decide(ctx: BrainContext):
    with patch(
        "modules.ai.brain.commerce.commerce_entry_catalog_delivery.try_commerce_entry_catalog_decision",
        return_value=None,
    ), patch(
        "modules.ai.brain.catalog.navigation._load_catalog_groups",
        return_value=COLLECTIONS,
    ):
        return DefaultDecisionEngine().decide(ctx)


def _evidence(titles: list[str] | None = None) -> ProductClaimGroundingEvidence:
    names = titles if titles is not None else list(_CATALOG_TITLES)
    available = tuple(
        {"id": i + 1, "title": title, "can_checkout": True}
        for i, title in enumerate(names)
    )
    return ProductClaimGroundingEvidence(available_products=available)


def _project_runtime_guard_metadata(decision: Decision, ctx: BrainContext | None = None) -> dict[str, Any]:
    """Mirror pipeline.py: Decision → contract → inbound_metadata for the guard."""
    contract = build_turn_owner_contract(decision, ctx=ctx)
    if ctx is not None:
        attach_turn_owner_contract(ctx, contract)
    meta: dict[str, Any] = {}
    if ctx is not None:
        profile = getattr(ctx, "profile", None) or {}
        if isinstance(profile, dict):
            meta.update(dict(profile.get("inbound_metadata") or {}))
        inbound = getattr(ctx, "inbound_metadata", None)
        if isinstance(inbound, dict):
            meta.update(inbound)
    meta["decision_topic"] = str((decision.args or {}).get("topic") or "")
    meta["turn_owner_contract"] = dict(contract.to_metadata())
    for flag in (
        "block_catalog_push",
        "block_staff_contact",
        "block_showroom_location",
        "pause_order_slot_collection",
    ):
        if flag in (decision.args or {}):
            meta[flag] = bool((decision.args or {}).get(flag))
    return meta


def _apply_runtime_catalog_guard(
    decision: Decision,
    reply: str,
    *,
    inbound_text: str = MSG_LIVE,
    ctx: BrainContext | None = None,
    evidence: ProductClaimGroundingEvidence | None = None,
):
    contract = build_turn_owner_contract(decision, ctx=ctx)
    meta = _project_runtime_guard_metadata(decision, ctx)
    result = apply_catalog_product_grounding_guard(
        reply=reply,
        inbound_text=inbound_text,
        evidence=evidence if evidence is not None else _evidence(),
        chosen_path="llm",
        inbound_metadata=meta,
        intent=getattr(ctx, "intent", None) if ctx is not None else None,
    )
    return contract, result


def _assert_not_groups(decision: Any) -> None:
    assert decision.action != ACTION_CATALOG_NAVIGATE or (
        decision.args.get("chosen_path") != PATH_GROUPS
        and decision.args.get("navigator_step") != STEP_SHOW_GROUPS
    )
    assert decision.args.get("chosen_path") != PATH_GROUPS
    assert decision.args.get("discovery_mode") != DiscoveryMode.COLLECTIONS_FIRST.value


class TestControlAContractOwnership:
    def test_purchase_channel_selection_contract_blocks_catalog_grounding(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": TOPIC_PURCHASE_CHANNEL_SELECTION,
                "response_goal": "help_customer_choose_purchase_channel",
                "available_purchase_channels": ["online_store", "whatsapp_quick_order"],
            },
        )
        contract = build_turn_owner_contract(decision)
        assert contract.topic == TOPIC_PURCHASE_CHANNEL_SELECTION
        assert contract.protected_final_reply is True
        assert contract.block_catalog_push is True
        assert contract.blocks(POSTPROCESS_CATALOG_GROUNDING) is True


class TestControlBLiveShapeRuntimePath:
    def test_engine_decision_projects_contract_and_preserves_brain_channel_text(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        ctx = _ctx(
            MSG_LIVE,
            intent_name="start_order",
            store_url=_STORE,
            sales=sales,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == TOPIC_PURCHASE_CHANNEL_SELECTION

        contract, result = _apply_runtime_catalog_guard(
            decision,
            BRAIN_CHANNEL_CHOICE,
            inbound_text=MSG_LIVE,
            ctx=ctx,
        )
        assert contract.protected_final_reply is True
        assert contract.block_catalog_push is True
        assert contract.blocks(POSTPROCESS_CATALOG_GROUNDING) is True
        assert result.replaced is False
        assert result.reply == BRAIN_CHANNEL_CHOICE
        assert "الخيارات المؤكدة من الكتالوج" not in result.reply


class TestControlCNoContractRegression:
    def test_same_numbered_text_without_channel_owner_still_grounds(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "open_category_inquiry"},
        )
        _, result = _apply_runtime_catalog_guard(
            decision,
            BRAIN_CHANNEL_CHOICE,
            inbound_text="وش المتوفر؟",
        )
        assert result.replaced is True
        assert result.reply != BRAIN_CHANNEL_CHOICE


class TestControlDAndMProductInvention:
    def test_ungrounded_product_list_still_rewritten_without_channel_owner(self) -> None:
        decision = Decision(action=ACTION_LLM_REPLY, args={"topic": "open_category_inquiry"})
        _, result = _apply_runtime_catalog_guard(
            decision,
            INVENTED_PRODUCT_LIST,
            inbound_text="وش أنواع العسل؟",
            evidence=_evidence(["عسل طلح نجد", "عسل سمر الحجاز"]),
        )
        assert result.replaced is True
        assert result.reply != INVENTED_PRODUCT_LIST
        for invented in ("عسل القطف", "عسل الشهد", "عسل السدر", "عسل الطلح البلدي"):
            assert invented not in result.reply

    def test_generic_commerce_option_list_still_caught(self) -> None:
        decision = Decision(action=ACTION_LLM_REPLY, args={"topic": "ask_product"})
        _, result = _apply_runtime_catalog_guard(
            decision,
            GENERIC_INVENTED_OPTIONS,
            inbound_text="وش تنصحني؟",
            evidence=_evidence(_CATALOG_TITLES),
        )
        assert result.replaced is True or result.would_rewrite is True
        assert result.action not in {"allowed", "allowed_non_option_prose"}


class TestControlETwoChannelRouting:
    def test_generic_purchase_two_channels_selects_purchase_channel(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        for msg in (MSG_START, MSG_LIVE):
            ctx = _ctx(msg, intent_name="start_order", store_url=_STORE, sales=sales)
            decision = _decide(ctx)
            _assert_not_groups(decision)
            assert decision.action == ACTION_LLM_REPLY
            assert decision.args.get("topic") == TOPIC_PURCHASE_CHANNEL_SELECTION
            assert decision.args.get("available_purchase_channels") == [
                "online_store",
                "whatsapp_quick_order",
            ]


class TestControlFThreeChannels:
    def test_three_channels_select_and_preserve_channel_labels(self) -> None:
        sales = _sales(
            store=True, whatsapp=True, showroom=True,
            store_url=_STORE, maps_url=_MAPS,
        )
        ctx = _ctx(
            MSG_LIVE,
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
            sales=sales,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == TOPIC_PURCHASE_CHANNEL_SELECTION
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        _, result = _apply_runtime_catalog_guard(
            decision, BRAIN_THREE_CHANNELS, inbound_text=MSG_LIVE, ctx=ctx,
        )
        assert result.replaced is False
        assert result.reply == BRAIN_THREE_CHANNELS


class TestControlGOneChannel:
    def test_whatsapp_only_skips_selector(self) -> None:
        sales = _sales(whatsapp=True)
        ctx = _ctx(MSG_LIVE, intent_name="start_order", sales=sales)
        decision = _decide(ctx)
        _assert_not_groups(decision)
        assert decision.args.get("topic") != TOPIC_PURCHASE_CHANNEL_SELECTION


class TestControlHRetiredGroups:
    def test_start_order_never_returns_groups_menu(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        ctx = _ctx(MSG_LIVE, intent_name="start_order", store_url=_STORE, sales=sales)
        decision = _decide(ctx)
        _assert_not_groups(decision)


class TestControlIStaleOrderShell:
    def test_empty_ordering_shell_is_not_active_commerce(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=8,
            order_prep={"quantity": 1, "checkout_channel": ""},
        )
        assert has_actionable_active_order_context(
            stage="ordering",
            order_prep=state.order_prep,
            state=state,
        ) is False
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        ctx = _ctx(
            MSG_LIVE,
            intent_name="start_order",
            store_url=_STORE,
            sales=sales,
            state=state,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == TOPIC_PURCHASE_CHANNEL_SELECTION


class TestControlJRealActiveCommerce:
    def test_product_focus_does_not_replay_channel_selector(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=4,
            current_product_focus={
                "id": "501",
                "external_id": "sku-white-shoe",
                "title": "حذاء رياضي أبيض",
            },
        )
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        ctx = _ctx(
            MSG_LIVE,
            intent_name="start_order",
            store_url=_STORE,
            sales=sales,
            state=state,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") != TOPIC_PURCHASE_CHANNEL_SELECTION


class TestControlKSocial:
    def test_social_turn_does_not_become_purchase_channel_or_catalog(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        ctx = _ctx(MSG_SOCIAL, store_url=_STORE, sales=sales)
        decision = _decide(ctx)
        assert decision.args.get("topic") != TOPIC_PURCHASE_CHANNEL_SELECTION
        _assert_not_groups(decision)


class TestControlLOtherProtectedOwners:
    def test_health_payment_knowledge_order_evidence_still_block_catalog(self) -> None:
        blocked_topics = (
            "health_advisory_product_safety",
            "payment_receipt_received",
            "product_knowledge_facts",
            "order_tracking",
        )
        for topic in blocked_topics:
            contract = build_turn_owner_contract(
                Decision(action=ACTION_LLM_REPLY, args={"topic": topic}),
            )
            assert contract.blocks(POSTPROCESS_CATALOG_GROUNDING) is True
            assert contract.block_catalog_push is True

    def test_shipping_contract_unchanged_does_not_gain_catalog_block(self) -> None:
        contract = build_turn_owner_contract(
            Decision(action=ACTION_LLM_REPLY, args={"topic": "shipping_inquiry"}),
        )
        assert contract.blocks(POSTPROCESS_CATALOG_GROUNDING) is False


class TestControlNTenantIsolation:
    def test_two_tenants_keep_independent_channel_facts(self) -> None:
        two = _sales(store=True, whatsapp=True, store_url=_STORE)
        three = _sales(
            store=True, whatsapp=True, showroom=True,
            store_url=_STORE, maps_url=_MAPS,
        )
        dec_a = _decide(_ctx(
            MSG_LIVE, tenant_id=11, intent_name="start_order",
            store_url=_STORE, sales=two,
        ))
        dec_b = _decide(_ctx(
            MSG_LIVE, tenant_id=33, intent_name="start_order",
            store_url=_STORE, maps_url=_MAPS, sales=three,
        ))
        assert dec_a.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
        ]
        assert dec_b.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        assert dec_a.args.get("available_purchase_channels") != dec_b.args.get(
            "available_purchase_channels"
        )
