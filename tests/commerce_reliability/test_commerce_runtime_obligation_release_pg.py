"""An acceptance obligation the runtime never took is given back.

The acknowledgement promised that **the commerce runtime** owes this inbound an
answer, and only the runtime's own post-turn bookkeeping clears that promise.
Four gates sit ahead of it in `whatsapp_webhook` and each ends the turn with a
`return` before the seam that would have asked it:

    5226  is_ai_disabled_for_conversation   (the conversation's own ai_paused)
    6393  should_skip_ai                    (blocklist, handoff, rate limit, bot loop)
    6662  has_billing_access                (closed for the wider stages by #1135)
    6675  wa_usage.check_limit              (the conversation quota)

Under `pilot` this cost nothing: in scope required an explicitly allowlisted
recipient, so the obligation was bounded to numbers an operator chose. Once the
merchant's own setting decides the recipient, it is every silenced inbound on
the platform — one pending row each, until `MAX_PENDING_DEFERRED` refuses the
next, the batch reads "not accepted", and that merchant's webhook is answered
503 for everything, not merely for AI.

Enumerating the four at classification time would close today's list and reopen
with tomorrow's. The release is asked once, where every inbound passes when its
turn is over, so a gate added later is covered without anyone remembering to —
and that is what makes "cannot accumulate" a property rather than a list.

**Division of labour, stated so these cases are not read as proving more than
they do.** Whether a turn the runtime really owns can be disposed of underneath
it is `handover.dispose_inbound`'s contract, enforced under the tenant's
exclusive lock and covered by that module's own cases. What is proved here is
what the release adds: it asks, it reports what came back, and it never turns a
refusal into a release.

Real PostgreSQL, the repository's own migration chain, the real ledger. Nothing
is sent anywhere.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.commerce_runtime import handover
from core.commerce_runtime import handover_models as hm
from core.commerce_runtime import pilot_guard as pgd
from services import commerce_runtime_acceptance as acc

from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
)

REVISION = "0111"
PHONE = "+966500000931"
PHONE_ID_PREFIX = "1555"


@pytest.fixture(scope="module")
def database(pg_admin_dsn: str) -> Any:
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True, pool_size=5, max_overflow=5)
    try:
        _alembic(dsn, REVISION)
        yield engine
    finally:
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


@pytest.fixture()
def store(database: Any) -> Dict[str, Any]:
    """A merchant of this case's own, so every count belongs to this case."""
    phone_id = PHONE_ID_PREFIX + str(uuid.uuid4().int)[:11]
    with database.begin() as conn:
        tenant_id = int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": f"متجر تجريبي عام {uuid.uuid4().hex[:8]}"}).scalar_one())
        conn.execute(
            text("INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
                 "VALUES (:t, :p, 'connected')"),
            {"t": tenant_id, "p": phone_id})
    return {"tenant_id": tenant_id, "phone_id": phone_id,
            "channel": f"wa:{phone_id}",
            "factory": sessionmaker(bind=database)}


@pytest.fixture()
def globally_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pgd.ENV_ENABLED, "true")
    monkeypatch.setenv(pgd.ENV_MODE, pgd.MODE_GLOBAL)
    monkeypatch.setenv(pgd.ENV_MODEL, "model-configured-for-this-runtime")
    monkeypatch.setenv(pgd.ENV_TENANT_ALLOWLIST, "")
    monkeypatch.setenv(pgd.ENV_RECIPIENT_ALLOWLIST, "")
    monkeypatch.delenv(pgd.ENV_GLOBAL_TENANT_DENYLIST, raising=False)


def _accept(store: Dict[str, Any], identity: str) -> Any:
    """Take the obligation exactly as the acceptance boundary does."""
    db = store["factory"]()
    try:
        return handover.record_inbound(
            db, tenant_id=store["tenant_id"], phone_number_id=store["phone_id"],
            channel_connection_ref=store["channel"], recipient=PHONE,
            provider_message_id=identity,
            payload={"text": "وش المنتجات المتوفرة عندكم؟", "type": "text"},
            reason=handover.REASON_ACCEPTED,
            barrier_generation=handover.read_barrier(
                db, tenant_id=store["tenant_id"]).generation)
    finally:
        db.close()


def _record(store: Dict[str, Any], identity: str) -> Any:
    db = store["factory"]()
    try:
        return handover.accepted_inbound(
            db, tenant_id=store["tenant_id"],
            channel_connection_ref=store["channel"], provider_message_id=identity)
    finally:
        db.close()


def _pending_count(store: Dict[str, Any]) -> int:
    db = store["factory"]()
    try:
        return int(db.query(hm.DeferredInbound).filter(
            hm.DeferredInbound.tenant_id == store["tenant_id"],
            hm.DeferredInbound.namespace == handover.NAMESPACE,
            hm.DeferredInbound.state == hm.DEFERRED_PENDING).count())
    finally:
        db.close()


def _release(store: Dict[str, Any], identity: str) -> str:
    return acc.release_unrouted_obligation(
        phone_number_id=store["phone_id"], provider_message_id=identity,
        session_factory=store["factory"])


# ── the obligation comes back ───────────────────────────────────────────────


