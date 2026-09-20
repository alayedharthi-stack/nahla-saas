"""What a 200 promises, at both webhook entry points.

Both routes acknowledge first and process in the background. That is right for
the legacy path — the provider gets its answer inside its timeout and
deduplication makes a retry safe — and wrong for a pilot-scoped message, because
the 200 ends the provider's retries before anything durable exists. A worker
that dies in that window takes the message with it, and nobody will send it
again.

These cases drive the real FastAPI routes through a real (SQLite) database and
hold the boundary to four things:

* a pilot-scoped inbound is on disk **before** the 200;
* a batch that could not be recorded is answered as retryable, and **nothing**
  in it is spawned, so the redelivery finds nothing already half-processed;
* unrelated messages in the same batch are neither recorded nor lost;
* nothing at all happens while the pilot is off, which is its default.

The background processing itself is not run here — that is what the routing and
recovery suites drive. What is proved here is the acknowledgement contract.
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import JSON, create_engine, event  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.commerce_runtime import handover  # noqa: E402
from core.commerce_runtime import pilot_guard as pg  # noqa: E402
from core.commerce_runtime.handover_models import create_handover_tables  # noqa: E402
from core.commerce_runtime.models import RuntimeBase  # noqa: E402
from database.models import Base, Tenant, WhatsAppConnection  # noqa: E402
from services import commerce_runtime_acceptance as acceptance  # noqa: E402

PHONE_ID = "PID_ACCEPT"
OTHER_PHONE_ID = "PID_OTHER"
SENDER = "966500000123"
NORMALIZED = "+966500000123"
STRANGER = "966500009999"
MODEL = "model-configured-for-this-pilot"


@event.listens_for(Base.metadata, "before_create")
@event.listens_for(RuntimeBase.metadata, "before_create")
def _remap_jsonb(target: Any, connection: Any, **kw: Any) -> None:
    """SQLite has no JSONB and cannot parse a ``::jsonb`` cast in a default."""
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
            default = getattr(col.server_default, "arg", None)
            if default is not None and "::" in str(default):
                col.server_default = None


@pytest.fixture()
def db() -> Any:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    create_handover_tables(engine)
    session = sessionmaker(bind=engine)()
    pilot = Tenant(name="متجر تجريبي عام", is_active=True)
    other = Tenant(name="متجر عطور", is_active=True)
    session.add_all([pilot, other])
    session.flush()
    session.add_all([
        WhatsAppConnection(tenant_id=pilot.id, phone_number_id=PHONE_ID,
                           phone_number="+966500000000",
                           whatsapp_business_account_id="WABA_A", status="connected"),
        WhatsAppConnection(tenant_id=other.id, phone_number_id=OTHER_PHONE_ID,
                           phone_number="+966500000001",
                           whatsapp_business_account_id="WABA_B", status="connected"),
    ])
    session.commit()
    session.tenant_id = pilot.id                          # type: ignore[attr-defined]
    session.other_tenant_id = other.id                    # type: ignore[attr-defined]
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(db.tenant_id))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, NORMALIZED)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)


def message(identity: str, *, sender: str = SENDER, text: str = "وين طلبي؟") -> Dict[str, Any]:
    return {"id": identity, "from": sender, "type": "text", "text": {"body": text}}


def body(*messages: Dict[str, Any], phone_number_id: str = PHONE_ID) -> Dict[str, Any]:
    return {"entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": list(messages),
    }}]}]}


def mixed_body(*groups: Any) -> Dict[str, Any]:
    """One request carrying several connections' messages, as a batch really can."""
    changes = [{"field": "messages", "value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": list(messages),
    }} for phone_number_id, messages in groups]
    return {"entry": [{"id": "WABA", "changes": changes}]}


# ── The recording itself ─────────────────────────────────────────────────────


def sessions(db: Any) -> Any:
    return lambda: db


