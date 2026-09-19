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
from sqlalchemy.orm import sessionmaker  # noqa: E402

from core.commerce_runtime import pilot_guard as pg  # noqa: E402
from core.commerce_runtime import recovery  # noqa: E402
from database.models import Base, Tenant, WhatsAppConnection  # noqa: E402

PHONE_ID = "PID_RECOVERY"
SENDER = "966500000123"
NORMALIZED = "+966500000123"
MODEL = "model-configured-for-this-pilot"


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target: Any, connection: Any, **kw: Any) -> None:
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest.fixture()
def db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
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


def unfinished(tenant_id: int, turn_id: int = 7) -> recovery.UnfinishedTurn:
    return recovery.UnfinishedTurn(tenant_id=tenant_id, turn_id=turn_id, conversation_id=3,
                                   provider_message_id="wamid.retry")


def deliver(db: Any, *, msg_id: str, ledger: Any = None) -> Dict[str, List[Any]]:
    """One inbound event through the real dispatcher and both dedup guards."""
    import routers.whatsapp_webhook as webhook

    seen: Dict[str, List[Any]] = {"handled": [], "sent": []}

    async def _handler(**kwargs: Any) -> None:
        seen["handled"].append(kwargs)

    def _lookup(**kwargs: Any) -> Optional[recovery.UnfinishedTurn]:
        return None if ledger is None else ledger(**kwargs)

    with (
        patch.object(webhook, "get_db", return_value=iter([db])),
        patch.object(webhook, "_is_platform_tenant", return_value=False),
        patch.object(webhook, "_handle_merchant_message", side_effect=_handler),
        patch.object(webhook, "_post_wa", new=AsyncMock(
            side_effect=lambda *a, **k: seen["sent"].append(a))),
        patch.object(recovery, "unfinished_turn_for", _lookup),
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

    def ledger(**kwargs: Any) -> Optional[recovery.UnfinishedTurn]:
        return unfinished(db.tenant_id) if state["open"] else None

    assert len(deliver(db, msg_id="wamid.closes", ledger=ledger)["handled"]) == 1
    assert len(deliver(db, msg_id="wamid.closes", ledger=ledger)["handled"]) == 1
    state["open"] = False
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

    monkeypatch.setattr(recovery, "unfinished_turn_for",
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
    monkeypatch.setattr(recovery, "unfinished_turn_for",
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
    monkeypatch.setattr(recovery, "unfinished_turn_for",
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
    monkeypatch.setattr(recovery, "unfinished_turn_for", lambda **kwargs: None)

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
