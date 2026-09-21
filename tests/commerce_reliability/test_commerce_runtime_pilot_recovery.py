"""The deduplication boundary, driven for real, with unfinished runtime work.

A provider retry is dropped before it reaches any handler — twice, by the
in-memory cache and then by the persisted idempotency guard. That is what stops
a duplicate producing a second answer, and it is also the only event that can
ever reach a commerce-runtime turn nobody finished.

These cases drive the real ``_dispatch_message`` through both guards and hold
the boundary to the three properties the correction names:

* a duplicate carrying unfinished runtime work reaches the handler;
* a duplicate of *finished* work is still dropped, exactly as today;
* nothing is resent — the retry that is let through carries no send of its own,
  and the runtime's own ledger decides what may be dispatched.

The ledger read behind the predicate is doubled here and proved against real
PostgreSQL in ``test_commerce_runtime_pilot_pg.py``; the configuration half of
the predicate, both deduplication guards, and the dispatcher are real.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import JSON, create_engine, event  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.commerce_runtime import pilot_guard as pg  # noqa: E402
from core.commerce_runtime import recovery  # noqa: E402
from core.commerce_runtime.handover_models import create_handover_tables  # noqa: E402
from core.commerce_runtime.models import RuntimeBase  # noqa: E402
from database.models import Base, Tenant, WhatsAppConnection  # noqa: E402

PHONE_ID = "PID_RECOVERY"
SENDER = "966500000123"
NORMALIZED = "+966500000123"
MODEL = "model-configured-for-this-pilot"


@event.listens_for(Base.metadata, "before_create")
@event.listens_for(RuntimeBase.metadata, "before_create")
def _remap_jsonb(target: Any, connection: Any, **kw: Any) -> None:
    """SQLite has no JSONB and cannot parse a ``::jsonb`` cast in a default.

    The columns are JSON here and JSONB on PostgreSQL, where these tables are
    actually proved; the values the code writes are identical either way. The
    remap is for the SQLite create only and is **undone** afterwards (below):
    the Table objects are shared by every module in the process, and a
    PostgreSQL case collected after this one must create the declared JSONB
    columns with their declared defaults, not this dialect's stand-ins.
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
    # StaticPool: one in-memory database shared by every connection, so a
    # second session (the handover barrier opens its own) sees the same rows
    # instead of a fresh empty database.
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    # The handover lives in the runtime's own relations (revision 0111), so a
    # fixture that creates only the application schema has no barrier to read.
    create_handover_tables(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.flush()
    session.add(WhatsAppConnection(
        tenant_id=tenant.id, phone_number_id=PHONE_ID, phone_number="+966500000000",
        whatsapp_business_account_id="WABA_RECOVERY", status="connected"))
    session.commit()
    session.tenant_id = tenant.id                       # type: ignore[attr-defined]
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    from core.inbound_dedup import reset_cache

    reset_cache()
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(db.tenant_id))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, NORMALIZED)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)


def unfinished(tenant_id: int, turn_id: int = 7) -> recovery.AdmittedInbound:
    return recovery.AdmittedInbound(tenant_id=tenant_id, turn_id=turn_id, conversation_id=3,
                                    provider_message_id="wamid.retry", finished=False)


def finished(tenant_id: int, turn_id: int = 7) -> recovery.AdmittedInbound:
    """The same turn, after some other invocation completed it."""
    return recovery.AdmittedInbound(tenant_id=tenant_id, turn_id=turn_id, conversation_id=3,
                                    provider_message_id="wamid.retry", finished=True)


def deliver(db: Any, *, msg_id: str, ledger: Any = None) -> Dict[str, List[Any]]:
    """One inbound event through the real dispatcher and both dedup guards."""
    import routers.whatsapp_webhook as webhook

    seen: Dict[str, List[Any]] = {"handled": [], "sent": []}

    async def _handler(**kwargs: Any) -> None:
        seen["handled"].append(kwargs)

    def _lookup(**kwargs: Any) -> Optional[recovery.AdmittedInbound]:
        return None if ledger is None else ledger(**kwargs)

    def _unfinished_only(**kwargs: Any) -> Optional[recovery.AdmittedInbound]:
        found = _lookup(**kwargs)
        return found if found is not None and found.unfinished else None

    with (
        patch.object(webhook, "get_db", return_value=iter([db])),
        patch.object(webhook, "_is_platform_tenant", return_value=False),
        patch.object(webhook, "_handle_merchant_message", side_effect=_handler),
        patch.object(webhook, "_post_wa", new=AsyncMock(
            side_effect=lambda *a, **k: seen["sent"].append(a))),
        patch.object(recovery, "admitted_turn_for", _lookup),
        patch.object(recovery, "unfinished_turn_for", _unfinished_only),
    ):
        asyncio.run(webhook._dispatch_message(PHONE_ID, {
            "from": SENDER, "id": msg_id, "type": "text", "text": {"body": "وين طلبي؟"},
        }, {"metadata": {"phone_number_id": PHONE_ID}}))
    return seen


# ── The boundary as it is today ──────────────────────────────────────────────


def test_a_first_delivery_reaches_the_handler(configured, db):
    assert len(deliver(db, msg_id="wamid.first")["handled"]) == 1


def test_a_retry_of_finished_work_is_still_dropped(configured, db):
    """Nothing unfinished exists, so the duplicate is a duplicate."""
    assert len(deliver(db, msg_id="wamid.same")["handled"]) == 1
    assert deliver(db, msg_id="wamid.same")["handled"] == []


def test_a_retry_is_dropped_while_the_pilot_is_off(monkeypatch, db):
    from core.inbound_dedup import reset_cache

    reset_cache()
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    always = lambda **kwargs: unfinished(db.tenant_id)   # noqa: E731
    assert len(deliver(db, msg_id="wamid.off", ledger=always)["handled"]) == 1
    assert deliver(db, msg_id="wamid.off", ledger=always)["handled"] == []


# ── Unfinished runtime work is reachable again ───────────────────────────────


def test_a_retry_carrying_unfinished_runtime_work_reaches_the_handler(configured, db):
    always = lambda **kwargs: unfinished(db.tenant_id)   # noqa: E731
    assert len(deliver(db, msg_id="wamid.open", ledger=always)["handled"]) == 1
    second = deliver(db, msg_id="wamid.open", ledger=always)
    assert len(second["handled"]) == 1                   # let through, not dropped
    assert second["sent"] == []                          # and it sent nothing by itself


