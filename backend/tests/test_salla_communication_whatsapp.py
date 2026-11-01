"""Synthetic-only checks; no Salla, Meta or production database access."""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from core.salla_communication_whatsapp import (
    dispatch_communication_whatsapp,
    inbox_event_type,
    parse_notice,
    record_meta_delivery_status,
)
from core.salla_webhook_identity import build_salla_webhook_external_event_id
from core.salla_comm_function_auth import signature_message, verify_function_request


STORE = "12345678"  # synthetic store identifier
PHONE = "0555000000"  # synthetic recipient; the provider is always mocked


def test_function_signature_is_independent_path_bound_and_fails_closed(monkeypatch):
    body = json.dumps(payload(), separators=(",", ":")).encode()
    key = "synthetic-function-key-distinct-from-webhook-123456"
    timestamp = "1793200000"
    nonce = "a" * 32
    sig = hmac.new(key.encode(), signature_message(body, timestamp, nonce), hashlib.sha256).hexdigest()
    headers = {
        "x-nahla-comm-timestamp": timestamp,
        "x-nahla-comm-nonce": nonce,
        "x-nahla-comm-signature": sig,
    }
    assert verify_function_request(body, headers, secret=key, now=int(timestamp)) == (True, nonce)
    monkeypatch.setattr("core.config.SALLA_WEBHOOK_SECRET", key)
    assert verify_function_request(body, headers, secret=key, now=int(timestamp))[0] is False
    monkeypatch.setattr("core.config.SALLA_WEBHOOK_SECRET", "different-webhook-test-key")
    assert verify_function_request(body, headers, secret="", now=int(timestamp))[0] is False
    assert verify_function_request(body, headers, secret="synthetic-salla-webhook-key-not-the-function-key", now=int(timestamp))[0] is False
    assert verify_function_request(body + b" ", headers, secret=key, now=int(timestamp))[0] is False
    assert verify_function_request(body, headers, secret=key, now=int(timestamp) + 301)[0] is False
    assert verify_function_request(body, {**headers, "x-nahla-comm-nonce": "b" * 32}, secret=key, now=int(timestamp))[0] is False


def payload(*, content="Order updated", recipient=PHONE, entity_id=42):
    return {
        "event": "communication.whatsapp.send",
        "merchant": STORE,
        "created_at": "2026-10-28T12:00:00Z",
        "data": {
            "type": "order.status.updated",
            "notifiable": [recipient],
            "content": content,
            "entity": {"type": "order", "id": entity_id},
        },
    }