def test_nothing_happens_while_the_pilot_is_off(db, monkeypatch):
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    outcome = acceptance.record_before_acknowledging(body(message("wamid.off")),
                                                     session_factory=sessions(db))
    assert outcome.ok and outcome.reason == "pilot_disabled"
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_a_pilot_scoped_inbound_is_durable_with_what_a_replay_needs(configured, db):
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.scoped", text="عندكم قميص؟")), session_factory=sessions(db))
    assert outcome.ok and outcome.recorded == ("wamid.scoped",)
    record = handover.pending_inbound(db, tenant_id=db.tenant_id)[0]
    assert record.provider_message_id == "wamid.scoped"
    assert record.recipient == NORMALIZED
    assert record.phone_number_id == PHONE_ID
    assert record.channel_connection_ref == f"wa:{PHONE_ID}"
    assert record.payload == {"text": "عندكم قميص؟", "type": "text"}
    assert record.reason == handover.REASON_ACCEPTED


def test_an_unrelated_recipient_is_neither_recorded_nor_refused(configured, db):
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.stranger", sender=STRANGER)), session_factory=sessions(db))
    assert outcome.ok and outcome.recorded == () and outcome.scoped == 0
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_another_tenant_s_connection_is_left_alone(configured, db):
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.other"), phone_number_id=OTHER_PHONE_ID),
        session_factory=sessions(db))
    assert outcome.ok and outcome.recorded == ()
    assert handover.pending_count(db, tenant_id=db.other_tenant_id) == 0


def test_a_mixed_batch_records_only_the_scoped_messages(configured, db):
    outcome = acceptance.record_before_acknowledging(
        mixed_body((PHONE_ID, [message("wamid.mine"), message("wamid.stranger",
                                                              sender=STRANGER)]),
                   (OTHER_PHONE_ID, [message("wamid.theirs")])),
        session_factory=sessions(db))
    assert outcome.ok and outcome.recorded == ("wamid.mine",) and outcome.scoped == 1
    assert [r.provider_message_id for r in handover.pending_inbound(db, tenant_id=db.tenant_id)] \
        == ["wamid.mine"]


def test_a_statuses_only_body_records_nothing(configured, db):
    payload = {"entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PHONE_ID},
        "statuses": [{"id": "wamid.status", "status": "delivered"}],
    }}]}]}
    outcome = acceptance.record_before_acknowledging(payload, session_factory=sessions(db))
    assert outcome.ok and outcome.reason == "no_messages"


def test_a_provider_retry_records_the_same_inbound_once(configured, db):
    first = acceptance.record_before_acknowledging(body(message("wamid.retry")),
                                                   session_factory=sessions(db))
    second = acceptance.record_before_acknowledging(body(message("wamid.retry")),
                                                    session_factory=sessions(db))
    assert first.ok and second.ok
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1


def test_a_record_that_cannot_be_written_is_not_an_acceptance(configured, db, monkeypatch):
    monkeypatch.setattr(handover, "record_inbound", lambda *a, **k: None)
    outcome = acceptance.record_before_acknowledging(body(message("wamid.lost")),
                                                     session_factory=sessions(db))
    assert outcome.ok is False and outcome.failed == ("wamid.lost",)
    assert outcome.reason == "not_persisted"


def test_a_database_that_raises_is_not_an_acceptance(configured, db, monkeypatch):
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the database is unavailable")

    monkeypatch.setattr(handover, "record_inbound", _boom)
    outcome = acceptance.record_before_acknowledging(body(message("wamid.down")),
                                                     session_factory=sessions(db))
    assert outcome.ok is False and outcome.reason.startswith("error:")


# ── The routes, driven for real ──────────────────────────────────────────────