def test_the_retry_stops_being_let_through_once_the_work_is_finished(configured, db):
    state = {"open": True}

    def ledger(**kwargs: Any) -> Optional[recovery.AdmittedInbound]:
        return unfinished(db.tenant_id) if state["open"] else finished(db.tenant_id)

    assert len(deliver(db, msg_id="wamid.closes", ledger=ledger)["handled"]) == 1
    assert len(deliver(db, msg_id="wamid.closes", ledger=ledger)["handled"]) == 1
    state["open"] = False                                # another invocation finished it
    assert deliver(db, msg_id="wamid.closes", ledger=ledger)["handled"] == []


def test_the_lookup_is_asked_only_for_the_configured_tenant(configured, db):
    asked: List[int] = []

    def ledger(**kwargs: Any) -> None:
        asked.append(int(kwargs["tenant_id"]))
        return None

    deliver(db, msg_id="wamid.scoped", ledger=ledger)    # first delivery: no dedup, no lookup
    assert asked == []
    deliver(db, msg_id="wamid.scoped", ledger=ledger)
    assert asked == [db.tenant_id]


def test_another_recipient_is_never_let_through_however_open_its_work(configured, db,
                                                                      monkeypatch):
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    always = lambda **kwargs: unfinished(db.tenant_id)   # noqa: E731
    assert len(deliver(db, msg_id="wamid.other", ledger=always)["handled"]) == 1
    assert deliver(db, msg_id="wamid.other", ledger=always)["handled"] == []


def test_a_lookup_that_fails_leaves_the_duplicate_a_duplicate(configured, db):
    def explode(**kwargs: Any) -> None:
        raise RuntimeError("the ledger is unreadable")

    assert len(deliver(db, msg_id="wamid.broken", ledger=explode)["handled"]) == 1
    assert deliver(db, msg_id="wamid.broken", ledger=explode)["handled"] == []


# ── A turn let through for recovery is never handed to the legacy path ───────


def test_a_refused_turn_the_runtime_has_not_finished_is_not_given_to_legacy(configured, db,
                                                                            monkeypatch):
    """Configuration can change between the boundary and the guard. If it does,
    the answer is silence from this runtime, never a second answer from V1."""
    from services import commerce_runtime_pilot as seam

    monkeypatch.setattr(recovery, "admitted_turn_for",
                        lambda **kwargs: unfinished(db.tenant_id))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")   # now refused

    class _Convo:
        id = 501
        customer_id = 9
        language = "ar"

    result = asyncio.run(seam.maybe_handle_with_commerce_runtime(
        db=db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="وين طلبي؟",
        convo=_Convo(), wa_msg_id="wamid.retry", inbound_metadata=None,
        trace=type("T", (), {"mark_outbound_sent": lambda self, **k: None})(),
        legacy_already_answered=False, ai_gate_skipped=False,
    ))
    assert result.handled is True
    assert result.reason == f"{seam.UNFINISHED_PREFIX}{pg.RECIPIENT_NOT_ALLOWLISTED}"


@pytest.mark.parametrize("reason_env, reason", [
    (pg.ENV_ENABLED, pg.PILOT_DISABLED),
    (pg.ENV_TENANT_ALLOWLIST, pg.TENANT_NOT_ALLOWLISTED),
])
def test_a_refusal_that_means_the_runtime_was_never_here_costs_no_lookup(configured, db,
                                                                         monkeypatch,
                                                                         reason_env, reason):
    from services import commerce_runtime_pilot as seam

    asked: List[Any] = []
    monkeypatch.setattr(recovery, "admitted_turn_for",
                        lambda **kwargs: asked.append(kwargs) or unfinished(db.tenant_id))
    monkeypatch.delenv(reason_env, raising=False)

    class _Convo:
        id = 501
        customer_id = 9
        language = "ar"

    result = asyncio.run(seam.maybe_handle_with_commerce_runtime(
        db=db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="وين طلبي؟",
        convo=_Convo(), wa_msg_id="wamid.retry", inbound_metadata=None,
        trace=type("T", (), {"mark_outbound_sent": lambda self, **k: None})(),
        legacy_already_answered=False, ai_gate_skipped=False,
    ))
    assert asked == []
    assert result.handled is False and result.reason == reason


# ── Handover: no new turns, and its own work finished (F4) ───────────────────


def test_a_draining_pilot_refuses_a_new_turn(configured, monkeypatch, db):
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    decision = pg.evaluate_pilot_route(
        db, tenant_id=db.tenant_id, customer_phone=NORMALIZED, phone_number_id=PHONE_ID,
        inbound_text="عندكم حذاء؟")
    assert decision.permitted is False and decision.reason == pg.PILOT_DRAINING


def test_a_draining_pilot_still_finishes_work_it_admitted(configured, monkeypatch, db):
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    decision = pg.evaluate_pilot_route(
        db, tenant_id=db.tenant_id, customer_phone=NORMALIZED, phone_number_id=PHONE_ID,
        inbound_text="عندكم حذاء؟", finishing_open_work=True)
    assert decision.permitted is True and decision.model == MODEL


def test_finishing_open_work_waives_only_the_draining_refusal(configured, monkeypatch, db):
    """Every other condition still decides. Draining is not an override."""
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    decision = pg.evaluate_pilot_route(
        db, tenant_id=db.tenant_id, customer_phone=NORMALIZED, phone_number_id=PHONE_ID,
        inbound_text="عندكم حذاء؟", finishing_open_work=True)
    assert decision.permitted is False and decision.reason == pg.RECIPIENT_NOT_ALLOWLISTED


def test_a_draining_pilot_still_recovers_through_deduplication(configured, monkeypatch, db):
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    always = lambda **kwargs: unfinished(db.tenant_id)   # noqa: E731
    assert len(deliver(db, msg_id="wamid.drain", ledger=always)["handled"]) == 1
    assert len(deliver(db, msg_id="wamid.drain", ledger=always)["handled"]) == 1


def test_a_fully_disabled_pilot_recovers_nothing(configured, monkeypatch, db):
    """The emergency stop is still one flag, and it stops everything."""
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    always = lambda **kwargs: unfinished(db.tenant_id)   # noqa: E731
    assert len(deliver(db, msg_id="wamid.stop", ledger=always)["handled"]) == 1
    assert deliver(db, msg_id="wamid.stop", ledger=always)["handled"] == []


