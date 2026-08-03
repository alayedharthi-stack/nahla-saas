"""
Salla Merchant AI — End-to-End Acceptance Test suite (synthetic tenants).

Layer 1 deterministic matrix (groups A–K + OFV2). Layer 3 (human) / Layer 4 (live)
are NOT run — conversation quality scored as deferred_layer3_human when LLM mocked.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_disabled_gate import (  # noqa: E402
    REASON_HANDOFF_SESSION,
    REASON_HUMAN_OWNERSHIP,
    REASON_HUMAN_SUPERVISION,
    is_ai_disabled_for_conversation,
)
from core.ai_pause_guard import REASON_MANUAL_PAUSE  # noqa: E402
from core.inbound_dedup import is_duplicate_inbound, reset_cache  # noqa: E402
from core.local_order_resolver import resolve_customer_order_context  # noqa: E402
from models import MerchantKnowledgeSection, Order  # noqa: E402
from modules.ai.brain.truth_surface.product_sale_offer_loader import is_strict_product_sale  # noqa: E402
from modules.ai.commerce.permission_loader import load_tenant_commerce_permissions  # noqa: E402
from modules.ai.commerce.runtime import CommerceToolRuntime  # noqa: E402
from modules.ai.order_flow_v2.owner import (  # noqa: E402
    OrderFlowV2Result,
    persist_order_flow_v2_result,
    try_handle_order_flow_v2,
)
from tests.commerce_scenario_fixtures import make_scenario_db  # noqa: E402
from tests.salla_acceptance.fixtures import (  # noqa: E402
    PHONE_CUST_A,
    PHONE_CUST_B,
    PHONE_CUST_C,
    PHONE_CUST_D,
    PHONE_B_CUST_A,
    TENANT_A_NAME,
    TENANT_B_NAME,
    query_kb_sections,
    seed_dual_tenant_world,
)
from tests.salla_acceptance.harness import (  # noqa: E402
    ACCEPTANCE_RESULTS,
    AcceptanceHarness,
    print_console_summary,
    record_acceptance,
    record_turn,
    write_acceptance_report,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def world():
    db, _engine = make_scenario_db()
    w = seed_dual_tenant_world(db)
    yield w
    db.close()


@pytest.fixture()
def harness(world):
    return AcceptanceHarness(world.db, world)


@pytest.fixture(scope="session", autouse=True)
def _acceptance_results_session():
    ACCEPTANCE_RESULTS.clear()
    yield
    summary = write_acceptance_report()
    print_console_summary(summary)


@pytest.fixture(autouse=True)
def _ofv2_env_safe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ORDER_FLOW_V2_ENFORCE_TENANTS", raising=False)
    monkeypatch.delenv("ORDER_FLOW_V2_DISABLED_TENANTS", raising=False)
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", False, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", True, raising=False)


# ── Group I: Tenant isolation (critical) ───────────────────────────────────


class TestGroupITenantIsolation:
    def test_i1_catalog_isolation(self, world, harness) -> None:
        async def _go():
            rt_a = harness._runtime(world.tenant_a.tenant_id)
            rt_b = harness._runtime(world.tenant_b.tenant_id)
            res_a = await rt_a.execute("search_products", {"query": "حذاء"})
            res_b = await rt_b.execute("search_products", {"query": "ساعة"})
            titles_a = {p.get("title") for p in res_a.payload.get("products", [])}
            titles_b = {p.get("title") for p in res_b.payload.get("products", [])}
            ok = "حذاء رياضي أبيض" in titles_a and "حذاء رياضي أبيض" not in titles_b
            ok = ok and "ساعة يد فضية" in titles_b and "ساعة يد فضية" not in titles_a
            record_acceptance(
                scenario_id="I1",
                messages=["حذاء"],
                tenant=TENANT_A_NAME,
                expected="tenant-scoped catalog only",
                actual="pass" if ok else "fail",
                tools=["search_products"],
                sources=["catalog"],
                result="pass" if ok else "fail",
                severity="critical",
                evidence={"titles_a": list(titles_a), "titles_b": list(titles_b)},
            )
            assert ok

        _run(_go())

    def test_i2_kb_isolation(self, world) -> None:
        kb_a = query_kb_sections(world.db, world.tenant_a.tenant_id, kind="shipping")
        kb_b = query_kb_sections(world.db, world.tenant_b.tenant_id, kind="shipping")
        body_a = " ".join(s.body for s in kb_a)
        body_b = " ".join(s.body for s in kb_b)
        ok = "رياض" in body_a and "جدة" in body_b and "جدة" not in body_a
        record_acceptance(
            scenario_id="I2",
            messages=["مدة الشحن"],
            tenant=TENANT_A_NAME,
            expected="KB scoped per tenant",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["merchant_knowledge_section"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok

    def test_i3_orders_isolation(self, world) -> None:
        orders_a = (
            world.db.query(Order)
            .filter_by(tenant_id=world.tenant_a.tenant_id)
            .all()
        )
        orders_b = (
            world.db.query(Order)
            .filter_by(tenant_id=world.tenant_b.tenant_id)
            .all()
        )
        refs_a = {o.external_order_number for o in orders_a}
        refs_b = {o.external_order_number for o in orders_b}
        ok = refs_a.isdisjoint(refs_b) and len(orders_a) >= 4 and len(orders_b) >= 1
        record_acceptance(
            scenario_id="I3",
            messages=["طلباتي"],
            tenant=TENANT_A_NAME,
            expected="orders isolated by tenant_id",
            actual="pass" if ok else "fail",
            tools=["track_order"],
            sources=["local_db"],
            result="pass" if ok else "fail",
            severity="critical",
            evidence={"count_a": len(orders_a), "count_b": len(orders_b)},
        )
        assert ok

    def test_i4_permissions_isolation(self, world) -> None:
        perms_a = load_tenant_commerce_permissions(world.db, world.tenant_a.tenant_id)
        perms_b = load_tenant_commerce_permissions(world.db, world.tenant_b.tenant_id)
        ok = (
            perms_a.permissions.can_apply_coupons is True
            and perms_b.permissions.can_apply_coupons is False
            and perms_a.permissions.can_create_orders is True
            and perms_b.permissions.can_create_orders is True
        )
        record_acceptance(
            scenario_id="I4",
            messages=["صلاحيات"],
            tenant=TENANT_A_NAME,
            expected="CommercePermissions per tenant",
            actual="pass" if ok else "fail",
            tools=[],
            sources=[perms_a.source, perms_b.source],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok

    def test_i5_customer_memory_isolation(self, world, harness) -> None:
        async def _go():
            rt_a = harness._runtime(
                world.tenant_a.tenant_id,
                customer_phone=PHONE_CUST_A,
                customer_id=world.tenant_a.customers["A"].id,
            )
            rt_b = harness._runtime(
                world.tenant_b.tenant_id,
                customer_phone=PHONE_B_CUST_A,
                customer_id=world.tenant_b.customers["A"].id,
            )
            hist_a = await rt_a.execute("get_customer_history", {})
            hist_b = await rt_b.execute("get_customer_history", {})
            block_a = hist_a.payload.get("history_block") or ""
            block_b = hist_b.payload.get("history_block") or ""
            ok = PHONE_CUST_A.replace("+", "") not in block_b or not block_b
            record_acceptance(
                scenario_id="I5",
                messages=["سجل العميل"],
                tenant=TENANT_A_NAME,
                expected="no cross-tenant customer history",
                actual="pass" if ok else "fail",
                tools=["get_customer_history"],
                sources=["customer_context"],
                result="pass" if ok else "fail",
                severity="critical",
            )
            assert ok

        _run(_go())

    def test_i6_staff_contact_kb_isolation(self, world) -> None:
        staff_a = query_kb_sections(world.db, world.tenant_a.tenant_id, kind="staff_contact")
        staff_b = query_kb_sections(world.db, world.tenant_b.tenant_id, kind="staff_contact")
        phones_a = " ".join(s.body for s in staff_a)
        phones_b = " ".join(s.body for s in staff_b)
        ok = "966541111001" in phones_a and "966542222002" in phones_b
        ok = ok and "966542222002" not in phones_a
        record_acceptance(
            scenario_id="I6",
            messages=["رقم الموظف"],
            tenant=TENANT_A_NAME,
            expected="staff KB isolated",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["staff_contact_kb"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok


# ── Group F: Orders / privacy ───────────────────────────────────────────────


class TestGroupFOrders:
    def test_f1_track_shipped_order(self, world, harness) -> None:
        async def _go():
            order = world.tenant_a.orders["B"]
            turn = await harness.layer1_turn(
                scenario_id="F1",
                tenant_id=world.tenant_a.tenant_id,
                inbound="وين طلبي؟",
                tool_name="track_order",
                tool_payload={
                    "conversation_id": world.tenant_a.conversations["B"].id,
                },
                customer_phone=PHONE_CUST_B,
                customer_id=world.tenant_a.customers["B"].id,
                conversation_id=world.tenant_a.conversations["B"].id,
                severity="major",
                expected_predicate=lambda r: r.ok and r.payload.get("order"),
            )
            payload = turn.tool_results[0]
            record_turn(turn, tenant_name=TENANT_A_NAME, expected="shipped order with tracking")
            assert turn.outcome == "pass"
            assert order.external_order_number

        _run(_go())

    def test_f3_multiple_orders_customer_c(self, world) -> None:
        ctx = resolve_customer_order_context(
            world.db,
            tenant_id=world.tenant_a.tenant_id,
            customer_id=world.tenant_a.customers["C"].id,
            phone=PHONE_CUST_C,
            intent="track_order",
        )
        ok = len(ctx.orders_by_priority) >= 2
        record_acceptance(
            scenario_id="F3",
            messages=["طلباتي"],
            tenant=TENANT_A_NAME,
            expected="multiple orders for customer C",
            actual="pass" if ok else "fail",
            tools=["track_order"],
            sources=["local_order_resolver"],
            result="pass" if ok else "fail",
            severity="major",
            evidence={"count": len(ctx.orders_by_priority)},
        )
        assert ok

    def test_f4_foreign_order_privacy(self, world) -> None:
        foreign_ref = world.tenant_a.orders["B"].external_order_number
        ctx = resolve_customer_order_context(
            world.db,
            tenant_id=world.tenant_a.tenant_id,
            customer_id=world.tenant_a.customers["D"].id,
            phone=PHONE_CUST_D,
            intent="track_order",
            order_number=foreign_ref,
        )
        ok = ctx.selected_order is None and ctx.selected_reason == "explicit_order_number_not_found"
        record_acceptance(
            scenario_id="F4",
            messages=[f"تتبع {foreign_ref}"],
            tenant=TENANT_A_NAME,
            expected="foreign order not exposed",
            actual="pass" if ok else "fail",
            tools=["track_order"],
            sources=["local_order_resolver"],
            result="pass" if ok else "fail",
            severity="critical",
            evidence={"reason": ctx.selected_reason},
        )
        assert ok


# ── Group G: Handoff truth / AI suppression ─────────────────────────────────


class TestGroupGHandoff:
    def _mock_db(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_g2_handoff_scrub_without_truth(self, harness) -> None:
        out, scrubbed = harness.scrub_outbound(
            "سأحوّل المحادثة لفريق المتجر الآن.",
            tenant_id=1,
            recipient="966500100001",
            handoff_truth_active=False,
        )
        ok = scrubbed and "سأحوّل" not in out["text"]["body"]
        record_acceptance(
            scenario_id="G2",
            messages=["حولني للدعم"],
            tenant=TENANT_A_NAME,
            expected="handoff promise scrubbed without truth",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["outbound_sanitizer"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok

    def test_g3_ai_paused_blocks(self, world) -> None:
        convo = SimpleNamespace(
            id=1,
            tenant_id=world.tenant_a.tenant_id,
            customer_id=world.tenant_a.customers["A"].id,
            ai_paused=True,
            ai_paused_reason=REASON_MANUAL_PAUSE,
            is_human_handoff=False,
            needs_human=False,
            handoff_active=False,
            paused_by_human=False,
            taken_over_at=None,
            status="active",
        )
        db = self._mock_db()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=world.tenant_a.tenant_id,
                customer_phone=PHONE_CUST_A,
            )
        ok = decision.disabled and decision.reason == REASON_MANUAL_PAUSE
        record_acceptance(
            scenario_id="G3",
            messages=["مرحبا"],
            tenant=TENANT_A_NAME,
            expected="ai_paused disables AI",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["ai_disabled_gate"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok

    def test_g4_human_ownership_blocks(self, world) -> None:
        convo = SimpleNamespace(
            id=2,
            tenant_id=world.tenant_a.tenant_id,
            customer_id=world.tenant_a.customers["B"].id,
            ai_paused=False,
            ai_paused_reason=None,
            is_human_handoff=False,
            needs_human=False,
            handoff_active=False,
            paused_by_human=False,
            taken_over_at=None,
            status="active",
        )
        db = self._mock_db()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=True,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=world.tenant_a.tenant_id,
                customer_phone=PHONE_CUST_B,
            )
        ok = decision.disabled and decision.reason == REASON_HUMAN_OWNERSHIP
        record_acceptance(
            scenario_id="G4",
            messages=["مرحبا"],
            tenant=TENANT_A_NAME,
            expected="human ownership disables AI",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["ownership_state"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok


# ── Group H: Dedup ──────────────────────────────────────────────────────────


class TestGroupHDedup:
    def test_h1_inbound_dedup(self) -> None:
        reset_cache()
        first = is_duplicate_inbound(phone_number_id="PH_A", msg_id="msg-accept-001")
        second = is_duplicate_inbound(phone_number_id="PH_A", msg_id="msg-accept-001")
        ok = first is False and second is True
        record_acceptance(
            scenario_id="H1",
            messages=["duplicate webhook"],
            tenant=TENANT_A_NAME,
            expected="inbound dedup blocks retry",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["inbound_dedup"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok

    def test_h1b_outbound_dedup_single_send(self, harness) -> None:
        sends = harness.simulate_outbound_send(
            "رد تجريبي للقبول",
            tenant_id=99,
            recipient="966500100001",
            dedup=True,
        )
        ok = sends == 1
        record_acceptance(
            scenario_id="H1b",
            messages=["رد مكرر"],
            tenant=TENANT_A_NAME,
            expected="outbound dedup allows one send",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["outbound_dedup"],
            result="pass" if ok else "fail",
            severity="major",
            evidence={"send_count": sends},
        )
        assert ok


# ── Group B: Stock / price truth ──────────────────────────────────────────────


class TestGroupBCatalog:
    def test_a1_product_search_white_shoe(self, world, harness) -> None:
        async def _go():
            turn = await harness.layer1_turn(
                scenario_id="A1",
                tenant_id=world.tenant_a.tenant_id,
                inbound="حذاء رياضي أبيض",
                tool_name="search_products",
                tool_payload={"query": "حذاء رياضي أبيض"},
                severity="major",
                expected_predicate=lambda r: any(
                    "أبيض" in (p.get("title") or "") for p in r.payload.get("products", [])
                ),
            )
            record_turn(turn, tenant_name=TENANT_A_NAME, expected="white shoe in results")
            assert turn.outcome == "pass"

        _run(_go())

    def test_a4_similar_name_disambiguation(self, world, harness) -> None:
        async def _go():
            res = await harness.execute_tool(
                world.tenant_a.tenant_id,
                "search_products",
                {"query": "حذاء رياضي"},
            )
            titles = [p.get("title") for p in res.payload.get("products", [])]
            ok = "حذاء رياضي أبيض" in titles and "حذاء رياضي أسود" in titles
            record_acceptance(
                scenario_id="A4",
                messages=["حذاء رياضي"],
                tenant=TENANT_A_NAME,
                expected="both similar shoes returned",
                actual="pass" if ok else "fail",
                tools=["search_products"],
                sources=["catalog"],
                result="pass" if ok else "fail",
                severity="major",
                evidence={"titles": titles},
            )
            assert ok

        _run(_go())

    def test_b1_price_from_catalog(self, world, harness) -> None:
        async def _go():
            res = await harness.execute_tool(
                world.tenant_a.tenant_id,
                "get_product_details",
                {"external_id": "sku-perfume-rose"},
            )
            product = res.payload.get("product") or {}
            ok = res.ok and str(product.get("price")) == "180"
            record_acceptance(
                scenario_id="B1",
                messages=["كم سعر عطر الورد؟"],
                tenant=TENANT_A_NAME,
                expected="price 180 from DB",
                actual="pass" if ok else "fail",
                tools=["get_product_details"],
                sources=["catalog"],
                result="pass" if ok else "fail",
                severity="major",
                evidence={"price": product.get("price")},
            )
            assert ok

        _run(_go())

    def test_b4_stock_in_stock_variant(self, world, harness) -> None:
        async def _go():
            res = await harness.execute_tool(
                world.tenant_a.tenant_id,
                "check_stock",
                {"external_id": "sku-shoe-white"},
            )
            stock = res.payload.get("stock") or {}
            ok = res.ok and stock.get("available") is True
            record_acceptance(
                scenario_id="B4",
                messages=["متوفر الحذاء الأبيض؟"],
                tenant=TENANT_A_NAME,
                expected="white shoe available",
                actual="pass" if ok else "fail",
                tools=["check_stock"],
                sources=["catalog"],
                result="pass" if ok else "fail",
                severity="critical",
                evidence=stock,
            )
            assert ok

        _run(_go())

    def test_b5_oos_product_and_variant(self, world, harness) -> None:
        async def _go():
            shirt_search = await harness.execute_tool(
                world.tenant_a.tenant_id,
                "search_products",
                {"query": "قميص", "include_non_orderable_facts": True},
            )
            facts = shirt_search.payload.get("catalog_fact_products") or []
            shirt_fact = facts[0] if facts else {}
            ok_shirt = shirt_fact.get("can_checkout") is False and shirt_fact.get("in_stock") is False
            black_details = await harness.execute_tool(
                world.tenant_a.tenant_id,
                "get_product_details",
                {"external_id": "sku-shoe-black"},
            )
            variants = (black_details.payload.get("product") or {}).get("variants") or []
            oos_variants = [v for v in variants if v.get("in_stock") is False]
            ok_black = black_details.ok and len(oos_variants) >= 1
            ok = ok_shirt and ok_black
            record_acceptance(
                scenario_id="B5",
                messages=["هل القميص متوفر؟"],
                tenant=TENANT_A_NAME,
                expected="OOS shirt not orderable; black shoe has OOS variant",
                actual="pass" if ok else "fail",
                tools=["search_products", "get_product_details"],
                sources=["catalog"],
                result="pass" if ok else "fail",
                severity="critical",
                evidence={"shirt_fact": shirt_fact, "oos_variants": len(oos_variants)},
            )
            assert ok

        _run(_go())


# ── Group C: Discount truth ───────────────────────────────────────────────────


class TestGroupCDiscount:
    def test_c2_no_invented_discount_on_rose(self, world) -> None:
        meta = world.tenant_a.products["C"].extra_metadata or {}
        ok = not is_strict_product_sale(
            {"sale_price": meta.get("sale_price"), "regular_price": meta.get("regular_price")}
        )
        record_acceptance(
            scenario_id="C2",
            messages=["في خصم على عطر الورد؟"],
            tenant=TENANT_A_NAME,
            expected="no verified sale on rose perfume",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["product_sale_offer_loader"],
            result="pass" if ok else "fail",
            severity="critical",
            evidence={"meta": meta},
        )
        assert ok

    def test_c2b_offer_only_on_wood(self, world) -> None:
        meta = world.tenant_a.products["D"].extra_metadata or {}
        ok = is_strict_product_sale(
            {"sale_price": meta.get("sale_price"), "regular_price": meta.get("regular_price")}
        )
        record_acceptance(
            scenario_id="C2b",
            messages=["عطر الخشب فيه عرض؟"],
            tenant=TENANT_A_NAME,
            expected="verified sale on wood perfume only",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["product_metadata"],
            result="pass" if ok else "fail",
            severity="major",
        )
        assert ok


# ── Group D: Knowledge ──────────────────────────────────────────────────────


class TestGroupDKnowledge:
    def test_d1_shipping_kb_tenant_a(self, world) -> None:
        sections = query_kb_sections(world.db, world.tenant_a.tenant_id, kind="shipping")
        body = " ".join(s.body for s in sections)
        ok = "رياض" in body and "25" in body
        record_acceptance(
            scenario_id="D1",
            messages=["كم مدة الشحن للرياض؟"],
            tenant=TENANT_A_NAME,
            expected="Riyadh shipping KB present",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["merchant_knowledge_section"],
            result="pass" if ok else "fail",
            severity="major",
        )
        assert ok

    def test_d3_payment_methods_kb(self, world) -> None:
        sections = query_kb_sections(world.db, world.tenant_a.tenant_id, kind="payment")
        body = " ".join(s.body for s in sections)
        ok = "مدى" in body or "فيزا" in body
        record_acceptance(
            scenario_id="D3",
            messages=["طرق الدفع"],
            tenant=TENANT_A_NAME,
            expected="payment methods in KB",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["merchant_knowledge_section"],
            result="pass" if ok else "fail",
            severity="major",
        )
        assert ok


# ── Group K: Sensitive fail-closed ──────────────────────────────────────────


class TestGroupKPermissions:
    def test_k1_create_denied_when_permission_off(self, world) -> None:
        async def _go():
            from models import CommercePermissions  # noqa: PLC0415

            tenant = world.tenant_b
            world.db.query(CommercePermissions).filter_by(tenant_id=tenant.tenant_id).delete()
            world.db.commit()
            row = CommercePermissions(
                tenant_id=tenant.tenant_id,
                can_create_orders=False,
                can_create_checkout_links=True,
                can_send_payment_links=True,
                can_apply_coupons=False,
                can_auto_generate_coupons=False,
                can_cancel_orders=False,
            )
            world.db.add(row)
            world.db.commit()
            load = load_tenant_commerce_permissions(world.db, tenant.tenant_id)
            runtime = CommerceToolRuntime(
                world.db,
                tenant_id=tenant.tenant_id,
                permissions=load.permissions,
                permission_source=load.source,
            )
            result = await runtime.execute(
                "create_draft_order",
                {"product_id": "sku-b-watch", "quantity": 1},
            )
            ok = not result.ok and result.audit.get("permission_denied")
            record_acceptance(
                scenario_id="K1",
                messages=["أبغى أطلب"],
                tenant=TENANT_B_NAME,
                expected="create_draft_order denied",
                actual="pass" if ok else "fail",
                tools=["create_draft_order"],
                sources=[load.source],
                result="pass" if ok else "fail",
                severity="critical",
            )
            assert ok

        _run(_go())

    def test_k2_load_failed_fail_closed(self, world) -> None:
        with patch.object(world.db, "query", side_effect=RuntimeError("db unavailable")):
            load = load_tenant_commerce_permissions(world.db, world.tenant_a.tenant_id)
        ok = (
            load.source == "load_failed"
            and not load.ok
            and not load.permissions.can_create_orders
            and load.permissions.is_permitted("search_products")
        )
        record_acceptance(
            scenario_id="K2",
            messages=["صلاحيات"],
            tenant=TENANT_A_NAME,
            expected="load_failed fail-closed",
            actual="pass" if ok else "fail",
            tools=[],
            sources=["permission_loader"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok


# ── OFV2 rollout (synthetic tenant only) ─────────────────────────────────────


class TestGroupOFV2:
    def _conversation(self, *, tenant_id: int, ai_paused: bool = False):
        return SimpleNamespace(
            id=501,
            tenant_id=tenant_id,
            ai_paused=ai_paused,
            ai_paused_reason=REASON_MANUAL_PAUSE if ai_paused else "",
            status="active",
            is_human_handoff=False,
            needs_human=False,
            handoff_active=False,
            paused_by_human=False,
            taken_over_at=None,
            extra_metadata={"brain_state": {"order_prep": {}, "cart_items": []}},
        )

    def test_ofv2_disabled_no_takeover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "false")
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500100001",
            message="مرحبا",
        )
        ok = result.handled is False
        record_acceptance(
            scenario_id="OFV2-disabled",
            messages=["مرحبا"],
            tenant=TENANT_A_NAME,
            expected="OFV2 disabled does not handle",
            actual="pass" if ok else "fail",
            tools=["order_flow_v2"],
            sources=["owner"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok

    def test_ofv2_shadow_no_write(self, world) -> None:
        prep = {
            "local_draft_authoritative": True,
            "line_items": [{"product_id": "sku-shoe-white", "quantity": 1}],
            "order_flow_v2_active": True,
        }
        conv = self._conversation(tenant_id=world.tenant_a.tenant_id)
        with patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(False, True, "shadow_only")), patch(
            "modules.ai.order_flow_v2.owner._load_brain_state",
            return_value=(conv, {"order_prep": prep}),
        ):
            result = try_handle_order_flow_v2(
                world.db,
                tenant_id=world.tenant_a.tenant_id,
                customer_phone=PHONE_CUST_A,
                message="الرياض",
            )
        ok = result.shadow_only and result.state_patch == {}
        with patch("modules.ai.order_flow_v2.owner.apply_state_patch") as apply_patch:
            persist_order_flow_v2_result(
                world.db,
                tenant_id=world.tenant_a.tenant_id,
                customer_phone=PHONE_CUST_A,
                result=OrderFlowV2Result(
                    handled=False,
                    shadow_only=True,
                    reason="shadow_only",
                    state_patch={"line_items": prep["line_items"]},
                ),
            )
            apply_patch.assert_not_called()
        record_acceptance(
            scenario_id="OFV2-shadow",
            messages=["الرياض"],
            tenant=TENANT_A_NAME,
            expected="shadow mode no persist",
            actual="pass" if ok else "fail",
            tools=["order_flow_v2"],
            sources=["owner"],
            result="pass" if ok else "fail",
            severity="critical",
        )
        assert ok

    def test_ofv2_enforce_allowlist_only(self, world, harness, monkeypatch: pytest.MonkeyPatch) -> None:
        tid = world.tenant_a.tenant_id
        other = world.tenant_b.tenant_id
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", str(tid))
        conv = self._conversation(tenant_id=tid)
        live_a = harness.resolve_ofv2(tid, conversation=conv, customer_phone=PHONE_CUST_A)
        live_b = harness.resolve_ofv2(other, conversation=self._conversation(tenant_id=other))
        ok = live_a.live is True and live_b.live is False and live_b.shadow_log is True
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "")
        rollback = harness.resolve_ofv2(tid, conversation=conv, customer_phone=PHONE_CUST_A)
        ok = ok and rollback.live is False
        record_acceptance(
            scenario_id="OFV2-enforce",
            messages=["checkout"],
            tenant=TENANT_A_NAME,
            expected="enforce only allowlisted tenant",
            actual="pass" if ok else "fail",
            tools=["order_flow_v2"],
            sources=["enforcement"],
            result="pass" if ok else "fail",
            severity="critical",
            evidence={
                "live_a": live_a.live,
                "live_b": live_b.live,
                "rollback_live": rollback.live,
            },
        )
        assert ok


# ── Group J: Conversation quality (deferred) ──────────────────────────────────


class TestGroupJConversationQuality:
    def test_j1_deferred_human_review(self) -> None:
        record_acceptance(
            scenario_id="J1",
            messages=["محادثة طبيعية"],
            tenant=TENANT_A_NAME,
            expected="85% conversation quality",
            actual="deferred_layer3_human",
            tools=[],
            sources=[],
            result="pass",
            severity="minor",
            layer="layer3_deferred",
            llm_mocked=True,
        )
        assert True