@pytest.mark.asyncio
async def test_function_persists_exact_verified_body_digest_on_first_and_duplicate(monkeypatch):
    """A duplicate identity must keep the first verified body and send result."""
    from routers.webhooks import salla_communication_function
    from services.salla_integration_resolver import ResolvedSallaIntegration

    class FakeDb:
        def add(self, _row):
            pass

        def commit(self):
            pass

        def refresh(self, _row):
            pass

    class FakeRequest:
        headers = {}

        def __init__(self, body):
            self._body = body

        async def body(self):
            return self._body

    verified = []
    persisted = []
    dispatched = []
    original = None

    def verify(body, _headers):
        verified.append(body)
        return True, f"{len(verified):032x}"

    def persist(_db, **kwargs):
        nonlocal original
        persisted.append(kwargs)
        if original is None:
            original = SimpleNamespace(
                raw_body=kwargs["raw_body"].decode("utf-8"),
                raw_headers=kwargs["headers"],
                communication_send_state="accepted",
                communication_provider_message_id="wamid.synthetic",
            )
        return original

    async def dispatch(_db, event, **kwargs):
        dispatched.append((event, kwargs))

    monkeypatch.setattr("core.salla_comm_function_auth.verify_function_request", verify)
    monkeypatch.setattr(
        "services.salla_integration_resolver.resolve_salla_integration_connection",
        lambda *_args, **_kwargs: ResolvedSallaIntegration(
            integration_id=7, tenant_id=1, matched_via="synthetic"
        ),
    )
    monkeypatch.setattr("routers.webhooks.persist_event", persist)
    monkeypatch.setattr(
        "core.salla_communication_whatsapp.dispatch_communication_whatsapp", dispatch
    )

    first = json.dumps(payload(content="\u062d\u0627\u0644\u0629 \u0627\u0644\u0637\u0644\u0628"), ensure_ascii=False).encode("utf-8")
    duplicate = json.dumps(
        payload(content="\u062d\u0627\u0644\u0629 \u0627\u0644\u0637\u0644\u0628"), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert first != duplicate
    for body in (first, duplicate):
        result = await salla_communication_function(FakeRequest(body), FakeDb())
        assert result["state"] == "provider_accepted"
    assert verified == [first, duplicate]
    assert [row["raw_body"] for row in persisted] == [first, duplicate]
    assert [row["headers"]["x-nahla-comm-body-sha256"] for row in persisted] == [
        hashlib.sha256(first).hexdigest(),
        hashlib.sha256(duplicate).hexdigest(),
    ]
    assert original.raw_body == first.decode("utf-8")
    assert original.raw_headers["x-nahla-comm-body-sha256"] == hashlib.sha256(first).hexdigest()
    assert len(dispatched) == 2 and all(item[0] is original for item in dispatched)


def test_identity_deduplicates_exact_retry_but_not_distinct_content():
    def identity(body):
        return build_salla_webhook_external_event_id(
            event_type=body["event"], parsed_payload=body
        )
    assert identity(payload()) == identity(payload())
    assert identity(payload()) != identity(payload(content="Different status"))
    assert identity(payload()) != identity(payload(recipient="0555906902"))
    later = payload()
    later["created_at"] = "2026-10-28T12:01:00Z"
    assert identity(payload()) != identity(later)
    assert identity(payload()) is not None


def test_only_function_handoff_is_a_send_work_item():
    assert inbox_event_type("communication.whatsapp.send", {}) == "communication.whatsapp.observed"
    # A replayed public webhook must never be promoted with an unsigned header.
    assert inbox_event_type(
        "communication.whatsapp.send", {"X-Nahla-Communication-Function": "1"}
    ) == "communication.whatsapp.observed"
    assert inbox_event_type("order.updated", {}) == "order.updated"
    body = payload()
    observed = build_salla_webhook_external_event_id(
        event_type=body["event"], parsed_payload=body, provider_prefix="salla_observed"
    )
    handoff = build_salla_webhook_external_event_id(
        event_type=body["event"], parsed_payload=body, provider_prefix="salla_function"
    )
    assert observed and handoff and observed != handoff


@pytest.mark.asyncio
async def test_observation_returns_before_tenant_resolution():
    from core.webhook_dispatcher import _dispatch_salla

    event = SimpleNamespace(
        provider="salla",
        event_type="communication.whatsapp.observed",
        store_id=None,
        parsed_payload={"event": "communication.whatsapp.send", "data": {}},
    )
    await _dispatch_salla(object(), event)


@pytest.mark.parametrize("change", [
    {"data": {"type": "order.created"}},
    {"data": {"notifiable": [PHONE, "0555906902"]}},
    {"data": {"entity": {"id": 42, "type": "product"}}},
    {"data": {"content": ""}},
])
def test_invalid_or_out_of_scope_payload_fails_closed(change):
    body = payload()
    body["data"].update(change["data"])
    with pytest.raises(ValueError):
        parse_notice(body)


def test_missing_provider_timestamp_cannot_collapse_later_same_content():
    body = payload()
    body.pop("created_at")
    with pytest.raises(ValueError, match="timestamp"):
        parse_notice(body)


class FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *criteria):
        for criterion in criteria:
            key = criterion.left.key
            expected = True if str(criterion.right).lower() == "true" else criterion.right.value
            if getattr(self.row, key, None) != expected:
                self.row = None
                break
        return self

    def with_for_update(self):
        return self

    def filter_by(self, **criteria):
        if any(getattr(self.row, key, None) != value for key, value in criteria.items()):
            self.row = None
        return self

    def one_or_none(self):
        return self.row