def test_the_draining_seam_runs_the_turn_rather_than_only_withholding_it(configured,
                                                                         monkeypatch, db):
    from core.commerce_runtime import runtime_entry as entry
    from services import commerce_runtime_pilot as seam

    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setattr(recovery, "admitted_turn_for",
                        lambda **kwargs: unfinished(db.tenant_id))
    monkeypatch.setattr(seam, "_history_rows", lambda db_, **kwargs: [])
    ran: List[Dict[str, Any]] = []

    def _run(**kwargs: Any) -> Any:
        ran.append(kwargs)
        return entry.TurnReport(reason=entry.HANDLED, tenant_id=db.tenant_id,
                                conversation_id=501, turn_id=7, dispatch_status="accepted",
                                provider_message_id="wamid.DRAINED", delivery_sequence_id=1,
                                reply_text="رد")

    monkeypatch.setattr(entry, "run_commerce_runtime_turn", _run)

    class _Convo:
        id = 501
        customer_id = 9
        language = "ar"

    class _Trace:
        def mark_outbound_sent(self, **kwargs: Any) -> None:
            pass

    result = asyncio.run(seam.maybe_handle_with_commerce_runtime(
        db=db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="وين طلبي؟",
        convo=_Convo(), wa_msg_id="wamid.retry", inbound_metadata=None, trace=_Trace(),
        legacy_already_answered=False, ai_gate_skipped=False,
    ))
    assert result.handled is True and result.reason == entry.HANDLED
    assert len(ran) == 1 and ran[0]["model"] == MODEL


def test_a_draining_pilot_takes_no_new_turn_even_through_the_seam(configured, monkeypatch, db):
    from services import commerce_runtime_pilot as seam

    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setattr(recovery, "admitted_turn_for", lambda **kwargs: None)

    class _Convo:
        id = 501
        customer_id = 9
        language = "ar"

    result = asyncio.run(seam.maybe_handle_with_commerce_runtime(
        db=db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="سؤال جديد",
        convo=_Convo(), wa_msg_id="wamid.new", inbound_metadata=None,
        trace=type("T", (), {"mark_outbound_sent": lambda self, **k: None})(),
        legacy_already_answered=False, ai_gate_skipped=False,
    ))
    assert result.handled is False and result.reason == pg.PILOT_DRAINING


# ── An admitted identity never becomes new legacy work (F3, re-review) ───────


def test_a_turn_completed_between_deduplication_and_routing_is_not_given_to_legacy(
        configured, monkeypatch, db):
    """The reviewer's race: the retry is let through because the turn is open,
    another invocation completes it, and by the time routing looks the recovery
    question answers "nothing to do" — which is not the same as "not ours"."""
    from services import commerce_runtime_pilot as seam

    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    # Deduplication saw an unfinished turn; routing sees a finished one.
    monkeypatch.setattr(recovery, "admitted_turn_for",
                        lambda **kwargs: finished(db.tenant_id))

    class _Convo:
        id = 501
        customer_id = 9
        language = "ar"

    result = asyncio.run(seam.maybe_handle_with_commerce_runtime(
        db=db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="وين طلبي؟",
        convo=_Convo(), wa_msg_id="wamid.retry", inbound_metadata=None,
        trace=type("T", (), {"mark_outbound_sent": lambda self, **k: None})(),
        legacy_already_answered=False, ai_gate_skipped=False,
    ))
    assert result.handled is True                          # not handed back
    assert result.reason == f"{seam.OWNED_PREFIX}{pg.PILOT_DRAINING}"


@pytest.mark.parametrize("refusal_env, refusal", [
    (pg.ENV_DRAINING, pg.PILOT_DRAINING),
    (pg.ENV_RECIPIENT_ALLOWLIST, pg.RECIPIENT_NOT_ALLOWLISTED),
])
def test_a_finished_runtime_turn_is_withheld_whatever_the_refusal(configured, monkeypatch, db,
                                                                  refusal_env, refusal):
    from services import commerce_runtime_pilot as seam

    if refusal_env == pg.ENV_DRAINING:
        monkeypatch.setenv(pg.ENV_DRAINING, "true")
    else:
        monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    monkeypatch.setattr(recovery, "admitted_turn_for",
                        lambda **kwargs: finished(db.tenant_id))

    class _Convo:
        id = 501
        customer_id = 9
        language = "ar"

    result = asyncio.run(seam.maybe_handle_with_commerce_runtime(
        db=db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="وين طلبي؟",
        convo=_Convo(), wa_msg_id="wamid.retry", inbound_metadata=None,
        trace=type("T", (), {"mark_outbound_sent": lambda self, **k: None})(),
        legacy_already_answered=False, ai_gate_skipped=False,
    ))
    assert result.handled is True and result.reason == f"{seam.OWNED_PREFIX}{refusal}"


def test_a_finished_turn_is_still_not_recovery_work(configured, db):
    """Withholding it from legacy is not the same as reopening it."""
    from services import commerce_runtime_pilot as seam

    assert seam._admitted_runtime_turn(
        tenant_id=db.tenant_id, phone_id=PHONE_ID, wa_msg_id="", refusal="x") is None


# ── The dispatcher claims ownership before its own owners act (F2) ───────────


def test_the_claim_is_made_for_a_configured_tenant_and_recipient(configured, db):
    from services import commerce_runtime_pilot as seam

    with patch.object(pg, "verified_connection", lambda _db, **kwargs: (f"wa:{PHONE_ID}", "17")):
        claim = seam.commerce_runtime_claims_inbound(
            db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="مرحبا",
            wa_msg_id="wamid.x")
    assert claim is not None and claim.basis == "configured"
    assert (claim.tenant_id, claim.recipient, claim.provider_message_id) == (
        db.tenant_id, NORMALIZED, "wamid.x")


