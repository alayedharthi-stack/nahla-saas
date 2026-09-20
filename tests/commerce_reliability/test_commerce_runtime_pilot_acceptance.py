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
    """SQLite has no JSONB and cannot parse a ``::jsonb`` cast in a default.

    The remap is for the SQLite create only and is **undone** afterwards
    (below): the Table objects are shared by every module in the process, and
    a PostgreSQL case collected after this one must create the declared
    JSONB columns with their declared defaults, not this dialect's stand-ins.
    """
    if getattr(getattr(connection, "dialect", None), "name", "") != "sqlite":
        return
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _REMAPPED.setdefault(col, (col.type, col.server_default))
                col.type = JSON()
            default = getattr(col.server_default, "arg", None)
            if default is not None and "::" in str(default):
                _REMAPPED.setdefault(col, (col.type, col.server_default))
                col.server_default = None


_REMAPPED: Dict[Any, Any] = {}


@event.listens_for(Base.metadata, "after_create")
@event.listens_for(RuntimeBase.metadata, "after_create")
def _restore_jsonb(target: Any, connection: Any, **kw: Any) -> None:
    """Put back what ``_remap_jsonb`` changed, once the SQLite create is done."""
    for col, (col_type, server_default) in list(_REMAPPED.items()):
        col.type = col_type
        col.server_default = server_default
    _REMAPPED.clear()


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
                                                     session_factory=sessions(db), authenticated=True)
    assert outcome.ok and outcome.reason == "pilot_disabled"
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_a_pilot_scoped_inbound_is_durable_with_what_a_replay_needs(configured, db):
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.scoped", text="عندكم قميص؟")), session_factory=sessions(db), authenticated=True)
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
        body(message("wamid.stranger", sender=STRANGER)), session_factory=sessions(db), authenticated=True)
    assert outcome.ok and outcome.recorded == () and outcome.scoped == 0
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_another_tenant_s_connection_is_left_alone(configured, db):
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.other"), phone_number_id=OTHER_PHONE_ID),
        session_factory=sessions(db), authenticated=True)
    assert outcome.ok and outcome.recorded == ()
    assert handover.pending_count(db, tenant_id=db.other_tenant_id) == 0


def test_a_mixed_batch_records_only_the_scoped_messages(configured, db):
    outcome = acceptance.record_before_acknowledging(
        mixed_body((PHONE_ID, [message("wamid.mine"), message("wamid.stranger",
                                                              sender=STRANGER)]),
                   (OTHER_PHONE_ID, [message("wamid.theirs")])),
        session_factory=sessions(db), authenticated=True)
    assert outcome.ok and outcome.recorded == ("wamid.mine",) and outcome.scoped == 1
    assert [r.provider_message_id for r in handover.pending_inbound(db, tenant_id=db.tenant_id)] \
        == ["wamid.mine"]


def test_a_statuses_only_body_records_nothing(configured, db):
    payload = {"entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PHONE_ID},
        "statuses": [{"id": "wamid.status", "status": "delivered"}],
    }}]}]}
    outcome = acceptance.record_before_acknowledging(payload, session_factory=sessions(db), authenticated=True)
    assert outcome.ok and outcome.reason == "no_messages"


def test_a_provider_retry_records_the_same_inbound_once(configured, db):
    first = acceptance.record_before_acknowledging(body(message("wamid.retry")),
                                                   session_factory=sessions(db), authenticated=True)
    second = acceptance.record_before_acknowledging(body(message("wamid.retry")),
                                                    session_factory=sessions(db), authenticated=True)
    assert first.ok and second.ok
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1


def test_a_record_that_cannot_be_written_is_not_an_acceptance(configured, db, monkeypatch):
    monkeypatch.setattr(handover, "record_inbound", lambda *a, **k: None)
    outcome = acceptance.record_before_acknowledging(body(message("wamid.lost")),
                                                     session_factory=sessions(db), authenticated=True)
    assert outcome.ok is False and outcome.failed == ("wamid.lost",)
    assert outcome.reason == "not_persisted"


