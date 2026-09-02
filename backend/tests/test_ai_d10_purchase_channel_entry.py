"""AI-D10 follow-up — purchase-channel ownership from semantic intent + capabilities.

Assert owner, capability count, topic, commitment, and product-focus state.
Do not assert exact customer wording.
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
    try_catalog_navigation_decision,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    _active_whatsapp_checkout,
    has_actionable_active_order_context,
    is_genuine_purchase_channel_entry,
    purchase_channel_committed,
    resolve_available_purchase_channel_facts,
    resolve_purchase_channel_entry_owner,
)
from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: E402
    is_active_order_flow,
)
from modules.ai.brain.commerce.commerce_navigator import (  # noqa: E402
    resolve_commerce_navigator,
)
from modules.ai.brain.commerce.discovery_strategy import DiscoveryMode  # noqa: E402
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    MerchantSalesChannels,
    SalesChannelSlot,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

MSG_START = "ابي اطلب"
MSG_LIVE = "طيب ابي اطلب"
MSG_SOCIAL = "مرحبا كيف الحال"
MSG_BROWSE = "وش عندكم"
MSG_PRODUCT = "أبغى حذاء رياضي أبيض مقاس 42"
VOICE_SOCIAL_START_ORDER = (
    "السلام عليكم كيف حالكم اليوم كنت أبغى أشتري شي "
    "من المتجر بس ما حددت المنتج بالضبط"
)

_STORE = "https://shop.example"
_MAPS = "https://maps.example.com/showroom"

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


def _facts(
    *,
    store_url: str = "",
    maps_url: str = "",
    store_name: str = "متجر تجريبي عام",
) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=24,
        in_stock_count=24,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name=store_name,
        store_url=store_url,
        store_url_source="structured_settings" if store_url else "none",
        maps_url=maps_url,
        top_products=[
            {
                "id": "501",
                "external_id": "sku-white-shoe",
                "title": "حذاء رياضي أبيض",
                "price": 249,
            },
        ],
    )


def _ctx(
    msg: str,
    *,
    tenant_id: int = 11,
    state: MerchantConversationState | None = None,
    db: Any = None,
    intent_name: str | None = None,
    store_url: str = "",
    maps_url: str = "",
    sales: MerchantSalesChannels | None = None,
    inbound_metadata: dict[str, Any] | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(
            name=intent_name or "general",
            confidence=0.5,
            raw_message=msg,
        )
    if intent_name:
        intent = Intent(name=intent_name, confidence=0.9, raw_message=msg)
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state
        or MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(store_url=store_url, maps_url=maps_url),
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    if sales is not None:
        ctx.merchant_sales_channels = sales  # type: ignore[attr-defined]
    if inbound_metadata is not None:
        ctx.inbound_metadata = inbound_metadata  # type: ignore[attr-defined]
    return ctx


def _assert_not_groups(decision: Any) -> None:
    if decision is None:
        return
    assert decision.action != ACTION_CATALOG_NAVIGATE or (
        decision.args.get("chosen_path") != PATH_GROUPS
        and decision.args.get("navigator_step") != STEP_SHOW_GROUPS
    )
    assert decision.args.get("chosen_path") != PATH_GROUPS
    assert decision.args.get("discovery_mode") != DiscoveryMode.COLLECTIONS_FIRST.value


def _decide(ctx: BrainContext):
    with patch(
        "modules.ai.brain.commerce.commerce_entry_catalog_delivery.try_commerce_entry_catalog_decision",
        return_value=None,
    ), patch(
        "modules.ai.brain.catalog.navigation._load_catalog_groups",
        return_value=COLLECTIONS,
    ):
        return DefaultDecisionEngine().decide(ctx)


_SHOE = {
    "id": "501",
    "external_id": "sku-white-shoe",
    "title": "حذاء رياضي أبيض",
    "price": 249,
}


def _stale_ordering_shell() -> MerchantConversationState:
    """Generic equivalent of the live empty ordering shell.

    Stage labels and identity are present. No product, referent, cart,
    draft, or committed channel owns the turn.
    """
    return MerchantConversationState(
        greeted=True,
        stage="ordering",
        turn=8,
        current_product_focus=None,
        commerce_session={
            "stage": "variant_selected",
            "active_product": "",
            "active_variant": "",
        },
        order_prep=OrderPreparationState(
            quantity=1,
            customer_first_name="أحمد",
            customer_last_name="سالم",
            city="الرياض",
            checkout_channel="",
            awaiting_checkout_channel=False,
            catalog_line_items_authoritative=False,
        ),
    )


def _two_channel_sales() -> MerchantSalesChannels:
    return _sales(store=True, whatsapp=True, store_url=_STORE)


def _stale_shell_ctx(
    msg: str,
    *,
    sales: MerchantSalesChannels | None = None,
    state: MerchantConversationState | None = None,
    intent_name: str | None = None,
    store_url: str = _STORE,
    maps_url: str = "",
    tenant_id: int = 11,
) -> BrainContext:
    chosen_sales = sales if sales is not None else _two_channel_sales()
    return _ctx(
        msg,
        tenant_id=tenant_id,
        state=state or _stale_ordering_shell(),
        db=MagicMock(),
        intent_name=intent_name,
        store_url=store_url,
        maps_url=maps_url,
        sales=chosen_sales,
    )


class TestGenuinePurchaseEntrySignal:
    def test_live_prefixed_purchase_is_genuine_and_compact_opener_is_genuine(
        self,
    ) -> None:
        assert is_genuine_purchase_channel_entry(message=MSG_START) is True
        assert is_genuine_purchase_channel_entry(message=MSG_LIVE) is True
        assert rules.match(MSG_LIVE).name == "start_order"

    def test_classifier_label_alone_is_not_genuine(self) -> None:
        forced = Intent(name="start_order", confidence=0.9, raw_message=MSG_SOCIAL)
        assert (
            is_genuine_purchase_channel_entry(message=MSG_SOCIAL, intent=forced)
            is False
        )
        assert rules.match(MSG_SOCIAL).name != "start_order"

    def test_mixed_greeting_stt_is_not_genuine(self) -> None:
        assert len(VOICE_SOCIAL_START_ORDER) > 64
        assert (
            is_genuine_purchase_channel_entry(
                message=VOICE_SOCIAL_START_ORDER,
                intent=Intent(
                    name="start_order",
                    confidence=0.9,
                    raw_message=VOICE_SOCIAL_START_ORDER,
                ),
            )
            is False
        )

    def test_named_or_embedded_product_is_not_channel_entry(self) -> None:
        embedded = "ما أبغى أمين أنا أبغى أشتري عسل"
        assert is_genuine_purchase_channel_entry(message=MSG_PRODUCT) is False
        assert is_genuine_purchase_channel_entry(message=embedded) is False
        owner = resolve_purchase_channel_entry_owner(
            message=embedded,
            merchant_sales_channels=_sales(
                store=True, whatsapp=True, store_url=_STORE
            ),
        )
        assert owner is None


class TestControlASocial:
    def test_social_conversation_does_not_own_purchase_or_groups(self) -> None:
        ctx = _ctx(
            MSG_SOCIAL,
            db=MagicMock(),
            store_url=_STORE,
            maps_url=_MAPS,
            sales=_sales(store=True, whatsapp=True, store_url=_STORE),
        )
        decision = _decide(ctx)
        _assert_not_groups(decision)
        assert decision.args.get("topic") != "purchase_channel_selection"
        nav = resolve_commerce_navigator(
            message=MSG_SOCIAL,
            intent_name=str(ctx.intent.name),
            merchant_sales_channels=_sales(
                store=True, whatsapp=True, showroom=True,
                store_url=_STORE, maps_url=_MAPS,
            ),
        )
        assert nav.stage != "purchase_channel_selection"


class TestControlBTwoChannels:
    def test_generic_purchase_two_channels_including_live_prefix(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        for msg in (MSG_START, MSG_LIVE):
            ctx = _ctx(
                msg,
                db=MagicMock(),
                intent_name="start_order",
                store_url=_STORE,
                sales=sales,
            )
            decision = _decide(ctx)
            _assert_not_groups(decision)
            assert decision.action == ACTION_LLM_REPLY
            assert decision.args.get("topic") == "purchase_channel_selection"
            channels = decision.args.get("available_purchase_channels")
            assert channels == ["online_store", "whatsapp_quick_order"]
            assert len(channels) == 2
            assert "showroom_visit" not in channels
            nav = resolve_commerce_navigator(
                message=msg,
                intent_name="start_order",
                merchant_sales_channels=sales,
            )
            assert nav.stage == "purchase_channel_selection"
            assert nav.available_purchase_channels == channels


class TestControlCThreeChannels:
    def test_generic_purchase_three_channels(self) -> None:
        sales = _sales(
            store=True, whatsapp=True, showroom=True,
            store_url=_STORE, maps_url=_MAPS,
        )
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
            sales=sales,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        channels = decision.args.get("available_purchase_channels")
        assert channels == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        assert len(channels) == 3


class TestControlDWhatsappOnly:
    def test_whatsapp_only_skips_redundant_selector(self) -> None:
        sales = _sales(whatsapp=True)
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            sales=sales,
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            decision = _decide(ctx)
        _assert_not_groups(decision)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "whatsapp_quick_order"
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "whatsapp_quick_order",
        ]
        owner = resolve_purchase_channel_entry_owner(
            message=MSG_LIVE,
            merchant_sales_channels=sales,
        )
        assert owner == "whatsapp_quick_order"
        nav = resolve_commerce_navigator(
            message=MSG_LIVE,
            intent_name="start_order",
            merchant_sales_channels=sales,
        )
        assert nav.stage == "whatsapp_quick_order"
        assert nav.stage != "purchase_channel_selection"


class TestControlEStoreOnly:
    def test_store_only_routes_directly_to_store(self) -> None:
        sales = _sales(store=True, whatsapp=False, store_url=_STORE)
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            sales=sales,
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "online_store_redirect"
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == ["online_store"]
        nav = resolve_commerce_navigator(
            message=MSG_LIVE,
            intent_name="start_order",
            merchant_sales_channels=sales,
        )
        assert nav.stage == "online_store_redirect"


class TestControlFShowroomOnly:
    def test_showroom_only_routes_directly_to_showroom(self) -> None:
        sales = _sales(showroom=True, whatsapp=False, maps_url=_MAPS)
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            maps_url=_MAPS,
            sales=sales,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == "showroom_visit"
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == ["showroom_visit"]
        nav = resolve_commerce_navigator(
            message=MSG_LIVE,
            intent_name="start_order",
            merchant_sales_channels=sales,
        )
        assert nav.stage == "showroom_visit"


class TestControlGHIExplicitCommitment:
    def test_explicit_whatsapp_does_not_replay_selector(self) -> None:
        sales = _sales(
            store=True, whatsapp=True, showroom=True,
            store_url=_STORE, maps_url=_MAPS,
        )
        ctx = _ctx(
            "طلب سريع واتساب",
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
            sales=sales,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") != "purchase_channel_selection"
        nav = resolve_commerce_navigator(
            message="طلب سريع واتساب",
            intent_name="start_order",
            merchant_sales_channels=sales,
        )
        assert nav.stage == "whatsapp_quick_order"

    def test_explicit_store_does_not_replay_selector(self) -> None:
        sales = _sales(
            store=True, whatsapp=True, showroom=True,
            store_url=_STORE, maps_url=_MAPS,
        )
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
            sales=sales,
            inbound_metadata={"button_id": "checkout_store_link"},
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == "online_store_redirect"
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_explicit_showroom_does_not_replay_selector(self) -> None:
        sales = _sales(
            store=True, whatsapp=True, showroom=True,
            store_url=_STORE, maps_url=_MAPS,
        )
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
            sales=sales,
            inbound_metadata={"button_id": "checkout_showroom_visit"},
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == "showroom_visit"
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_persisted_channel_commitment_is_not_replayed(self) -> None:
        prep = OrderPreparationState(
            checkout_channel="whatsapp_fast",
            purchase_channel_execution_active=True,
            purchase_channel_selection_source="verified_button_payload",
        )
        assert purchase_channel_committed(prep) is True
        owner = resolve_purchase_channel_entry_owner(
            message=MSG_LIVE,
            order_prep=prep,
            merchant_sales_channels=_sales(
                store=True, whatsapp=True, store_url=_STORE,
            ),
        )
        assert owner is None


class TestControlJEstablishedProduct:
    def test_product_focus_keeps_commerce_owner_off_selector(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            turn=4,
            current_product_focus={
                "id": "501",
                "external_id": "sku-white-shoe",
                "title": "حذاء رياضي أبيض",
                "price": 249,
            },
        )
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            sales=sales,
            state=state,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


class TestControlKAmbiguousStartOrder:
    def test_forced_start_order_on_social_does_not_select_channel(self) -> None:
        sales = _sales(
            store=True, whatsapp=True, showroom=True,
            store_url=_STORE, maps_url=_MAPS,
        )
        ctx = _ctx(
            MSG_SOCIAL,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
            sales=sales,
        )
        decision = _decide(ctx)
        _assert_not_groups(decision)
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.action == ACTION_LLM_REPLY


class TestControlLRetiredGroups:
    def test_start_order_never_returns_groups_menu(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        for msg in (MSG_START, MSG_LIVE, MSG_SOCIAL):
            ctx = _ctx(
                msg,
                db=MagicMock(),
                intent_name="start_order",
                store_url=_STORE,
                sales=sales,
            )
            decision = _decide(ctx)
            _assert_not_groups(decision)
            nav = try_catalog_navigation_decision(ctx)
            assert nav is None or nav.args.get("chosen_path") != PATH_GROUPS


class TestControlMTenantIsolation:
    def test_capabilities_are_tenant_scoped(self) -> None:
        tenant_store = _sales(store=True, whatsapp=True, store_url=_STORE)
        tenant_wa = _sales(whatsapp=True)
        a = resolve_available_purchase_channel_facts(
            merchant_sales_channels=tenant_store,
        )
        b = resolve_available_purchase_channel_facts(
            merchant_sales_channels=tenant_wa,
        )
        assert "online_store" in a
        assert "online_store" not in b
        ctx_a = _ctx(
            MSG_LIVE,
            tenant_id=41,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            sales=tenant_store,
        )
        ctx_b = _ctx(
            MSG_LIVE,
            tenant_id=42,
            db=MagicMock(),
            intent_name="start_order",
            sales=tenant_wa,
        )
        dec_a = _decide(ctx_a)
        dec_b = _decide(ctx_b)
        assert dec_a.args.get("topic") == "purchase_channel_selection"
        assert "online_store" in dec_a.args.get("available_purchase_channels")
        assert dec_b.args.get("topic") != "purchase_channel_selection"


class TestControlNGenericCommerce:
    def test_generic_perfume_merchant_is_not_honey_specific(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        ctx = _ctx(
            MSG_LIVE,
            tenant_id=77,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            sales=sales,
        )
        ctx.facts.store_name = "متجر تجريبي عام"
        ctx.facts.top_products = [
            {
                "id": "901",
                "external_id": "sku-rose-perfume",
                "title": "عطر ورد 100ml",
                "price": 180,
            },
        ]
        decision = _decide(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
        ]


def _actionable(state: MerchantConversationState, *, referent: Any = None) -> bool:
    return has_actionable_active_order_context(
        order_prep=state.order_prep,
        state=state,
        selected_product_referent=referent,
        current_product_focus=state.current_product_focus,
        stage=state.stage,
    )


class TestStaleShellControlALiveOrderingShell:
    def test_empty_ordering_shell_is_fresh_two_channel_entry(self) -> None:
        state = _stale_ordering_shell()
        assert state.stage == "ordering"
        assert (state.commerce_session or {}).get("stage") == "variant_selected"
        assert state.current_product_focus is None
        assert state.order_prep.checkout_channel == ""
        assert state.order_prep.awaiting_checkout_channel is False
        assert state.order_prep.quantity == 1
        assert state.order_prep.catalog_line_items_authoritative is False
        assert state.order_prep.customer_first_name
        assert is_active_order_flow(stage=state.stage, order_prep=state.order_prep) is True
        assert _actionable(state) is False
        assert is_genuine_purchase_channel_entry(
            message=MSG_LIVE,
            order_prep=state.order_prep,
            state=state,
            current_product_focus=None,
            selected_product_referent=None,
            stage=state.stage,
        ) is True
        ctx = _stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state)
        decision = _decide(ctx)
        _assert_not_groups(decision)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
        ]
        nav = resolve_commerce_navigator(
            message=MSG_LIVE,
            intent_name="start_order",
            stage=state.stage,
            order_prep=state.order_prep,
            state=state,
            merchant_sales_channels=_two_channel_sales(),
        )
        assert nav.stage == "purchase_channel_selection"


class TestStaleShellControlBLegacyVariantSelected:
    def test_variant_selected_label_without_product_is_not_active_checkout(self) -> None:
        state = _stale_ordering_shell()
        assert (state.commerce_session or {}).get("stage") == "variant_selected"
        assert _actionable(state) is False
        nav = resolve_commerce_navigator(
            message=MSG_LIVE,
            intent_name="start_order",
            stage=state.stage,
            order_prep=state.order_prep,
            state=state,
            merchant_sales_channels=_two_channel_sales(),
        )
        assert nav.stage != "whatsapp_quick_order"
        assert nav.stage == "purchase_channel_selection"


class TestStaleShellControlCIdentityDoesNotOwn:
    def test_name_and_address_alone_are_not_commerce_ownership(self) -> None:
        state = _stale_ordering_shell()
        assert state.order_prep.customer_first_name == "أحمد"
        assert state.order_prep.city == "الرياض"
        assert _actionable(state) is False
        decision = _decide(_stale_shell_ctx(MSG_START, intent_name="start_order", state=state))
        assert decision.args.get("topic") == "purchase_channel_selection"


class TestStaleShellControlDDefaultQuantityDoesNotOwn:
    def test_quantity_one_alone_is_not_active_order_evidence(self) -> None:
        state = _stale_ordering_shell()
        assert state.order_prep.quantity == 1
        assert _actionable(state) is False
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") == "purchase_channel_selection"


class TestStaleShellControlERealProductReferent:
    def test_valid_current_product_continues_commerce(self) -> None:
        state = _stale_ordering_shell()
        state.current_product_focus = dict(_SHOE)
        assert _actionable(state) is True
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


class TestStaleShellControlFAuthoritativeLineItems:
    def test_real_cart_items_continue_active_flow(self) -> None:
        state = _stale_ordering_shell()
        state.order_prep.line_items = [
            {
                "id": "501",
                "external_id": "sku-white-shoe",
                "title": "حذاء رياضي أبيض",
                "quantity": 1,
                "from_catalog_order": True,
                "product_retailer_id": "sku-white-shoe",
                "catalog_product_id": 501,
                "price": 249,
            },
        ]
        state.order_prep.catalog_line_items_authoritative = True
        assert _actionable(state) is True
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") != "purchase_channel_selection"


class TestStaleShellControlGValidDraft:
    def test_real_draft_order_continues_active_flow(self) -> None:
        state = _stale_ordering_shell()
        state.draft_order_id = "draft-901"
        assert _actionable(state) is True
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") != "purchase_channel_selection"


class TestStaleShellControlHCommittedChannel:
    def test_channel_only_leftover_reopens_selector(self) -> None:
        state = _stale_ordering_shell()
        state.order_prep.checkout_channel = "whatsapp_fast"
        assert _actionable(state) is False
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") == "purchase_channel_selection"
        nav = resolve_commerce_navigator(
            message=MSG_LIVE,
            intent_name="start_order",
            stage=state.stage,
            order_prep=state.order_prep,
            state=state,
            merchant_sales_channels=_two_channel_sales(),
        )
        assert nav.stage == "purchase_channel_selection"

    def test_verified_execution_does_not_replay_selector(self) -> None:
        state = _stale_ordering_shell()
        state.order_prep.checkout_channel = "whatsapp_fast"
        state.order_prep.purchase_channel_execution_active = True
        state.order_prep.purchase_channel_selection_source = "verified_button_payload"
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") != "purchase_channel_selection"
        nav = resolve_commerce_navigator(
            message=MSG_LIVE,
            intent_name="start_order",
            stage=state.stage,
            order_prep=state.order_prep,
            state=state,
            merchant_sales_channels=_two_channel_sales(),
        )
        assert nav.stage != "purchase_channel_selection"


class TestStaleShellControlIPaymentFulfillment:
    def test_real_payment_order_continues(self) -> None:
        state = _stale_ordering_shell()
        state.order_prep.product_id = "501"
        state.order_prep.order_status = "awaiting_payment"
        state.order_prep.awaiting_payment_receipt = True
        assert _actionable(state) is True
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") != "purchase_channel_selection"


class TestStaleShellControlJTwoChannels:
    def test_empty_shell_two_channels_selects_both(self) -> None:
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order"))
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
        ]


class TestStaleShellControlKThreeChannels:
    def test_empty_shell_three_channels_selects_all(self) -> None:
        sales = _sales(
            store=True,
            whatsapp=True,
            showroom=True,
            store_url=_STORE,
            maps_url=_MAPS,
        )
        ctx = _stale_shell_ctx(
            MSG_LIVE,
            sales=sales,
            intent_name="start_order",
            maps_url=_MAPS,
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]


class TestStaleShellControlLOneChannel:
    def test_empty_shell_one_channel_routes_directly(self) -> None:
        sales = _sales(store=True, whatsapp=False, store_url=_STORE)
        ctx = _stale_shell_ctx(MSG_LIVE, sales=sales, intent_name="start_order")
        decision = _decide(ctx)
        assert decision.args.get("topic") == "online_store_redirect"
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == ["online_store"]


class TestStaleShellControlMSocial:
    def test_social_plus_stale_shell_does_not_activate_commerce(self) -> None:
        state = _stale_ordering_shell()
        assert _actionable(state) is False
        ctx = _stale_shell_ctx(MSG_SOCIAL, state=state)
        decision = _decide(ctx)
        _assert_not_groups(decision)
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.args.get("topic") != "order_recovery"
        nav = resolve_commerce_navigator(
            message=MSG_SOCIAL,
            intent_name=str(ctx.intent.name),
            stage=state.stage,
            order_prep=state.order_prep,
            state=state,
            merchant_sales_channels=_two_channel_sales(),
        )
        assert nav.stage != "purchase_channel_selection"
        assert nav.stage != "whatsapp_quick_order"


class TestStaleShellControlNRetiredGroups:
    def test_stale_shell_start_order_never_returns_groups(self) -> None:
        ctx = _stale_shell_ctx(MSG_LIVE, intent_name="start_order")
        decision = _decide(ctx)
        _assert_not_groups(decision)
        nav = try_catalog_navigation_decision(ctx)
        assert nav is None or nav.args.get("chosen_path") != PATH_GROUPS


class TestStaleShellControlOPriorD10:
    def test_empty_state_two_channel_selector_still_passes(self) -> None:
        ctx = _ctx(
            MSG_LIVE,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            sales=_two_channel_sales(),
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
        ]


class TestStaleShellControlQTenantIsolation:
    def test_stale_shell_capabilities_remain_tenant_scoped(self) -> None:
        store_sales = _two_channel_sales()
        wa_sales = _sales(whatsapp=True)
        dec_a = _decide(
            _stale_shell_ctx(
                MSG_LIVE,
                tenant_id=41,
                sales=store_sales,
                intent_name="start_order",
            )
        )
        dec_b = _decide(
            _stale_shell_ctx(
                MSG_LIVE,
                tenant_id=42,
                sales=wa_sales,
                store_url="",
                intent_name="start_order",
            )
        )
        assert dec_a.args.get("topic") == "purchase_channel_selection"
        assert "online_store" in dec_a.args.get("available_purchase_channels")
        assert dec_b.args.get("topic") != "purchase_channel_selection"


_FREE_TEXT_LINE_ITEM = {
    "title": "حذاء رياضي أبيض",
    "name": "حذاء رياضي أبيض",
    "quantity": 1,
    "source": "free_text_mention",
    "match_status": "needs_review",
}

_CATALOG_LINE_ITEM = {
    "id": "501",
    "external_id": "sku-white-shoe",
    "title": "حذاء رياضي أبيض",
    "quantity": 1,
    "from_catalog_order": True,
    "product_retailer_id": "sku-white-shoe",
    "catalog_product_id": 501,
    "price": 249,
}


def _route_prep_with_embedded_state(
    *,
    current_product_focus: dict[str, Any] | None = None,
    draft_order_id: str = "",
    cart_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Mimic load_checkout_route_context embedding `_brain_state` on order_prep."""
    state = _stale_ordering_shell()
    state.current_product_focus = current_product_focus
    state.draft_order_id = draft_order_id or None
    if cart_items is not None:
        state.cart_items = list(cart_items)
    prep = state.order_prep.to_dict()
    prep["_brain_state"] = state.to_dict()
    return prep