@pytest.mark.parametrize("unset", [pg.ENV_ENABLED, pg.ENV_MODEL, pg.ENV_RECIPIENT_ALLOWLIST])
def test_nothing_the_pilot_is_not_configured_for_is_ever_claimed(configured, monkeypatch, db,
                                                                 unset):
    from services import commerce_runtime_pilot as seam

    monkeypatch.delenv(unset, raising=False)
    monkeypatch.setattr(recovery, "admitted_turn_for", lambda **kwargs: None)
    with patch.object(pg, "verified_connection", lambda _db, **kwargs: (f"wa:{PHONE_ID}", "17")):
        assert seam.commerce_runtime_claims_inbound(
            db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="مرحبا",
            wa_msg_id="wamid.x") is None


def test_an_inbound_the_runtime_already_owns_is_claimed_even_while_draining(configured,
                                                                            monkeypatch, db):
    """The recovery case: draining takes no new turns and still owns its own."""
    from services import commerce_runtime_pilot as seam

    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setattr(recovery, "admitted_turn_for", lambda **kwargs: unfinished(db.tenant_id))
    claim = seam.commerce_runtime_claims_inbound(
        db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="وين طلبي؟",
        wa_msg_id="wamid.retry")
    assert claim is not None and claim.basis == "admitted_open"


def test_a_draining_pilot_takes_no_new_turn_and_gives_it_to_nobody(configured, monkeypatch, db):
    """Draining stops new work. It does not hand the conversation to legacy.

    The runtime being drained may still have a send in flight, so releasing the
    next inbound to another owner is how a handover produces the second answer
    it exists to prevent. The inbound is claimed, buffered for the operator, and
    answered by nobody.
    """
    from core.commerce_runtime import handover
    from services import commerce_runtime_pilot as seam

    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setattr(recovery, "admitted_turn_for", lambda **kwargs: None)
    claim = seam.commerce_runtime_claims_inbound(
        db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="سؤال جديد",
        wa_msg_id="wamid.new")
    assert claim is not None and claim.basis == "drain_buffered"
    pending = handover.pending_inbound(db, tenant_id=db.tenant_id)
    assert [e.provider_message_id for e in pending] == ["wamid.new"]
    assert pending[0].reason == handover.REASON_PROCESS_DRAINING
    # Identity and payload, not a line in a list: enough to replay it.
    assert pending[0].recipient == NORMALIZED
    assert pending[0].phone_number_id == PHONE_ID
    assert pending[0].payload == {"text": "سؤال جديد"}


def test_a_drained_process_still_leaves_traffic_it_never_owned_alone(configured, monkeypatch, db):
    """A recipient outside the allowlist keeps exactly today's behaviour.

    Draining is scoped to the conversations this pilot would otherwise own.
    Buffering anything else would silence a customer the runtime never touched.
    """
    from core.commerce_runtime import handover
    from services import commerce_runtime_pilot as seam

    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setattr(recovery, "admitted_turn_for", lambda **kwargs: None)
    assert seam.commerce_runtime_claims_inbound(
        db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to="966500009999", text="سؤال",
        wa_msg_id="wamid.stranger") is None
    assert handover.pending_inbound(db, tenant_id=db.tenant_id) == ()


def test_a_claim_check_that_raises_is_not_a_claim(configured, monkeypatch, db):
    from services import commerce_runtime_pilot as seam

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("guard unavailable")

    monkeypatch.setattr(pg, "evaluate_pilot_route", explode)
    assert seam.commerce_runtime_claims_inbound(
        db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=SENDER, text="مرحبا",
        wa_msg_id="wamid.x") is None


# ── The dispatcher's own owners no longer act first (F2, re-review) ──────────


def drive_payment_claim(db: Any, *, msg_id: str, claimed: Any = None) -> Dict[str, Any]:
    """One inbound the dispatcher's payment-claim short circuit would take.

    Its decision function is doubled so the branch is certain to fire; the
    branch itself, the dispatcher and the ownership claim are real.
    """
    import core.order_flow as order_flow
    import core.payment_intent as payment_intent
    import routers.whatsapp_webhook as webhook

    seen: Dict[str, Any] = {"handled": [], "short_circuit": [], "patched": []}

    async def _handler(**kwargs: Any) -> None:
        seen["handled"].append(kwargs)

    def _claim(**kwargs: Any) -> Dict[str, Any]:
        seen["short_circuit"].append(kwargs)
        return {"reply": "تم استلام مبلغك", "state_patch": {"stage": "awaiting_receipt"}}

    def _patch(*args: Any, **kwargs: Any) -> None:
        seen["patched"].append(kwargs)

    with (
        patch.object(webhook, "get_db", return_value=iter([db])),
        patch.object(webhook, "_is_platform_tenant", return_value=False),
        patch.object(webhook, "_handle_merchant_message", side_effect=_handler),
        patch.object(webhook, "_post_wa", new=AsyncMock(return_value=True)),
        patch.object(payment_intent, "maybe_handle_payment_claim", _claim),
        patch.object(order_flow, "apply_state_patch", _patch),
        patch.object(pg, "verified_connection", lambda _db, **kwargs: (f"wa:{PHONE_ID}", "17")),
        patch.object(recovery, "admitted_turn_for",
                     lambda **kwargs: claimed(**kwargs) if claimed else None),
    ):
        asyncio.run(webhook._dispatch_message(PHONE_ID, {
            "from": SENDER, "id": msg_id, "type": "text", "text": {"body": "المبلغ: 126 ريال"},
        }, {"metadata": {"phone_number_id": PHONE_ID}}))
    return seen


def test_the_payment_short_circuit_takes_the_turn_while_the_pilot_is_off(monkeypatch, db):
    """Proof the competing dispatcher owner is real and would otherwise act."""
    from core.inbound_dedup import reset_cache

    reset_cache()
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    seen = drive_payment_claim(db, msg_id="wamid.pay.off")
    assert len(seen["short_circuit"]) == 1                 # it decided
    assert len(seen["patched"]) == 1                       # and mutated order state
    assert seen["handled"] == []                           # the handler was never reached


def test_a_claimed_inbound_never_reaches_the_payment_short_circuit(configured, db):
    seen = drive_payment_claim(db, msg_id="wamid.pay.claimed")
    assert seen["short_circuit"] == []                     # not even asked
    assert seen["patched"] == []                           # no state mutated
    assert len(seen["handled"]) == 1
    assert seen["handled"][0]["commerce_runtime_claim"] is not None


