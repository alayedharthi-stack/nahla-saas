"""AI-D10 — catalog navigation ownership must not claim generic start_order.

Repair is signal ownership in navigation_signals.py, not phrase maps or
tenant-specific routing. Assert chosen_path / action / navigator None only.
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
from modules.ai.brain.catalog.navigation_signals import (  # noqa: E402
    evaluate_catalog_navigation_signals,
    message_indicates_catalog_browse,
)
from modules.ai.brain.commerce.discovery_strategy import (  # noqa: E402
    CatalogContextSnapshot,
    DiscoveryMode,
    resolve_discovery_strategy,
)
from modules.ai.brain.commerce.commerce_objective import COMMERCE_OBJECTIVE_DISCOVERY  # noqa: E402
from modules.ai.brain.discovery.entry import START_ORDER_BARE  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

MSG_START = "\u0627\u0628\u064a \u0627\u0637\u0644\u0628"
MSG_BROWSE = "\u0648\u0634 \u0639\u0646\u062f\u0643\u0645"
MSG_PRODUCT = "\u0623\u0628\u063a\u0649 \u062d\u0630\u0627\u0621 \u0631\u064a\u0627\u0636\u064a \u0623\u0628\u064a\u0636 \u0645\u0642\u0627\u0633 42"
VOICE_SOCIAL_START_ORDER = (
    "\u0627\u0644\u0633\u0644\u0627\u0645 \u0639\u0644\u064a\u0643\u0645 "
    "\u0643\u064a\u0641 \u062d\u0627\u0644\u0643\u0645 \u0627\u0644\u064a\u0648\u0645 "
    "\u0643\u0646\u062a \u0623\u0628\u063a\u0649 \u0623\u0634\u062a\u0631\u064a \u0634\u064a "
    "\u0645\u0646 \u0627\u0644\u0645\u062a\u062c\u0631 \u0628\u0633 \u0645\u0627 \u062d\u062f\u062f\u062a "
    "\u0627\u0644\u0645\u0646\u062a\u062c \u0628\u0627\u0644\u0636\u0628\u0637"
)

_STORE = "https://shop.example"
_MAPS = "https://maps.example.com/showroom"

COLLECTIONS = [
    {"group_id": "shoes", "group_name": "\u0627\u0644\u0623\u062d\u0630\u064a\u0629", "browse_rank": 1},
    {"group_id": "shirts", "group_name": "\u0627\u0644\u0642\u0645\u0627\u0635", "browse_rank": 2},
]


def _facts(
    *,
    tenant_id: int = 11,
    store_url: str = "",
    maps_url: str = "",
) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=24,
        in_stock_count=24,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="\u0645\u062a\u062c\u0631 \u062a\u062c\u0631\u064a\u0628\u064a \u0639\u0627\u0645",
        store_url=store_url,
        maps_url=maps_url,
        top_products=[
            {
                "id": "501",
                "external_id": "sku-white-shoe",
                "title": "\u062d\u0630\u0627\u0621 \u0631\u064a\u0627\u0636\u064a \u0623\u0628\u064a\u0636",
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
        state=state or MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(tenant_id=tenant_id, store_url=store_url, maps_url=maps_url),
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _assert_not_groups_owner(decision: Any) -> None:
    if decision is None:
        return
    assert decision.action != ACTION_CATALOG_NAVIGATE or (
        decision.args.get("chosen_path") != PATH_GROUPS
        and decision.args.get("navigator_step") != STEP_SHOW_GROUPS
    )


class TestGenericStartOrderDoesNotOwnGroups:
    def test_start_order_intent_without_browse_frames_returns_none(self) -> None:
        ctx = _ctx("مرحبا", intent_name="start_order")
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.catalog_browse_intent is False
        with patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            return_value=COLLECTIONS,
        ):
            decision = try_catalog_navigation_decision(ctx)
        assert decision is None

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_bare_start_order_phrase_navigator_none(self, _mock_groups) -> None:
        decision = try_catalog_navigation_decision(_ctx(MSG_START, db=MagicMock()))
        assert decision is None

    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_bare_start_order_engine_uses_purchase_channel_selection(self, _mock_groups) -> None:
        ctx = _ctx(
            MSG_START,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.action != ACTION_CATALOG_NAVIGATE
        _assert_not_groups_owner(decision)

    def test_message_indicates_catalog_browse_rejects_bare_start_order(self) -> None:
        assert message_indicates_catalog_browse(MSG_START, intent_name="start_order") is False


class TestExplicitBrowseStillOwnsGroups:
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_explicit_browse_still_returns_path_groups(self, _mock_groups) -> None:
        decision = try_catalog_navigation_decision(_ctx(MSG_BROWSE, db=MagicMock()))
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") == PATH_GROUPS
        assert decision.args.get("navigator_step") == STEP_SHOW_GROUPS


class TestVoiceShapedStartOrderDoesNotOwnGroups:
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_long_social_voice_transcript_navigator_none(self, _mock_groups) -> None:
        assert len(VOICE_SOCIAL_START_ORDER) > 64
        ctx = _ctx(
            VOICE_SOCIAL_START_ORDER,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
        )
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.catalog_browse_intent is False
        decision = try_catalog_navigation_decision(ctx)
        assert decision is None

    @patch(
        "modules.ai.brain.commerce.commerce_entry_catalog_delivery.try_commerce_entry_catalog_decision",
        return_value=None,
    )
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_long_social_voice_transcript_engine_not_groups(
        self,
        _mock_groups,
        _mock_ce2,
    ) -> None:
        ctx = _ctx(
            VOICE_SOCIAL_START_ORDER,
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        _assert_not_groups_owner(decision)
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") != PATH_GROUPS
        assert decision.args.get("discovery_mode") != DiscoveryMode.COLLECTIONS_FIRST.value

    @patch(
        "modules.ai.brain.commerce.commerce_entry_catalog_delivery.try_commerce_entry_catalog_decision",
        return_value=None,
    )
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_social_unresolved_start_order_intent_stays_with_brain(
        self,
        _mock_groups,
        _mock_ce2,
    ) -> None:
        ctx = _ctx(
            "مرحبا كيف الحال",
            db=MagicMock(),
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        _assert_not_groups_owner(decision)
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") != "purchase_channel_selection"


class TestProductSpecificPurchaseNotGroups:
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_product_specific_start_order_not_path_groups(self, _mock_groups) -> None:
        decision = try_catalog_navigation_decision(
            _ctx(MSG_PRODUCT, db=MagicMock(), intent_name="start_order"),
        )
        assert decision is None or decision.args.get("chosen_path") != PATH_GROUPS


class TestTenantIsolation:
    @patch("modules.ai.brain.catalog.navigation._load_catalog_groups", return_value=COLLECTIONS)
    def test_start_order_groups_ownership_isolated_per_tenant(self, mock_groups) -> None:
        tenant_a = _ctx(MSG_START, tenant_id=41, db=MagicMock())
        tenant_b = _ctx(MSG_BROWSE, tenant_id=42, db=MagicMock())

        assert try_catalog_navigation_decision(tenant_a) is None

        browse_decision = try_catalog_navigation_decision(tenant_b)
        assert browse_decision is not None
        assert browse_decision.args.get("chosen_path") == PATH_GROUPS

        assert mock_groups.call_count >= 1


class TestBareStartOrderDoesNotSelectCollectionsFirst:
    def test_start_order_bare_strategy_is_not_collections_first(self) -> None:
        plan = resolve_discovery_strategy(
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            entry_type=START_ORDER_BARE,
            catalog_context=CatalogContextSnapshot(product_count=24, collection_count=4),
        )
        assert plan.mode != DiscoveryMode.COLLECTIONS_FIRST
        assert plan.mode == DiscoveryMode.FEATURED_FIRST


class TestOwnershipNotPhraseMaps:
    def test_repair_is_signal_ownership_not_phrase_whitelist(self) -> None:
        ctx = _ctx(MSG_START, intent_name="start_order")
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.evidence.get("order_without_target") is True
        assert signals.catalog_browse_intent is False
        assert signals.catalog_browse_score < 0.62
