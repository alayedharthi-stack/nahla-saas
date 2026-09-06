"""PR #955 Sol final blockers: COD button consume-always, confirmation-OFF checkout."""
from __future__ import annotations

import ast
import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR, REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.order_updates import (  # noqa: E402
    LEGACY_DEFAULT_ON_KEYS,
    REASON_SETTINGS_UNAVAILABLE,
    set_order_update_flags,
)
from models import TenantSettings  # noqa: E402
from services.cod_confirmation import (  # noqa: E402
    COD_CHECKOUT_PUSH_IMMEDIATE,
    COD_CHECKOUT_SETTINGS_UNAVAILABLE,
    COD_CHECKOUT_WAIT_FOR_CUSTOMER,
    COD_INBOUND_CONSUMED,
    COD_INBOUND_PASSTHROUGH,
    STATUS_PENDING_CUSTOMER,
    STATUS_PENDING_MERCHANT,
    CodCheckoutSettingsUnavailable,
    CodOrderingDisabled,
    intercept_cod_button_inbound,
    is_owned_cod_button_payload,
    nahla_owns_cod_customer_confirmation,
    plan_cod_checkout,
    require_cod_checkout_plan,
)


def _make_db(*models) -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    use = models or (TenantSettings,)
    for model in use:
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


def _run(coro):
    return asyncio.run(coro)


def _pending_query(orders: List[Any]) -> MagicMock:
    class _Query:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def all(self):
            return list(orders)

        def first(self):
            return orders[0] if orders else None

    db = MagicMock()
    db.query = lambda *_a, **_k: _Query()
    db.commit = lambda: None
    return db


