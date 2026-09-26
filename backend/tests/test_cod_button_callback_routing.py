"""Regression coverage for deterministic COD button callback routing."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _path in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.automation_engine import _lifecycle_quick_reply_id, _try_execute  # noqa: E402
from routers.whatsapp_webhook import _send_cod_followup_message  # noqa: E402
from services.cod_confirmation import (  # noqa: E402
    COD_INBOUND_CONSUMED,
    apply_claimed_structured_cod_control,
    consume_owned_cod_button_inbound,
    resolve_owned_cod_button_payload_from_context,
    stamp_initial_cod_automation_send_success,
)


def _run(coro):
    return asyncio.run(coro)


class _Query:
    def __init__(self, db, *, evidence=False):
        self.db = db
        self.evidence = evidence
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
        return self.db.evidence if self.evidence else []

    def first(self):
        if self.evidence:
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
            id=195,
            tenant_id=order.tenant_id,
            automation_id=34,
            event_id=25919,
            customer_id=order.customer_id,
            status="sent",
            action_taken={
                "wa_message_id": context_wamid,
                "to": order.customer_info["phone"],
            },
        )
        self.event = SimpleNamespace(
            id=25919,
            tenant_id=order.tenant_id,
            event_type="order_cod_pending",
            processed=True,
            customer_id=order.customer_id,
            automation_id=34,
            payload={
                "order_id": order.id,
                "order_internal_id": order.id,
                "external_id": order.external_id,
                "payment_method": "cod",
                "message_type": "initial_confirmation",
            },
        )
        self.automation = SimpleNamespace(
            id=34,
            tenant_id=order.tenant_id,
            automation_type="cod_confirmation",
        )
        self.evidence = [(self.execution, self.event, self.automation)]
        self.commits = 0

    def query(self, *entities):
        return _Query(self, evidence=len(entities) == 3)

    def add(self, _row):
        return None

    def flush(self):
        return None

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


def test_claimed_salla_cod_button_updates_store_before_natural_reply(monkeypatch):
    """A real lifecycle prompt is a store instruction even with the pilot on."""
    order = _order()
    order.extra_metadata.update({
        "nahla_cod_confirmation_origin": "external_store",
        "nahla_cod_confirmation_wamid": "wamid.cod.prompt",
    })
    db = _DB(order)
    update = AsyncMock(return_value=True)
    monkeypatch.setattr("store_integration.order_service.update_order_status", update)
    monkeypatch.setattr("observability.event_logger.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("services.cod_confirmation.flag_modified", lambda *args, **kwargs: None)

    result = _run(apply_claimed_structured_cod_control(
        db, tenant_id=1, customer_phone="966555906901", text="تأكيد الطلب",
        button_payload="nahla_cod_confirm:155", context_wamid="wamid.cod.prompt",
    ))

    assert result == ("confirm", order)
    update.assert_awaited_once_with(1, "472240005", "under_review")
    assert order.status == "under_review"
    assert order.extra_metadata["cod_confirmed_at"]


def test_claimed_cod_button_rejects_foreign_context_and_plain_text(monkeypatch):
    order = _order()
    order.extra_metadata["nahla_cod_confirmation_wamid"] = "wamid.cod.prompt"
    db = _DB(order)
    update = AsyncMock(return_value=True)
    monkeypatch.setattr("store_integration.order_service.update_order_status", update)

    for payload, context in (("nahla_cod_confirm:155", "wamid.foreign"), ("", "")):
        assert _run(apply_claimed_structured_cod_control(
            db, tenant_id=1, customer_phone="966555906901", text="تأكيد الطلب",
            button_payload=payload, context_wamid=context,
        )) == (None, None)
    update.assert_not_awaited()
    assert order.status == "in_progress"


def test_claimed_store_cod_button_uses_send_context_when_meta_payload_is_opaque(monkeypatch):
    from models import Order

    order = _order()
    order.extra_metadata.update({
        "nahla_cod_confirmation_origin": "external_store",
        "nahla_cod_confirmation_wamid": "wamid.cod.prompt",
    })
    db = _DB(order)
    original_query = db.query

    def query(*entities):
        result = original_query(*entities)
        if entities == (Order,):
            result.all = lambda: [order]
        return result

    db.query = query
    update = AsyncMock(return_value=True)
    monkeypatch.setattr("store_integration.order_service.update_order_status", update)
    monkeypatch.setattr("observability.event_logger.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("services.cod_confirmation.flag_modified", lambda *args, **kwargs: None)

    assert _run(apply_claimed_structured_cod_control(
        db, tenant_id=1, customer_phone="966555906901", text="تأكيد الطلب",
        button_payload="opaque-meta-template-payload", context_wamid="wamid.cod.prompt",
    )) == ("confirm", order)
    update.assert_awaited_once_with(1, "472240005", "under_review")


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
            context_wamid="wamid.cod.prompt",
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
            context_wamid="wamid.cod.prompt",
            followup_send=final_confirmation,
        )
    )
    assert replay == COD_INBOUND_CONSUMED
    update.assert_awaited_once()
    final_confirmation.assert_awaited_once()


def test_stale_order_uses_only_fully_correlated_sent_cod_evidence(monkeypatch):
    order = _order()
    order.customer_id = None  # production A1 external-order rows are unlinked
    order.extra_metadata = {"payment_method": "waiting", "is_cod": True}
    db = _DB(order)
    db.event.customer_id = 69
    db.execution.customer_id = 69
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

    result = _run(consume_owned_cod_button_inbound(
        db,
        tenant_id=1,
        customer_phone="966555906901",
        text="تأكيد الطلب",
        button_payload="nahla_cod_confirm:155",
        context_wamid="wamid.cod.prompt",
        followup_send=final_confirmation,
    ))

    assert result == COD_INBOUND_CONSUMED
    assert order.status == "under_review"
    update.assert_awaited_once_with(1, "472240005", "under_review")
    final_confirmation.assert_awaited_once_with("confirm", order)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("event", "tenant_id", 2),
        ("event", "customer_id", 70),
        ("event_payload", "external_id", "foreign-order"),
        ("event_payload", "payment_method", "waiting"),
        ("event_payload", "message_type", "reminder"),
        ("execution", "status", "failed"),
        ("execution", "customer_id", 70),
        ("execution_action", "wa_message_id", "wamid.other"),
        ("automation", "automation_type", "order_notifications"),
        ("order_info", "phone", "+966500000999"),
    ],
)
def test_stale_order_rejects_incomplete_or_mismatched_evidence(
    monkeypatch, target, field, value
):
    order = _order()
    order.customer_id = None
    order.extra_metadata = {"payment_method": "waiting", "is_cod": True}
    db = _DB(order)
    db.event.customer_id = 69
    db.execution.customer_id = 69
    containers = {
        "event": db.event,
        "event_payload": db.event.payload,
        "execution": db.execution,
        "execution_action": db.execution.action_taken,
        "automation": db.automation,
        "order_info": order.customer_info,
    }
    container = containers[target]
    if isinstance(container, dict):
        container[field] = value
    else:
        setattr(container, field, value)
    update = AsyncMock(return_value=True)
    final_confirmation = AsyncMock()
    monkeypatch.setattr(
        "store_integration.order_service.update_order_status", update
    )

    result = _run(consume_owned_cod_button_inbound(
        db,
        tenant_id=1,
        customer_phone="966555906901",
        text="تأكيد الطلب",
        button_payload="nahla_cod_confirm:155",
        context_wamid="wamid.cod.prompt",
        followup_send=final_confirmation,
    ))

    assert result == COD_INBOUND_CONSUMED
    assert order.status == "in_progress"
    update.assert_not_awaited()
    final_confirmation.assert_not_awaited()


@pytest.mark.parametrize("context_wamid", ["", "wamid.unrelated"])
def test_stale_order_rejects_missing_event_execution_correlation(
    monkeypatch, context_wamid
):
    order = _order()
    order.customer_id = None
    order.extra_metadata = {"payment_method": "waiting", "is_cod": True}
    db = _DB(order)
    db.event.customer_id = 69
    db.execution.customer_id = 69
    if context_wamid == "wamid.unrelated":
        db.evidence = []
    update = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "store_integration.order_service.update_order_status", update
    )

    result = _run(consume_owned_cod_button_inbound(
        db,
        tenant_id=1,
        customer_phone="966555906901",
        text="تأكيد الطلب",
        button_payload="nahla_cod_confirm:155",
        context_wamid=context_wamid,
    ))

    assert result == COD_INBOUND_CONSUMED
    assert order.status == "in_progress"
    update.assert_not_awaited()


def test_successful_initial_automation_send_stamps_bound_order(monkeypatch):
    order = _order()
    order.extra_metadata = {"payment_method": "waiting", "is_cod": True}
    db = _DB(order)
    monkeypatch.setattr(
        "services.cod_confirmation.flag_modified", lambda *args, **kwargs: None
    )

    stamped = stamp_initial_cod_automation_send_success(
        db,
        tenant_id=1,
        event=db.event,
        automation=db.automation,
        action_info=db.execution.action_taken,
        execution_id=195,
    )

    assert stamped is True
    assert order.extra_metadata["payment_method"] == "cod"
    assert order.extra_metadata["nahla_cod_confirmation_sent"] is True
    assert order.extra_metadata["nahla_cod_confirmation_sent_at"]
    assert order.extra_metadata["nahla_cod_confirmation_wamid"] == "wamid.cod.prompt"
    assert order.extra_metadata["nahla_cod_confirmation_execution_id"] == 195


def test_failed_initial_automation_send_does_not_stamp_bound_order(monkeypatch):
    order = _order()
    order.extra_metadata = {"payment_method": "cod", "is_cod": True}
    db = _DB(order)
    monkeypatch.setattr(
        "services.cod_confirmation.flag_modified", lambda *args, **kwargs: None
    )

    stamped = stamp_initial_cod_automation_send_success(
        db,
        tenant_id=1,
        event=db.event,
        automation=db.automation,
        action_info={"error": "provider_rejected"},
        execution_id=195,
    )

    assert stamped is False
    assert "nahla_cod_confirmation_sent" not in order.extra_metadata


@pytest.mark.parametrize("provider_success", [True, False])
def test_automation_engine_stamps_only_after_provider_success(
    monkeypatch, provider_success
):
    class _EmptyQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

    db = SimpleNamespace(query=lambda *args: _EmptyQuery())
    event = SimpleNamespace(
        id=25919,
        tenant_id=1,
        event_type="order_cod_pending",
        customer_id=None,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        payload={
            "order_id": 155,
            "order_internal_id": 155,
            "external_id": "472240005",
            "payment_method": "cod",
            "message_type": "initial_confirmation",
        },
    )
    automation = SimpleNamespace(
        id=34,
        tenant_id=1,
        automation_type="cod_confirmation",
        config={},
        stats_triggered=0,
        stats_sent=0,
        updated_at=None,
    )
    action_info = (
        {"wa_message_id": "wamid.cod.prompt", "to": "+966555906901"}
        if provider_success
        else {"error": "provider_rejected"}
    )
    stamp = MagicMock(return_value=True)
    monkeypatch.setattr(
        "core.automation_engine._execute_action",
        AsyncMock(return_value=(provider_success, action_info)),
    )
    monkeypatch.setattr("core.automation_engine._write_execution", lambda *a, **k: 195)
    monkeypatch.setattr(
        "services.cod_confirmation.stamp_initial_cod_automation_send_success",
        stamp,
    )

    result = _run(
        _try_execute(
            db,
            1,
            event,
            automation,
            datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )

    assert result == ("sent" if provider_success else "failed")
    assert stamp.call_count == (1 if provider_success else 0)


def test_confirm_followup_uses_canonical_final_order_template_once(monkeypatch):
    canonical_final = AsyncMock(return_value={"sent": True, "duplicate": False})
    monkeypatch.setattr(
        "services.cod_confirmation.send_order_confirmation_after_cod",
        canonical_final,
    )
    order = _order()
    order.status = "under_review"
    order.extra_metadata["cod_confirmed_at"] = "2026-09-14T11:02:26+00:00"

    _run(_send_cod_followup_message(
        phone_id="phone-id",
        to="+966555906901",
        decision="confirm",
        order=order,
        _tenant_id=1,
        _db=SimpleNamespace(),
    ))

    canonical_final.assert_awaited_once()


def test_failed_salla_confirmation_sends_no_final_template(monkeypatch):
    canonical_final = AsyncMock()
    monkeypatch.setattr(
        "services.cod_confirmation.send_order_confirmation_after_cod",
        canonical_final,
    )

    _run(_send_cod_followup_message(
        phone_id="phone-id",
        to="+966555906901",
        decision="confirm_failed",
        order=_order(),
        _tenant_id=1,
        _db=SimpleNamespace(),
    ))

    canonical_final.assert_not_awaited()


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
