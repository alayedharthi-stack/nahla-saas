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

from core import webhook_security as _security  # noqa: E402
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
    assert record.payload["text"] == "عندكم قميص؟"
    assert record.payload["type"] == "text"
    assert record.payload["connection_id"] == "1"
    # Enough of the inbound itself to rebuild a provider body and replay it.
    assert record.payload["raw"]["id"] == "wamid.scoped"
    assert record.payload["raw"]["from"] == SENDER
    assert record.payload["raw"]["text"] == {"body": "عندكم قميص؟"}
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
        patch.object(webhook, "evaluate_replay_claim",
                     lambda *a, **k: _security.ReplayVerdict(reject=False, claimed=False)),
    ):
        return asyncio.run(webhook.whatsapp_incoming(request))


_REAL_RECORDER = acceptance.record_before_acknowledging


@pytest.fixture()
def bound(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    """Point the real recorder at this database. Nothing else is doubled."""
    monkeypatch.setattr(acceptance, "record_before_acknowledging",
                        functools.partial(_REAL_RECORDER, session_factory=sessions(db)))


def test_a_scoped_inbound_is_recorded_before_the_route_answers(configured, db, bound):
    spawned: List[Any] = []
    response = call_meta(body(message("wamid.route")), spawned)
    assert response.status_code == 200
    assert spawned                                        # processing was scheduled
    assert [r.provider_message_id for r in handover.pending_inbound(db, tenant_id=db.tenant_id)] \
        == ["wamid.route"]


def test_a_route_that_cannot_record_answers_retryable_and_spawns_nothing(
        configured, db, bound, monkeypatch):
    monkeypatch.setattr(handover, "record_inbound", lambda *a, **k: None)
    spawned: List[Any] = []
    response = call_meta(body(message("wamid.unpersisted")), spawned)
    assert response.status_code == 503
    assert spawned == []                                  # nothing was half-processed
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_a_mixed_batch_that_fails_is_refused_whole_rather_than_split(
        configured, db, bound, monkeypatch):
    """The unaffected messages are not lost: the batch is redelivered intact."""
    monkeypatch.setattr(handover, "record_inbound", lambda *a, **k: None)
    spawned: List[Any] = []
    response = call_meta(mixed_body((PHONE_ID, [message("wamid.mine")]),
                               (OTHER_PHONE_ID, [message("wamid.theirs")])), spawned)
    assert response.status_code == 503
    assert spawned == []


def test_a_route_carrying_nothing_of_ours_answers_200_as_before(configured, db, bound):
    spawned: List[Any] = []
    response = call_meta(body(message("wamid.stranger", sender=STRANGER)), spawned)
    assert response.status_code == 200 and spawned
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_the_routes_are_untouched_while_the_pilot_is_off(db, bound, monkeypatch):
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    spawned: List[Any] = []
    response = call_meta(body(message("wamid.off")), spawned)
    assert response.status_code == 200 and spawned


# ── Authenticated, and unambiguous about whose the traffic is ────────────────


def test_a_scope_lookup_that_fails_is_not_unrelated_traffic(configured, db, monkeypatch):
    """The reviewed shape answered ``None`` for both, and a failed lookup then
    acknowledged pilot-owned work with nothing recorded for it."""
    def _down(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("could not resolve the connection")

    monkeypatch.setattr(pg, "resolve_pilot_scope", _down)
    outcome = acceptance.record_before_acknowledging(body(message("wamid.blind")),
                                                     session_factory=sessions(db))
    assert outcome.ok is False
    assert outcome.reason.startswith("error:") or outcome.reason == "scope_undecidable"
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_a_lookup_that_returns_unavailable_refuses_rather_than_acknowledging(
        configured, db, monkeypatch):
    monkeypatch.setattr(pg, "resolve_pilot_scope",
                        lambda *_a, **_k: pg.ScopeLookup(status=pg.SCOPE_UNAVAILABLE,
                                                         detail="OperationalError"))
    outcome = acceptance.record_before_acknowledging(body(message("wamid.unavailable")),
                                                     session_factory=sessions(db))
    assert outcome.ok is False and outcome.reason == "scope_undecidable"


def test_two_allowlisted_tenants_on_one_number_is_ambiguous_not_a_pick(
        configured, db, monkeypatch):
    """A phone number id claimed by two allowlisted tenants selects neither."""
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, f"{db.tenant_id},{db.other_tenant_id}")
    other = (db.query(WhatsAppConnection)
             .filter(WhatsAppConnection.tenant_id == db.other_tenant_id).first())
    other.phone_number_id = PHONE_ID                      # the same number, two tenants
    db.commit()
    found = pg.resolve_pilot_scope(db, phone_number_id=PHONE_ID)
    assert found.status == pg.SCOPE_AMBIGUOUS and found.resolved is False

    outcome = acceptance.record_before_acknowledging(body(message("wamid.ambiguous")),
                                                     session_factory=sessions(db))
    assert outcome.ok is False and outcome.reason == "scope_undecidable"
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_verified_out_of_scope_traffic_is_still_acknowledged_untouched(configured, db):
    """The distinction cuts both ways: a *decided* "not ours" changes nothing."""
    found = pg.resolve_pilot_scope(db, phone_number_id=OTHER_PHONE_ID)
    assert found.status == pg.SCOPE_NOT_OURS and found.decided is True
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.theirs"), phone_number_id=OTHER_PHONE_ID),
        session_factory=sessions(db))
    assert outcome.ok is True and outcome.scoped == 0
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_the_verified_connection_travels_into_persistence(configured, db):
    """Not a reference rebuilt from the payload: the row the guard checked."""
    outcome = acceptance.record_before_acknowledging(body(message("wamid.verified")),
                                                     session_factory=sessions(db))
    assert outcome.ok
    record = handover.pending_inbound(db, tenant_id=db.tenant_id)[0]
    verified = pg.verified_connection(db, tenant_id=db.tenant_id, phone_number_id=PHONE_ID)
    assert verified is not None
    assert record.channel_connection_ref == verified[0]
    assert record.payload["connection_id"] == verified[1]