def call_meta(body_payload: Dict[str, Any], spawned: List[Any]) -> Any:
    """The real Meta route, with only the background spawn observed."""
    import asyncio

    import routers.whatsapp_webhook as webhook
    from fastapi import Request

    async def _receive() -> Dict[str, Any]:
        import json
        return {"type": "http.request", "body": json.dumps(body_payload).encode()}

    request = Request({"type": "http", "method": "POST", "path": "/webhook/whatsapp",
                       "headers": [], "query_string": b""}, receive=_receive)

    def _spawn(coro: Any, name: str = "") -> None:
        spawned.append(name)
        coro.close()

    with (
        patch("core.runtime_perf.spawn_background", _spawn),
        patch.object(webhook, "_record_signature_audit", lambda *a, **k: None),
        patch.object(webhook, "_meta_should_reject", lambda _r: False),
        patch.object(webhook, "evaluate_replay", lambda *a, **k: False),
    ):
        return asyncio.run(webhook.whatsapp_incoming(request))


def call_360(body_payload: Dict[str, Any], spawned: List[Any]) -> Any:
    """The real 360dialog route, with only the background spawn observed."""
    import asyncio

    import routers.whatsapp_webhook as webhook
    from fastapi import Request

    async def _receive() -> Dict[str, Any]:
        import json
        return {"type": "http.request", "body": json.dumps(body_payload).encode()}

    request = Request({"type": "http", "method": "POST",
                       "path": "/webhook/whatsapp/360dialog",
                       "headers": [], "query_string": b""}, receive=_receive)

    def _spawn(coro: Any, name: str = "") -> None:
        spawned.append(name)
        coro.close()

    with patch("core.runtime_perf.spawn_background", _spawn):
        return asyncio.run(webhook._safe_360dialog_ack(request, scope="any", name="t"))


_REAL_RECORDER = acceptance.record_before_acknowledging


@pytest.fixture()
def bound(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    """Point the real recorder at this database. Nothing else is doubled."""
    monkeypatch.setattr(acceptance, "record_before_acknowledging",
                        functools.partial(_REAL_RECORDER, session_factory=sessions(db)))


@pytest.mark.parametrize("call", [call_meta, call_360])
def test_a_scoped_inbound_is_recorded_before_the_route_answers(configured, db, bound, call):
    spawned: List[Any] = []
    response = call(body(message("wamid.route")), spawned)
    assert response.status_code == 200
    assert spawned                                        # processing was scheduled
    assert [r.provider_message_id for r in handover.pending_inbound(db, tenant_id=db.tenant_id)] \
        == ["wamid.route"]


@pytest.mark.parametrize("call", [call_meta, call_360])
def test_a_route_that_cannot_record_answers_retryable_and_spawns_nothing(
        configured, db, bound, call, monkeypatch):
    monkeypatch.setattr(handover, "record_inbound", lambda *a, **k: None)
    spawned: List[Any] = []
    response = call(body(message("wamid.unpersisted")), spawned)
    assert response.status_code == 503
    assert spawned == []                                  # nothing was half-processed
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


@pytest.mark.parametrize("call", [call_meta, call_360])
def test_a_mixed_batch_that_fails_is_refused_whole_rather_than_split(
        configured, db, bound, call, monkeypatch):
    """The unaffected messages are not lost: the batch is redelivered intact."""
    monkeypatch.setattr(handover, "record_inbound", lambda *a, **k: None)
    spawned: List[Any] = []
    response = call(mixed_body((PHONE_ID, [message("wamid.mine")]),
                               (OTHER_PHONE_ID, [message("wamid.theirs")])), spawned)
    assert response.status_code == 503
    assert spawned == []


@pytest.mark.parametrize("call", [call_meta, call_360])
def test_a_route_carrying_nothing_of_ours_answers_200_as_before(configured, db, bound, call):
    spawned: List[Any] = []
    response = call(body(message("wamid.stranger", sender=STRANGER)), spawned)
    assert response.status_code == 200 and spawned
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


@pytest.mark.parametrize("call", [call_meta, call_360])
def test_the_routes_are_untouched_while_the_pilot_is_off(db, bound, call, monkeypatch):
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    spawned: List[Any] = []
    response = call(body(message("wamid.off")), spawned)
    assert response.status_code == 200 and spawned