def test_a_recovering_retry_never_reaches_the_payment_short_circuit(configured, monkeypatch,
                                                                    db):
    """The exposure the reviewer named: a retry let through to finish runtime
    work must not be answered a second time by a different owner."""
    monkeypatch.setenv(pg.ENV_DRAINING, "true")            # takes no new turns
    seen = drive_payment_claim(db, msg_id="wamid.pay.recover",
                               claimed=lambda **kwargs: unfinished(db.tenant_id))
    assert seen["short_circuit"] == [] and seen["patched"] == []
    assert len(seen["handled"]) == 1
    assert seen["handled"][0]["commerce_runtime_claim"] is not None


def test_a_draining_pilot_with_no_open_work_still_withholds_the_short_circuit(configured,
                                                                              monkeypatch, db):
    """Even with nothing open, a drained conversation is not the short circuit's.

    The short circuit mutates order state and answers. Letting it run for a
    conversation the runtime is handing over is the same second answer, from a
    different owner.
    """
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    seen = drive_payment_claim(db, msg_id="wamid.pay.drained")
    assert seen["short_circuit"] == []
    assert len(seen["handled"]) == 1
    assert seen["handled"][0]["commerce_runtime_claim"].basis == "drain_buffered"


# ── The dispatcher's COD routes are owners too (sticky COD ownership) ────────


def drive_cod_button(db: Any, *, msg_id: str, kind: str = "interactive",
                     payload: str = "nahla_cod_confirm", context_wamid: str = "",
                     correlated: str = "") -> Dict[str, Any]:
    """One COD button tap through the real dispatcher.

    ``consume_owned_cod_button_inbound`` is doubled so the branch is certain to
    be observable; the branch itself, the payload predicate, the dispatcher and
    the ownership claim are real.
    """
    import routers.whatsapp_webhook as webhook
    import services.cod_confirmation as cod

    seen: Dict[str, Any] = {"handled": [], "cod": [], "followup": [], "correlated": []}

    async def _handler(**kwargs: Any) -> None:
        seen["handled"].append(kwargs)

    async def _consume(_db: Any, **kwargs: Any) -> Any:
        seen["cod"].append(kwargs)
        followup = kwargs.get("followup_send")
        if followup is not None:
            await followup("confirm", object())
        return cod.COD_INBOUND_CONSUMED

    async def _followup(**kwargs: Any) -> None:
        seen["followup"].append(kwargs)

    def _correlate(_db: Any, **kwargs: Any) -> Any:
        # The context-id correlation: it reads this tenant's recent COD sends to
        # decide that a noncanonical payload *is* a COD reply. Observing it is
        # how "the classification never ran" becomes checkable.
        seen["correlated"].append(kwargs)
        return correlated or ""

    if kind == "interactive":
        msg = {"from": SENDER, "id": msg_id, "type": "interactive",
               "interactive": {"type": "button_reply",
                               "button_reply": {"id": payload, "title": "تأكيد الطلب"}}}
    else:
        msg = {"from": SENDER, "id": msg_id, "type": "button",
               "button": {"payload": payload, "text": "تأكيد الطلب"}}
        if context_wamid:
            msg["context"] = {"id": context_wamid}

    with (
        patch.object(webhook, "get_db", return_value=iter([db])),
        patch.object(webhook, "_is_platform_tenant", return_value=False),
        patch.object(webhook, "_handle_merchant_message", side_effect=_handler),
        patch.object(webhook, "_post_wa", new=AsyncMock(return_value=True)),
        patch.object(webhook, "_send_cod_followup_message", _followup),
        patch.object(cod, "consume_owned_cod_button_inbound", _consume),
        patch.object(cod, "resolve_owned_cod_button_payload_from_context", _correlate),
        patch.object(pg, "verified_connection", lambda _db, **kwargs: (f"wa:{PHONE_ID}", "17")),
        patch.object(recovery, "admitted_turn_for", lambda **kwargs: None),
    ):
        asyncio.run(webhook._dispatch_message(
            PHONE_ID, msg, {"metadata": {"phone_number_id": PHONE_ID}}))
    return seen


def test_the_cod_button_route_takes_the_tap_while_the_pilot_is_off(monkeypatch, db):
    """Proof the dispatcher's COD owner is real: it consumes and answers."""
    from core.inbound_dedup import reset_cache

    reset_cache()
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    seen = drive_cod_button(db, msg_id="wamid.codbtn.off")
    assert len(seen["cod"]) == 1                           # it consumed the tap
    assert len(seen["followup"]) == 1                      # and answered the customer
    assert seen["handled"] == []                           # the handler was never reached


def test_a_claimed_cod_button_tap_never_reaches_the_cod_route(configured, db):
    seen = drive_cod_button(db, msg_id="wamid.codbtn.claimed")
    assert seen["cod"] == [] and seen["followup"] == []
    assert len(seen["handled"]) == 1
    assert seen["handled"][0]["commerce_runtime_claim"] is not None


def test_a_claimed_template_button_tap_never_reaches_the_cod_route(configured, db):
    seen = drive_cod_button(db, msg_id="wamid.codtpl.claimed", kind="template")
    assert seen["cod"] == [] and seen["followup"] == []
    assert len(seen["handled"]) == 1
    assert seen["handled"][0]["commerce_runtime_claim"] is not None


def test_a_non_allowlisted_cod_button_tap_keeps_todays_behaviour(configured, monkeypatch, db):
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    seen = drive_cod_button(db, msg_id="wamid.codbtn.stranger")
    assert len(seen["cod"]) == 1 and len(seen["followup"]) == 1
    assert seen["handled"] == []


# ── The correlation is a classification, and it is guarded too ───────────────


def test_a_noncanonical_template_button_is_never_correlated_for_owned_traffic(
        configured, db):
    """The reviewed guard sat in front of the *action*, not the classification.

    ``resolve_owned_cod_button_payload_from_context`` is what decides that a
    payload nobody recognises, carrying a ``context.id``, is one of this
    tenant's COD sends — it reads the tenant's own recent sends to reach that
    verdict. Running it for a turn the runtime owns already puts a second owner
    on the turn, so the claim is honoured before it, not after.
    """
    seen = drive_cod_button(db, msg_id="wamid.codtpl.noncanonical", kind="template",
                            payload="bt_1a2b3c", context_wamid="wamid.cod.sent.earlier",
                            correlated="nahla_cod_confirm")
    assert seen["correlated"] == []                        # never classified
    assert seen["cod"] == [] and seen["followup"] == []     # no mutation, no send
    assert len(seen["handled"]) == 1
    assert seen["handled"][0]["commerce_runtime_claim"] is not None