# ── A refusal the provider can retry ─────────────────────────────────────────


class _Redis:
    """Enough of Redis for the nonce: SET NX EX, DELETE."""

    def __init__(self) -> None:
        self.keys: Dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int = 0) -> Any:
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def delete(self, key: str) -> int:
        return 1 if self.keys.pop(key, None) is not None else 0


@pytest.fixture()
def replay_protected(monkeypatch: pytest.MonkeyPatch) -> _Redis:
    """Replay protection on, rejecting — the state this defect appears in."""
    import core.redis_client as redis_client
    from core import config as core_config

    store = _Redis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: store)
    monkeypatch.setattr(core_config, "WEBHOOK_REPLAY_PROTECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(core_config, "WEBHOOK_REPLAY_REJECT_ENABLED", True, raising=False)
    return store


def call_meta_protected(body_payload: Dict[str, Any], spawned: List[Any]) -> Any:
    """The Meta route with real replay protection — only the spawn is observed."""
    import asyncio
    import json as _json

    import routers.whatsapp_webhook as webhook
    from fastapi import Request

    raw = _json.dumps(body_payload).encode()

    async def _receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": raw}

    request = Request({"type": "http", "method": "POST", "path": "/webhook/whatsapp",
                       "headers": [], "query_string": b""}, receive=_receive)

    def _spawn(coro: Any, name: str = "") -> None:
        spawned.append(name)
        coro.close()

    with (
        patch("core.runtime_perf.spawn_background", _spawn),
        patch.object(webhook, "_record_signature_audit", lambda *a, **k: None),
        patch.object(webhook, "_meta_should_reject", lambda _r: False),
    ):
        return asyncio.run(webhook.whatsapp_incoming(request))


def test_a_refused_acceptance_gives_the_nonce_back_so_the_retry_is_a_retry(
        configured, db, bound, replay_protected, monkeypatch):
    """The defect: the first request claims the nonce, fails to persist and
    answers 503; without giving the nonce back the provider's identical retry
    is dropped as a replay — a 200 for a message nothing ever processed."""
    real_record = handover.record_inbound
    broken = {"now": True}
    monkeypatch.setattr(handover, "record_inbound",
                        lambda *a, **k: None if broken["now"] else real_record(*a, **k))
    spawned: List[Any] = []
    payload = body(message("wamid.retryable"))
    first = call_meta_protected(payload, spawned)
    assert first.status_code == 503 and spawned == []
    assert replay_protected.keys == {}, "the nonce this request claimed was given back"

    # The provider retries the identical body. It must be able to persist.
    broken["now"] = False
    second = call_meta_protected(payload, spawned)
    assert second.status_code == 200
    assert spawned == ["webhook_meta"]
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1


def test_an_accepted_request_keeps_its_nonce_so_a_lost_ack_is_still_idempotent(
        configured, db, bound, replay_protected):
    """Commit succeeded, the acknowledgement was lost, the provider retries."""
    spawned: List[Any] = []
    payload = body(message("wamid.acked"))
    assert call_meta_protected(payload, spawned).status_code == 200
    assert len(replay_protected.keys) == 1                 # the nonce is held

    again = call_meta_protected(payload, spawned)
    assert again.status_code == 200
    assert spawned == ["webhook_meta"]                     # processed exactly once
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1


def test_a_rejected_signature_never_reaches_acceptance(configured, db, bound):
    """Authentication is before authoritative persistence, not after it."""
    import asyncio
    import json as _json

    import routers.whatsapp_webhook as webhook
    from fastapi import Request

    async def _receive() -> Dict[str, Any]:
        return {"type": "http.request",
                "body": _json.dumps(body(message("wamid.forged"))).encode()}

    request = Request({"type": "http", "method": "POST", "path": "/webhook/whatsapp",
                       "headers": [], "query_string": b""}, receive=_receive)
    spawned: List[Any] = []
    with (
        patch("core.runtime_perf.spawn_background",
              lambda coro, name="": (spawned.append(name), coro.close())),
        patch.object(webhook, "_record_signature_audit", lambda *a, **k: None),
        patch.object(webhook, "_meta_should_reject", lambda _r: True),
    ):
        response = asyncio.run(webhook.whatsapp_incoming(request))
    assert response.status_code == 200                     # Meta's retry storm is not invited
    assert spawned == []
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0
