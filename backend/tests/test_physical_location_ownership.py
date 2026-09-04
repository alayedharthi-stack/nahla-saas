"""
tests/test_physical_location_ownership.py
──────────────────────────────────────────
PR-D2 — physical location ownership before catalog/storefront fallback.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Type

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_MAPS_URL = "https://maps.app.goo.gl/test-branch"
_STORE_URL = "https://shop.example.sa"


class _BranchRow:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StructuredQuery:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def filter(self, *args: Any, **kwargs: Any) -> "_StructuredQuery":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_StructuredQuery":
        return self

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)


class _Conv:
    def __init__(self, meta: Dict[str, Any]) -> None:
        self.extra_metadata = meta


class _StructuredDB:
    def __init__(
        self,
        *,
        branches: Optional[List[Any]] = None,
        contacts: Optional[List[Any]] = None,
    ) -> None:
        self.branches = branches or []
        self.contacts = contacts or []

    def query(self, model: Type[Any]) -> _StructuredQuery:
        name = getattr(model, "__name__", str(model))
        if name == "MerchantBranch":
            return _StructuredQuery(self.branches)
        if name == "BranchContact":
            return _StructuredQuery(self.contacts)
        if name in {"BranchEscalationStep", "BranchArrivalKeyword"}:
            return _StructuredQuery([])
        return _StructuredQuery([])

    def add(self, obj: Any) -> None:
        pass

    def flush(self) -> None:
        pass


def _branch(**kwargs: Any) -> _BranchRow:
    defaults = dict(
        id=1,
        tenant_id=10,
        name="المعرض",
        city="",
        district="",
        address="",
        maps_url=_MAPS_URL,
        sort_order=0,
        is_active=True,
        location_response_mode="location_plus_reception",
        arrival_response_mode="reception_only",
        location_instructions_text="",
    )
    defaults.update(kwargs)
    return _BranchRow(**defaults)


def _reception(**kwargs: Any) -> _BranchRow:
    defaults = dict(
        id=12,
        branch_id=1,
        display_name="استقبال",
        role="reception",
        phone_e164="966500000099",
        whatsapp_e164="",
        sort_order=0,
        is_active=True,
        is_default_reception=True,
    )
    defaults.update(kwargs)
    return _BranchRow(**defaults)


def _brain_ctx(message: str, *, maps_url: str = _MAPS_URL, store_url: str = _STORE_URL):
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState

    intent = match(message) or Intent(name="general", confidence=0.5, raw_message=message)
    ctx = BrainContext(
        tenant_id=10,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=MerchantConversationState(),
        facts=CommerceFacts(
            has_products=True,
            store_url=store_url,
            maps_url=maps_url,
        ),
    )
    return DefaultDecisionEngine().decide(ctx)


@pytest.fixture(autouse=True)
def _structured_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")


class TestPhysicalLocationOwnership:
    def test_t1_wain_mawqecom_physical_not_storefront(self) -> None:
        from modules.ai.brain.commerce.link_intent import LinkIntentType, resolve_link_intent
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_LOCATION

        msg = "وين موقعكم؟"
        assert resolve_link_intent(msg) == LinkIntentType.PHYSICAL_LOCATION
        decision = _brain_ctx(msg)
        assert decision.action in {ACTION_FAQ_REPLY, ACTION_LLM_REPLY}
        topic = str(decision.args.get("topic") or "")
        kind = str(decision.args.get("question_kind") or "")
        assert topic in {TOPIC_LOCATION, "location_delivery"} or kind == "location"
        assert "store_info" not in topic

    def test_t2_mawqe_almaarid_direct_no_preference_ask(
        self,
    ) -> None:
        from modules.ai.brain.commerce.branch_trigger_router import (
            evaluate_branch_trigger_routing,
        )
        from modules.ai.brain.commerce.link_intent import (
            is_explicit_direct_location_request,
        )
        from modules.ai.brain.commerce.location_link_policy import (
            evaluate_location_link_policy,
        )

        msg = "موقع المعرض"
        assert is_explicit_direct_location_request(msg)

        llp = evaluate_location_link_policy(object(), tenant_id=10, message=msg)
        assert llp is None

        db = _StructuredDB(
            branches=[_branch(location_response_mode="location_plus_reception")],
            contacts=[_reception()],
        )
        btr = evaluate_branch_trigger_routing(
            db, tenant_id=10, message="وين موقعكم؟",
        )
        assert btr is None

    def test_t3_send_location_not_website(self) -> None:
        from modules.ai.brain.commerce.link_intent import LinkIntentType, resolve_link_intent
        from modules.ai.brain.commerce.physical_location_ownership import (
            is_website_storefront_request,
        )

        msg = "أرسل الموقع"
        assert resolve_link_intent(msg) == LinkIntentType.PHYSICAL_LOCATION
        assert not is_website_storefront_request(msg)

    def test_t4_website_stays_website(self) -> None:
        from modules.ai.brain.commerce.link_intent import LinkIntentType, resolve_link_intent
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = "رابط المتجر الإلكتروني"
        assert resolve_link_intent(msg) == LinkIntentType.WEBSITE_URL
        decision = _brain_ctx(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STORE_INFO

    def test_t5_almawqe_alelectroni_stays_website(self) -> None:
        from modules.ai.brain.commerce.link_intent import LinkIntentType, resolve_link_intent
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = "الموقع الإلكتروني"
        assert resolve_link_intent(msg) == LinkIntentType.WEBSITE_URL
        decision = _brain_ctx(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STORE_INFO

    def test_t6_missing_maps_no_catalog_substitute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.native_catalog_fallback import compose_native_catalog_failure_decision
        from modules.ai.brain.commerce.contact_route_policy import MSG_LOCATION_NOT_CONFIGURED
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_LOCATION

        msg = "وين موقعكم؟"
        decision = _brain_ctx(msg, maps_url="")
        assert decision.action in {ACTION_FAQ_REPLY, ACTION_LLM_REPLY}
        topic = str(decision.args.get("topic") or "")
        kind = str(decision.args.get("question_kind") or "")
        assert topic in {TOPIC_LOCATION, "location_delivery"} or kind == "location"
        assert _STORE_URL not in str(decision.args)

        fallback = compose_native_catalog_failure_decision(
            None, 10, customer_message=msg,
        )
        assert fallback.text == MSG_LOCATION_NOT_CONFIGURED
        assert fallback.cta_url == ""
        assert _STORE_URL not in fallback.text

    def test_t7_pending_yes_consumes_choice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modules.ai.brain.commerce.pending_operational_choice import (
            PENDING_PICKUP_MAPS_OR_CONTACT,
            evaluate_pending_operational_choice_routing,
        )

        meta: Dict[str, Any] = {
            "brain_state": {
                "order_prep": {
                    "pending_operational_choice": PENDING_PICKUP_MAPS_OR_CONTACT,
                    "pending_operational_branch_id": 1,
                },
            },
        }
        conv = _Conv(meta)
        db = _StructuredDB(
            branches=[_branch()],
            contacts=[_reception()],
        )

        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda _db, tenant_id, phone: (conv, meta["brain_state"]),
        )

        decision = evaluate_pending_operational_choice_routing(
            db, tenant_id=10, message="نعم", customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.maps_url == _MAPS_URL

    def test_t8_pending_naam_arsil_consumes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modules.ai.brain.commerce.pending_operational_choice import (
            PENDING_PICKUP_MAPS_OR_CONTACT,
            evaluate_pending_operational_choice_routing,
        )

        meta: Dict[str, Any] = {
            "brain_state": {
                "order_prep": {
                    "pending_operational_choice": PENDING_PICKUP_MAPS_OR_CONTACT,
                    "pending_operational_branch_id": 1,
                },
            },
        }
        conv = _Conv(meta)
        db = _StructuredDB(
            branches=[_branch()],
            contacts=[_reception()],
        )

        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda _db, tenant_id, phone: (conv, meta["brain_state"]),
        )

        decision = evaluate_pending_operational_choice_routing(
            db, tenant_id=10, message="نعم ارسل", customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.maps_url == _MAPS_URL

    def test_t9_catalog_browse_unaffected(self) -> None:
        from modules.ai.brain.commerce.physical_location_ownership import (
            is_physical_location_request,
        )
        from modules.ai.brain.decision.actions import (
            ACTION_CATALOG_NAVIGATE,
            ACTION_SEARCH_PRODUCTS,
        )

        msg = "وش الأنواع المتوفرة؟"
        assert not is_physical_location_request(msg)
        decision = _brain_ctx(msg)
        assert decision.action in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}