def test_the_same_noncanonical_tap_is_correlated_and_consumed_while_the_pilot_is_off(
        monkeypatch, db):
    """Proof the correlation is real and reachable: only ownership stops it."""
    from core.inbound_dedup import reset_cache

    reset_cache()
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    seen = drive_cod_button(db, msg_id="wamid.codtpl.noncanonical.off", kind="template",
                            payload="bt_1a2b3c", context_wamid="wamid.cod.sent.earlier",
                            correlated="nahla_cod_confirm")
    assert len(seen["correlated"]) == 1
    assert seen["correlated"][0]["context_wamid"] == "wamid.cod.sent.earlier"
    assert len(seen["cod"]) == 1 and len(seen["followup"]) == 1
    assert seen["handled"] == []


def test_readiness_failure_after_acceptance_holds_the_turn_instead_of_releasing_it(
        configured, db, monkeypatch):
    """A barrier that cannot be read is not evidence the runtime does not own this.

    The guard has already said this tenant, recipient and connection are the
    pilot's. If the handover state then cannot be read — the schema is not
    there, the database will not answer — handing the turn to the COD route or
    the legacy brain is how a conversation the runtime may still be answering
    gets a second answer. The claim is held instead, and nothing executes.
    """
    from core.commerce_runtime import handover

    def _unreadable(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("relation \"commerce_runtime_handover_barrier\" does not exist")

    monkeypatch.setattr(handover, "read_barrier", _unreadable)
    seen = drive_cod_button(db, msg_id="wamid.codbtn.noschema")
    claim = seen["handled"][0]["commerce_runtime_claim"]
    assert claim is not None
    assert claim.basis == "ownership_unavailable"
    assert seen["cod"] == [] and seen["followup"] == []
    assert seen["correlated"] == []


def test_traffic_that_was_never_the_pilots_is_unchanged_when_readiness_fails(
        configured, db, monkeypatch):
    """The hold is scoped: it covers what the guard established, and nothing else."""
    from core.commerce_runtime import handover

    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")

    def _unreadable(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no barrier here either")

    monkeypatch.setattr(handover, "read_barrier", _unreadable)
    seen = drive_cod_button(db, msg_id="wamid.codbtn.noschema.stranger")
    assert len(seen["cod"]) == 1 and len(seen["followup"]) == 1   # today's behaviour
    assert seen["handled"] == []


# ── Recovery, end to end: an acceptance nobody admitted is handed back ───────


def accept(db: Any, identity: str, *, text: str = "وين طلبي؟") -> Any:
    """The webhook's own acceptance, for real."""
    from services import commerce_runtime_acceptance as acceptance

    body = {"entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PHONE_ID},
        "messages": [{"id": identity, "from": SENDER, "type": "text",
                      "text": {"body": text}}],
    }}]}]}
    outcome = acceptance.record_before_acknowledging(body, session_factory=lambda: db,
                                                     authenticated=True)
    assert outcome.ok and outcome.recorded == (identity,)
    return outcome


def test_an_accepted_inbound_nobody_admitted_passes_both_dedup_boundaries(configured, db):
    """The deduplication question, asked for a record rather than a turn.

    The acknowledgement was written before the webhook answered; no turn was
    ever admitted. A "is there an unfinished turn" check answers no for that —
    which is why the reviewed shape could never replay one. Both boundaries now
    ask the wider question, for an allowlisted tenant and recipient only.
    """
    from core.inbound_dedup import is_duplicate_inbound, reset_cache
    import routers.whatsapp_webhook as webhook

    reset_cache()
    accept(db, "wamid.accepted.only")
    # First sighting marks it; the next one is the duplicate a retry or a
    # recovery replay arrives as.
    assert is_duplicate_inbound(phone_number_id=PHONE_ID,
                                msg_id="wamid.accepted.only") is False
    assert is_duplicate_inbound(phone_number_id=PHONE_ID,
                                msg_id="wamid.accepted.only") is True

    with patch("database.session.SessionLocal", lambda: db):
        assert webhook._duplicate_is_unfinished_runtime_work(
            phone_number_id=PHONE_ID, sender=SENDER,
            msg_id="wamid.accepted.only") is True
        # A record an operator has closed is finished work, and stays a
        # duplicate. (It cannot be *resolved* here: resolution needs the
        # runtime's own accepted reply, and nothing answered this one.)
        from core.commerce_runtime import handover

        assert handover.resolve_inbound(db, tenant_id=db.tenant_id,
                                        channel_connection_ref=f"wa:{PHONE_ID}",
                                        provider_message_id="wamid.accepted.only") is False
        entry = handover.pending_inbound(db, tenant_id=db.tenant_id)[0]
        closed = handover.dispose_inbound(
            db, tenant_id=db.tenant_id, entry_ids=[entry.id], disposition="not_required",
            evidence={"authorized_by": "owner", "why": "test: no answer owed"}, by="owner")
        assert closed.disposed == (entry.id,), closed.refused
        assert webhook._duplicate_is_unfinished_runtime_work(
            phone_number_id=PHONE_ID, sender=SENDER,
            msg_id="wamid.accepted.only") is False


def test_nothing_outside_the_allowlist_is_ever_let_through_for_recovery(configured, db,
                                                                       monkeypatch):
    import routers.whatsapp_webhook as webhook

    accept(db, "wamid.scoped.only")
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    with patch("database.session.SessionLocal", lambda: db):
        assert webhook._duplicate_is_unfinished_runtime_work(
            phone_number_id=PHONE_ID, sender=SENDER,
            msg_id="wamid.scoped.only") is False


def test_the_recovery_run_rebuilds_the_provider_body_from_what_was_stored(configured, db):
    """A replay is the inbound itself, not a note that one was lost."""
    from services import commerce_runtime_recovery as runner

    accept(db, "wamid.replayable", text="عندكم قميص قطني أزرق؟")
    from core.commerce_runtime import handover

    record = handover.pending_inbound(db, tenant_id=db.tenant_id)[0]
    body = runner._webhook_body(record)
    assert body is not None
    value = body["entry"][0]["changes"][0]["value"]
    assert value["metadata"]["phone_number_id"] == PHONE_ID
    message = value["messages"][0]
    assert message["id"] == "wamid.replayable"
    assert message["from"] == SENDER
    assert message["text"] == {"body": "عندكم قميص قطني أزرق؟"}
    assert body["_nahla_recovery"] is True