def test_a_database_that_raises_is_not_an_acceptance(configured, db, monkeypatch):
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the database is unavailable")

    monkeypatch.setattr(handover, "record_inbound", _boom)
    outcome = acceptance.record_before_acknowledging(body(message("wamid.down")),
                                                     session_factory=sessions(db), authenticated=True)
    assert outcome.ok is False and outcome.reason.startswith("error:")


# ── The routes, driven for real ──────────────────────────────────────────────


# The signature is real: every request below is signed with this secret using
# Meta's own scheme, verified by the route's own evaluator. Nothing stubs the
# authentication decision.
from tests.commerce_reliability.runtime_support import (  # noqa: E402
    META_TEST_APP_SECRET as APP_SECRET,
    meta_webhook_request as meta_request,
)


def call_meta(body_payload: Dict[str, Any], spawned: List[Any], *,
              signature: str = "valid") -> Any:
    """The real Meta route, with only the background spawn observed."""
    import asyncio

    import routers.whatsapp_webhook as webhook

    request = meta_request(body_payload, signature=signature)

    def _spawn(coro: Any, name: str = "") -> None:
        spawned.append(name)
        coro.close()

    with (
        patch("core.runtime_perf.spawn_background", _spawn),
        patch.object(webhook, "META_APP_SECRET", APP_SECRET),
        patch.object(webhook, "_record_signature_audit", lambda *a, **k: None),
        patch.object(webhook, "evaluate_replay_claim",
                     lambda *a, **k: _security.ReplayVerdict(reject=False, claimed=False)),
    ):
        return asyncio.run(webhook.whatsapp_incoming(request))


_REAL_RECORDER = acceptance.record_before_acknowledging
_REAL_DURABLE = acceptance.durable_status


