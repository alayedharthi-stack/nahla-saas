"""Customer / order intelligence recovery — natural-order acceptance suite.

Asserts owner, evidence, scoping, and projection.
Does not assert exact customer-facing Arabic sentences.
Does not add phrase routers for سابق / قبل / طلباتي / آخر طلب.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.customer_commerce_ledger import (  # noqa: E402
    list_recent_order_snapshots,
)
from core.local_order_resolver import (  # noqa: E402
    LocalOrderSnapshot,
    _fetch_tenant_orders_for_customer,
    local_order_to_track_payload,
    resolve_customer_order_context,
)
from modules.ai.brain.commerce.catalog_reasoning_evidence import (  # noqa: E402
    collect_catalog_reasoning_candidates,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.commerce.customer_order_evidence import (  # noqa: E402
    collect_customer_order_evidence,
    customer_order_evidence_available,
)
from modules.ai.brain.compose.brain_state_slim import (  # noqa: E402
    should_slim_general_brain_state,
)
from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: E402
    _slim_known_facts,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRODUCT,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
    INTENT_TRACK_ORDER,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_shipment,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_SHOE = "حذاء رياضي أبيض"
GENERIC_SHIRT = "قميص قطني أزرق"
GENERIC_PERFUME = "عطر ورد 100ml"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_ORDER_REF = "284719365"
GENERIC_OLDER_REF = "284719100"
OTHER_CUSTOMER_PHONE = "+966500000099"
OTHER_TENANT_PHONE = "+966511111111"

LIVE_PREVIOUS_ORDER = "الطلب اللي طلبته منكم قبل، وش كان؟"
LIVE_CURRENT_ORDER = "وين طلبي الحالي؟"

CURRENT_ORDER_FAMILY = (
    "وين طلبي؟",
    "وش حالة طلبي؟",
    "إيش صار على طلبي؟",
    LIVE_CURRENT_ORDER,
)
PREVIOUS_ORDER_FAMILY = (
    "وش كان طلبي الأخير؟",
    "وش طلبت منكم قبل؟",
    "تذكر آخر طلب لي؟",
    "وش طلباتي السابقة؟",
    LIVE_PREVIOUS_ORDER,
)
ORDER_CONTENT_FAMILY = (
    "وش كان داخل طلبي؟",
    "وش المنتجات اللي طلبتها؟",
    "وش طلبت بالضبط؟",
)
ACTUAL_SHIPPING_FAMILY = (
    "طلبي مع أي شركة؟",
    "مين موصل طلبي؟",
)
ACTUAL_PAYMENT_FAMILY = (
    "وش طريقة الدفع في طلبي؟",
    "أنا دفعت كيف؟",
)
LEDGER_ROUTED_INTENTS = {
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
}


def _facts() -> CommerceFacts:
    return CommerceFacts(
        store_name=GENERIC_MERCHANT,
        has_products=True,
        product_count=3,
        in_stock_count=3,
        orderable=True,
        discovery_products=[
            {"id": 11, "title": GENERIC_SHOE, "in_stock": True, "can_checkout": False},
            {"id": 12, "title": GENERIC_SHIRT, "in_stock": True, "external_id": "ext-shirt", "can_checkout": True},
            {"id": 13, "title": GENERIC_PERFUME, "in_stock": True, "can_checkout": False},
        ],
        payment_methods=["cod", "bank"],
        shipping_methods=["Dev Company"],
        merchant_capabilities={
            "source": "salla",
            "kind": "merchant_enabled",
            "payments": {
                "status": "known",
                "methods": [{"code": "cod", "enabled": True}],
            },
            "shipping": {
                "companies_status": "known",
                "companies": [{"id": 1, "name": "Dev Company", "enabled": True}],
            },
        },
    )


def _intent(message: str, name: str) -> Intent:
    return Intent(
        name=name,
        confidence=0.85,
        slots={},
        raw_message=message,
        extraction_method="rules",
    )


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_ASK_PRODUCT,
    tenant_id: int = 1,
    phone: str = "966500000001",
    history: List[Dict[str, str]] | None = None,
    customer_id: int | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=_intent(message, intent_name),
        state=MerchantConversationState(stage="exploring", turn=4, greeted=True),
        facts=_facts(),
        history=history or [],
        profile={"inbound_metadata": {}},
        commerce_bundle={},
        customer_id=customer_id,
    )


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def world(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    older = seed_order(
        db,
        tenant.id,
        source="salla",
        status="delivered",
        external_id=GENERIC_OLDER_REF,
        external_order_number=GENERIC_OLDER_REF,
        customer_info={"phone": DEFAULT_PHONE_E164},
        line_items=[{"title": GENERIC_SHIRT, "quantity": 1, "variant": "M"}],
        extra_metadata={
            "created_at": "2026-07-01T10:00:00+00:00",
            "payment_method": "bank_transfer",
        },
    )
    older.total = "189"
    older.customer_id = customer.id
    current = seed_order(
        db,
        tenant.id,
        source="salla",
        status="processing",
        external_id=GENERIC_ORDER_REF,
        external_order_number=GENERIC_ORDER_REF,
        customer_info={"phone": DEFAULT_PHONE_E164},
        line_items=[
            {"name": GENERIC_SHOE, "quantity": 2, "variant": "42"},
        ],
        extra_metadata={
            "created_at": "2026-08-10T10:00:00+00:00",
            "payment_method": "cod",
        },
    )
    current.total = "741"
    current.customer_id = customer.id
    db.commit()
    shipment = seed_shipment(
        db,
        tenant.id,
        current.id,
        tracking_number="TRK-284719365",
        provider="smsa",
        status="in_transit",
    )
    return SimpleNamespace(
        tenant=tenant,
        customer=customer,
        conversation=conv,
        older=older,
        current=current,
        shipment=shipment,
        phone=DEFAULT_PHONE_E164,
    )


def _evidence(db, world, message: str = "") -> Dict[str, Any]:
    payload = collect_customer_order_evidence(
        db=db,
        tenant_id=world.tenant.id,
        phone=world.phone,
        customer_id=world.customer.id,
        conversation_id=world.conversation.id,
        message=message,
    )
    assert payload is not None
    return payload


class TestLivePhraseIsNotKeywordRouted:
    def test_previous_order_live_phrase_is_not_ledger_or_track_rule(self) -> None:
        matched = rules.match(LIVE_PREVIOUS_ORDER)
        name = str(getattr(matched, "name", "") or "")
        assert name not in LEDGER_ROUTED_INTENTS
        assert name != INTENT_TRACK_ORDER

    def test_current_order_live_phrase_still_tracks(self) -> None:
        matched = rules.match(LIVE_CURRENT_ORDER)
        assert matched is not None
        assert matched.name == INTENT_TRACK_ORDER
        ctx = _ctx(LIVE_CURRENT_ORDER, intent_name=INTENT_TRACK_ORDER)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_TRACK_ORDER

    def test_previous_order_live_phrase_does_not_use_canned_ledger(self) -> None:
        matched = rules.match(LIVE_PREVIOUS_ORDER)
        intent = matched or _intent(LIVE_PREVIOUS_ORDER, "general")
        ctx = BrainContext(
            tenant_id=1,
            customer_phone=DEFAULT_PHONE_E164,
            message=LIVE_PREVIOUS_ORDER,
            intent=intent,
            state=MerchantConversationState(),
            facts=_facts(),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY


class TestACurrentOrder:
    @pytest.mark.parametrize("message", CURRENT_ORDER_FAMILY)
    def test_current_order_evidence_is_the_open_order(self, db, world, message: str) -> None:
        payload = _evidence(db, world, message)
        current = payload["current_order"]
        assert current is not None
        assert current["display_reference"] == GENERIC_ORDER_REF
        assert current["is_open"] is True
        assert str(current.get("total")) == "741"
        assert payload["latest_open_order"]["display_reference"] == GENERIC_ORDER_REF


class TestBLatestPreviousHistory:
    @pytest.mark.parametrize("message", PREVIOUS_ORDER_FAMILY)
    def test_history_is_available_without_phrase_router(self, db, world, message: str) -> None:
        payload = _evidence(db, world, message)
        assert customer_order_evidence_available(payload) is True
        refs = {row["display_reference"] for row in payload["orders"]}
        assert GENERIC_ORDER_REF in refs
        assert GENERIC_OLDER_REF in refs
        assert payload["latest_order"]["display_reference"] == GENERIC_ORDER_REF
        assert payload["latest_open_order"]["display_reference"] == GENERIC_ORDER_REF
        assert payload["order_count"] == 2


class TestCOrderContents:
    @pytest.mark.parametrize("message", ORDER_CONTENT_FAMILY)
    def test_line_items_come_from_the_order_not_catalog(self, db, world, message: str) -> None:
        payload = _evidence(db, world, message)
        current_items = payload["current_order"]["line_items"]
        names = {item["name"] for item in current_items}
        assert GENERIC_SHOE in names
        assert GENERIC_PERFUME not in names
        shoe = next(item for item in current_items if item["name"] == GENERIC_SHOE)
        assert shoe["quantity"] == 2
        assert shoe["variant"] == "42"
        catalog = collect_catalog_reasoning_candidates(facts=_facts())
        catalog_titles = {row["title"] for row in catalog}
        assert GENERIC_PERFUME in catalog_titles
        assert GENERIC_PERFUME not in names

    def test_nested_salla_product_name_is_projected(self, db, world) -> None:
        nested = seed_order(
            db,
            world.tenant.id,
            source="salla",
            status="paid",
            external_id="284719200",
            external_order_number="284719200",
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[
                {
                    "quantity": 3,
                    "product_title": GENERIC_PERFUME,
                    "product": {"name": GENERIC_PERFUME, "sku": "PRF-100"},
                }
            ],
            extra_metadata={
                "created_at": "2026-07-15T10:00:00+00:00",
                "payment": {"method": "mada"},
                "shipping_company": "aramex",
            },
        )
        nested.customer_id = world.customer.id
        db.commit()
        payload = _evidence(db, world, "وش كان داخل طلبي؟")
        found = None
        for row in payload["orders"]:
            if row["display_reference"] == "284719200":
                found = row
                break
        assert found is not None
        names = {item["name"] for item in found["line_items"]}
        assert GENERIC_PERFUME in names
        assert found["payment_method"] == "mada"
        assert found["carrier"] == "aramex"

    def test_track_payload_reads_salla_product_title(self) -> None:
        snap = LocalOrderSnapshot(
            order_id=99,
            external_id="257404293",
            external_order_number="257404293",
            status="in_progress",
            source="salla",
            total="741",
            customer_name=GENERIC_CUSTOMER,
            line_items=[
                {"product_id": "1", "product_title": GENERIC_SHOE, "quantity": 1},
                {"product_id": "2", "product_title": GENERIC_SHIRT, "quantity": 3},
            ],
        )
        payload = local_order_to_track_payload(snap)
        names = {item["name"] for item in payload["items"]}
        assert GENERIC_SHOE in names
        assert GENERIC_SHIRT in names
        assert payload["items"][1]["quantity"] == 3


class TestDOrderNumber:
    def test_explicit_number_loads_that_scoped_order(self, db, world) -> None:
        payload = _evidence(db, world, f"وش فيه الطلب {GENERIC_OLDER_REF}؟")
        refs = {row["display_reference"] for row in payload["orders"]}
        assert GENERIC_OLDER_REF in refs
        older = next(
            row for row in payload["orders"] if row["display_reference"] == GENERIC_OLDER_REF
        )
        names = {item["name"] for item in older["line_items"]}
        assert GENERIC_SHIRT in names
        assert older["payment_method"] == "bank_transfer"

    def test_foreign_order_number_is_not_returned(self, db, world) -> None:
        other = seed_customer(
            db, world.tenant.id, phone=OTHER_CUSTOMER_PHONE, name="نورة عبدالله",
        )
        foreign = seed_order(
            db,
            world.tenant.id,
            source="salla",
            status="paid",
            external_id="999888777",
            external_order_number="999888777",
            customer_info={"phone": OTHER_CUSTOMER_PHONE},
            line_items=[{"title": GENERIC_PERFUME, "quantity": 1}],
        )
        foreign.customer_id = other.id
        db.commit()
        payload = _evidence(db, world, "وش فيه الطلب 999888777؟")
        refs = {row["display_reference"] for row in payload["orders"]}
        assert "999888777" not in refs
        names = {
            item["name"]
            for row in payload["orders"]
            for item in row.get("line_items") or []
        }
        assert GENERIC_PERFUME not in names


class TestEActualShipping:
    @pytest.mark.parametrize("message", ACTUAL_SHIPPING_FAMILY)
    def test_actual_carrier_outranks_merchant_enabled_list(self, db, world, message: str) -> None:
        payload = _evidence(db, world, message)
        current = payload["current_order"]
        assert current["carrier"] == "smsa"
        assert current["carrier"] != "Dev Company"
        snapshots = list_recent_order_snapshots(
            db,
            tenant_id=world.tenant.id,
            phone=world.phone,
            customer_id=world.customer.id,
            limit=5,
        )
        current_snap = next(
            snap for snap in snapshots if snap.display_reference == GENERIC_ORDER_REF
        )
        assert current_snap.carrier == "smsa"


class TestFActualPayment:
    @pytest.mark.parametrize("message", ACTUAL_PAYMENT_FAMILY)
    def test_actual_order_payment_is_projected(self, db, world, message: str) -> None:
        payload = _evidence(db, world, message)
        assert payload["current_order"]["payment_method"] == "cod"
        older = next(
            row
            for row in payload["orders"]
            if row["display_reference"] == GENERIC_OLDER_REF
        )
        assert older["payment_method"] == "bank_transfer"


class TestGShoppingOrderSwitching:
    def test_order_evidence_does_not_own_browse(self, db, world) -> None:
        payload = _evidence(db, world, "أبي هدية")
        assert customer_order_evidence_available(payload) is True
        ctx = _ctx("أبي هدية", tenant_id=world.tenant.id, customer_id=world.customer.id)
        contract = build_commerce_turn_contract(ctx, db=db)
        assert contract.known_facts.get("existing_order_support_only") is not True
        assert "do_not_search_products" not in set(contract.forbidden_actions)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "هدية"}, reason="browse")
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_SEARCH_PRODUCTS

    def test_track_turn_still_owns_support(self, db, world) -> None:
        ctx = _ctx(
            "وين طلبي؟",
            intent_name=INTENT_TRACK_ORDER,
            tenant_id=world.tenant.id,
            history=[
                {"direction": "in", "body": GENERIC_ORDER_REF},
                {"direction": "out", "body": "طلبك قيد التجهيز"},
            ],
        )
        contract = build_commerce_turn_contract(ctx, db=db)
        assert contract.known_facts.get("existing_order_support_only") is True
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "جاكيت"}, reason="noise")
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_LLM_REPLY


class TestHUnknownTruth:
    def test_known_customer_without_orders_is_honest_empty(self, db) -> None:
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
        payload = collect_customer_order_evidence(
            db=db,
            tenant_id=tenant.id,
            phone=DEFAULT_PHONE_E164,
            customer_id=customer.id,
            message=LIVE_PREVIOUS_ORDER,
        )
        assert payload is not None
        assert payload["order_count"] == 0
        assert payload["orders"] == []
        assert payload["current_order"] is None
        assert payload["latest_open_order"] is None
        assert customer_order_evidence_available(payload) is False

    def test_unscoped_identity_returns_none(self, db, world) -> None:
        payload = collect_customer_order_evidence(
            db=db,
            tenant_id=world.tenant.id,
            phone="",
            customer_id=None,
        )
        assert payload is None


class TestRetrievalNotHiddenByOtherCustomers:
    def test_customer_order_survives_newer_foreign_volume(self, db, world) -> None:
        other = seed_customer(
            db, world.tenant.id, phone=OTHER_CUSTOMER_PHONE, name="نورة عبدالله",
        )
        for idx in range(60):
            row = seed_order(
                db,
                world.tenant.id,
                source="salla",
                status="paid",
                external_id=f"noise-{idx}",
                external_order_number=f"NOISE-{idx:04d}",
                customer_info={"phone": OTHER_CUSTOMER_PHONE},
                line_items=[{"title": GENERIC_PERFUME, "quantity": 1}],
            )
            row.customer_id = other.id
        db.commit()
        rows = _fetch_tenant_orders_for_customer(
            db,
            tenant_id=world.tenant.id,
            phone=world.phone,
            customer_id=world.customer.id,
            limit=50,
        )
        refs = {str(row.external_order_number or "") for row in rows}
        assert GENERIC_ORDER_REF in refs
        ctx = resolve_customer_order_context(
            db,
            tenant_id=world.tenant.id,
            customer_id=world.customer.id,
            phone=world.phone,
            intent="track_order",
        )
        assert ctx.selected_order is not None
        assert ctx.selected_order.display_reference == GENERIC_ORDER_REF


class TestTenantIsolation:
    def test_other_tenant_orders_are_invisible(self, db, world) -> None:
        other_tenant = seed_tenant(db, name="متجر آخر")
        other_customer = seed_customer(
            db, other_tenant.id, phone=OTHER_TENANT_PHONE, name="نورة عبدالله",
        )
        foreign = seed_order(
            db,
            other_tenant.id,
            source="salla",
            status="paid",
            external_id="555444333",
            external_order_number="555444333",
            customer_info={"phone": OTHER_TENANT_PHONE},
            line_items=[{"title": GENERIC_PERFUME, "quantity": 9}],
        )
        foreign.customer_id = other_customer.id
        db.commit()
        payload = _evidence(db, world, "وش طلباتي السابقة؟")
        refs = {row["display_reference"] for row in payload["orders"]}
        assert "555444333" not in refs
        other_payload = collect_customer_order_evidence(
            db=db,
            tenant_id=other_tenant.id,
            phone=OTHER_TENANT_PHONE,
            customer_id=other_customer.id,
        )
        assert other_payload is not None
        other_refs = {row["display_reference"] for row in other_payload["orders"]}
        assert GENERIC_ORDER_REF not in other_refs
        assert "555444333" in other_refs


class TestSlimAndProjection:
    def test_slim_keeps_customer_order_evidence(self, db, world) -> None:
        payload = _evidence(db, world, LIVE_PREVIOUS_ORDER)
        slim = _slim_known_facts(
            {
                "customer_order_evidence": payload,
                "catalog_reasoning_candidates": [{"title": GENERIC_SHIRT}],
                "has_products": True,
            },
        )
        assert slim["customer_order_evidence"]["current_order"]["display_reference"] == GENERIC_ORDER_REF
        assert slim["catalog_reasoning_candidates"][0]["title"] == GENERIC_SHIRT

    def test_general_intent_does_not_slim_away_order_evidence(self, db, world) -> None:
        payload = _evidence(db, world, LIVE_PREVIOUS_ORDER)
        state = BrainReplyState(
            intent_name="general",
            known_facts={"customer_order_evidence": payload},
        )
        ok, reason = should_slim_general_brain_state(state)
        assert ok is False
        assert reason == "customer_order_evidence"

    def test_greeting_still_eligible_for_slim(self, db, world) -> None:
        payload = _evidence(db, world)
        state = BrainReplyState(
            intent_name="greeting",
            known_facts={"customer_order_evidence": payload},
        )
        ok, reason = should_slim_general_brain_state(state)
        assert ok is True


class TestCancelledNewerThanOpen:
    def test_current_is_open_latest_is_cancelled(self, db) -> None:
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
        conv = seed_conversation(db, tenant.id, customer_id=customer.id)
        open_order = seed_order(
            db,
            tenant.id,
            source="salla",
            status="in_progress",
            external_id="257404293",
            external_order_number="257404293",
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[
                {"product_title": GENERIC_SHOE, "quantity": 1},
                {"product_title": GENERIC_SHIRT, "quantity": 3},
            ],
            extra_metadata={"created_at": "2026-05-04T10:00:00+00:00"},
        )
        open_order.customer_id = customer.id
        cancelled = seed_order(
            db,
            tenant.id,
            source="salla",
            status="cancelled",
            external_id="269977976",
            external_order_number="269977976",
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[{"product_title": "تنورة", "quantity": 1}],
            extra_metadata={
                "created_at": "2026-07-02T10:00:00+00:00",
                "payment_method": "cod",
            },
        )
        cancelled.customer_id = customer.id
        db.commit()
        payload = collect_customer_order_evidence(
            db=db,
            tenant_id=tenant.id,
            phone=DEFAULT_PHONE_E164,
            customer_id=customer.id,
            conversation_id=conv.id,
            last_discussed_order_ref="269977976",
        )
        assert payload is not None
        assert payload["current_order"]["display_reference"] == "257404293"
        assert payload["latest_open_order"]["display_reference"] == "257404293"
        assert payload["latest_order"]["display_reference"] == "269977976"
        assert payload["latest_order"]["status"] == "cancelled"
        assert payload["current_open_order"]["display_reference"] == "257404293"
        prev_refs = {row["display_reference"] for row in payload["previous_orders"]}
        assert "269977976" in prev_refs
        assert "257404293" not in prev_refs
        names = {item["name"] for item in payload["latest_order"]["line_items"]}
        assert "تنورة" in names
        assert payload["referenced_order"]["display_reference"] == "269977976"
        follow_names = {
            item["name"] for item in payload["referenced_order"]["line_items"]
        }
        assert "تنورة" in follow_names

    def test_slot_track_without_status_rule_is_evidence_compose(self) -> None:
        ctx = _ctx(
            "وش طلباتي السابقة؟",
            intent_name=INTENT_TRACK_ORDER,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "order_history"
        assert decision.args.get("block_catalog_push") is True

    def test_latest_summary_is_evidence_compose_not_canned(self) -> None:
        matched = rules.match("آخر طلب لي وش كان فيه؟")
        assert matched is not None
        assert matched.name == INTENT_LATEST_ORDER_SUMMARY
        ctx = _ctx("آخر طلب لي وش كان فيه؟", intent_name=INTENT_LATEST_ORDER_SUMMARY)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.args.get("ledger_topic") == INTENT_LATEST_ORDER_SUMMARY
        assert decision.args.get("topic") == "latest_order_summary"
        assert decision.args.get("block_catalog_push") is True