def _pending_order(**kwargs):
    defaults = dict(
        id=77,
        tenant_id=9,
        status="pending_confirmation",
        customer_info={"phone": "+966500111222"},
        extra_metadata={"payment_method": "cod"},
        line_items=[],
        external_id=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


async def _route_inbound(
    db,
    *,
    text: str,
    button_payload: Optional[str],
    brain: AsyncMock,
    followup: AsyncMock,
) -> str:
    disposition, decision, order = await intercept_cod_button_inbound(
        db,
        tenant_id=9,
        customer_phone="+966500111222",
        text=text,
        button_payload=button_payload,
    )
    if disposition == COD_INBOUND_CONSUMED:
        if order is not None:
            await followup(decision, order)
        return "cod"
    await brain()
    return "brain"


class TestRecognizedCodButtonAlwaysConsumed:
    def test_interactive_confirm_valid_pending_action_brain_zero(self):
        order = _pending_order()
        db = _pending_query([order])
        brain = AsyncMock()
        followup = AsyncMock()
        push = AsyncMock(return_value="salla-77")
        with patch("services.cod_confirmation._push_cod_to_store", push), patch(
            "observability.event_logger.log_event", lambda *a, **k: None
        ), patch("services.cod_confirmation.flag_modified", lambda *a, **k: None):
            route = _run(
                _route_inbound(
                    db,
                    text="تأكيد الطلب ✅",
                    button_payload="nahla_cod_confirm:77",
                    brain=brain,
                    followup=followup,
                )
            )
        assert route == "cod"
        assert order.status == "under_review"
        push.assert_awaited_once()
        followup.assert_awaited_once()
        brain.assert_not_awaited()

    def test_interactive_confirm_no_pending_brain_zero(self):
        db = _pending_query([])
        brain = AsyncMock()
        followup = AsyncMock()
        route = _run(
            _route_inbound(
                db,
                text="تأكيد الطلب ✅",
                button_payload="nahla_cod_confirm",
                brain=brain,
                followup=followup,
            )
        )
        assert route == "cod"
        followup.assert_not_awaited()
        brain.assert_not_awaited()

    def test_interactive_cancel_no_pending_brain_zero(self):
        db = _pending_query([])
        brain = AsyncMock()
        followup = AsyncMock()
        route = _run(
            _route_inbound(
                db,
                text="إلغاء الطلب ❌",
                button_payload="nahla_cod_cancel",
                brain=brain,
                followup=followup,
            )
        )
        assert route == "cod"
        followup.assert_not_awaited()
        brain.assert_not_awaited()

    def test_duplicate_tap_brain_zero(self):
        order = _pending_order(
            status="under_review",
            extra_metadata={"cod_confirmed_at": "2026-09-06T08:00:00Z"},
        )
        db = _pending_query([])
        brain = AsyncMock()
        followup = AsyncMock()
        push = AsyncMock(return_value="again")
        with patch("services.cod_confirmation._push_cod_to_store", push):
            route = _run(
                _route_inbound(
                    db,
                    text="تأكيد الطلب ✅",
                    button_payload="nahla_cod_confirm:77",
                    brain=brain,
                    followup=followup,
                )
            )
        assert route == "cod"
        push.assert_not_awaited()
        followup.assert_not_awaited()
        brain.assert_not_awaited()
        assert order.status == "under_review"

    def test_foreign_order_id_brain_zero(self):
        order = _pending_order()
        db = _pending_query([order])

        class _Query:
            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def all(self):
                return [order]

            def first(self):
                return None

        db.query = lambda *_a, **_k: _Query()
        brain = AsyncMock()
        followup = AsyncMock()
        route = _run(
            _route_inbound(
                db,
                text="تأكيد الطلب ✅",
                button_payload="nahla_cod_confirm:999",
                brain=brain,
                followup=followup,
            )
        )
        assert route == "cod"
        assert order.status == "pending_confirmation"
        followup.assert_not_awaited()
        brain.assert_not_awaited()

    def test_template_button_confirm_no_pending_brain_zero(self):
        db = _pending_query([])
        brain = AsyncMock()
        followup = AsyncMock()
        route = _run(
            _route_inbound(
                db,
                text="تأكيد الطلب ✅",
                button_payload="nahla_cod_confirm:77",
                brain=brain,
                followup=followup,
            )
        )
        assert route == "cod"
        followup.assert_not_awaited()
        brain.assert_not_awaited()

    def test_unrelated_button_uses_merchant_routing(self):
        order = _pending_order()
        db = _pending_query([order])
        brain = AsyncMock()
        followup = AsyncMock()
        route = _run(
            _route_inbound(
                db,
                text="تأكيد الطلب ✅",
                button_payload="pick_1",
                brain=brain,
                followup=followup,
            )
        )
        assert route == "brain"
        assert order.status == "pending_confirmation"
        followup.assert_not_awaited()
        brain.assert_awaited_once()

    def test_title_does_not_steal_when_payload_is_foreign(self):
        assert is_owned_cod_button_payload("pick_1") is False
        assert is_owned_cod_button_payload("nahla_cod_confirm") is True
        assert is_owned_cod_button_payload("nahla_cod_cancel:12") is True
        disposition, decision, order = _run(
            intercept_cod_button_inbound(
                MagicMock(),
                tenant_id=9,
                customer_phone="+966500111222",
                text="تأكيد الطلب ✅",
                button_payload="merchant_custom_yes",
            )
        )
        assert disposition == COD_INBOUND_PASSTHROUGH
        assert decision is None
        assert order is None

    def test_webhook_returns_on_consumed_without_order(self):
        src = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
            encoding="utf-8"
        )
        interactive = src.index('normalized_type == "interactive"')
        generic = src.index("button_reply (generic)")
        block = src[interactive:generic]
        assert "intercept_cod_button_inbound" in block
        assert "if disposition == COD_INBOUND_CONSUMED" in block
        consumed_at = block.index("if disposition == COD_INBOUND_CONSUMED")
        return_at = block.index("return", consumed_at)
        order_guard = block.find("if order is not None", consumed_at, return_at)
        assert order_guard > 0
        assert block[return_at:return_at + 6] == "return"
        rescue = src.index('msg_type == "button"')
        merchant = src.index("_handle_merchant_message", rescue)
        rescue_block = src[rescue:merchant]
        assert "if disposition == COD_INBOUND_CONSUMED" in rescue_block
        assert "classify_cod_reply(_wa_text)" not in rescue_block