def test_a_turn_the_runtime_never_took_gives_its_obligation_back(store, globally_owned):
    """The case every one of the four gates produces: accepted, then silenced."""
    identity = f"wamid.{uuid.uuid4().hex}"
    assert _accept(store, identity) is not None
    assert _pending_count(store) == 1

    assert _release(store, identity) == acc.RELEASED

    assert _pending_count(store) == 0
    row = _record(store, identity)
    assert row is not None and not row.pending
    assert row.state == hm.DEFERRED_DISPOSED
    assert row.disposition == hm.DISPOSITION_NOT_REQUIRED


def test_the_disposal_says_who_authorised_it_and_why(store, globally_owned):
    """Not a note somebody has to interpret: the row carries its own reason."""
    identity = f"wamid.{uuid.uuid4().hex}"
    _accept(store, identity)
    assert _release(store, identity) == acc.RELEASED

    evidence = dict(_record(store, identity).disposition_evidence or {})
    assert evidence.get("authorized_by") == "platform:whatsapp_webhook_dispatch"
    assert "never asked" in str(evidence.get("why", ""))
    assert evidence.get("verified_at")          # the API's own check, stored with it


def test_releasing_twice_is_not_a_second_disposal(store, globally_owned):
    """A provider retry must not re-open or re-dispose anything."""
    identity = f"wamid.{uuid.uuid4().hex}"
    _accept(store, identity)
    assert _release(store, identity) == acc.RELEASED
    assert _release(store, identity) == acc.RELEASE_NOTHING_PENDING
    assert _pending_count(store) == 0


# ── and it is never taken back where it is owed ─────────────────────────────


def test_a_refusal_is_reported_as_kept_and_never_as_a_release(store, globally_owned,
                                                              monkeypatch):
    """The release reports what came back; it never upgrades a refusal.

    Whether a running turn *can* be disposed is `dispose_inbound`'s contract,
    held under the tenant lock and covered by its own cases. This pins the half
    the release owns: a refused entry stays pending and is reported as kept.
    """
    identity = f"wamid.{uuid.uuid4().hex}"
    _accept(store, identity)
    entry_id = int(_record(store, identity).id)

    real = handover.dispose_inbound

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        return handover.DispositionResult(
            refused={entry_id: "turn_admitted_and_unfinished:41"})

    monkeypatch.setattr(handover, "dispose_inbound", _refuse)
    assert _release(store, identity) == acc.RELEASE_KEPT
    assert _pending_count(store) == 1, "a refused entry must stay pending"

    monkeypatch.setattr(handover, "dispose_inbound", real)
    assert _release(store, identity) == acc.RELEASED


def test_nothing_pending_is_not_an_error(store, globally_owned):
    assert _release(store, f"wamid.{uuid.uuid4().hex}") == acc.RELEASE_NOTHING_PENDING


def test_a_connection_that_is_nobodys_is_left_alone(store, globally_owned):
    assert acc.release_unrouted_obligation(
        phone_number_id="1555000000000", provider_message_id=f"wamid.{uuid.uuid4().hex}",
        session_factory=store["factory"]) == acc.RELEASE_NOT_OURS


def test_an_unreadable_scope_leaves_the_row_pending(store, globally_owned, monkeypatch):
    """A release we cannot justify is worse than one we did not make."""
    identity = f"wamid.{uuid.uuid4().hex}"
    _accept(store, identity)

    monkeypatch.setattr(pgd, "resolve_pilot_scope",
                        lambda *a, **k: pgd.ScopeLookup(status=pgd.SCOPE_UNAVAILABLE,
                                                        detail="OperationalError"))
    assert _release(store, identity) == acc.RELEASE_UNAVAILABLE
    assert _pending_count(store) == 1


# ── the property the cap rests on ──────────────────────────────────────────


def test_silenced_inbounds_do_not_accumulate_toward_the_cap(store, globally_owned):
    """The accumulation, run rather than argued.

    Before the release, this is exactly the shape that reaches
    `MAX_PENDING_DEFERRED`: every inbound accepted, every one silenced by a gate
    ahead of the runtime, nothing clearing any of them. Twenty-five is not the
    cap — the point is that the pending count does not *grow*, so no number of
    inbounds reaches it.
    """
    identities = [f"wamid.{uuid.uuid4().hex}" for _ in range(25)]
    high_water = 0
    for identity in identities:
        assert _accept(store, identity) is not None
        high_water = max(high_water, _pending_count(store))
        assert _release(store, identity) == acc.RELEASED

    assert _pending_count(store) == 0
    assert high_water == 1, "at most one obligation is ever outstanding at a time"
    assert high_water < handover.MAX_PENDING_DEFERRED


def test_the_release_is_asked_where_every_inbound_passes_not_at_each_gate(store):
    """Gate-agnostic by construction, so a gate added later is covered.

    Asserted on the wiring rather than the prose: the dispatch loop calls it,
    and `_classify` still knows nothing about any of the four gate functions.
    """
    import inspect

    from routers import whatsapp_webhook as wh

    body = inspect.getsource(wh._handle_whatsapp_body)
    assert "_dispatch_message" in body
    assert "_release_unrouted_obligation" in body

    classify = inspect.getsource(acc._classify)
    for gate in ("should_skip_ai", "check_limit", "ai_paused",
                 "is_ai_disabled_for_conversation"):
        assert gate not in classify, f"_classify should not need to know about {gate}"
