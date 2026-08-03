"""P0-B tenant commerce permissions — MerchantBrain + canonical loader."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from models import CommercePermissions  # noqa: E402
from modules.ai.brain.commerce.permission_gate import deny_reason_for_brain_action
from modules.ai.brain.decision.actions import (
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.execution.executor import DefaultActionExecutor
from modules.ai.brain.types import BrainContext, CommerceFacts, Decision, Intent, MerchantConversationState
from modules.ai.commerce.permission_loader import load_tenant_commerce_permissions
from modules.ai.commerce.permissions import CommercePermissionSet
from modules.ai.commerce.runtime import CommerceToolRuntime
from modules.ai.orchestrator.pipeline import AIOrchestrationPipeline
from modules.ai.orchestrator.types import AIContext, AIOrchestrationRequest
from tests.commerce_scenario_fixtures import make_scenario_db, seed_tenant  # noqa: E402


def _seed_permissions(
    db,
    tenant_id: int,
    *,
    can_create_orders: bool = True,
    can_create_checkout_links: bool = True,
    can_send_payment_links: bool = True,
    can_apply_coupons: bool = True,
    can_auto_generate_coupons: bool = True,
    can_cancel_orders: bool = False,
) -> CommercePermissions:
    row = CommercePermissions(
        tenant_id=tenant_id,
        can_create_orders=can_create_orders,
        can_create_checkout_links=can_create_checkout_links,
        can_send_payment_links=can_send_payment_links,
        can_apply_coupons=can_apply_coupons,
        can_auto_generate_coupons=can_auto_generate_coupons,
        can_cancel_orders=can_cancel_orders,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _minimal_ctx(
    db,
    tenant_id: int,
    *,
    permissions: CommercePermissionSet | None = None,
    permission_source: str = "db_row",
) -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone="+966500000001",
        message="test",
        intent=Intent(name="general", confidence=1.0),
        state=MerchantConversationState(),
        facts=CommerceFacts(),
        commerce_permissions=permissions,
        permission_source=permission_source,
    )
    ctx._db = db  # type: ignore[attr-defined]
    return ctx


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


def test_load_from_db_row(db) -> None:
    tenant = seed_tenant(db, name="متجر أ")
    _seed_permissions(
        db,
        tenant.id,
        can_create_orders=False,
        can_send_payment_links=True,
        can_apply_coupons=False,
    )
    result = load_tenant_commerce_permissions(db, tenant.id)
    assert result.source == "db_row"
    assert result.ok is True
    assert result.permissions.can_create_orders is False
    assert result.permissions.can_send_payment_links is True
    assert result.permissions.can_apply_coupons is False


def test_missing_row_returns_defaults(db) -> None:
    tenant = seed_tenant(db, name="متجر ب")
    result = load_tenant_commerce_permissions(db, tenant.id)
    assert result.source == "defaults_missing_row"
    assert result.permissions.can_create_orders is True
    assert result.permissions.can_cancel_orders is False


def test_load_failure_fail_closed(db) -> None:
    tenant = seed_tenant(db, name="متجر ج")
    with patch.object(db, "query", side_effect=RuntimeError("db unavailable")):
        result = load_tenant_commerce_permissions(db, tenant.id)

    assert result.source == "load_failed"
    assert result.ok is False
    perms = result.permissions
    assert perms.can_create_orders is False
    assert perms.can_send_payment_links is False
    assert perms.can_apply_coupons is False
    assert perms.is_permitted("search_products") is True
    assert perms.is_permitted("track_order") is True
    assert perms.is_permitted("create_draft_order") is False


def test_tenant_a_allows_create_order_tenant_b_denies(db) -> None:
    tenant_a = seed_tenant(db, name="متجر تجريبي عام")
    tenant_b = seed_tenant(db, name="متجر آخر")
    _seed_permissions(db, tenant_a.id, can_create_orders=True)
    _seed_permissions(db, tenant_b.id, can_create_orders=False)

    perms_a = load_tenant_commerce_permissions(db, tenant_a.id).permissions
    perms_b = load_tenant_commerce_permissions(db, tenant_b.id).permissions

    assert perms_a.is_permitted("create_draft_order") is True
    assert perms_b.is_permitted("create_draft_order") is False


def test_coupons_denied_for_one_allowed_for_other(db) -> None:
    tenant_allow = seed_tenant(db, name="كوبون مسموح")
    tenant_deny = seed_tenant(db, name="كوبون ممنوع")
    _seed_permissions(db, tenant_allow.id, can_apply_coupons=True)
    _seed_permissions(db, tenant_deny.id, can_apply_coupons=False)

    assert load_tenant_commerce_permissions(db, tenant_allow.id).permissions.is_permitted("apply_coupon")
    assert not load_tenant_commerce_permissions(db, tenant_deny.id).permissions.is_permitted("apply_coupon")


def test_payment_links_per_tenant(db) -> None:
    tenant_allow = seed_tenant(db, name="دفع مسموح")
    tenant_deny = seed_tenant(db, name="دفع ممنوع")
    _seed_permissions(db, tenant_allow.id, can_send_payment_links=True)
    _seed_permissions(db, tenant_deny.id, can_send_payment_links=False)

    assert load_tenant_commerce_permissions(db, tenant_allow.id).permissions.is_permitted("send_payment_link")
    assert not load_tenant_commerce_permissions(db, tenant_deny.id).permissions.is_permitted("send_payment_link")


def test_track_and_search_allowed_when_create_orders_false(db) -> None:
    tenant = seed_tenant(db, name="قراءة فقط")
    _seed_permissions(db, tenant.id, can_create_orders=False)
    perms = load_tenant_commerce_permissions(db, tenant.id).permissions
    assert perms.is_permitted("create_draft_order") is False
    assert perms.is_permitted("search_products") is True
    assert perms.is_permitted("track_order") is True


def test_sequential_load_no_cross_tenant_leak(db) -> None:
    tenant_a = seed_tenant(db, name="عزل أ")
    tenant_b = seed_tenant(db, name="عزل ب")
    _seed_permissions(db, tenant_a.id, can_create_orders=True, can_apply_coupons=False)
    _seed_permissions(db, tenant_b.id, can_create_orders=False, can_apply_coupons=True)

    first = load_tenant_commerce_permissions(db, tenant_a.id).permissions
    second = load_tenant_commerce_permissions(db, tenant_b.id).permissions
    third = load_tenant_commerce_permissions(db, tenant_a.id).permissions

    assert first.tenant_id == tenant_a.id
    assert second.tenant_id == tenant_b.id
    assert third.can_create_orders is True
    assert second.can_apply_coupons is True
    assert second.can_create_orders is False


def test_runtime_denies_sensitive_tool_with_audit(db) -> None:
    async def _run() -> None:
        tenant = seed_tenant(db, name="متجر ممنوع الطلب")
        _seed_permissions(db, tenant.id, can_create_orders=False)
        perms = load_tenant_commerce_permissions(db, tenant.id).permissions
        runtime = CommerceToolRuntime(
            db,
            tenant_id=tenant.id,
            permissions=perms,
            permission_source="db_row",
        )
        result = await runtime.execute(
            "create_draft_order",
            {"product_id": "sku-1", "quantity": 1},
        )
        assert result.ok is False
        assert "can_create_orders" in (result.error or "")
        assert result.audit.get("permission_denied") is True
        assert result.audit.get("permission_source") == "db_row"

    asyncio.run(_run())


def test_executor_denies_draft_order_before_handler(db) -> None:
    async def _run() -> None:
        tenant = seed_tenant(db, name="متجر تنفيذ")
        _seed_permissions(db, tenant.id, can_create_orders=False)
        load = load_tenant_commerce_permissions(db, tenant.id)
        ctx = _minimal_ctx(db, tenant.id, permissions=load.permissions, permission_source=load.source)
        executor = DefaultActionExecutor()
        result = await executor.execute(
            Decision(action=ACTION_PROPOSE_DRAFT_ORDER, args={"product": {"external_id": "x"}}),
            ctx,
        )
        assert result.success is False
        assert result.data.get("permission_denied") is True
        assert result.data.get("permission_source") == "db_row"

    asyncio.run(_run())


def test_executor_denies_payment_and_coupon(db) -> None:
    async def _run() -> None:
        tenant = seed_tenant(db, name="متجر دفع وكوبون")
        _seed_permissions(
            db,
            tenant.id,
            can_send_payment_links=False,
            can_apply_coupons=False,
        )
        load = load_tenant_commerce_permissions(db, tenant.id)
        ctx = _minimal_ctx(db, tenant.id, permissions=load.permissions, permission_source=load.source)
        executor = DefaultActionExecutor()

        pay = await executor.execute(Decision(action=ACTION_SEND_PAYMENT_LINK, args={}), ctx)
        coupon = await executor.execute(Decision(action=ACTION_SUGGEST_COUPON, args={}), ctx)

        assert pay.success is False and pay.data.get("permission_denied")
        assert coupon.success is False and coupon.data.get("permission_denied")

    asyncio.run(_run())


def test_executor_allows_track_when_create_denied(db) -> None:
    tenant = seed_tenant(db, name="تتبع مسموح")
    _seed_permissions(db, tenant.id, can_create_orders=False)
    load = load_tenant_commerce_permissions(db, tenant.id)
    ctx = _minimal_ctx(db, tenant.id, permissions=load.permissions, permission_source=load.source)

    denial = deny_reason_for_brain_action(ctx, ACTION_TRACK_ORDER)
    assert denial is None

    denial_search = deny_reason_for_brain_action(ctx, ACTION_SEARCH_PRODUCTS)
    assert denial_search is None


def test_orchestrator_pipeline_loads_db_permissions(db) -> None:
    tenant = seed_tenant(db, name="أوركسترا")
    _seed_permissions(db, tenant.id, can_create_orders=False, can_apply_coupons=True)
    pipeline = AIOrchestrationPipeline(engine=MagicMock())
    request = AIOrchestrationRequest(
        context=AIContext(tenant_id=tenant.id, metadata={"db": db}),
        tools_requested=["create_draft_order", "search_products", "apply_coupon"],
    )
    perms = pipeline.build_permission_snapshot(request)
    allowed, blocked, notes = pipeline.apply_policy_validation(request, perms)

    assert "search_products" in allowed
    assert "create_draft_order" in blocked
    assert "apply_coupon" in allowed
    assert any("can_create_orders" in n for n in notes)


def test_runtime_search_allowed_under_load_failed(db) -> None:
    async def _run() -> None:
        tenant = seed_tenant(db, name="فشل تحميل")
        with patch.object(db, "query", side_effect=RuntimeError("db unavailable")):
            failed = load_tenant_commerce_permissions(db, tenant.id)
        runtime = CommerceToolRuntime(
            db,
            tenant_id=tenant.id,
            permissions=failed.permissions,
            permission_source=failed.source,
        )
        with patch.object(runtime.catalog, "get_top_products", return_value=[{"id": "p1", "title": "حذاء"}]):
            result = await runtime.execute("search_products", {"query": "", "limit": 3})
        assert result.ok is True
        assert failed.source == "load_failed"

    asyncio.run(_run())