class FakeSession:
    def __init__(self, order, connection):
        self.order = order
        self.connection = connection
        self.commits = []
        self.event = None
        self.store_state = {"send_state": None}

    def query(self, model):
        from models import Order, WebhookEvent
        if model is Order:
            return FakeQuery(self.order)
        if model is WebhookEvent:
            return FakeQuery(self.event)
        return FakeQuery(self.connection)

    def add(self, obj):
        self.last_added = obj

    def commit(self):
        self.commits.append(self.last_added.communication_send_state)
        self.store_state["send_state"] = self.last_added.communication_send_state

    def rollback(self):
        pass

    def refresh(self, obj):
        pass

    def execute(self, statement):
        assert statement.table.name == "webhook_events"
        assert self.event is not None
        matched = True
        for criterion in statement._where_criteria:
            key = criterion.left.key
            if key == "id":
                matched &= self.event.id == criterion.right.value
            elif key == "communication_send_state":
                matched &= self.store_state["send_state"] is None
            else:
                assert key in {"tenant_id", "provider", "event_type", "store_id", "signature_valid"}
                expected = True if str(criterion.right).lower() == "true" else criterion.right.value
                matched &= getattr(self.event, key, None) == expected
        if matched:
            self.event.communication_send_state = "sending"
            self.last_added = self.event
        return SimpleNamespace(scalar_one_or_none=lambda: self.event.id if matched else None)


def setup_send(monkeypatch):
    monkeypatch.setenv("SALLA_COMM_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("SALLA_COMM_WHATSAPP_TENANTS", "1")
    monkeypatch.setenv("SALLA_COMM_WHATSAPP_STORES", STORE)
    monkeypatch.setenv("SALLA_COMM_WHATSAPP_RECIPIENTS", PHONE)
    monkeypatch.setattr(
        "core.commerce_lifecycle.canary_guard.commerce_lifecycle_dispatch_tenant_permitted",
        lambda tenant_id: False,
    )
    monkeypatch.setattr(
        "core.commerce_lifecycle.window.lifecycle_service_window_is_open",
        lambda *args: (True, "synthetic"),
    )
    order = SimpleNamespace(
        tenant_id=1, source="salla", integration_connection_id=7,
        external_id="42", customer_info={"mobile": PHONE}
    )
    connection = SimpleNamespace(
        tenant_id=1, status="connected", sending_enabled=True,
        provider="meta", phone_number_id="test-phone-id"
    )
    raw_body = json.dumps(payload(), ensure_ascii=False, separators=(",", ":"))
    event = SimpleNamespace(
        id=1, tenant_id=1, provider="salla_function", event_type="communication.whatsapp.send",
        signature_valid=True, external_event_id="stable-event-id",
        store_id=STORE, parsed_payload=payload(), raw_body=raw_body,
        raw_headers={"x-nahla-comm-body-sha256": hashlib.sha256(raw_body.encode()).hexdigest()},
        communication_send_state=None,
        communication_provider_message_id=None, communication_send_error_code=None,
        communication_send_attempted_at=None,
    )
    db = FakeSession(order, connection)
    db.event = event
    return db, event


@pytest.mark.asyncio
async def test_engine_boundary_rejects_unsigned_or_rebound_send(monkeypatch):
    from core.automation_engine import send_reserved_salla_communication

    db, event = setup_send(monkeypatch)
    for change, reason in (
        ({"signature_valid": False}, "unverified_communication_source"),
        ({"tenant_id": 2}, "communication_tenant_mismatch"),
        ({"store_id": "other"}, "communication_store_mismatch"),
    ):
        original = {key: getattr(event, key) for key in change}
        for key, value in change.items():
            setattr(event, key, value)
        with pytest.raises(RuntimeError, match=reason):
            await send_reserved_salla_communication(
                db, event, tenant_id=1, store_id=STORE,
                integration_connection_id=7,
            )
        for key, value in original.items():
            setattr(event, key, value)

    altered = json.loads(event.raw_body)
    altered["data"]["content"] = "Altered after verification"
    event.raw_body = json.dumps(altered)
    with pytest.raises(RuntimeError, match="communication_signed_body_mismatch"):
        await send_reserved_salla_communication(
            db, event, tenant_id=1, store_id=STORE,
            integration_connection_id=7,
        )
    assert db.commits == []


@pytest.mark.asyncio
async def test_engine_boundary_requires_persisted_verified_body_digest(monkeypatch):
    from core.automation_engine import send_reserved_salla_communication

    db, event = setup_send(monkeypatch)
    event.raw_headers = {}
    with pytest.raises(RuntimeError, match="communication_signed_body_mismatch"):
        await send_reserved_salla_communication(
            db, event, tenant_id=1, store_id=STORE,
            integration_connection_id=7,
        )
    assert db.commits == []


def test_meta_receipt_authority_binds_exact_signed_status_and_phone():
    from services.meta_receipt_context import permits_receipt, verify_receipt_context

    secret = "synthetic-meta-key"
    status = {"id": "wamid.synthetic", "status": "delivered", "recipient_id": PHONE}
    body = json.dumps({"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "phone-one"}, "statuses": [status],
    }}]}]}, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_receipt_context(body, "sha256=" + "0" * 64, secret=secret) is None
    context = verify_receipt_context(body, signature, secret=secret)
    assert permits_receipt(context, status, "phone-one")
    assert not permits_receipt(context, status, "phone-two")
    assert not permits_receipt(context, {**status, "status": "read"}, "phone-one")
    assert not permits_receipt(None, status, "phone-one")


