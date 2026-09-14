"""Regression coverage for deterministic COD button callback routing."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _path in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.automation_engine import _lifecycle_quick_reply_id  # noqa: E402
from services.cod_confirmation import (  # noqa: E402
    COD_INBOUND_CONSUMED,
    consume_owned_cod_button_inbound,
    resolve_owned_cod_button_payload_from_context,
)


def _run(coro):
    return asyncio.run(coro)


class _Query:
    def __init__(self, db, *, pairs=False):
        self.db = db
        self.pairs = pairs
        self.filters = []

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        self.filters.extend(args)
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self.db.pairs if self.pairs else []

    def first(self):
        if self.pairs:
            return None
        pending_filter = any("orders.status IN" in str(expr) for expr in self.filters)
        if pending_filter and self.db.order.status not in {
            "pending_confirmation",
            "payment_pending",
            "pending_payment",
            "waiting_payment",
            "awaiting_payment",
            "in_progress",
        }:
            return None
        return self.db.order


class _DB:
    def __init__(self, order, *, context_wamid="wamid.cod.prompt"):
        self.order = order
        self.execution = SimpleNamespace(
            action_taken={"wa_message_id": context_wamid},
        )
        self.event = SimpleNamespace(
            customer_id=order.customer_id,
            payload={
                "order_id": order.id,
                "order_internal_id": order.id,
            },
        )
        self.pairs = [(self.execution, self.event)]
        self.commits = 0

    def query(self, *entities):
        return _Query(self, pairs=len(entities) == 2)

    def commit(self):
        self.commits += 1


def _order():
    return SimpleNamespace(
        id=155,
        tenant_id=1,
        customer_id=66,
        external_id="472240005",
        external_order_number="285660706",
        status="in_progress",
        customer_info={"phone": "+966555906901"},
        extra_metadata={
            "payment_method": "cod",
            "nahla_cod_confirmation_sent": True,
        },
        line_items=[],
    )


def _resolve(db, title="تأكيد الطلب", wamid="wamid.cod.prompt"):
    return resolve_owned_cod_button_payload_from_context(
        db,
        tenant_id=1,
        customer_phone="966555906901",
        button_text=title,
        context_wamid=wamid,
    )


def test_automation_sender_builds_order_bound_cod_payloads():
    assert (
        _lifecycle_quick_reply_id("تأكيد الطلب", 0, order_id=155)
        == "nahla_cod_confirm:155"
    )
    assert (
        _lifecycle_quick_reply_id("إلغاء الطلب", 1, order_id=155)
        == "nahla_cod_cancel:155"
    )
    source = (BACKEND_DIR / "core" / "automation_engine.py").read_text(
        encoding="utf-8"
    )
    main_builder = source.index(
        "COD initial-confirmation quick replies must carry a deterministic"
    )
    payload_build = source.index(
        '"sub_type": "quick_reply"', main_builder
    )
    provider_send = source.index("provider_send_message(", payload_build)
    assert main_builder < payload_build < provider_send
    assert '"order_cod_pending"' in source[main_builder:payload_build]
    assert '_payload_for_btn.get("order_internal_id")' in source[
        main_builder:payload_build
    ]


def test_context_wamid_resolves_only_correlated_cod_prompt():
    db = _DB(_order())
    assert _resolve(db) == "nahla_cod_confirm:155"
    assert _resolve(db, title="إلغاء الطلب") == "nahla_cod_cancel:155"
    assert _resolve(db, wamid="wamid.unrelated") is None
    assert _resolve(db, title="عرض المنتجات") is None


def test_manual_text_cannot_impersonate_button_callback():
    db = _DB(_order())
    assert _resolve(db, wamid="") is None

    source = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
        encoding="utf-8"
    )
    rescue = source.index("# ── Button-tap rescue:")
    route = source.index(
        "resolve_owned_cod_button_payload_from_context", rescue
    )
    merchant = source.index("_handle_merchant_message(", route)
    block = source[rescue:merchant]
    assert 'msg_type == "button"' in block
    assert "context_wamid=_context_wamid" in block


@pytest.mark.parametrize(
    ("mutated_field", "mutated_value"),
    [
        ("tenant_id", 2),
        ("customer_id", 99),
    ],
)
def test_context_correlation_rejects_foreign_order_identity(
    mutated_field, mutated_value
):
    order = _order()
    setattr(order, mutated_field, mutated_value)
    db = _DB(order)
    if mutated_field == "tenant_id":
        assert _resolve(db) is None
    else:
        db.event.customer_id = 66
        assert _resolve(db) is None


def test_confirm_is_transactional_and_replay_is_idempotent(monkeypatch):
    order = _order()
    db = _DB(order)
    update = AsyncMock(return_value=True)
    final_confirmation = AsyncMock()

    monkeypatch.setattr(
        "store_integration.order_service.update_order_status", update
    )
    monkeypatch.setattr(
        "observability.event_logger.log_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "services.cod_confirmation.flag_modified", lambda *args, **kwargs: None
    )

    payload = _resolve(db)
    assert payload == "nahla_cod_confirm:155"
    first = _run(
        consume_owned_cod_button_inbound(
            db,
            tenant_id=1,
            customer_phone="966555906901",
            text="تأكيد الطلب",
            button_payload=payload,
            followup_send=final_confirmation,
        )
    )
    assert first == COD_INBOUND_CONSUMED
    assert order.status == "under_review"
    update.assert_awaited_once_with(1, "472240005", "under_review")
    final_confirmation.assert_awaited_once_with("confirm", order)

    replay_payload = _resolve(db)
    assert replay_payload == "nahla_cod_confirm:155"
    replay = _run(
        consume_owned_cod_button_inbound(
            db,
            tenant_id=1,
            customer_phone="966555906901",
            text="تأكيد الطلب",
            button_payload=replay_payload,
            followup_send=final_confirmation,
        )
    )
    assert replay == COD_INBOUND_CONSUMED
    update.assert_awaited_once()
    final_confirmation.assert_awaited_once()


def test_cancel_is_separate_transactional_path(monkeypatch):
    order = _order()
    db = _DB(order)
    update = AsyncMock(return_value=True)
    cancel_followup = AsyncMock()

    monkeypatch.setattr(
        "store_integration.order_service.update_order_status", update
    )
    monkeypatch.setattr(
        "observability.event_logger.log_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "services.cod_confirmation.flag_modified", lambda *args, **kwargs: None
    )

    payload = _resolve(db, title="إلغاء الطلب")
    result = _run(
        consume_owned_cod_button_inbound(
            db,
            tenant_id=1,
            customer_phone="966555906901",
            text="إلغاء الطلب",
            button_payload=payload,
            followup_send=cancel_followup,
        )
    )
    assert result == COD_INBOUND_CONSUMED
    assert order.status == "cancelled"
    update.assert_awaited_once_with(1, "472240005", "cancelled")
    cancel_followup.assert_awaited_once_with("cancel", order)


def test_owned_template_button_returns_before_brain_route():
    source = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
        encoding="utf-8"
    )
    rescue = source.index("# ── Button-tap rescue:")
    correlated = source.index(
        "resolve_owned_cod_button_payload_from_context", rescue
    )
    consume = source.index(
        "await consume_owned_cod_button_inbound", correlated
    )
    owned_return = source.index("\n                        return", consume)
    brain = source.index("await _handle_merchant_message(", owned_return)
    assert rescue < correlated < consume < owned_return < brain