class TestAuthoritativeLineItemSourceOfTruth:
    def test_free_text_needs_review_item_is_not_actionable(self) -> None:
        state = _stale_ordering_shell()
        state.order_prep.catalog_line_items_authoritative = False
        state.order_prep.line_items = [dict(_FREE_TEXT_LINE_ITEM)]
        assert state.order_prep.line_items[0]["title"]
        assert _actionable(state) is False
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") == "purchase_channel_selection"

    def test_catalog_backed_item_without_flag_is_actionable(self) -> None:
        state = _stale_ordering_shell()
        state.order_prep.catalog_line_items_authoritative = False
        state.order_prep.line_items = [dict(_CATALOG_LINE_ITEM)]
        assert _actionable(state) is True
        decision = _decide(_stale_shell_ctx(MSG_LIVE, intent_name="start_order", state=state))
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_free_text_cart_items_are_not_actionable(self) -> None:
        state = _stale_ordering_shell()
        state.cart_items = [dict(_FREE_TEXT_LINE_ITEM)]
        assert _actionable(state) is False

    def test_catalog_backed_cart_items_are_actionable(self) -> None:
        state = _stale_ordering_shell()
        state.cart_items = [dict(_CATALOG_LINE_ITEM)]
        assert _actionable(state) is True


class TestPersistedBrainStateProjection:
    def test_embedded_product_focus_is_actionable_without_explicit_state(self) -> None:
        prep = _route_prep_with_embedded_state(current_product_focus=dict(_SHOE))
        assert has_actionable_active_order_context(
            order_prep=prep,
            stage="ordering",
        ) is True
        assert _active_whatsapp_checkout(stage="ordering", order_prep=prep) is True

    def test_embedded_draft_id_is_actionable_without_explicit_state(self) -> None:
        prep = _route_prep_with_embedded_state(draft_order_id="draft-901")
        assert has_actionable_active_order_context(
            order_prep=prep,
            stage="ordering",
        ) is True
        assert _active_whatsapp_checkout(stage="ordering", order_prep=prep) is True

    def test_embedded_identity_shell_is_not_actionable(self) -> None:
        prep = _route_prep_with_embedded_state()
        assert prep.get("customer_first_name")
        assert prep.get("quantity") == 1
        assert has_actionable_active_order_context(
            order_prep=prep,
            stage="ordering",
        ) is False
        assert _active_whatsapp_checkout(stage="ordering", order_prep=prep) is False