def test_meta_receipt_is_the_only_delivery_evidence(monkeypatch):
    from services.meta_receipt_context import verify_receipt_context
    from services.salla_communication_receipt_owner import CommunicationReceiptConflict

    secret = "synthetic-receipt-key"
    monkeypatch.setattr(
        "services.salla_communication_receipt_owner.competing_receipt_owner",
        lambda *_args: False,
    )

    def signed_status(state, *, message_id="wamid.synthetic", phone_id="test-phone-id"):
        status = {"id": message_id, "status": state, "recipient_id": PHONE}
        raw = json.dumps({
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": phone_id}, "statuses": [status],
            }}]}],
        }, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        context = verify_receipt_context(raw, signature, secret=secret)
        assert context is not None
        return status, context

    def observe(db, state, *, message_id="wamid.synthetic", phone_id="test-phone-id",
                receipt_context=None):
        signed, context = signed_status(state, message_id=message_id, phone_id=phone_id)
        return record_meta_delivery_status(
            db, message_id=message_id, status=state, phone_number_id=phone_id,
            receipt_payload=signed,
            receipt_context=context if receipt_context is None else receipt_context,
        )

    db, event = setup_send(monkeypatch)
    event.communication_send_state = "accepted"
    event.communication_provider_message_id = "wamid.synthetic"
    assert observe(db, "delivered", message_id="other") == (False, False)
    assert event.communication_send_state == "accepted"
    signed, _ = signed_status("delivered")
    assert record_meta_delivery_status(
        db, message_id="wamid.synthetic", status="delivered",
        phone_number_id="test-phone-id", receipt_payload=signed,
        receipt_context=None,
    ) == (False, False)
    assert observe(db, "delivered") == (True, True)
    assert event.communication_send_state == "delivered"
    assert observe(db, "delivered") == (True, False)
    assert observe(db, "read") == (True, True)
    assert observe(db, "failed") == (True, False)
    assert event.communication_send_state == "read"

    db, event = setup_send(monkeypatch)
    event.communication_send_state = "accepted"
    event.communication_provider_message_id = "wamid.synthetic"
    assert observe(db, "failed") == (True, True)
    assert event.communication_send_state == "delivery_failed"

    db, event = setup_send(monkeypatch)
    event.communication_send_state = "accepted"
    event.communication_provider_message_id = "wamid.synthetic"
    db.connection.tenant_id = 2
    with pytest.raises(CommunicationReceiptConflict, match="tenant_mismatch"):
        observe(db, "delivered")
    assert event.communication_send_state == "accepted"

    db, event = setup_send(monkeypatch)
    event.communication_send_state = "accepted"
    event.communication_provider_message_id = "wamid.synthetic"
    monkeypatch.setattr(
        "services.salla_communication_receipt_owner.competing_receipt_owner",
        lambda *_args: True,
    )
    with pytest.raises(CommunicationReceiptConflict, match="owner_ambiguous"):
        observe(db, "delivered")
    assert event.communication_send_state == "accepted"


