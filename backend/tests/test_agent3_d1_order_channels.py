"""AGENT3-D1A — channel availability and verified chrome execution.

Proven scope: canonical availability, storefront URL, WhatsApp order
readiness, WhatsApp-only entry, verified button/title chrome, validation,
required persistence, deterministic CTA/state execution.

NATURAL_LANGUAGE_SELECTION_IMPLEMENTED=NO
ARCHITECTURE_BLOCKER=POST-SEMANTIC STRUCTURED ACTION PATH MISSING

Generic commerce fixtures only. Phrases are acceptance examples, not runtime
triggers. Assert owner/state/ids — not customer-facing wording.

Structured-action contract tests may inject ``action=select_purchase_channel``
to prove validation/persistence/execution. That is not semantic-language proof.

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
    EXECUTION_EVIDENCE_SHOWROOM_CTA,
    EXECUTION_EVIDENCE_STORE_CTA,
    EXECUTION_EVIDENCE_WHATSAPP_STATE,
    REASON_AWAITING_PERSIST_FAILED,
    REASON_CAPABILITY_EXECUTION_FAILED,
    SELECTION_SOURCE_CONSTRAINED_TEXT,
    SELECTION_SOURCE_VERIFIED_BUTTON,
    apply_selected_purchase_channel,
    extract_structured_purchase_channel_id,
    resolve_explicit_purchase_channel_payload,
    resolve_available_purchase_channel_facts,
    resolve_purchase_channel_entry_owner,
    resolve_purchase_channel_turn,
    validate_selected_purchase_channel,
    has_actionable_active_order_context,
    has_verified_purchase_channel_execution,
    persist_checkout_route_state,
    purchase_channel_blocks_new_entry,
    should_block_bare_start_product_prompt,
)
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    MerchantSalesChannels,
    SalesChannelSlot,
    resolve_merchant_sales_channels,
    whatsapp_order_processing_ready,
)
from modules.ai.brain.commerce.commerce_navigator import (  # noqa: E402
    resolve_commerce_navigator,
)
from modules.ai.brain.commerce.store_url_resolver import (  # noqa: E402
    canonical_merchant_storefront_url,
)
from modules.ai.brain.commerce.conversation_context_reset import (  # noqa: E402
    clear_active_order_context,
)
from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_bare_start_order_guard_reply,
)
from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: E402
    try_guard_recovery_reply,
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
    if offered is None:
        offered = ["online_store", "whatsapp_quick_order", "showroom_visit"]
    return MerchantConversationState(
        greeted=True,
        stage="purchase_channel_selection",
        turn=3,
        order_prep=OrderPreparationState(
            awaiting_checkout_channel=True,
            checkout_channel=channel,
            offered_purchase_channel_ids=list(offered),
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


def _decide_live(ctx: BrainContext):
    """Production-shaped decide — catalog delivery is not stubbed."""
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


def _run_persist(
    conv: MagicMock,
    db: MagicMock,
    **kwargs: Any,
) -> bool:
    with patch(
        "core.order_flow._load_brain_state",
        return_value=(
            conv,
            dict(conv.extra_metadata.get("brain_state") or {}),
        ),
    ):
        return persist_checkout_route_state(
            db,
            tenant_id=_TENANT_A,
            phone=_PHONE_A,
            **kwargs,
        )


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
        assert decision.args.get("available_purchase_channels") == [
            "whatsapp_quick_order",
        ]
        assert decision.args.get("topic") != "purchase_channel_selection"
        # No db → persist fails; do not claim a committed WhatsApp order.
        assert decision.args.get("committed") is not True
        assert decision.args.get("execution_evidence") != EXECUTION_EVIDENCE_WHATSAPP_STATE
        assert decision.args.get("topic") != "whatsapp_quick_order"

    def test_whatsapp_only_persist_success_commits_order_state(self) -> None:
        sales = _sales(whatsapp=True)
        intent = Intent(name="start_order", confidence=0.9, raw_message="ابي اطلب")
        db, conv = _persist_ok_db(offered=["whatsapp_quick_order"])
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(
                conv,
                {"order_prep": dict(conv.extra_metadata["brain_state"]["order_prep"])},
            ),
        ):
            turn = resolve_purchase_channel_turn(
                phase="entry",
                message="ابي اطلب",
                intent=intent,
                merchant_sales_channels=sales,
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
                db=db,
            )
        assert turn is not None
        assert turn.args.get("committed") is True
        assert turn.args.get("executed") is True
        assert turn.args.get("persist_ok") is True
        assert turn.args.get("execution_evidence") == EXECUTION_EVIDENCE_WHATSAPP_STATE
        assert turn.args.get("topic") == "whatsapp_quick_order"
        assert turn.args.get("awaiting_checkout_channel") is False
        assert turn.args.get("checkout_channel") == CHECKOUT_CHANNEL_WHATSAPP
        op = conv.extra_metadata["brain_state"]["order_prep"]
        assert op["checkout_channel"] == CHECKOUT_CHANNEL_WHATSAPP
        assert op["awaiting_checkout_channel"] is False

    def test_whatsapp_only_persist_failure_is_not_committed(self) -> None:
        sales = _sales(whatsapp=True)
        intent = Intent(name="start_order", confidence=0.9, raw_message="ابي اطلب")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ):
            turn = resolve_purchase_channel_turn(
                phase="entry",
                message="ابي اطلب",
                intent=intent,
                merchant_sales_channels=sales,
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
                db=MagicMock(),
            )
        assert turn is not None
        assert turn.reason == "persist_failed"
        assert turn.args.get("committed") is False
        assert turn.args.get("executed") is False
        assert turn.args.get("persist_ok") is False
        assert turn.args.get("execution_evidence") in {None, ""}
        assert turn.args.get("checkout_channel") in {None, ""}
        assert turn.args.get("topic") != "whatsapp_quick_order"
        assert turn.args.get("response_goal") != "collect_product_for_whatsapp_order"
        assert turn.args.get("cta_url") in {None, ""}
        assert turn.args.get("awaiting_checkout_channel") is True
        assert turn.args.get("available_purchase_channels") == ["whatsapp_quick_order"]

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


class TestVerifiedChromeProducer:
    """D1A producer is verified interactive chrome, not customer language."""

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
        assert decision.args.get("selection_source") == SELECTION_SOURCE_CONSTRAINED_TEXT

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
        assert decision.args.get("selection_source") == SELECTION_SOURCE_VERIFIED_BUTTON

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
        assert decision.args.get("selection_source") == SELECTION_SOURCE_VERIFIED_BUTTON

    def test_numbered_index_still_chrome(self) -> None:
        from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: PLC0415
            CheckoutChannelCapabilities,
        )

        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True, store_link=True, showroom_visit=True, store_url=_STORE
        )
        assert resolve_explicit_purchase_channel_payload("2", caps=caps) == CHECKOUT_CHANNEL_STORE
        assert (
            resolve_explicit_purchase_channel_payload(
                "2",
                caps=caps,
                offered_purchase_channel_ids=[
                    "whatsapp_quick_order",
                    "online_store",
                    "showroom_visit",
                ],
            )
            == CHECKOUT_CHANNEL_STORE
        )


class TestNaturalLanguageSelectionNotImplemented:
    """D1A: raw paraphrases must not emit select_purchase_channel."""

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

    def test_navigator_rejects_untrusted_slot_and_metadata_ids(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        awaiting = {
            "awaiting_checkout_channel": True,
            "offered_purchase_channel_ids": [
                "online_store",
                "whatsapp_quick_order",
                "showroom_visit",
            ],
        }
        nav = resolve_commerce_navigator(
            message=_RAW_AMBIGUOUS,
            intent_name="general",
            intent_slots={"selected_channel_id": "online_store"},
            inbound_metadata={"selected_channel_id": "showroom_visit"},
            order_prep=awaiting,
            merchant_sales_channels=sales,
            store_url=_STORE,
            maps_url=_MAPS,
        )
        assert nav.stage == "purchase_channel_selection"
        nav_chrome = resolve_commerce_navigator(
            message="",
            intent_name="general",
            inbound_metadata={"button_id": "checkout_store_link"},
            order_prep=awaiting,
            merchant_sales_channels=sales,
            store_url=_STORE,
            maps_url=_MAPS,
        )
        assert nav_chrome.stage == "online_store_redirect"


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


class TestStructuredActionContractPersistAndExecute:
    def _structured_action(self, channel: str) -> Decision:
        return Decision(
            action=ACTION_SELECT_PURCHASE_CHANNEL,
            args={"selected_channel_id": channel},
            reason="structured-action contract injection — not semantic-language proof",
            confidence=0.99,
        )

    def test_rejected_selection_returns_success_false(self) -> None:
        ctx = _ctx(
            "",
            sales=_sales(store=True, store_url=_STORE),
            state=_awaiting_state(offered=["online_store"]),
        )
        result = _handle_select(self._structured_action("showroom_visit"), ctx)
        assert result.success is False
        assert result.data.get("accepted") is False
        assert result.data.get("executed") is False

    def test_cta_prepared_then_persistence_fails_does_not_commit_or_send(self) -> None:
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
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ):
            result = _handle_select(self._structured_action("online_store"), ctx)
        assert result.success is False
        assert result.data.get("reason") == "persist_failed"
        assert result.data.get("executed") is False
        assert result.data.get("committed") is False
        assert result.data.get("cta_url") in {None, ""}
        op = conv.extra_metadata["brain_state"]["order_prep"]
        assert op.get("checkout_channel") in {None, ""}
        assert op["awaiting_checkout_channel"] is True

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
            result = _handle_select(self._structured_action("online_store"), ctx)
        assert result.success is True
        assert result.data.get("persist_ok") is True
        assert result.data.get("executed") is True
        assert result.data.get("execution_owner") == "storefront_cta_owner"
        assert result.data.get("cta_url") == _STORE
        store_owner.assert_called_once()
        assert result.data.get("execution_evidence") == EXECUTION_EVIDENCE_STORE_CTA

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
            result = _handle_select(self._structured_action("showroom_visit"), ctx)
        assert result.success is True
        assert result.data.get("execution_owner") == "showroom_maps_owner"
        assert result.data.get("cta_url") == _MAPS
        showroom_owner.assert_called_once()
        assert result.data.get("execution_evidence") == EXECUTION_EVIDENCE_SHOWROOM_CTA

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
            result = _handle_select(self._structured_action("whatsapp_quick_order"), ctx)
        assert result.success is True
        assert result.data.get("execution_owner") == "whatsapp_quick_order_owner"
        assert result.data.get("checkout_channel") == CHECKOUT_CHANNEL_WHATSAPP
        store_owner.assert_not_called()
        showroom_owner.assert_not_called()
        assert result.data.get("execution_evidence") == EXECUTION_EVIDENCE_WHATSAPP_STATE


class TestExecutionTruthGate:
    def _structured_action(self, channel: str) -> Decision:
        return Decision(
            action=ACTION_SELECT_PURCHASE_CHANNEL,
            args={"selected_channel_id": channel},
            reason="structured-action contract injection — not semantic-language proof",
            confidence=0.99,
        )

    def _op(self, conv: MagicMock) -> dict[str, Any]:
        return dict(conv.extra_metadata["brain_state"]["order_prep"])

    def test_store_execution_failure_keeps_awaiting_recoverable(self) -> None:
        db, conv = _persist_ok_db()
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx("", sales=sales, state=_awaiting_state(), db=db)
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(self._op(conv))}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._storefront_delivery_decision",
            side_effect=RuntimeError("store owner down"),
        ):
            result = _handle_select(self._structured_action("online_store"), ctx)
        assert result.success is False
        assert result.data.get("executed") is False
        assert result.data.get("committed") is False
        assert result.data.get("cta_url") in {None, ""}
        assert result.data.get("reason") == REASON_CAPABILITY_EXECUTION_FAILED
        op = self._op(conv)
        assert op.get("checkout_channel") in {None, ""}
        assert op["awaiting_checkout_channel"] is True
        assert op["offered_purchase_channel_ids"] == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        next_ctx = _ctx(
            "",
            sales=sales,
            state=_awaiting_state(),
            inbound_metadata={"button_id": "checkout_whatsapp_fast"},
        )
        nxt = _decide(next_ctx)
        assert nxt.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert nxt.args.get("selected_channel_id") == "whatsapp_quick_order"

    def test_showroom_execution_failure_keeps_awaiting_recoverable(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(self._op(conv))}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._showroom_delivery_decision",
            return_value=CheckoutRouteDecision(
                reply_text="",
                reason="showroom_visit_unavailable",
                cta_url="",
            ),
        ):
            result = _handle_select(self._structured_action("showroom_visit"), ctx)
        assert result.success is False
        assert result.data.get("executed") is False
        assert result.data.get("committed") is False
        op = self._op(conv)
        assert op.get("checkout_channel") in {None, ""}
        assert op["awaiting_checkout_channel"] is True
        assert op["offered_purchase_channel_ids"] == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]

    def test_cta_prepared_then_persistence_fails_does_not_send_or_commit(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(self._op(conv))}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._storefront_delivery_decision",
            return_value=CheckoutRouteDecision(
                reply_text="",
                reason="store_link_delivered",
                cta_url=_STORE,
                cta_label="المتجر",
            ),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ):
            result = _handle_select(self._structured_action("online_store"), ctx)
        assert result.success is False
        assert result.data.get("cta_url") in {None, ""}
        assert result.data.get("executed") is False
        assert result.data.get("committed") is False
        op = self._op(conv)
        assert op.get("checkout_channel") in {None, ""}
        assert op["awaiting_checkout_channel"] is True

    def test_store_and_showroom_full_success_persists_after_cta(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(self._op(conv))}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._storefront_delivery_decision",
            return_value=CheckoutRouteDecision(
                reply_text="",
                reason="store_link_delivered",
                cta_url=_STORE,
                cta_label="المتجر",
            ),
        ):
            store = _handle_select(self._structured_action("online_store"), ctx)
        assert store.success is True
        assert store.data.get("cta_url") == _STORE
        assert store.data.get("executed") is True
        assert store.data.get("committed") is True
        op = self._op(conv)
        assert op["checkout_channel"] == CHECKOUT_CHANNEL_STORE
        assert op["awaiting_checkout_channel"] is False

        db2, conv2 = _persist_ok_db()
        ctx2 = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db2,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv2, {"order_prep": dict(self._op(conv2))}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner._showroom_delivery_decision",
            return_value=CheckoutRouteDecision(
                reply_text="",
                reason="showroom_location_delivered",
                cta_url=_MAPS,
                cta_label="موقع المعرض",
            ),
        ):
            showroom = _handle_select(self._structured_action("showroom_visit"), ctx2)
        assert showroom.success is True
        assert showroom.data.get("cta_url") == _MAPS
        assert showroom.data.get("executed") is True
        assert self._op(conv2)["awaiting_checkout_channel"] is False

    def test_whatsapp_persistence_failure_keeps_awaiting_recoverable(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ):
            result = _handle_select(self._structured_action("whatsapp_quick_order"), ctx)
        assert result.success is False
        assert result.data.get("executed") is False
        assert result.data.get("committed") is False
        op = self._op(conv)
        assert op.get("checkout_channel") in {None, ""}
        assert op["awaiting_checkout_channel"] is True

    def test_whatsapp_persistence_success_commits_order_state(self) -> None:
        db, conv = _persist_ok_db()
        ctx = _ctx(
            "",
            sales=_sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS),
            state=_awaiting_state(),
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": dict(self._op(conv))}),
        ):
            result = _handle_select(self._structured_action("whatsapp_quick_order"), ctx)
        assert result.success is True
        assert result.data.get("executed") is True
        assert result.data.get("execution_evidence") == EXECUTION_EVIDENCE_WHATSAPP_STATE
        op = self._op(conv)
        assert op["checkout_channel"] == CHECKOUT_CHANNEL_WHATSAPP
        assert op["awaiting_checkout_channel"] is False

    def test_initial_awaiting_persist_failure_disables_selection_actions(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        intent = Intent(name="start_order", confidence=0.9, raw_message="ابي اطلب")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ):
            turn = resolve_purchase_channel_turn(
                phase="entry",
                message="ابي اطلب",
                intent=intent,
                merchant_sales_channels=sales,
                store_url=_STORE,
                maps_url=_MAPS,
                store_url_source="structured_settings",
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
                db=MagicMock(),
            )
        assert turn is not None
        assert turn.reason == REASON_AWAITING_PERSIST_FAILED
        assert turn.args.get("durable_choice_state") is False
        assert turn.args.get("selection_actions_enabled") is False
        assert turn.args.get("offered_purchase_channel_ids") == []
        ctx = _ctx(
            "المتجر الإلكتروني",
            intent_name="ask_store_info",
            sales=sales,
            state=_awaiting_state(offered=[]),
        )
        assert _decide(ctx).action != ACTION_SELECT_PURCHASE_CHANNEL
        assert extract_structured_purchase_channel_id(
            message="المتجر الإلكتروني",
            offered_purchase_channel_ids=[],
        ) is None
        assert extract_structured_purchase_channel_id(
            message="2",
            offered_purchase_channel_ids=[],
        ) is None


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


_LIVE_BUY = "ابي اشتري"


class TestLivePickerBypassRegression:
    """Tenant-33 live shape: «ابي اشتري» must use resolved platform channels."""

    def _three(self) -> MerchantSalesChannels:
        return _sales(
            store=True,
            whatsapp=True,
            showroom=True,
            store_url=_STORE,
            maps_url=_MAPS,
        )

    def test_a_three_channels_unpatched_catalog_opens_picker(self) -> None:
        from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (
            try_commerce_entry_catalog_decision,
        )

        sales = self._three()
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=sales,
            store_url=_STORE,
            maps_url=_MAPS,
            db=MagicMock(),
        )
        assert try_commerce_entry_catalog_decision(ctx) is None
        decision = _decide_live(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        assert decision.args.get("topic") != "whatsapp_quick_order"

    def test_b_whatsapp_and_showroom_opens_picker(self) -> None:
        sales = _sales(whatsapp=True, showroom=True, maps_url=_MAPS)
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=sales,
            store_url="",
            maps_url=_MAPS,
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "whatsapp_quick_order",
            "showroom_visit",
        ]

    def test_c_online_and_whatsapp_opens_picker(self) -> None:
        sales = _sales(store=True, whatsapp=True, store_url=_STORE)
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=sales,
            store_url=_STORE,
            maps_url="",
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
        ]

    def test_d_showroom_only_direct_owner(self) -> None:
        sales = _sales(showroom=True, maps_url=_MAPS)
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=sales,
            store_url="",
            maps_url=_MAPS,
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") == "showroom_visit"
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_e_whatsapp_only_persist_gate_unchanged(self) -> None:
        sales = _sales(whatsapp=True)
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=sales,
            store_url="",
            maps_url="",
            db=MagicMock(),
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            decision = _decide_live(ctx)
        assert decision.args.get("topic") == "whatsapp_quick_order"
        assert decision.args.get("committed") is True
        assert decision.args.get("execution_evidence") == EXECUTION_EVIDENCE_WHATSAPP_STATE
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ):
            failed = _decide_live(ctx)
        assert failed.args.get("committed") is not True
        assert failed.args.get("topic") != "whatsapp_quick_order"
        assert failed.args.get("cta_url") in {None, ""}

    def test_f_online_only_direct_owner(self) -> None:
        sales = _sales(store=True, store_url=_STORE)
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=sales,
            store_url=_STORE,
            maps_url="",
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") == "online_store_redirect"
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_g_stale_catalog_shell_does_not_suppress_picker(self) -> None:
        sales = self._three()
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=8,
            current_product_focus=None,
            commerce_session={"stage": "browsing", "active_product": ""},
            order_prep=OrderPreparationState(
                quantity=1,
                checkout_channel="",
                awaiting_checkout_channel=False,
            ),
        )
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=sales,
            state=state,
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert "showroom_visit" in (decision.args.get("available_purchase_channels") or [])

    def test_h_clean_state_opens_picker(self) -> None:
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=self._three(),
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"

    def test_i_active_checkout_is_preserved(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="checkout",
            turn=6,
            current_product_focus={
                "id": "501",
                "title": "حذاء رياضي أبيض",
            },
            order_prep=OrderPreparationState(
                product_id="501",
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                awaiting_checkout_channel=False,
            ),
        )
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=self._three(),
            state=state,
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_j_greeting_does_not_open_picker(self) -> None:
        ctx = _ctx(
            "مرحبا كيف الحال",
            intent_name="greeting",
            sales=self._three(),
            db=MagicMock(),
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_production_ctx_without_sales_object_uses_tenant_capabilities(self) -> None:
        sales = self._three()
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            store_url="",
            maps_url="",
            db=MagicMock(),
        )
        assert getattr(ctx, "merchant_sales_channels", None) is None
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            return_value=sales,
        ):
            decision = _decide_live(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]

    def test_cross_tenant_resolver_uses_ctx_tenant_id(self) -> None:
        seen: list[int] = []

        def _fake_resolve(db, tenant_id, **kwargs):
            seen.append(int(tenant_id))
            if int(tenant_id) == _TENANT_A:
                return _sales(store=True, whatsapp=True, store_url=_STORE)
            return _sales(whatsapp=True)

        ctx_a = _ctx(
            _LIVE_BUY,
            tenant_id=_TENANT_A,
            intent_name="start_order",
            store_url="",
            maps_url="",
            db=MagicMock(),
        )
        ctx_b = _ctx(
            _LIVE_BUY,
            tenant_id=_TENANT_B,
            phone=_PHONE_B,
            intent_name="start_order",
            store_url="",
            maps_url="",
            db=MagicMock(),
        )
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            side_effect=_fake_resolve,
        ):
            dec_a = _decide_live(ctx_a)
            dec_b = _decide_live(ctx_b)
        assert set(seen) == {_TENANT_A, _TENANT_B}
        assert dec_a.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
        ]
        assert dec_b.args.get("available_purchase_channels") == ["whatsapp_quick_order"]
        assert dec_a.args.get("topic") == "purchase_channel_selection"
        assert dec_b.args.get("topic") != "purchase_channel_selection"

    def test_resolver_exception_does_not_fabricate_whatsapp(self) -> None:
        intent = Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY)
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            side_effect=RuntimeError("capability lookup failed"),
        ):
            turn = resolve_purchase_channel_turn(
                phase="entry",
                message=_LIVE_BUY,
                intent=intent,
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
                db=MagicMock(),
            )
        assert turn is None
        assert resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=intent,
        ) is None

    def test_resolver_returns_none_does_not_fabricate_whatsapp(self) -> None:
        intent = Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY)
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            return_value=None,
        ):
            turn = resolve_purchase_channel_turn(
                phase="entry",
                message=_LIVE_BUY,
                intent=intent,
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
                db=MagicMock(),
            )
        assert turn is None

    def test_no_db_empty_facts_does_not_fabricate_whatsapp(self) -> None:
        intent = Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY)
        ids = resolve_available_purchase_channel_facts()
        assert ids == []
        assert "whatsapp_quick_order" not in ids
        assert resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=intent,
        ) is None
        turn = resolve_purchase_channel_turn(
            phase="entry",
            message=_LIVE_BUY,
            intent=intent,
        )
        assert turn is None
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            store_url="",
            maps_url="",
        )
        decision = _decide_live(ctx)
        assert decision.args.get("topic") != "whatsapp_quick_order"
        assert "whatsapp_quick_order" not in (
            decision.args.get("available_purchase_channels") or []
        )

    def test_attached_trusted_sales_object_unchanged(self) -> None:
        sales = self._three()
        intent = Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY)
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            side_effect=AssertionError("must not resolve when sales attached"),
        ):
            owner = resolve_purchase_channel_entry_owner(
                message=_LIVE_BUY,
                intent=intent,
                merchant_sales_channels=sales,
            )
            turn = resolve_purchase_channel_turn(
                phase="entry",
                message=_LIVE_BUY,
                intent=intent,
                merchant_sales_channels=sales,
                db=MagicMock(),
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
            )
        assert owner == "purchase_channel_selection"
        assert turn is not None
        assert turn.args.get("topic") == "purchase_channel_selection"
        assert turn.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]


class TestD1CStaleChannelCommitment:
    """Stale checkout_channel without an executable order must not own a new buy."""

    def _three(self) -> MerchantSalesChannels:
        return _sales(
            store=True,
            whatsapp=True,
            showroom=True,
            store_url=_STORE,
            maps_url=_MAPS,
        )

    def _stale_whatsapp_state(self) -> MerchantConversationState:
        return MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=12,
            current_product_focus=None,
            draft_order_id="",
            order_prep=OrderPreparationState(
                checkout_channel="whatsapp_quick_order",
                awaiting_checkout_channel=False,
                awaiting_payment_receipt=False,
                missing_fields=["product", "city", "payment_method"],
            ),
        )

    def test_a_stale_channel_only_reopens_picker(self) -> None:
        state = self._stale_whatsapp_state()
        assert has_actionable_active_order_context(
            order_prep=state.order_prep, state=state, stage=state.stage
        ) is False
        assert has_verified_purchase_channel_execution(state.order_prep) is False
        assert purchase_channel_blocks_new_entry(
            order_prep=state.order_prep, state=state
        ) is False
        db, conv = _persist_ok_db()
        conv.extra_metadata["brain_state"]["order_prep"]["checkout_channel"] = (
            "whatsapp_quick_order"
        )
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=self._three(),
            state=state,
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(
                conv,
                {"order_prep": dict(conv.extra_metadata["brain_state"]["order_prep"])},
            ),
        ):
            decision = _decide(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("available_purchase_channels") == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        canned = build_bare_start_order_guard_reply(_LIVE_BUY)
        assert canned not in str(decision.args or {})
        assert should_block_bare_start_product_prompt(
            order_prep=state.order_prep,
            merchant_sales_channels=self._three(),
        ) is True
        op = conv.extra_metadata["brain_state"]["order_prep"]
        assert op.get("checkout_channel") in {None, ""}
        assert op.get("awaiting_checkout_channel") is True
        assert op.get("purchase_channel_execution_active") is not True

    def test_b_verified_chrome_pick_preserves_whatsapp(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=4,
            order_prep=OrderPreparationState(
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                purchase_channel_execution_active=True,
                purchase_channel_selection_source=SELECTION_SOURCE_VERIFIED_BUTTON,
            ),
        )
        assert has_verified_purchase_channel_execution(state.order_prep) is True
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=self._three(),
            state=state,
            db=MagicMock(),
        )
        decision = _decide(ctx)
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert should_block_bare_start_product_prompt(
            order_prep=state.order_prep,
            merchant_sales_channels=self._three(),
        ) is False

    def test_c_active_product_order_is_preserved(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=6,
            current_product_focus={
                "id": "501",
                "title": "قميص قطني أزرق",
            },
            order_prep=OrderPreparationState(
                product_id="501",
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            ),
        )
        decision = _decide(
            _ctx(
                _LIVE_BUY,
                intent_name="start_order",
                sales=self._three(),
                state=state,
                db=MagicMock(),
            )
        )
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_d_payment_receipt_tied_to_current_order_is_preserved(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="checkout",
            turn=9,
            current_product_focus={
                "id": "501",
                "title": "قميص قطني أزرق",
            },
            order_prep=OrderPreparationState(
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                product_id="501",
                payment_receipt_received=True,
                awaiting_payment_receipt=True,
            ),
        )
        assert has_actionable_active_order_context(
            order_prep=state.order_prep, state=state
        ) is True
        decision = _decide(
            _ctx(
                _LIVE_BUY,
                intent_name="start_order",
                sales=self._three(),
                state=state,
                db=MagicMock(),
            )
        )
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_d_orphan_receipt_without_current_order_opens_picker(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="checkout",
            turn=9,
            current_product_focus=None,
            draft_order_id="",
            order_prep=OrderPreparationState(
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                payment_receipt_received=True,
                payment_evidence_received=True,
                awaiting_payment_receipt=True,
                order_status="paid",
                checkout_payment_id="old-scope-1",
            ),
        )
        assert has_actionable_active_order_context(
            order_prep=state.order_prep, state=state
        ) is False
        db, conv = _persist_ok_db()
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=self._three(),
            state=state,
            db=db,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(
                conv,
                {"order_prep": dict(conv.extra_metadata["brain_state"]["order_prep"])},
            ),
        ):
            decision = _decide(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"

    def test_e_delivery_address_is_preserved(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=8,
            order_prep=OrderPreparationState(
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                pending_delivery_location={
                    "city": "الرياض",
                    "short_address_code": "RRRD1234",
                },
            ),
        )
        decision = _decide(
            _ctx(
                _LIVE_BUY,
                intent_name="start_order",
                sales=self._three(),
                state=state,
                db=MagicMock(),
            )
        )
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_f_capability_counts(self) -> None:
        stale = OrderPreparationState(checkout_channel="whatsapp_quick_order")
        three = resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY),
            order_prep=stale,
            merchant_sales_channels=self._three(),
        )
        two = resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY),
            order_prep=stale,
            merchant_sales_channels=_sales(
                store=True, whatsapp=True, store_url=_STORE
            ),
        )
        one = resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY),
            order_prep=stale,
            merchant_sales_channels=_sales(whatsapp=True),
        )
        assert three == "purchase_channel_selection"
        assert two == "purchase_channel_selection"
        assert one == "whatsapp_quick_order"

    def test_g_capability_failure_is_fail_closed(self) -> None:
        stale = OrderPreparationState(checkout_channel="whatsapp_quick_order")
        owner = resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY),
            order_prep=stale,
            merchant_sales_channels=None,
        )
        assert owner is None
        turn = resolve_purchase_channel_turn(
            phase="entry",
            message=_LIVE_BUY,
            intent=Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY),
            order_prep=stale,
            merchant_sales_channels=None,
            db=None,
            tenant_id=0,
            phone=_PHONE_A,
        )
        assert turn is None

    def test_h_greeting_does_not_open_picker_or_drop_channel(self) -> None:
        state = self._stale_whatsapp_state()
        decision = _decide(
            _ctx(
                "مرحبا كيف الحال",
                intent_name="greeting",
                sales=self._three(),
                state=state,
                db=MagicMock(),
            )
        )
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert state.order_prep.checkout_channel == "whatsapp_quick_order"

    def test_i_tenant_isolation(self) -> None:
        prep_a = OrderPreparationState(
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            purchase_channel_execution_active=True,
            purchase_channel_selection_source=SELECTION_SOURCE_VERIFIED_BUTTON,
        )
        prep_b = OrderPreparationState(checkout_channel="whatsapp_quick_order")
        owner_a = resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY),
            order_prep=prep_a,
            merchant_sales_channels=self._three(),
        )
        owner_b = resolve_purchase_channel_entry_owner(
            message=_LIVE_BUY,
            intent=Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY),
            order_prep=prep_b,
            merchant_sales_channels=_sales(whatsapp=True, showroom=True, maps_url=_MAPS),
        )
        assert owner_a is None
        assert owner_b == "purchase_channel_selection"
        assert prep_a.purchase_channel_execution_active is True
        assert prep_b.purchase_channel_execution_active is False

    def test_persist_failure_does_not_show_durable_picker(self) -> None:
        state = self._stale_whatsapp_state()
        ctx = _ctx(
            _LIVE_BUY,
            intent_name="start_order",
            sales=self._three(),
            state=state,
            db=MagicMock(),
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=False,
        ):
            decision = _decide(ctx)
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("persist_ok") is False
        assert decision.args.get("selection_actions_enabled") is False
        assert decision.args.get("offered_purchase_channel_ids") == []
        assert state.order_prep.checkout_channel == "whatsapp_quick_order"

    def test_round_trip_execution_stamp(self) -> None:
        original = OrderPreparationState(
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            purchase_channel_execution_active=True,
            purchase_channel_selection_source=SELECTION_SOURCE_VERIFIED_BUTTON,
        )
        restored = OrderPreparationState.from_dict(original.to_dict())
        assert restored.purchase_channel_execution_active is True
        assert restored.purchase_channel_selection_source == SELECTION_SOURCE_VERIFIED_BUTTON
        assert has_verified_purchase_channel_execution(restored) is True

    def test_whatsapp_only_auto_commit_does_not_stamp_execution(self) -> None:
        sales = _sales(whatsapp=True)
        intent = Intent(name="start_order", confidence=0.9, raw_message=_LIVE_BUY)
        db, conv = _persist_ok_db(offered=["whatsapp_quick_order"])
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(
                conv,
                {"order_prep": dict(conv.extra_metadata["brain_state"]["order_prep"])},
            ),
        ):
            turn = resolve_purchase_channel_turn(
                phase="entry",
                message=_LIVE_BUY,
                intent=intent,
                merchant_sales_channels=sales,
                tenant_id=_TENANT_A,
                phone=_PHONE_A,
                db=db,
            )
        assert turn is not None
        op = conv.extra_metadata["brain_state"]["order_prep"]
        assert op["checkout_channel"] == CHECKOUT_CHANNEL_WHATSAPP
        assert op.get("purchase_channel_execution_active") is not True


class TestD1CPaymentIdentityLifecycleAndPersist:
    """Review follow-up: current-order payment identity, stamp lifecycle, fail-closed persist."""

    def _three(self) -> MerchantSalesChannels:
        return _sales(
            store=True,
            whatsapp=True,
            showroom=True,
            store_url=_STORE,
            maps_url=_MAPS,
        )

    def test_orphan_receipt_flags_are_not_active_order(self) -> None:
        prep = OrderPreparationState(
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            payment_receipt_received=True,
            payment_evidence_received=True,
            order_status="confirmed",
            checkout_payment_id="old-scope-1",
        )
        assert has_actionable_active_order_context(order_prep=prep) is False
        assert purchase_channel_blocks_new_entry(order_prep=prep) is False

    def test_receipt_tied_to_cart_preserves_owner(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="checkout",
            turn=5,
            cart_items=[
                {
                    "id": "701",
                    "external_id": "sku-white-shoe",
                    "title": "حذاء رياضي أبيض",
                    "quantity": 1,
                    "from_catalog_order": True,
                    "product_retailer_id": "sku-white-shoe",
                    "catalog_product_id": 701,
                    "price": 249,
                }
            ],
            order_prep=OrderPreparationState(
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                payment_receipt_received=True,
                catalog_line_items_authoritative=True,
                line_items=[
                    {
                        "id": "701",
                        "external_id": "sku-white-shoe",
                        "title": "حذاء رياضي أبيض",
                        "quantity": 1,
                        "from_catalog_order": True,
                        "product_retailer_id": "sku-white-shoe",
                        "catalog_product_id": 701,
                        "price": 249,
                    }
                ],
            ),
        )
        assert has_actionable_active_order_context(
            order_prep=state.order_prep, state=state
        ) is True
        decision = _decide(
            _ctx(
                _LIVE_BUY,
                intent_name="start_order",
                sales=self._three(),
                state=state,
                db=MagicMock(),
            )
        )
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_receipt_tied_to_draft_preserves_owner(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="checkout",
            turn=6,
            draft_order_id="draft-901",
            order_prep=OrderPreparationState(
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                payment_evidence_received=True,
                salla_order_id="salla-901",
            ),
        )
        assert has_actionable_active_order_context(
            order_prep=state.order_prep, state=state
        ) is True
        decision = _decide(
            _ctx(
                _LIVE_BUY,
                intent_name="start_order",
                sales=self._three(),
                state=state,
                db=MagicMock(),
            )
        )
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_execution_stamp_consumed_when_current_order_exists(self) -> None:
        prep = OrderPreparationState(
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            purchase_channel_execution_active=True,
            purchase_channel_selection_source=SELECTION_SOURCE_VERIFIED_BUTTON,
            product_id="801",
        )
        assert has_verified_purchase_channel_execution(prep) is False
        assert has_actionable_active_order_context(order_prep=prep) is True
        assert purchase_channel_blocks_new_entry(order_prep=prep) is True

    def test_completed_order_clears_execution_stamp_on_persist(self) -> None:
        db, conv = _persist_ok_db()
        conv.extra_metadata["brain_state"]["order_prep"] = {
            "checkout_channel": CHECKOUT_CHANNEL_WHATSAPP,
            "purchase_channel_execution_active": True,
            "purchase_channel_selection_source": SELECTION_SOURCE_VERIFIED_BUTTON,
            "product_id": "801",
            "product_name": "عطر ورد 100ml",
            "order_status": "completed",
        }
        ok = _run_persist(conv, db)
        assert ok is True
        written = conv.extra_metadata["brain_state"]["order_prep"]
        assert written.get("purchase_channel_execution_active") is not True
        assert not str(written.get("purchase_channel_selection_source") or "").strip()
        assert has_verified_purchase_channel_execution(written) is False

    def test_cancelled_order_clears_execution_stamp_on_persist(self) -> None:
        db, conv = _persist_ok_db()
        conv.extra_metadata["brain_state"]["order_prep"] = {
            "checkout_channel": CHECKOUT_CHANNEL_WHATSAPP,
            "purchase_channel_execution_active": True,
            "purchase_channel_selection_source": SELECTION_SOURCE_VERIFIED_BUTTON,
            "order_status": "cancelled",
        }
        ok = _run_persist(conv, db)
        assert ok is True
        written = conv.extra_metadata["brain_state"]["order_prep"]
        assert written.get("purchase_channel_execution_active") is not True
        assert has_verified_purchase_channel_execution(written) is False

    def test_reset_clears_execution_stamp(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=7,
            order_prep=OrderPreparationState(
                checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                purchase_channel_execution_active=True,
                purchase_channel_selection_source=SELECTION_SOURCE_VERIFIED_BUTTON,
                product_id="801",
            ),
        )
        clear_active_order_context(state, reason="customer_cancelled")
        assert state.order_prep.purchase_channel_execution_active is False
        assert not str(state.order_prep.purchase_channel_selection_source or "").strip()
        assert has_verified_purchase_channel_execution(state.order_prep) is False
        assert has_actionable_active_order_context(
            order_prep=state.order_prep, state=state
        ) is False

    def test_persist_execution_active_fails_closed_without_trusted_source(self) -> None:
        db, conv = _persist_ok_db()
        before = dict(conv.extra_metadata["brain_state"]["order_prep"])
        empty = persist_checkout_route_state(
            db,
            tenant_id=_TENANT_A,
            phone=_PHONE_A,
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            purchase_channel_execution_active=True,
            purchase_channel_selection_source="",
        )
        untrusted = persist_checkout_route_state(
            db,
            tenant_id=_TENANT_A,
            phone=_PHONE_A,
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            purchase_channel_execution_active=True,
            purchase_channel_selection_source="llm_guess",
        )
        assert empty is False
        assert untrusted is False
        assert conv.extra_metadata["brain_state"]["order_prep"] == before

    def test_persist_trusted_source_writes_complete_execution_fact(self) -> None:
        db, conv = _persist_ok_db()
        ok = _run_persist(
            conv,
            db,
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            awaiting_checkout_channel=False,
            purchase_channel_execution_active=True,
            purchase_channel_selection_source=SELECTION_SOURCE_VERIFIED_BUTTON,
        )
        assert ok is True
        written = conv.extra_metadata["brain_state"]["order_prep"]
        assert written.get("purchase_channel_execution_active") is True
        assert written.get("purchase_channel_selection_source") == (
            SELECTION_SOURCE_VERIFIED_BUTTON
        )

    def test_silent_recovery_current_order_in_state_skips_canned(self) -> None:
        canned = build_bare_start_order_guard_reply(_LIVE_BUY)
        state = {
            "current_product_focus": {
                "id": "501",
                "title": "قميص قطني أزرق",
            },
            "order_prep": {
                "product_id": "501",
                "product_name": "قميص قطني أزرق",
                "checkout_channel": CHECKOUT_CHANNEL_WHATSAPP,
                "purchase_channel_execution_active": True,
                "purchase_channel_selection_source": SELECTION_SOURCE_VERIFIED_BUTTON,
            },
        }
        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            return_value=self._three(),
        ):
            recovery = try_guard_recovery_reply(
                inbound_text=_LIVE_BUY,
                state=state,
                db=MagicMock(),
                tenant_id=_TENANT_A,
            )
        assert recovery.needs_persona_compose is True
        assert recovery.source == "purchase_channel_selection_pending"
        assert recovery.reply != canned
        assert "كتالوج واتساب" not in (recovery.reply or "")

    def test_should_block_consumes_stamp_when_order_exists_in_state_only(self) -> None:
        prep = OrderPreparationState(
            checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
            purchase_channel_execution_active=True,
            purchase_channel_selection_source=SELECTION_SOURCE_VERIFIED_BUTTON,
        )
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            turn=3,
            current_product_focus={
                "id": "501",
                "title": "قميص قطني أزرق",
            },
            order_prep=prep,
        )
        assert has_verified_purchase_channel_execution(prep) is True
        assert has_verified_purchase_channel_execution(prep, state=state) is False
        assert should_block_bare_start_product_prompt(
            order_prep=prep,
            merchant_sales_channels=self._three(),
            state=state,
        ) is True
        assert should_block_bare_start_product_prompt(
            order_prep=prep,
            merchant_sales_channels=self._three(),
        ) is False