def test_a_record_with_nothing_replayable_is_reported_not_invented(configured, db):
    from services import commerce_runtime_recovery as runner
    from core.commerce_runtime import handover

    handover.record_inbound(
        db, tenant_id=db.tenant_id, phone_number_id=PHONE_ID,
        channel_connection_ref=f"wa:{PHONE_ID}", recipient=NORMALIZED,
        provider_message_id="wamid.empty", payload={}, reason=handover.REASON_ACCEPTED,
        barrier_generation=0)
    record = [r for r in handover.pending_inbound(db, tenant_id=db.tenant_id)
              if r.provider_message_id == "wamid.empty"][0]
    assert runner._webhook_body(record) is None

    outcome = runner.recover_tenant(db, tenant_id=db.tenant_id, dry_run=False)
    assert outcome.counted().get(runner.SKIPPED_NOT_REPLAYABLE) == 1


def test_the_recovery_run_hands_the_inbound_back_to_the_dispatcher(configured, db):
    """The whole lifecycle: accepted, acknowledged, never admitted, replayed."""
    from services import commerce_runtime_recovery as runner

    accept(db, "wamid.handed.back")
    replayed: List[Any] = []

    async def _dispatcher(body: Any) -> None:
        replayed.append(body)

    with patch.object(runner, "_replay", _dispatcher):
        outcome = runner.recover_tenant(db, tenant_id=db.tenant_id, dry_run=False)

    assert outcome.counted() == {runner.REPLAYED: 1}
    assert len(replayed) == 1
    assert replayed[0]["entry"][0]["changes"][0]["value"]["messages"][0]["id"] == \
        "wamid.handed.back"


# ── Ownership, once established, is not re-decided by a failure ──────────────