@pytest.mark.asyncio
async def test_one_send_then_completion_retry_cannot_send_again(monkeypatch):
    db, event = setup_send(monkeypatch)
    sent = []

    async def fake_provider(*args, **kwargs):
        sent.append(kwargs["payload"])
        return {"messages": [{"id": "wamid.synthetic"}]}, object()

    monkeypatch.setattr("services.whatsapp_platform.service.provider_send_message", fake_provider)
    await dispatch_communication_whatsapp(
        db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
    )
    await dispatch_communication_whatsapp(
        db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
    )
    assert len(sent) == 1
    assert sent[0]["text"]["body"] == payload()["data"]["content"]
    assert db.commits == ["sending", "accepted"]


@pytest.mark.asyncio
async def test_oversize_provider_id_is_ambiguous_and_never_automatically_resent(monkeypatch):
    db, event = setup_send(monkeypatch)
    sends = []

    async def fake_provider(*args, **kwargs):
        sends.append(kwargs["payload"])
        return {"messages": [{"id": "x" * 129}]}, object()

    monkeypatch.setattr("services.whatsapp_platform.service.provider_send_message", fake_provider)
    with pytest.raises(RuntimeError, match="provider_message_id_too_long"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7,
        )
    assert event.communication_send_state == "ambiguous"
    assert event.communication_provider_message_id is None
    with pytest.raises(RuntimeError, match="requires_reconciliation"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7,
        )
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_unknown_result_never_retries_provider(monkeypatch):
    db, event = setup_send(monkeypatch)
    sent = []

    async def fake_provider(*args, **kwargs):
        sent.append(1)
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr("services.whatsapp_platform.service.provider_send_message", fake_provider)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="communication_"):
            await dispatch_communication_whatsapp(
                db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
            )
    assert len(sent) == 1
    assert db.commits == ["sending", "ambiguous"]


