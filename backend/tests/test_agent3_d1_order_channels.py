"""AGENT-3 D1 — purchase-channel availability, trusted selection, persist/execute.

Generic commerce fixtures only. Phrases are acceptance examples, not runtime
triggers. Assert owner/state/ids — not customer-facing wording.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CHECKOUT_CHANNEL_STORE,
    CHECKOUT_CHANNEL_WHATSAPP,
    CheckoutRouteDecision,
    apply_selected_purchase_channel,
    extract_structured_purchase_channel_id,
    resolve_explicit_purchase_channel_payload,
    resolve_purchase_channel_entry_owner,
    validate_selected_purchase_channel,
)
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    MerchantSalesChannels,
    SalesChannelSlot,
    resolve_merchant_sales_channels,
    whatsapp_order_processing_ready,
)
from modules.ai.brain.commerce.store_url_resolver import (  # noqa: E402
    canonical_merchant_storefront_url,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_FAQ_REPLY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SELECT_PURCHASE_CHANNEL,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.executor import DefaultActionExecutor  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_STORE = "https://shop.example.sa"
_MAPS = "https://maps.google.com/?q=showroom"
_PHONE_A = "966500000011"
_PHONE_B = "966511111122"
_TENANT_A = 11
_TENANT_B = 44

_RAW_NL_ONLINE = "طيب أبي أطلب من الموقع حقكم"
_RAW_NL_SHOWROOM = "أجيكم للمعرض بعد الظهر"
_RAW_NL_WHATSAPP = "خلنا نكمل الطلب من الواتساب هنا"
_RAW_AMBIGUOUS = "مو متأكد"


def _slot(*, enabled: bool, available: bool, evidence: str) -> SalesChannelSlot:
    return SalesChannelSlot(enabled=enabled, available=available, evidence=evidence)


def _sales(
    *,
    store: bool = False,
    whatsapp: bool = False,
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
            evidence="whatsapp_order_processing" if whatsapp else "whatsapp_connection_unavailable",
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
        product_count=12,
        in_stock_count=12,
        has_active_integration=True,
        orderable=True,
        store_name="متجر تجريبي عام",
        store_url=store_url,
        maps_url=maps_url,
        store_url_source="structured_settings" if store_url else "none",
    )


def _awaiting_state(
    *,
    offered: list[str] | None = None,
    channel: str = "",
) -> MerchantConversationState:
    return MerchantConversationState(
        greeted=True,
        stage="purchase_channel_selection",
        turn=3,
        order_prep=OrderPreparationState(
            awaiting_checkout_channel=True,
            checkout_channel=channel,
            offered_purchase_channel_ids=list(
                offered
                or ["online_store", "whatsapp_quick_order", "showroom_visit"]
            ),
        ),
    )


def _ctx(
    msg: str,
    *,
    tenant_id: int = _TENANT_A,
    phone: str = _PHONE_A,
    intent_name: str = "general",
    sales: MerchantSalesChannels | None = None,
    state: MerchantConversationState | None = None,
    inbound_metadata: dict[str, Any] | None = None,
    intent_slots: dict[str, Any] | None = None,
    store_url: str = _STORE,
    maps_url: str = _MAPS,
    db: Any = None,
) -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=msg,
        intent=Intent(
            name=intent_name,
            confidence=0.9,
            raw_message=msg,
            slots=dict(intent_slots or {}),
        ),
        state=state or MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(store_url=store_url, maps_url=maps_url),
    )
    if sales is not None:
        ctx.merchant_sales_channels = sales  # type: ignore[attr-defined]
    if inbound_metadata is not None:
        ctx.inbound_metadata = inbound_metadata  # type: ignore[attr-defined]
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _decide(ctx: BrainContext):
    with patch(
        "modules.ai.brain.commerce.commerce_entry_catalog_delivery.try_commerce_entry_catalog_decision",
        return_value=None,
    ):
        return DefaultDecisionEngine().decide(ctx)


def _handle_select(decision: Decision, ctx: BrainContext):
    return asyncio.run(DefaultActionExecutor().execute(decision, ctx))


def _persist_ok_db(offered: list[str] | None = None) -> tuple[MagicMock, MagicMock]:
    conv = MagicMock()
    op = {
        "awaiting_checkout_channel": True,
        "offered_purchase_channel_ids": list(
            offered or ["online_store", "whatsapp_quick_order", "showroom_visit"]
        ),
    }
    conv.extra_metadata = {"brain_state": {"order_prep": dict(op)}}
    db = MagicMock()
    return db, conv


class TestAvailabilityA1A8:
    def test_a1_three_channels(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            _TENANT_A,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert sales.available_purchase_channel_ids() == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]

    def test_a2_online_and_whatsapp_omit_showroom(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            _TENANT_A,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url="",
            whatsapp_order_ready=True,
        )
        assert sales.available_purchase_channel_ids() == [
            "online_store",
            "whatsapp_quick_order",
        ]
        assert "showroom_visit" not in sales.available_purchase_channel_ids()

    def test_a3_showroom_only_direct_owner(self) -> None:
        sales = _sales(showroom=True, maps_url=_MAPS)
        owner = resolve_purchase_channel_entry_owner(
            message="ابي اطلب",
            intent=Intent(name="start_order", confidence=0.9, raw_message="ابي اطلب"),
            merchant_sales_channels=sales,
        )
        assert owner == "showroom_visit"
        ctx = _ctx(
            "ابي اطلب",
            intent_name="start_order",
            sales=sales,
            store_url="",
            maps_url=_MAPS,
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "showroom_visit"
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_a4_missing_online_url(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            _TENANT_A,
            store_url="",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert "online_store" not in sales.available_purchase_channel_ids()
        assert sales.store_url == ""

    def test_a5_malformed_online_url(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            _TENANT_A,
            store_url="not a url",
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert "online_store" not in sales.available_purchase_channel_ids()
        assert sales.store_url == ""
        assert canonical_merchant_storefront_url("not a url") == ""
        assert canonical_merchant_storefront_url("http://") == ""
        assert canonical_merchant_storefront_url("ftp://shop.example.sa") == ""
        assert canonical_merchant_storefront_url("https://app.nahlah.ai/register") == ""

    def test_a6_whatsapp_enabled_capability_unavailable(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            _TENANT_A,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=False,
        )
        assert sales.whatsapp_quick_order.enabled is True
        assert sales.whatsapp_quick_order.available is False
        assert sales.whatsapp_quick_order.evidence == "whatsapp_connection_unavailable"
        assert "whatsapp_quick_order" not in sales.available_purchase_channel_ids()

    def test_a7_whatsapp_only_ready_direct_owner(self) -> None:
        sales = _sales(whatsapp=True)
        ctx = _ctx(
            "ابي اطلب",
            intent_name="start_order",
            sales=sales,
            store_url="",
            maps_url="",
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("topic") == "whatsapp_quick_order"
        assert decision.args.get("available_purchase_channels") == [
            "whatsapp_quick_order",
        ]

    def test_a8_showroom_missing_valid_location(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            _TENANT_A,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url="not a maps url",
            whatsapp_order_ready=True,
        )
        assert "showroom_visit" not in sales.available_purchase_channel_ids()
        assert sales.maps_url == ""


class TestTrustedChromeProducer:
    """The existing semantic producer for selection is chrome, not NL slots."""

    def test_exact_button_title_selects_online_store(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "المتجر الإلكتروني",
            intent_name="ask_store_info",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "online_store"
        assert decision.args.get("selection_source") == "interactive_chrome"

    def test_button_id_selects_whatsapp(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "",
            intent_name="start_order",
            sales=sales,
            state=_awaiting_state(),
            inbound_metadata={"button_id": "checkout_whatsapp_fast"},
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "whatsapp_quick_order"

    def test_button_id_selects_showroom(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "",
            intent_name="ask_location",
            sales=sales,
            state=_awaiting_state(),
            inbound_metadata={"button_id": "checkout_showroom_visit"},
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "showroom_visit"

    def test_numbered_index_still_chrome(self) -> None:
        from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: PLC0415
            CheckoutChannelCapabilities,
        )

        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True, store_link=True, showroom_visit=True, store_url=_STORE
        )
        assert resolve_explicit_purchase_channel_payload("2", caps=caps) == CHECKOUT_CHANNEL_STORE


class TestRawNaturalLanguageIsNotAProducer:
    """Architecture: decision engine is pre-LLM and cannot emit structured NL selection."""

    @pytest.mark.parametrize(
        "message",
        [_RAW_NL_ONLINE, _RAW_NL_SHOWROOM, _RAW_NL_WHATSAPP, _RAW_AMBIGUOUS, "من هناك"],
    )
    def test_raw_paraphrase_does_not_emit_select_action(self, message: str) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            message,
            intent_name="general",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") not in {
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        }
        assert extract_structured_purchase_channel_id(message=message) is None


class TestUntrustedInboundRejected:
    def test_untrusted_inbound_selected_channel_id_rejected(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            _RAW_NL_WHATSAPP,
            intent_name="start_order",
            sales=sales,
            state=_awaiting_state(),
            inbound_metadata={
                "action": "select_purchase_channel",
                "selected_channel_id": "whatsapp_quick_order",
            },
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_SELECT_PURCHASE_CHANNEL
        assert extract_structured_purchase_channel_id(
            inbound_metadata={
                "action": "select_purchase_channel",
                "selected_channel_id": "online_store",
            }
        ) is None

    def test_intent_slot_injection_is_not_trusted(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            _RAW_NL_ONLINE,
            intent_name="ask_store_info",
            sales=sales,
            state=_awaiting_state(),
            intent_slots={"selected_channel_id": "online_store"},
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_SELECT_PURCHASE_CHANNEL


class TestFaqSocialDoNotStealOrForce:
    def test_faq_does_not_steal_valid_chrome_selection(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "المتجر الإلكتروني",
            intent_name="ask_store_info",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "online_store"

    def test_unrelated_faq_is_not_forcibly_a_selection(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "وش ساعات العمل؟",
            intent_name="ask_store_info",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") not in {
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        }

    def test_location_question_is_not_forcibly_a_selection(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "وين موقعكم؟",
            intent_name="ask_location",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("topic") != "showroom_visit" or decision.action == ACTION_FAQ_REPLY

    def test_ambiguous_choice_remains_with_the_model(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            _RAW_AMBIGUOUS,
            intent_name="general",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") in {None, ""}


class TestValidationAndPersistence:
    def test_selected_not_offered_rejected(self) -> None:
        result = validate_selected_purchase_channel(
            selected_channel_id="showroom_visit",
            tenant_id=_TENANT_A,
            merchant_sales_channels=_sales(store=True, whatsapp=True, store_url=_STORE),
            offered_purchase_channel_ids=["online_store", "whatsapp_quick_order"],
        )
        assert result.accepted is False
        assert result.reason == "channel_not_offered"

    def test_empty_offered_list_fails_closed(self) -> None:
        result = validate_selected_purchase_channel(
            selected_channel_id="online_store",
            tenant_id=_TENANT_A,
            merchant_sales_channels=_sales(store=True, store_url=_STORE),
            offered_purchase_channel_ids=[],
        )
        assert result.accepted is False
        assert result.reason == "no_offered_channels"

    def test_channel_unavailable_before_execution_rejected(self) -> None:
        result = validate_selected_purchase_channel(
            selected_channel_id="online_store",
            tenant_id=_TENANT_A,
            merchant_sales_channels=_sales(whatsapp=True),
            offered_purchase_channel_ids=["online_store", "whatsapp_quick_order"],
        )
        assert result.accepted is False
        assert result.reason == "channel_unavailable"

    def test_persist_keeps_offered_list_authority(self) -> None:
        db, conv = _persist_ok_db(
            offered=["online_store", "whatsapp_quick_order", "showroom_visit"]
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(
                conv,
                {
                    "order_prep": {
                        "awaiting_checkout_channel": True,
                        "offered_purchase_channel_ids": [
                            "online_store",
                            "whatsapp_quick_order",
                            "showroom_visit",
                        ],
                    }
                },
            ),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._storefront_delivery_decision",
            return_value=CheckoutRouteDecision(
                reply_text="",
                reason="store_link_delivered",
                cta_url=_STORE,
                cta_label="المتجر",
            ),
        ):
            result = apply_selected_purchase_channel(
                db,
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
                selected_channel_id="online_store",
                merchant_sales_channels=_sales(
                    store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS
                ),
                offered_purchase_channel_ids=[
                    "online_store",
                    "whatsapp_quick_order",
                    "showroom_visit",
                ],
            )
        assert result.accepted is True
        assert result.persist_ok is True
        op = conv.extra_metadata["brain_state"]["order_prep"]
        assert op["checkout_channel"] == CHECKOUT_CHANNEL_STORE
        assert op["awaiting_checkout_channel"] is False
        assert op["offered_purchase_channel_ids"] == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]


class TestHandlerPersistAndExecute:
    def _brain_decision(self, channel: str) -> Decision:
        return Decision(
            action=ACTION_SELECT_PURCHASE_CHANNEL,
            args={"selected_channel_id": channel},
            reason="injected brain structured decision",
            confidence=0.99,
        )

    def test_rejected_selection_returns_success_false(self) -> None:
        ctx = _ctx(
            "",
            sales=_sales(store=True, store_url=_STORE),
            state=_awaiting_state(offered=["online_store"]),
        )
        result = _handle_select(self._brain_decision("showroom_visit"), ctx)
        assert result.success is False
        assert result.data.get("accepted") is False
        assert result.data.get("executed") is False

    def test_persistence_failure_returns_success_false_and_does_not_execute(self) -> None:
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=MagicMock(),
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._storefront_delivery_decision",
        ) as store_owner, patch(
            "modules.ai.brain.commerce.checkout_route_owner._showroom_delivery_decision",
        ) as showroom_owner:
            result = _handle_select(self._brain_decision("online_store"), ctx)
        assert result.success is False
        assert result.data.get("reason") == "persist_failed"
        assert result.data.get("executed") is False
        assert result.data.get("execution_owner") in {None, ""}
        store_owner.assert_not_called()
        showroom_owner.assert_not_called()

    def test_persist_success_executes_storefront_owner(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(conv.extra_metadata["brain_state"]["order_prep"])}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._storefront_delivery_decision",
            return_value=CheckoutRouteDecision(
                reply_text="",
                reason="store_link_delivered",
                cta_url=_STORE,
                cta_label="المتجر",
            ),
        ) as store_owner:
            result = _handle_select(self._brain_decision("online_store"), ctx)
        assert result.success is True
        assert result.data.get("persist_ok") is True
        assert result.data.get("executed") is True
        assert result.data.get("execution_owner") == "storefront_cta_owner"
        assert result.data.get("cta_url") == _STORE
        store_owner.assert_called_once()

    def test_persist_success_executes_showroom_owner(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(conv.extra_metadata["brain_state"]["order_prep"])}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._showroom_delivery_decision",
            return_value=CheckoutRouteDecision(
                reply_text="",
                reason="showroom_location_delivered",
                cta_url=_MAPS,
                cta_label="موقع المعرض",
            ),
        ) as showroom_owner:
            result = _handle_select(self._brain_decision("showroom_visit"), ctx)
        assert result.success is True
        assert result.data.get("execution_owner") == "showroom_maps_owner"
        assert result.data.get("cta_url") == _MAPS
        showroom_owner.assert_called_once()

    def test_persist_success_executes_whatsapp_order_owner(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(conv.extra_metadata["brain_state"]["order_prep"])}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._storefront_delivery_decision",
        ) as store_owner, patch(
            "modules.ai.brain.commerce.checkout_route_owner._showroom_delivery_decision",
        ) as showroom_owner:
            result = _handle_select(self._brain_decision("whatsapp_quick_order"), ctx)
        assert result.success is True
        assert result.data.get("execution_owner") == "whatsapp_quick_order_owner"
        assert result.data.get("checkout_channel") == CHECKOUT_CHANNEL_WHATSAPP
        store_owner.assert_not_called()
        showroom_owner.assert_not_called()


class TestCrossTenantB10:
    def test_b10_distinct_tenants_phones_offers_and_configs(self) -> None:
        sales_a = _sales(store=True, store_url=_STORE)
        sales_b = _sales(whatsapp=True, showroom=True, maps_url=_MAPS)
        offered_a = ["online_store"]
        offered_b = ["whatsapp_quick_order", "showroom_visit"]
        assert _TENANT_A != _TENANT_B
        assert _PHONE_A != _PHONE_B
        assert offered_a != offered_b
        assert sales_a.available_purchase_channel_ids() != sales_b.available_purchase_channel_ids()

        mine = validate_selected_purchase_channel(
            selected_channel_id="online_store",
            tenant_id=_TENANT_A,
            merchant_sales_channels=sales_a,
            offered_purchase_channel_ids=offered_a,
        )
        other_on_a = validate_selected_purchase_channel(
            selected_channel_id="whatsapp_quick_order",
            tenant_id=_TENANT_A,
            merchant_sales_channels=sales_a,
            offered_purchase_channel_ids=offered_a,
        )
        other = validate_selected_purchase_channel(
            selected_channel_id="whatsapp_quick_order",
            tenant_id=_TENANT_B,
            merchant_sales_channels=sales_b,
            offered_purchase_channel_ids=offered_b,
        )
        steal = validate_selected_purchase_channel(
            selected_channel_id="online_store",
            tenant_id=_TENANT_B,
            merchant_sales_channels=sales_b,
            offered_purchase_channel_ids=offered_b,
        )
        assert mine.accepted is True
        assert other_on_a.accepted is False
        assert other.accepted is True
        assert steal.accepted is False

        ctx_a = _ctx(
            "المتجر الإلكتروني",
            tenant_id=_TENANT_A,
            phone=_PHONE_A,
            sales=sales_a,
            state=_awaiting_state(offered=offered_a),
            store_url=_STORE,
            maps_url="",
        )
        ctx_b = _ctx(
            "",
            tenant_id=_TENANT_B,
            phone=_PHONE_B,
            intent_name="start_order",
            sales=sales_b,
            state=_awaiting_state(offered=offered_b),
            inbound_metadata={"button_id": "checkout_whatsapp_fast"},
            store_url="",
            maps_url=_MAPS,
        )
        dec_a = _decide(ctx_a)
        dec_b = _decide(ctx_b)
        assert dec_a.args.get("selected_channel_id") == "online_store"
        assert dec_b.args.get("selected_channel_id") == "whatsapp_quick_order"
        assert ctx_a.tenant_id != ctx_b.tenant_id
        assert ctx_a.customer_phone != ctx_b.customer_phone


class TestWhatsAppOrderTruthVsCatalog:
    def test_native_catalog_unavailable_but_conversational_whatsapp_ready(self) -> None:
        conn = MagicMock()
        conn.status = "connected"
        conn.phone_number_id = "1234567890"
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = conn
        with patch(
            "core.native_catalog_capability.evaluate_native_catalog_capability",
            return_value=MagicMock(eligible=False),
        ):
            from core.native_catalog_capability import (  # noqa: PLC0415
                evaluate_native_catalog_capability,
            )

            catalog_ready = bool(evaluate_native_catalog_capability(db, _TENANT_A).eligible)
            wa_ready = whatsapp_order_processing_ready(db, _TENANT_A)
        assert catalog_ready is False
        assert wa_ready is True
        sales = resolve_merchant_sales_channels(
            db,
            _TENANT_A,
            store_url="",
            maps_url="",
            whatsapp_order_ready=True,
        )
        assert sales.whatsapp_quick_order.available is True
        assert "whatsapp_quick_order" in sales.available_purchase_channel_ids()

    def test_whatsapp_order_ready_uses_connection_not_catalog_file(self) -> None:
        import inspect

        import modules.ai.brain.commerce.sales_channel_capabilities as sc  # noqa: PLC0415

        source = inspect.getsource(sc.whatsapp_order_processing_ready)
        assert "from core.native_catalog_capability" not in source
        assert "WhatsAppConnection" in source


class TestTenant33ReadinessContract:
    def test_tenant33_readiness_contract_read_only(self) -> None:
        url = (
            os.environ.get("DATABASE_PUBLIC_URL")
            or os.environ.get("DATABASE_URL")
            or ""
        ).strip()
        if not url or "sqlite" in url.lower():
            pytest.skip("no live postgres url for tenant-33 read-only probe")
        from sqlalchemy import create_engine, text  # noqa: PLC0415

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("BEGIN READ ONLY"))
            wa = conn.execute(
                text(
                    """
                    SELECT status, phone_number_id
                    FROM whatsapp_connections
                    WHERE tenant_id = 33
                    LIMIT 1
                    """
                )
            ).mappings().first()
            toggle_row = conn.execute(
                text(
                    """
                    SELECT store_settings
                    FROM tenant_settings
                    WHERE tenant_id = 33
                    LIMIT 1
                    """
                )
            ).mappings().first()
            conn.rollback()
        status = str((wa or {}).get("status") or "")
        phone_id = (wa or {}).get("phone_number_id")
        settings = dict((toggle_row or {}).get("store_settings") or {})
        channels = settings.get("sales_channels") or {}
        wa_toggle = True
        if isinstance(channels, dict):
            entry = channels.get("whatsapp_quick_order") or {}
            if isinstance(entry, dict) and "enabled" in entry:
                wa_toggle = bool(entry.get("enabled"))
        wa_ready = bool(wa_toggle and status == "connected" and phone_id)
        assert wa_ready is True


class TestCanonicalStoreUrl:
    def test_valid_https_kept(self) -> None:
        assert canonical_merchant_storefront_url(_STORE) == _STORE

    def test_empty_rejected(self) -> None:
        assert canonical_merchant_storefront_url("") == ""
        assert canonical_merchant_storefront_url("   ") == ""
