"""No duplicate send: the claim itself refuses a recipient unless every trace
of it in the campaign proves no earlier copy was accepted by Meta — whatever
the scheduler, a resume, a retry or a corrupted row status says.

Every test drives the real leased dispatch path (``claim_recipient`` →
``mark_request_started`` → provider → ``record_*``) against a scripted Meta,
on SQLite and PostgreSQL. The negative controls remove one guard at a time
and show the scenario it owns sends a second copy.

Generic merchant data; no real sends.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

import test_campaign_send_ledger as _ledger_suite
from test_campaign_send_ledger import FakeMeta, _attempts, _conn, _logs, _run, _seed
from models import (  # noqa: E402
    Campaign, CampaignDispatchLease, CampaignSendAttempt, CampaignSendLog, Conversation, Customer,
    MessageDeliveryEvent, MessageEvent,
)
from services import campaign_dispatcher as disp  # noqa: E402
from services import campaign_send_ledger as ledger  # noqa: E402

dbf = _ledger_suite.dbf
fake_meta = _ledger_suite.fake_meta
_fast = _ledger_suite._fast

X = "+966500001111"          # neutral generic recipient
Y = "+966500002222"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dispatch(Session, ids, times=1):
    for _ in range(times):
        asyncio.run(_run(Session, ids, _conn()))


def _row(Session, campaign_id, phone):
    db = Session()
    try:
        r = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == campaign_id,
                                             CampaignSendLog.customer_phone_e164 == phone).one()
        return r.status, r.skip_reason
    finally:
        db.close()


def _requeue(Session, campaign_id, phone, *, clear_row=False):
    """What a buggy scheduler, an operator or a bad retry could do: put the
    row back to ``queued``. With ``clear_row`` the row also forgets its
    wamid / sent_at / receipts, so only the attempts remember the send."""
    db = Session()
    r = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == campaign_id,
                                         CampaignSendLog.customer_phone_e164 == phone).one()
    r.status = "queued"
    if clear_row:
        r.provider_message_id = r.sent_at = r.delivered_at = r.read_at = r.failed_at = None
    db.commit()
    db.close()


def _set_attempt(Session, campaign_id, **values):
    db = Session()
    for a in db.query(CampaignSendAttempt).filter(CampaignSendAttempt.campaign_id == campaign_id):
        for k, v in values.items():
            setattr(a, k, v)
    db.commit()
    db.close()


def _without(monkeypatch, guard):
    """Negative control: run with one guard removed."""
    if guard == "lock":
        monkeypatch.setattr(ledger, "_lock_recipient", lambda *a, **k: None)
    else:
        monkeypatch.setattr(ledger, "PRIOR_SEND_CHECKS", tuple(
            c for c in ledger.PRIOR_SEND_CHECKS if c.__name__ != guard))


# ── Scenarios, each owned by exactly one guard ──────────────────────────


def _scenario_attempt(Session, fake_meta, receipt):
    """A copy the attempts alone remember (row requeued and cleared)."""
    ids = _seed(Session, phones=[X])
    script = {X: ["timeout"]} if receipt == "uncertain" else {}
    meta = fake_meta(FakeMeta(script))
    _dispatch(Session, ids)
    if receipt == "delivered":
        _set_attempt(Session, ids.campaign_id, delivered_at=_now())
    elif receipt == "read":
        _set_attempt(Session, ids.campaign_id, delivered_at=_now(), read_at=_now())
    _requeue(Session, ids.campaign_id, X, clear_row=True)
    _dispatch(Session, ids, times=3)
    return ids, meta


def _scenario_duplicate_row(Session, fake_meta):
    """A pre-ledger copy on another row of the same number (966… vs +966…)."""
    ids = _seed(Session, phones=[X])
    db = Session()
    db.add(CampaignSendLog(tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                           customer_phone_e164=X.lstrip("+"), template_name="generic_offer",
                           template_language="ar", status="sent", provider_message_id="w.legacy",
                           sent_at=_now() - timedelta(days=1), attempt_count=1))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(Session, ids, times=3)
    return ids, meta


def _scenario_message_events(Session, fake_meta):
    """The row looks untouched; only message_events holds the earlier copy
    (its wamid overwritten on the row, or a pre-ledger send)."""
    ids = _seed(Session, phones=[X])
    db = Session()
    cust = db.query(Customer).filter(Customer.tenant_id == ids.tenant_id,
                                     Customer.normalized_phone == X).one()
    conv = Conversation(tenant_id=ids.tenant_id, customer_id=cust.id, status="active")
    db.add(conv)
    db.commit()
    db.add(MessageEvent(tenant_id=ids.tenant_id, conversation_id=conv.id, direction="outbound",
                        event_type="campaign", created_at=_now() - timedelta(days=1),
                        extra_metadata={"campaign_id": ids.campaign_id, "wa_message_id": "w.first",
                                        "_status_delivered": True, "_status_read": True}))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(Session, ids, times=3)
    return ids, meta


def _scenario_delivery_event(Session, fake_meta):
    """Only a provider receipt linked to the row remembers the copy."""
    ids = _seed(Session, phones=[X])
    db = Session()
    rid = db.query(CampaignSendLog.id).filter(CampaignSendLog.campaign_id == ids.campaign_id).scalar()
    db.add(MessageDeliveryEvent(tenant_id=ids.tenant_id, wamid="w.receipt", status="delivered",
                                campaign_send_log_id=rid, source="meta"))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(Session, ids, times=3)
    return ids, meta


def _scenario_legacy_count(Session, fake_meta):
    """The row was attempted by pre-ledger code (counter 1, no ledger
    attempt, no wamid): nothing proves that attempt was not accepted."""
    ids = _seed(Session, phones=[X])
    db = Session()
    db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id).update(
        {"attempt_count": 1})
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(Session, ids, times=3)
    return ids, meta


def _race_duplicate_rows(Session, fake_meta, monkeypatch, *, lease_serialises=True):
    """Two sessions of the lease holder claim the two rows of one number at
    the same instant; the evidence read is slowed so both would pass it
    unserialised. The lease heartbeat's row lock already serialises claims
    of one campaign; ``lease_serialises=False`` takes it away so the
    per-recipient lock is tested on its own."""
    ids = _seed(Session, phones=[X])
    db = Session()
    db.add(CampaignSendLog(tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                           customer_phone_e164=X.lstrip("+"), template_name="generic_offer",
                           template_language="ar", status="queued"))
    db.commit()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w1")
    rows = [r for (r,) in db.query(CampaignSendLog.id).filter(
        CampaignSendLog.campaign_id == ids.campaign_id).order_by(CampaignSendLog.id)]
    db.close()
    real = ledger._check_attempts
    inside = threading.Barrier(2)

    def slow(db, ctx):
        # Both claims meet here before either reads the evidence -- unless
        # one is held outside by the lock, in which case the other waits
        # out the timeout and goes on alone.
        try:
            inside.wait(timeout=1.5)
        except threading.BrokenBarrierError:
            pass
        return real(db, ctx)
    monkeypatch.setattr(ledger, "PRIOR_SEND_CHECKS", tuple(
        slow if c is real else c for c in ledger.PRIOR_SEND_CHECKS))
    if not lease_serialises:
        monkeypatch.setattr(ledger, "heartbeat", lambda *a, **k: ledger.HeartbeatResult(
            held=True, stop_requested=False))
    barrier = threading.Barrier(2)
    got = []

    def claim(rid):
        s = Session()
        try:
            barrier.wait()
            r = ledger.claim_recipient(s, log_id=rid, campaign=s.get(Campaign, ids.campaign_id),
                                       owner="w1", scope_key=None, phone_number_id=None)
            got.append(r.reason)
        finally:
            s.close()
    ts = [threading.Thread(target=claim, args=(r,)) for r in rows]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return got


# ── 1-12 ────────────────────────────────────────────────────────────────


def test_01_sent_then_resume_never_calls_meta_again(dbf, fake_meta):
    ids = _seed(dbf, phones=[X])
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    _dispatch(dbf, ids, times=3)                 # plain resumes
    _requeue(dbf, ids.campaign_id, X)            # a resume that re-queued the row
    _dispatch(dbf, ids, times=3)
    assert meta.calls == [X]
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_PRIOR_SEND, "attempt_accepted")


def test_02_accepted_without_receipt_is_never_resent(dbf, fake_meta):
    ids, meta = _scenario_attempt(dbf, fake_meta, "accepted")
    assert meta.calls == [X]
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_PRIOR_SEND, "attempt_accepted")


def test_03_delivered_is_never_resent(dbf, fake_meta):
    ids, meta = _scenario_attempt(dbf, fake_meta, "delivered")
    assert meta.calls == [X]
    assert _row(dbf, ids.campaign_id, X)[1] == "attempt_delivered"


def test_04_read_is_never_resent(dbf, fake_meta):
    ids, meta = _scenario_attempt(dbf, fake_meta, "read")
    assert meta.calls == [X]
    assert _row(dbf, ids.campaign_id, X)[1] == "attempt_read"


def test_05_uncertain_is_never_resent_automatically(dbf, fake_meta):
    ids, meta = _scenario_attempt(dbf, fake_meta, "uncertain")
    assert meta.calls == [X]
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_PRIOR_SEND, "attempt_uncertain")


@pytest.mark.parametrize("recovered", [False, True])
def test_06_request_started_then_crash_is_never_resent_blind(dbf, fake_meta, recovered):
    ids = _seed(dbf, phones=[X])
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="dead")
    rid = db.query(CampaignSendLog.id).filter(CampaignSendLog.campaign_id == ids.campaign_id).scalar()
    att = ledger.claim_recipient(db, log_id=rid, campaign=db.get(Campaign, ids.campaign_id),
                                 owner="dead", scope_key=None, phone_number_id=None).attempt
    ledger.mark_request_started(db, att)         # the request left; then the worker died
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.expires_at = _now() - timedelta(seconds=1)
    db.commit()
    db.close()
    if recovered:
        _ledger_suite._age(dbf, CampaignSendAttempt, 600, campaign_id=ids.campaign_id)
    else:
        _requeue(dbf, ids.campaign_id, X)        # someone re-queued it before recovery
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids, times=3)
    assert meta.calls == []
    status, reason = _row(dbf, ids.campaign_id, X)
    assert status in (ledger.LOG_UNCERTAIN, ledger.LOG_SKIPPED_PRIOR_SEND)
    if not recovered:
        assert reason == "attempt_request_started"


def test_07_a_proven_pre_accept_rejection_may_be_retried(dbf, fake_meta):
    ids = _seed(dbf, phones=[X])
    meta = fake_meta(FakeMeta({X: ["reject:130429:Rate limit hit"]}))
    _dispatch(dbf, ids)
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 1
    db.commit()
    db.close()
    _dispatch(dbf, ids, times=2)
    assert meta.calls == [X, X]                  # the rejection, then exactly one accepted copy
    assert [a[2] for a in _attempts(dbf, ids.campaign_id)] == [
        ledger.ATTEMPT_REJECTED, ledger.ATTEMPT_ACCEPTED]
    assert _row(dbf, ids.campaign_id, X)[0] == "sent"


def test_08_an_untouched_queued_recipient_is_sent_exactly_once(dbf, fake_meta):
    ids = _seed(dbf, phones=[X, Y])
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids, times=4)
    assert sorted(meta.calls) == sorted([X, Y])
    assert {s for s, _, _ in _logs(dbf, ids.campaign_id).values()} == {"sent"}


@pytest.mark.parametrize("other", ["queued", "sent_before"])
def test_09_one_number_in_two_formats_is_one_recipient(dbf, fake_meta, other):
    if other == "sent_before":
        ids, meta = _scenario_duplicate_row(dbf, fake_meta)
        assert meta.calls == []
        assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_PRIOR_SEND, "row_accepted")
        return
    ids = _seed(dbf, phones=[X])
    db = dbf()
    db.add(CampaignSendLog(tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                           customer_phone_e164=X.lstrip("+"), template_name="generic_offer",
                           template_language="ar", status="queued"))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids, times=3)
    assert len(meta.calls) == 1
    statuses = sorted(s for s, _, _ in _logs(dbf, ids.campaign_id).values())
    assert statuses == sorted(["sent", ledger.LOG_SKIPPED_PRIOR_SEND])


@pytest.mark.parametrize("lease_serialises", [True, False], ids=["with_lease", "lock_alone"])
def test_10_two_workers_claiming_one_recipient_concurrently(dbf, fake_meta, monkeypatch,
                                                            lease_serialises):
    """Both rows of one number claimed at the same instant: exactly one claim."""
    got = _race_duplicate_rows(dbf, fake_meta, monkeypatch, lease_serialises=lease_serialises)
    assert sorted(got) == sorted(["claimed", ledger.PRIOR_SEND_ERROR])


def test_10b_two_dispatchers_at_once_send_each_recipient_once(dbf, fake_meta):
    ids = _seed(dbf, phones=[X, Y])
    meta = fake_meta(FakeMeta(delay=0.01))

    async def both():
        return await asyncio.gather(_run(dbf, ids, _conn()), _run(dbf, ids, _conn()))
    asyncio.run(both())
    assert sorted(meta.calls) == sorted([X, Y])


def test_11_other_campaigns_restarts_and_resume_loops_never_add_a_copy(dbf, fake_meta):
    first = _seed(dbf, phones=[X])
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, first)
    db = dbf()
    from models import Tenant
    tenant = db.get(Tenant, first.tenant_id)
    db.close()
    second = _seed(dbf, phones=[X, Y], campaign_name="حملة ثانية", tenant=tenant)
    db = dbf()
    disp._apply_frequency_cap(db, second.tenant_id, second.campaign_id)   # the cross-campaign contract
    db.commit()
    db.close()
    for _ in range(5):                                                     # resume loop
        _dispatch(dbf, second)
    db = dbf()                                                             # a restart mid-way
    lease = db.get(CampaignDispatchLease, second.campaign_id)
    lease.owner, lease.expires_at = "dead", _now() - timedelta(seconds=1)
    db.commit()
    db.close()
    _dispatch(dbf, second, times=2)
    _dispatch(dbf, first, times=2)
    assert sorted(meta.calls) == sorted([X, Y])                            # X once overall, Y once


@pytest.mark.parametrize("source", ["message_events", "delivery_event"])
def test_12_history_outside_the_row_blocks_a_queued_looking_row(dbf, fake_meta, source):
    scenario = _scenario_message_events if source == "message_events" else _scenario_delivery_event
    ids, meta = scenario(dbf, fake_meta)
    assert meta.calls == []
    assert _row(dbf, ids.campaign_id, X) == (
        ledger.LOG_SKIPPED_PRIOR_SEND,
        "message_events_copy" if source == "message_events" else "delivery_event_delivered")


def test_legacy_attempt_without_proof_blocks(dbf, fake_meta):
    ids, meta = _scenario_legacy_count(dbf, fake_meta)
    assert meta.calls == []
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_PRIOR_SEND, "legacy_attempt_unproven")


# ── Negative controls: remove one guard, its scenario duplicates ─────────


@pytest.mark.parametrize("guard, scenario", [
    ("_check_attempts", lambda S, fm: _scenario_attempt(S, fm, "accepted")),
    ("_check_attempts", lambda S, fm: _scenario_attempt(S, fm, "read")),
    ("_check_attempts", lambda S, fm: _scenario_attempt(S, fm, "uncertain")),
    ("_check_rows", _scenario_duplicate_row),
    ("_check_rows", _scenario_legacy_count),
    ("_check_message_events", _scenario_message_events),
    ("_check_delivery_events", _scenario_delivery_event),
], ids=["attempts-accepted", "attempts-read", "attempts-uncertain", "rows-duplicate",
        "rows-legacy-count", "message-events", "delivery-events"])
def test_negative_control_each_guard_owns_its_duplicate(dbf, fake_meta, monkeypatch, guard,
                                                         scenario):
    _without(monkeypatch, guard)
    _ids, meta = scenario(dbf, fake_meta)
    # The attempts scenarios made the first copy through Meta themselves; the
    # others seeded it as history. Either way one more call is a duplicate.
    copies_before = 1 if guard == "_check_attempts" else 0
    assert meta.calls.count(X) > copies_before


def test_negative_control_without_the_recipient_lock_both_rows_claim(request, dbf, fake_meta,
                                                                    monkeypatch):
    if "postgres" not in request.node.callspec.id:
        pytest.skip("SQLite serialises writers itself; the advisory lock is PostgreSQL's")
    _without(monkeypatch, "lock")
    got = _race_duplicate_rows(dbf, fake_meta, monkeypatch, lease_serialises=False)
    assert got == ["claimed", "claimed"]