@pytest.mark.asyncio
async def test_wrong_store_or_recipient_fails_before_send(monkeypatch):
    db, event = setup_send(monkeypatch)
    sent = []

    async def fake_provider(*args, **kwargs):
        sent.append(1)
        return {}, object()

    monkeypatch.setattr("services.whatsapp_platform.service.provider_send_message", fake_provider)
    with pytest.raises(RuntimeError, match="store_mismatch"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id="other", integration_connection_id=7
        )
    event.parsed_payload = payload(recipient="0555906902")
    event.raw_body = json.dumps(event.parsed_payload, separators=(",", ":"))
    event.raw_headers["x-salla-signature"] = hmac.new(
        b"synthetic-test-secret", event.raw_body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    event.raw_headers["x-nahla-comm-body-sha256"] = hashlib.sha256(
        event.raw_body.encode("utf-8")
    ).hexdigest()
    with pytest.raises(RuntimeError, match="pilot_not_enabled"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    assert not sent and not db.commits


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_field,value", [
    ("tenant_id", 2),
    ("integration_connection_id", 8),
    ("external_id", "43"),
    ("source", "shopify"),
])
async def test_order_binding_filters_before_send(monkeypatch, broken_field, value):
    db, event = setup_send(monkeypatch)
    setattr(db.order, broken_field, value)
    called = []

    async def provider(*args, **kwargs):
        called.append(1)
        return {"messages": [{"id": "unexpected"}]}, object()

    monkeypatch.setattr("services.whatsapp_platform.service.provider_send_message", provider)
    with pytest.raises(RuntimeError, match="communication_order_not_verified"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    assert not called and not db.commits


@pytest.mark.asyncio
async def test_live_reservation_is_not_replaced_by_a_second_worker(monkeypatch):
    db, event = setup_send(monkeypatch)
    sent = []

    async def provider(*args, **kwargs):
        sent.append(1)
        # A second session observes the same row while the first provider
        # request is outstanding. It cannot reserve or send it again.
        second = FakeSession(db.order, db.connection)
        second.store_state = db.store_state
        second.event = SimpleNamespace(**vars(event))
        second.event.communication_send_state = None  # stale ORM snapshot
        with pytest.raises(RuntimeError, match="requires_reconciliation|already_reserved"):
            await dispatch_communication_whatsapp(
                second, second.event, tenant_id=1, store_id=STORE, integration_connection_id=7
            )
        return {"messages": [{"id": "wamid.synthetic"}]}, object()

    monkeypatch.setattr("services.whatsapp_platform.service.provider_send_message", provider)
    await dispatch_communication_whatsapp(
        db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
    )
    assert sent == [1]
    assert db.commits == ["sending", "accepted"]


@pytest.mark.asyncio
async def test_monitor_mode_accepted_signature_is_not_send_authority(monkeypatch):
    db, event = setup_send(monkeypatch)
    event.provider = "salla"  # ordinary webhook, even with a forged marker
    event.raw_headers = {"x-nahla-communication-function": "1"}
    with pytest.raises(RuntimeError, match="unverified_communication_source"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    assert not db.commits

    db, event = setup_send(monkeypatch)
    event.signature_valid = False
    with pytest.raises(RuntimeError, match="unverified_communication_source"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    assert not db.commits

    db, event = setup_send(monkeypatch)
    event.event_type = "communication.whatsapp.observed"
    with pytest.raises(RuntimeError, match="unverified_communication_source"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    assert not db.commits


@pytest.mark.asyncio
async def test_closed_window_and_lifecycle_conflict_block_before_attempt(monkeypatch):
    db, event = setup_send(monkeypatch)
    monkeypatch.setattr(
        "core.commerce_lifecycle.canary_guard.commerce_lifecycle_dispatch_tenant_permitted",
        lambda tenant_id: True,
    )
    with pytest.raises(RuntimeError, match="lifecycle_sender_conflict"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    monkeypatch.setattr(
        "core.commerce_lifecycle.canary_guard.commerce_lifecycle_dispatch_tenant_permitted",
        lambda tenant_id: False,
    )
    monkeypatch.setattr(
        "core.commerce_lifecycle.window.lifecycle_service_window_is_open",
        lambda *args: (False, "synthetic"),
    )
    with pytest.raises(RuntimeError, match="approved_template_required"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    assert not db.commits


@pytest.mark.asyncio
async def test_reclaimed_sending_marker_and_provider_rejection_do_not_resend(monkeypatch):
    db, event = setup_send(monkeypatch)
    event.communication_send_state = "sending"
    with pytest.raises(RuntimeError, match="requires_reconciliation"):
        await dispatch_communication_whatsapp(
            db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
        )
    assert not db.commits
    event.communication_send_state = None
    sent = []

    async def rejected(*args, **kwargs):
        sent.append(1)
        return {"error": {"code": "401"}}, object()

    monkeypatch.setattr("services.whatsapp_platform.service.provider_send_message", rejected)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="communication_"):
            await dispatch_communication_whatsapp(
                db, event, tenant_id=1, store_id=STORE, integration_connection_id=7
            )
    assert sent == [1]
    assert db.commits == ["sending", "failed"]
    assert event.communication_send_error_code == "401"