@pytest.fixture()
def bound(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    """Point the real recorder — and the real durable-status question the
    replay path asks — at this database. Nothing else is doubled; in
    particular the route, not this fixture, states whether the signature
    verified."""
    monkeypatch.setattr(acceptance, "record_before_acknowledging",
                        functools.partial(_REAL_RECORDER, session_factory=sessions(db)))
    monkeypatch.setattr(acceptance, "durable_status",
                        functools.partial(_REAL_DURABLE, session_factory=sessions(db)))


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
                                                     session_factory=sessions(db), authenticated=True)
    assert outcome.ok is False
    assert outcome.reason.startswith("error:") or outcome.reason == "scope_undecidable"
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_a_lookup_that_returns_unavailable_refuses_rather_than_acknowledging(
        configured, db, monkeypatch):
    monkeypatch.setattr(pg, "resolve_pilot_scope",
                        lambda *_a, **_k: pg.ScopeLookup(status=pg.SCOPE_UNAVAILABLE,
                                                         detail="OperationalError"))
    outcome = acceptance.record_before_acknowledging(body(message("wamid.unavailable")),
                                                     session_factory=sessions(db), authenticated=True)
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
                                                     session_factory=sessions(db), authenticated=True)
    assert outcome.ok is False and outcome.reason == "scope_undecidable"
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_verified_out_of_scope_traffic_is_still_acknowledged_untouched(configured, db):
    """The distinction cuts both ways: a *decided* "not ours" changes nothing."""
    found = pg.resolve_pilot_scope(db, phone_number_id=OTHER_PHONE_ID)
    assert found.status == pg.SCOPE_NOT_OURS and found.decided is True
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.theirs"), phone_number_id=OTHER_PHONE_ID),
        session_factory=sessions(db), authenticated=True)
    assert outcome.ok is True and outcome.scoped == 0
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_the_verified_connection_travels_into_persistence(configured, db):
    """Not a reference rebuilt from the payload: the row the guard checked."""
    outcome = acceptance.record_before_acknowledging(body(message("wamid.verified")),
                                                     session_factory=sessions(db), authenticated=True)
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

    import routers.whatsapp_webhook as webhook

    request = meta_request(body_payload)

    def _spawn(coro: Any, name: str = "") -> None:
        spawned.append(name)
        coro.close()

    with (
        patch("core.runtime_perf.spawn_background", _spawn),
        patch.object(webhook, "META_APP_SECRET", APP_SECRET),
        patch.object(webhook, "_record_signature_audit", lambda *a, **k: None),
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
    """Enforce mode: authentication is before authoritative persistence."""
    import asyncio

    import routers.whatsapp_webhook as webhook

    request = meta_request(body(message("wamid.forged")), signature="invalid")
    spawned: List[Any] = []
    with (
        patch("core.runtime_perf.spawn_background",
              lambda coro, name="": (spawned.append(name), coro.close())),
        patch.object(webhook, "META_APP_SECRET", APP_SECRET),
        patch.object(webhook, "META_WEBHOOK_ENFORCE_SIGNATURE", True),
        patch.object(webhook, "_record_signature_audit", lambda *a, **k: None),
    ):
        response = asyncio.run(webhook.whatsapp_incoming(request))
    assert response.status_code == 200                     # Meta's retry storm is not invited
    assert spawned == []
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


# ── Authentication is a condition of the obligation, not of the audit ────────
#
# The route's own signature evaluator runs on every request below; nothing
# stubs the decision. What is held: a pilot-scoped batch is recorded and
# processed only when the signature **verified**, whatever the legacy path's
# audit mode lets through for everything else.


@pytest.mark.parametrize("signature", ["missing", "invalid"])
def test_pilot_scoped_work_is_refused_retryable_without_a_valid_signature(
        configured, db, bound, signature):
    spawned: List[Any] = []
    response = call_meta(body(message(f"wamid.{signature}")), spawned, signature=signature)
    assert response.status_code == 503
    assert response.body and b"pilot_scope_unauthenticated" in bytes(response.body)
    assert spawned == []                                  # not processed either
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_a_valid_signature_records_and_processes(configured, db, bound):
    spawned: List[Any] = []
    response = call_meta(body(message("wamid.signed")), spawned, signature="valid")
    assert response.status_code == 200 and spawned == ["webhook_meta"]
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1


def test_verified_out_of_scope_traffic_keeps_the_legacy_audit_mode(configured, db, bound):
    """A stranger's message with a bad signature is exactly what it is today:
    audited, processed by the legacy path, recorded by nobody."""
    spawned: List[Any] = []
    response = call_meta(body(message("wamid.stranger.unsigned", sender=STRANGER)), spawned,
                         signature="invalid")
    assert response.status_code == 200 and spawned == ["webhook_meta"]
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


def test_an_unavailable_scope_with_a_valid_signature_is_refused_retryable(
        configured, db, bound, monkeypatch):
    def _down(*_a: Any, **_k: Any) -> Any:
        return pg.ScopeLookup(status=pg.SCOPE_UNAVAILABLE, detail="db_down")

    monkeypatch.setattr(pg, "resolve_pilot_scope", _down)
    spawned: List[Any] = []
    response = call_meta(body(message("wamid.blind.signed")), spawned)
    assert response.status_code == 503 and spawned == []


def test_a_failed_second_lookup_does_not_become_out_of_scope(configured, db, monkeypatch):
    """Acceptance resolved the connection once. The guard is handed that row
    rather than looking it up again, so a failed second read cannot turn a
    verified association into ``connection_not_verified`` and drop the message."""
    def _lookup_fails(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("second lookup: connection reset")

    monkeypatch.setattr(pg, "verified_connection", _lookup_fails)
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.once.verified")), session_factory=sessions(db), authenticated=True)
    assert outcome.ok and outcome.recorded == ("wamid.once.verified",)
    record = handover.pending_inbound(db, tenant_id=db.tenant_id)[0]
    assert record.channel_connection_ref == f"wa:{PHONE_ID}"


def test_the_recorder_takes_no_obligation_unless_told_the_signature_verified(configured, db):
    """The default is fail-closed: a caller that says nothing records nothing."""
    outcome = acceptance.record_before_acknowledging(
        body(message("wamid.unstated")), session_factory=sessions(db))
    assert outcome.ok is False and outcome.reason == acceptance.REFUSED_UNAUTHENTICATED
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0


# ── A nonce is not an acceptance ─────────────────────────────────────────────


def test_a_nonce_left_by_a_dead_process_does_not_answer_200(configured, db, bound,
                                                             replay_protected):
    """The reproduction: a process claimed the nonce and died before writing
    anything down. The provider's identical retry must persist and process."""
    import json as _json

    payload = body(message("wamid.orphaned.nonce"))
    raw = _json.dumps(payload).encode()
    replay_protected.keys[_security.replay_nonce_key("meta", raw)] = "1"   # the dead process's
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0

    spawned: List[Any] = []
    response = call_meta_protected(payload, spawned)
    assert response.status_code == 200
    assert spawned == ["webhook_meta"]
    assert [r.provider_message_id for r in handover.pending_inbound(db, tenant_id=db.tenant_id)] \
        == ["wamid.orphaned.nonce"]

    # A completed duplicate is still a duplicate: the record exists now.
    again = call_meta_protected(payload, spawned)
    assert again.status_code == 200 and spawned == ["webhook_meta"]
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1


def test_a_nonce_for_unrelated_traffic_is_still_a_replay(configured, db, bound,
                                                          replay_protected):
    """Nothing pilot-scoped in the body: the nonce keeps its meaning."""
    import json as _json

    payload = body(message("wamid.stranger.replay", sender=STRANGER))
    replay_protected.keys[_security.replay_nonce_key("meta", _json.dumps(payload).encode())] = "1"
    spawned: List[Any] = []
    response = call_meta_protected(payload, spawned)
    assert response.status_code == 200 and spawned == []
    assert bytes(response.body) and b"replay" in bytes(response.body)


def test_an_unreadable_durable_status_retries_rather_than_drops(configured, db, bound,
                                                                 replay_protected, monkeypatch):
    import json as _json

    payload = body(message("wamid.unreadable.status"))
    replay_protected.keys[_security.replay_nonce_key("meta", _json.dumps(payload).encode())] = "1"

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("status store unavailable")

    monkeypatch.setattr(acceptance, "durable_status", _boom)
    spawned: List[Any] = []
    response = call_meta_protected(payload, spawned)
    assert response.status_code == 200 and spawned == ["webhook_meta"]
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 1


# ── After release, nothing new is accepted for the tenant ────────────────────


def test_after_release_a_new_inbound_is_refused_not_accepted_and_abandoned(
        configured, db, bound, monkeypatch):
    from core.commerce_runtime import handover_models as hm

    handover.open_drain(db, tenant_id=db.tenant_id)
    row = db.query(hm.HandoverBarrier).filter_by(tenant_id=db.tenant_id).one()
    row.state = hm.STATE_RELEASED
    db.commit()

    spawned: List[Any] = []
    response = call_meta(body(message("wamid.after.release")), spawned)
    assert response.status_code == 503
    assert b"pilot_released" in bytes(response.body)
    assert spawned == [] and handover.pending_count(db, tenant_id=db.tenant_id) == 0

    # Once the switch is off, the same request is the legacy path's.
    monkeypatch.delenv(pg.ENV_ENABLED)
    response = call_meta(body(message("wamid.after.release")), spawned)
    assert response.status_code == 200 and spawned == ["webhook_meta"]
    assert handover.pending_count(db, tenant_id=db.tenant_id) == 0