class TestCodDisabledDoesNotStrand:
    def test_confirmation_on_waits_for_customer(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(db, 9, {"cod_confirmation": True}, commit=True)
        plan = require_cod_checkout_plan(db, 9, ordering_allowed=True)
        assert plan.policy == COD_CHECKOUT_WAIT_FOR_CUSTOMER
        assert plan.local_status == STATUS_PENDING_CUSTOMER
        assert plan.push_to_store_now is False
        assert plan.send_confirmation is True

    def test_confirmation_off_pushes_immediately_no_message(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(db, 9, {"cod_confirmation": False}, commit=True)
        plan = require_cod_checkout_plan(db, 9, ordering_allowed=True)
        assert plan.policy == COD_CHECKOUT_PUSH_IMMEDIATE
        assert plan.local_status == STATUS_PENDING_MERCHANT
        assert plan.local_status != STATUS_PENDING_CUSTOMER
        assert plan.push_to_store_now is True
        assert plan.send_confirmation is False

    def test_confirmation_off_default_is_not_pending(self):
        db, _ = _make_db(TenantSettings)
        plan = plan_cod_checkout(db, 9)
        assert "cod_confirmation" not in LEGACY_DEFAULT_ON_KEYS
        assert plan.policy == COD_CHECKOUT_PUSH_IMMEDIATE
        assert plan.local_status != STATUS_PENDING_CUSTOMER

    def test_settings_unavailable_is_not_disabled_or_send(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        plan = plan_cod_checkout(db, 9)
        assert plan.policy == COD_CHECKOUT_SETTINGS_UNAVAILABLE
        assert plan.reason == REASON_SETTINGS_UNAVAILABLE
        assert plan.push_to_store_now is False
        assert plan.send_confirmation is False
        assert plan.local_status is None
        with pytest.raises(CodCheckoutSettingsUnavailable):
            require_cod_checkout_plan(db, 9, ordering_allowed=True)

    def test_merchant_cod_ordering_disabled_still_rejects(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(db, 9, {"cod_confirmation": True}, commit=True)
        with pytest.raises(CodOrderingDisabled):
            require_cod_checkout_plan(db, 9, ordering_allowed=False)

    def test_bypass_metadata_owns_confirmation_without_second_send(self):
        order = SimpleNamespace(
            extra_metadata={
                "cod_confirmation_bypassed": True,
                "cod_pushed_external_id": "salla-1",
            }
        )
        assert nahla_owns_cod_customer_confirmation(order) is True

    def test_ai_sales_create_order_uses_plan_before_order_row(self):
        src = (BACKEND_DIR / "routers" / "ai_sales.py").read_text(encoding="utf-8")
        fn = src.split("async def ai_sales_create_order", 1)[1].split(
            "async def get_ai_sales_logs", 1
        )[0]
        assert fn.index("require_cod_checkout_plan") < fn.index("order = Order(")
        assert fn.index("CodCheckoutSettingsUnavailable") < fn.index("order = Order(")
        assert "status_code=503" in fn
        assert "REASON_SETTINGS_UNAVAILABLE" in fn
        assert "cod_plan.push_to_store_now" in fn
        assert "cod_plan.send_confirmation" in fn
        assert fn.index("send_cod_confirmation_template") > fn.index("order = Order(")

    def test_create_order_does_not_hardcode_pending_for_cod(self):
        src = (BACKEND_DIR / "routers" / "ai_sales.py").read_text(encoding="utf-8")
        fn = src.split("async def ai_sales_create_order", 1)[1].split("async def get_ai_sales_logs", 1)[0]
        assert "order_status = \"pending_confirmation\"" not in fn
        assert "order_status = cod_plan.local_status" in fn
        assert "store_create_order" in fn
        assert "cod_plan.push_to_store_now" in fn

    def test_zero_ai_in_checkout_policy(self):
        forbidden = ("openai", "anthropic", "generate_cart_recovery_text")
        src = inspect.getsource(plan_cod_checkout) + inspect.getsource(intercept_cod_button_inbound)
        for token in forbidden:
            assert token not in src
        tree = ast.parse(inspect.getsource(plan_cod_checkout))
        assert tree is not None