def test_a_positive_decision_survives_every_later_read_failing(configured, db, monkeypatch):
    """The reviewer's case: the guard permits, the barrier read fails, and every
    lookup after it fails too — including the guard itself, were it asked
    again. The held claim is built from the decision already taken; nothing is
    evaluated a second time, and COD consumes nothing."""
    from core.commerce_runtime import handover

    evaluations: List[Any] = []
    real_route = pg.evaluate_pilot_route

    def _once(*args: Any, **kwargs: Any) -> Any:
        evaluations.append(kwargs.get("inbound_text"))
        if len(evaluations) > 1:
            raise RuntimeError("the guard would fail on a second evaluation")
        return real_route(*args, **kwargs)

    def _unreadable(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(pg, "evaluate_pilot_route", _once)
    monkeypatch.setattr(handover, "read_barrier", _unreadable)
    monkeypatch.setattr(handover, "accepted_inbound", _unreadable)
    seen = drive_cod_button(db, msg_id="wamid.codbtn.everything.down")
    claim = seen["handled"][0]["commerce_runtime_claim"]
    assert claim is not None and claim.basis == "ownership_unavailable"
    assert claim.provider_message_id == "wamid.codbtn.everything.down"
    assert len(evaluations) == 1                          # decided once, held from that
    assert seen["cod"] == [] and seen["followup"] == [] and seen["correlated"] == []


def test_a_negative_decision_plus_a_failed_read_claims_nothing(configured, db, monkeypatch):
    """The hold is scoped to what the guard established. A recipient outside
    the allowlist gets today's behaviour whatever fails afterwards, and the one
    durable read that could still establish scope — the acceptance record —
    finds nothing."""
    from core.commerce_runtime import handover

    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")

    def _unreadable(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(handover, "read_barrier", _unreadable)
    seen = drive_cod_button(db, msg_id="wamid.codbtn.stranger.down")
    assert len(seen["cod"]) == 1 and len(seen["followup"]) == 1
    assert seen["handled"] == []


def test_a_durable_record_holds_the_turn_when_the_guard_could_not_decide(configured, db,
                                                                          monkeypatch):
    """Undecidable is not negative: with an acceptance on record for this exact
    inbound, the turn is held for the runtime rather than given to COD."""
    from core.commerce_runtime import handover

    accept(db, "wamid.codbtn.recorded.undecidable")

    def _undecidable(*_a: Any, **kwargs: Any) -> Any:
        return pg._refused(pg.GUARD_ERROR, kwargs.get("tenant_id"))

    def _unreadable(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("barrier unreadable")

    monkeypatch.setattr(pg, "evaluate_pilot_route", _undecidable)
    monkeypatch.setattr(handover, "read_barrier", _unreadable)
    seen = drive_cod_button(db, msg_id="wamid.codbtn.recorded.undecidable")
    claim = seen["handled"][0]["commerce_runtime_claim"]
    assert claim is not None and claim.basis == "ownership_unavailable"
    assert seen["cod"] == [] and seen["followup"] == []


# ── A recovery grant names one identity and needs a pending record ───────────


def test_a_recovery_grant_is_honoured_for_its_own_identity_only(configured, db):
    from core.commerce_runtime import handover
    from services import commerce_runtime_recovery as runner

    accept(db, "wamid.granted")
    record = handover.accepted_inbound(db, tenant_id=db.tenant_id,
                                       channel_connection_ref=f"wa:{PHONE_ID}",
                                       provider_message_id="wamid.granted")
    handover.open_drain(db, tenant_id=db.tenant_id)

    with runner.granted(record):
        # This identity, under this grant, while draining: claimed for the runtime.
        seen = deliver(db, msg_id="wamid.granted")
        assert seen["handled"][0]["commerce_runtime_claim"].basis == "recovery_admitted"
        # Another identity under the same grant: deferred, as any new work is.
        seen = deliver(db, msg_id="wamid.not.granted")
        assert seen["handled"][0]["commerce_runtime_claim"].basis == "drain_buffered"
        assert handover.accepted_inbound(db, tenant_id=db.tenant_id,
                                         channel_connection_ref=f"wa:{PHONE_ID}",
                                         provider_message_id="wamid.not.granted") is not None

    # No grant in force: an accepted identity is deferred, not admitted. (A
    # redelivery of ``wamid.granted`` itself is now a duplicate the in-memory
    # boundary drops, which is right; a second accepted inbound shows the rule.)
    accept(db, "wamid.granted.later")
    seen = deliver(db, msg_id="wamid.granted.later")
    assert seen["handled"][0]["commerce_runtime_claim"].basis == "drain_buffered"


def test_a_grant_without_a_pending_record_authorises_nothing(configured, db):
    """The grant restates what the database owes; it cannot invent an obligation."""
    from core.commerce_runtime import handover
    from services import commerce_runtime_recovery as runner
    from types import SimpleNamespace

    handover.open_drain(db, tenant_id=db.tenant_id)
    forged = SimpleNamespace(tenant_id=db.tenant_id, channel_connection_ref=f"wa:{PHONE_ID}",
                             provider_message_id="wamid.never.accepted", id=999)
    with runner.granted(forged):
        seen = deliver(db, msg_id="wamid.never.accepted")
    assert seen["handled"][0]["commerce_runtime_claim"].basis == "drain_buffered"


# ── The grant is checked again where it is used: on the admitting connection ─

from types import SimpleNamespace  # noqa: E402

RECIPIENT = "+" + SENDER


def test_recovery_admission_checks_the_grant_s_entry_on_the_admitting_connection(configured, db):
    """``admits_recovery_on`` is what the admission transaction asks. It reads
    the barrier **and** the grant's durable entry on that same connection: the
    entry must exist, name this tenant, connection and identity, and still be
    pending. A disposition committed in between withdraws the grant."""
    from core.commerce_runtime import handover

    accept(db, "wamid.grant.checked")
    record = handover.accepted_inbound(db, tenant_id=db.tenant_id,
                                       channel_connection_ref=f"wa:{PHONE_ID}",
                                       provider_message_id="wamid.grant.checked")
    handover.open_drain(db, tenant_id=db.tenant_id)
    engine = db.get_bind()

    def admits(**overrides: Any) -> bool:
        claim = dict(entry_id=record.id, channel_connection_ref=f"wa:{PHONE_ID}",
                     provider_message_id="wamid.grant.checked")
        claim.update(overrides)
        with engine.connect() as conn:
            return handover.admits_recovery_on(conn, tenant_id=db.tenant_id, **claim)

    assert admits() is True                                             # pending, draining
    assert admits(entry_id=None) is False                                # no grant: not recovery
    assert admits(entry_id=record.id + 1000) is False                    # no such entry
    assert admits(provider_message_id="wamid.someone.else") is False     # wrong identity
    assert admits(channel_connection_ref="wa:OTHER") is False            # wrong connection
    with engine.connect() as conn:                                       # wrong tenant
        assert handover.admits_recovery_on(
            conn, tenant_id=db.tenant_id + 1, entry_id=record.id,
            channel_connection_ref=f"wa:{PHONE_ID}",
            provider_message_id="wamid.grant.checked") is False

    # The operator disposes of the entry: the grant now authorises nothing.
    closed = handover.dispose_inbound(
        db, tenant_id=db.tenant_id, entry_ids=[record.id], disposition="not_required",
        evidence={"authorized_by": "owner", "why": "withdrawn while draining"}, by="owner")
    assert closed.disposed == (record.id,), closed.refused
    assert admits() is False


def test_recovery_admission_still_refuses_a_settled_barrier_whatever_the_entry_says(configured, db):
    from core.commerce_runtime import handover
    from core.commerce_runtime import handover_models as hm

    accept(db, "wamid.grant.settled")
    record = handover.accepted_inbound(db, tenant_id=db.tenant_id,
                                       channel_connection_ref=f"wa:{PHONE_ID}",
                                       provider_message_id="wamid.grant.settled")
    handover.open_drain(db, tenant_id=db.tenant_id)
    row = db.query(hm.HandoverBarrier).filter_by(tenant_id=db.tenant_id).one()
    row.state = hm.STATE_SETTLED
    db.commit()
    with db.get_bind().connect() as conn:
        assert handover.admits_recovery_on(
            conn, tenant_id=db.tenant_id, entry_id=record.id,
            channel_connection_ref=f"wa:{PHONE_ID}",
            provider_message_id="wamid.grant.settled") is False


def test_the_seam_hands_the_grant_s_identity_to_the_admission_check(configured, db, monkeypatch):
    """The seam does not ask 'is the barrier open' on its own; it asks about
    the grant's entry, by id, connection and identity."""
    from core.commerce_runtime import handover
    from services import commerce_runtime_pilot as seam
    from services import commerce_runtime_recovery as runner

    accept(db, "wamid.grant.identity")
    record = handover.accepted_inbound(db, tenant_id=db.tenant_id,
                                       channel_connection_ref=f"wa:{PHONE_ID}",
                                       provider_message_id="wamid.grant.identity")
    asked: List[Dict[str, Any]] = []

    def _admits(conn: Any, **kwargs: Any) -> bool:
        asked.append(dict(kwargs))
        return False

    monkeypatch.setattr(handover, "admits_recovery_on", _admits)

    captured: Dict[str, Any] = {}

    def _run(**kwargs: Any) -> Any:
        captured["barrier"] = kwargs["admission_barrier"]
        raise RuntimeError("stop here: the barrier callback is what this test reads")

    from core.commerce_runtime import runtime_entry as entry
    monkeypatch.setattr(entry, "run_commerce_runtime_turn", _run)
    handover.open_drain(db, tenant_id=db.tenant_id)
    with runner.granted(record):
        with pytest.raises(RuntimeError):
            asyncio.run(seam._own_turn(  # noqa: SLF001
                db=db, tenant_id=db.tenant_id, phone_id=PHONE_ID, to=RECIPIENT,
                text="x", convo=SimpleNamespace(id=1, customer_id=None),
                wa_msg_id="wamid.grant.identity", inbound_metadata=None, trace=None,
                decision=SimpleNamespace(connection_ref=f"wa:{PHONE_ID}", connection_id="1",
                                         recipient=RECIPIENT, model="model-x"),
                customer_name="", recovery_grant=runner.current_grant()))
    assert captured["barrier"](object()) is False
    assert asked == [{"tenant_id": db.tenant_id, "entry_id": record.id,
                      "channel_connection_ref": f"wa:{PHONE_ID}",
                      "provider_message_id": "wamid.grant.identity"}]
