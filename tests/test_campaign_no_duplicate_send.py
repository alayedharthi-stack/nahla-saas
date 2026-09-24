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


# Equivalent spellings of X the platform's identity recognises.
SPELLINGS = [X, X.lstrip("+"), "00" + X.lstrip("+"), "0" + X[4:], "+966 50 000 1111"]
PAIRS = [(a, b) for i, a in enumerate(SPELLINGS) for b in SPELLINGS[i + 1:]]
PAIRS = PAIRS + [(b, a) for a, b in PAIRS]                     # both send orders


def _seed_rows(Session, spellings, statuses=None):
    """One campaign whose rows are ``spellings`` of one recipient, in that
    order (the dispatcher claims in id order)."""
    ids = _seed(Session, phones=[spellings[0]])
    db = Session()
    for i, sp in enumerate(spellings[1:], 1):
        st = (statuses or {}).get(i, "queued")
        db.add(CampaignSendLog(tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                               customer_phone_e164=sp, template_name="generic_offer",
                               template_language="ar", status=st))
        db.commit()
    db.close()
    return ids


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


def _race_duplicate_rows(Session, fake_meta, monkeypatch, *, lease_serialises=True,
                         spellings=(X, X.lstrip("+"))):
    """Two sessions of the lease holder claim the two rows of one number at
    the same instant; the evidence read is slowed so both would pass it
    unserialised. The lease heartbeat's row lock already serialises claims
    of one campaign; ``lease_serialises=False`` takes it away so the
    per-recipient lock is tested on its own."""
    ids = _seed_rows(Session, spellings)
    db = Session()
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
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_SEND_GUARD, "attempt_accepted")


def test_02_accepted_without_receipt_is_never_resent(dbf, fake_meta):
    ids, meta = _scenario_attempt(dbf, fake_meta, "accepted")
    assert meta.calls == [X]
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_SEND_GUARD, "attempt_accepted")


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
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_SEND_GUARD, "attempt_uncertain")


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
    assert status in (ledger.LOG_UNCERTAIN, ledger.LOG_SKIPPED_SEND_GUARD)
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


@pytest.mark.parametrize("first, second", PAIRS, ids=[f"{a}|{b}" for a, b in PAIRS])
def test_09_one_number_in_any_two_spellings_is_one_recipient(dbf, fake_meta, first, second):
    """Both rows queued, in either order: exactly one copy, the other row
    refused by the guard."""
    ids = _seed_rows(dbf, [first, second])
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids, times=3)
    assert meta.calls == [first]
    logs = _logs(dbf, ids.campaign_id)
    assert logs[first][0] == "sent"
    assert logs[second][0] == ledger.LOG_SKIPPED_SEND_GUARD


def test_09b_a_pre_ledger_copy_on_another_spelling_blocks(dbf, fake_meta):
    ids, meta = _scenario_duplicate_row(dbf, fake_meta)
    assert meta.calls == []
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_SEND_GUARD, "row_accepted")


def test_09c_a_number_without_a_validated_identity_is_never_sent(dbf, fake_meta):
    ids = _seed_rows(dbf, [X, "+96650000111"])          # one digit short: nobody
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert meta.calls == [X]
    assert _row(dbf, ids.campaign_id, "+96650000111") == (
        ledger.LOG_SKIPPED_SEND_GUARD, "recipient_identity_unresolved")


RACE_PAIRS = [(X, "00" + X.lstrip("+")), ("0" + X[4:], X.lstrip("+")), ("+966 50 000 1111", X)]


@pytest.mark.parametrize("pair", RACE_PAIRS, ids=["+966|00966", "05|966", "spaced|+966"])
@pytest.mark.parametrize("lease_serialises", [True, False], ids=["with_lease", "lock_alone"])
def test_10_two_claims_on_two_spellings_at_once(dbf, fake_meta, monkeypatch, lease_serialises,
                                                pair):
    """Both rows of one recipient claimed at the same instant: exactly one."""
    got = _race_duplicate_rows(dbf, fake_meta, monkeypatch, lease_serialises=lease_serialises,
                               spellings=pair)
    assert sorted(got) == sorted(["claimed", ledger.PRIOR_SEND_ERROR])


@pytest.mark.parametrize("pair", RACE_PAIRS, ids=["+966|00966", "05|966", "spaced|+966"])
def test_10c_concurrent_dispatch_runs_over_two_spellings(dbf, fake_meta, pair):
    """Two real dispatch runs at once over a campaign whose recipient is
    stored twice: one copy."""
    ids = _seed_rows(dbf, list(pair))
    meta = fake_meta(FakeMeta(delay=0.05))

    async def both():
        return await asyncio.gather(_run(dbf, ids, _conn()), _run(dbf, ids, _conn()))
    asyncio.run(both())
    assert len(meta.calls) == 1


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
        ledger.LOG_SKIPPED_SEND_GUARD,
        "message_events_copy" if source == "message_events" else "delivery_event_delivered")


def test_legacy_attempt_without_proof_blocks(dbf, fake_meta):
    ids, meta = _scenario_legacy_count(dbf, fake_meta)
    assert meta.calls == []
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_SEND_GUARD, "legacy_attempt_unproven")


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
    got = _race_duplicate_rows(dbf, fake_meta, monkeypatch, lease_serialises=False,
                               spellings=RACE_PAIRS[0])
    assert got == ["claimed", "claimed"]


# ── Historical copies that are hard to place (F2) ────────────────────────

Z = "+966500003333"


def _copy(Session, ids, *, phone=None, wamid="w.hist", conversation=True, event_id=None, **flags):
    """An outbound campaign copy in message_events, linked to ``phone``'s
    customer through a conversation — or to nothing."""
    db = Session()
    conv_id = None
    if conversation and phone:
        cust = db.query(Customer).filter(Customer.tenant_id == ids.tenant_id,
                                         Customer.normalized_phone == phone).first()
        if cust is None:
            cust = Customer(tenant_id=ids.tenant_id, phone=phone, normalized_phone=phone,
                            name="نورة عبدالله", extra_metadata={})
            db.add(cust)
            db.commit()
        conv = Conversation(tenant_id=ids.tenant_id, customer_id=cust.id, status="active")
        db.add(conv)
        db.commit()
        conv_id = conv.id
    md = {"campaign_id": ids.campaign_id, **flags}
    if wamid:
        md["wa_message_id"] = wamid
    ev = MessageEvent(tenant_id=ids.tenant_id, conversation_id=conv_id, direction="outbound",
                      event_type="campaign", created_at=_now() - timedelta(days=1),
                      extra_metadata=md)
    if event_id is not None:
        ev.id = event_id
    db.add(ev)
    db.commit()
    db.close()


def test_a_copy_nobody_can_be_tied_to_pauses_the_campaign_with_rows_queued(dbf, fake_meta):
    """It could be anyone's: nobody is sent, and nobody is skipped for good —
    the run pauses and every row stays queued until it is resolved."""
    ids = _seed(dbf, phones=[X, Y])
    _copy(dbf, ids, phone=None, wamid="w.orphan", conversation=False)
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids, times=2)
    assert meta.calls == []
    assert {_row(dbf, ids.campaign_id, p)[0] for p in (X, Y)} == {"queued"}
    db = dbf()
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    assert (lease.pause_reason, lease.pause_detail) == (
        ledger.PAUSE_EVIDENCE_UNRESOLVED, "message_events_unplaceable_copy")
    db.close()


def test_a_copy_without_a_conversation_is_placed_by_its_wamid(dbf, fake_meta):
    """The copy's wamid is owned by X's earlier attempt: X is blocked, Y is
    not held hostage by an 'unplaceable' copy."""
    ids = _seed(dbf, phones=[X, Y])
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)                                  # X and Y accepted once each
    wamid_x = next(a[3] for a in _attempts(dbf, ids.campaign_id) if a[0] == X)
    _copy(dbf, ids, phone=None, wamid=wamid_x, conversation=False)
    third = _seed_rows(dbf, [Z])                          # a fresh campaign is unaffected
    _dispatch(dbf, third)
    assert Z in meta.calls


def test_a_conflicting_copy_blocks_every_candidate(dbf, fake_meta):
    """Linked to X's conversation but its wamid belongs to Y's row."""
    ids = _seed(dbf, phones=[X, Y, Z])
    db = dbf()
    y = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id,
                                         CampaignSendLog.customer_phone_e164 == Y).one()
    y.provider_message_id = "w.of-y"
    db.commit()
    db.close()
    _copy(dbf, ids, phone=X, wamid="w.of-y")
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert meta.calls == [Z]
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_SEND_GUARD, "message_events_conflict")


def test_a_copy_linked_only_by_an_unvalidated_spelling_blocks_conservatively(dbf, fake_meta):
    ids = _seed(dbf, phones=[X, Y])
    _copy(dbf, ids, phone="0000500001111")               # no identity; its tail is X's number
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert meta.calls == [Y]
    assert _row(dbf, ids.campaign_id, X) == (
        ledger.LOG_SKIPPED_SEND_GUARD, "message_events_possible_copy")


def test_the_evidence_cache_never_hides_a_copy_committed_later(dbf, fake_meta):
    """One session claims X (the index is built), another commits a copy for
    Y with a LOWER id than the newest copy, then the first session claims Y:
    refused. The fingerprint is (count, max id), so id order cannot hide it."""
    ids = _seed(dbf, phones=[X, Y])
    _copy(dbf, ids, phone=Z, wamid="w.z", event_id=500)   # someone else's copy, id 500
    s = dbf()
    ledger.acquire_lease(s, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w1")
    rows = dict((p, i) for i, p in s.query(CampaignSendLog.id, CampaignSendLog.customer_phone_e164)
                .filter(CampaignSendLog.campaign_id == ids.campaign_id))
    camp = s.get(Campaign, ids.campaign_id)
    r = ledger.claim_recipient(s, log_id=rows[X], campaign=camp, owner="w1", scope_key=None,
                               phone_number_id=None)
    assert r.reason == "claimed"
    _copy(dbf, ids, phone=Y, wamid="w.y-first", event_id=400)   # committed by another session
    r = ledger.claim_recipient(s, log_id=rows[Y], campaign=camp, owner="w1", scope_key=None,
                               phone_number_id=None)
    s.close()
    assert (r.reason, r.evidence) == (ledger.PRIOR_SEND_ERROR, "message_events_copy")


def test_an_unreadable_history_source_refuses_and_pauses(dbf, fake_meta):
    from sqlalchemy import text
    ids = _seed(dbf, phones=[X, Y])
    db = dbf()
    db.execute(text("DROP TABLE message_events" + (" CASCADE" if db.get_bind().dialect.name
                                                    == "postgresql" else "")))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    sent, failed, errors = asyncio.run(_run(dbf, ids, _conn()))
    assert meta.calls == []
    assert {s for s, _, _ in _logs(dbf, ids.campaign_id).values()} == {"queued"}
    db = dbf()
    assert db.get(CampaignDispatchLease, ids.campaign_id).pause_reason == \
        ledger.PAUSE_EVIDENCE_UNREADABLE
    db.close()


# ── Negative controls for the F2 / identity guards ──────────────────────


def test_negative_control_punctuation_stripping_is_not_an_identity(dbf, fake_meta, monkeypatch):
    """Replace the validated identity with bare digit stripping (the earlier
    head's rule): +966… and 00966… become two people and both are sent."""
    import services.recipient_identity as ri
    monkeypatch.setattr(ri, "canonical_recipient", lambda raw: ri.digits(raw) or None)
    ids = _seed_rows(dbf, [X, "00" + X.lstrip("+")])
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert len(meta.calls) == 2


def test_negative_control_dropping_unplaceable_copies_sends(dbf, fake_meta, monkeypatch):
    """The earlier head dropped copies it could not place."""
    import dataclasses
    real = ledger._build_copy_index
    monkeypatch.setattr(ledger, "_build_copy_index",
                        lambda *a, **k: dataclasses.replace(real(*a, **k), unplaceable=0))
    ids = _seed(dbf, phones=[X, Y])
    _copy(dbf, ids, phone=None, wamid="w.orphan", conversation=False)
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert sorted(meta.calls) == sorted([X, Y])


def test_negative_control_a_fail_open_read_sends(dbf, fake_meta, monkeypatch):
    from sqlalchemy import text
    real = ledger._check_message_events

    def fail_open(db, ctx):
        try:
            with db.begin_nested():
                return real(db, ctx)
        except Exception:  # noqa: BLE001 — the behaviour under test
            return None
    monkeypatch.setattr(ledger, "PRIOR_SEND_CHECKS", tuple(
        fail_open if c is real else c for c in ledger.PRIOR_SEND_CHECKS))
    ids = _seed(dbf, phones=[X])
    db = dbf()
    db.execute(text("DROP TABLE message_events" + (" CASCADE" if db.get_bind().dialect.name
                                                    == "postgresql" else "")))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert meta.calls == [X]


# ── Review round 3: partial attribution overlap, in-place changes ────────


def _claim_pair(Session, ids, first, second, between):
    """One session claims ``first`` (evidence read), ``between`` changes the
    database from another session, then the same session claims ``second``."""
    s = Session()
    ledger.acquire_lease(s, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w1")
    rows = dict((p, i) for i, p in s.query(CampaignSendLog.id, CampaignSendLog.customer_phone_e164)
                .filter(CampaignSendLog.campaign_id == ids.campaign_id))
    camp = s.get(Campaign, ids.campaign_id)
    r1 = ledger.claim_recipient(s, log_id=rows[first], campaign=camp, owner="w1", scope_key=None,
                                phone_number_id=None)
    between()
    r2 = ledger.claim_recipient(s, log_id=rows[second], campaign=camp, owner="w1", scope_key=None,
                                phone_number_id=None)
    s.close()
    return r1, r2


def _conv_for(Session, ids, phone, *, meta_phone=None):
    db = Session()
    cust = db.query(Customer).filter(Customer.tenant_id == ids.tenant_id,
                                     Customer.normalized_phone == phone).first()
    if cust is None:
        cust = Customer(tenant_id=ids.tenant_id, phone=phone, normalized_phone=phone,
                        name="أحمد سالم", extra_metadata={})
        db.add(cust)
        db.commit()
    conv = Conversation(tenant_id=ids.tenant_id, customer_id=cust.id, status="active",
                        extra_metadata={"customer_phone": meta_phone} if meta_phone else {})
    db.add(conv)
    db.commit()
    out = (conv.id, cust.id)
    db.close()
    return out


def _event_on(Session, ids, conv_id, wamid):
    db = Session()
    db.add(MessageEvent(tenant_id=ids.tenant_id, conversation_id=conv_id, direction="outbound",
                        event_type="campaign", created_at=_now() - timedelta(days=1),
                        extra_metadata={"campaign_id": ids.campaign_id, "wa_message_id": wamid}))
    db.commit()
    db.close()


def test_a_copy_naming_two_recipients_blocks_both_even_when_one_owns_it(dbf, fake_meta):
    """linked = {X (customer), Y (conversation.metadata.customer_phone)},
    owned = {X (X's row holds the wamid)}. The earlier head intersected the
    two sets, blocked X and let Y through."""
    ids = _seed(dbf, phones=[X, Y, Z])
    db = dbf()
    x = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id,
                                         CampaignSendLog.customer_phone_e164 == X).one()
    x.provider_message_id = "w.of-x"
    db.commit()
    db.close()
    conv_id, _ = _conv_for(dbf, ids, X, meta_phone=Y)
    _event_on(dbf, ids, conv_id, "w.of-x")
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert meta.calls == [Z]
    assert _row(dbf, ids.campaign_id, Y) == (ledger.LOG_SKIPPED_SEND_GUARD, "message_events_conflict")


def test_a_conversation_relinked_in_place_is_seen_by_the_next_claim(dbf, fake_meta):
    """The copy starts on Z's conversation; after the first claim the
    conversation is re-pointed to Y's customer (no row added, count and max
    id unchanged). Y's claim must see it."""
    ids = _seed(dbf, phones=[X, Y])
    conv_id, _ = _conv_for(dbf, ids, Z)
    _event_on(dbf, ids, conv_id, "w.moved")
    _, y_customer = _conv_for(dbf, ids, Y)

    def relink():
        db = dbf()
        db.get(Conversation, conv_id).customer_id = y_customer
        db.commit()
        db.close()
    r1, r2 = _claim_pair(dbf, ids, X, Y, relink)
    assert r1.reason == "claimed"
    assert (r2.reason, r2.evidence) == (ledger.PRIOR_SEND_ERROR, "message_events_copy")


def test_a_customer_phone_changed_in_place_is_seen_by_the_next_claim(dbf, fake_meta):
    ids = _seed(dbf, phones=[X, Y])
    conv_id, z_customer = _conv_for(dbf, ids, Z)
    _event_on(dbf, ids, conv_id, "w.renumbered")

    def renumber():
        db = dbf()
        c = db.get(Customer, z_customer)
        c.phone = c.normalized_phone = Y.lstrip("+")          # Y, in another spelling
        db.commit()
        db.close()
    r1, r2 = _claim_pair(dbf, ids, X, Y, renumber)
    assert r1.reason == "claimed"
    assert r2.reason == ledger.PRIOR_SEND_ERROR


def _fingerprint_cache(monkeypatch):
    """The previous head's cache: reuse the index while (count, max id) of
    the campaign's copies is unchanged."""
    from sqlalchemy import text
    real = ledger._build_copy_index
    cache = {}

    def cached(db, ctx):
        fp = tuple(db.execute(text(
            "SELECT count(*), coalesce(max(id), 0) FROM message_events WHERE tenant_id = :t"),
            {"t": ctx["tenant_id"]}).one())
        if cache.get("fp") != fp:
            cache.update(fp=fp, idx=real(db, ctx))
        return cache["idx"]
    monkeypatch.setattr(ledger, "_build_copy_index", cached)


@pytest.mark.parametrize("change", ["relink", "renumber"])
def test_negative_control_a_count_max_id_cache_hides_in_place_changes(dbf, fake_meta, monkeypatch,
                                                                      change):
    _fingerprint_cache(monkeypatch)
    ids = _seed(dbf, phones=[X, Y])
    conv_id, z_customer = _conv_for(dbf, ids, Z)
    _event_on(dbf, ids, conv_id, "w.hidden")
    _, y_customer = _conv_for(dbf, ids, Y)

    def mutate():
        db = dbf()
        if change == "relink":
            db.get(Conversation, conv_id).customer_id = y_customer
        else:
            c = db.get(Customer, z_customer)
            c.phone = c.normalized_phone = Y.lstrip("+")
        db.commit()
        db.close()
    _, r2 = _claim_pair(dbf, ids, X, Y, mutate)
    assert r2.reason == "claimed"                          # the hidden copy: a duplicate


# ── Independent review: A1 non-ASCII digits, A2 event's own recipient, A3 ──


@pytest.mark.parametrize("spelling", ["٠٥٠٠٠٠١١١١", "+٩٦٦٥٠٠٠٠١١١١", "۰۵۰۰۰۰۱۱۱۱",
                                      "+966５０００٠١١١١"],
                         ids=["arabic-indic-local", "arabic-indic-intl", "persian", "mixed-fullwidth"])
def test_a_sent_copy_stored_in_non_ascii_digits_blocks(dbf, fake_meta, spelling):
    """The identity reads Arabic-Indic / Persian / fullwidth digits; the SQL
    prefilter used to drop them before the identity ever saw them."""
    from services.recipient_identity import canonical_recipient
    assert canonical_recipient(spelling) == X
    ids = _seed_rows(dbf, [X, spelling], statuses={1: "sent"})
    db = dbf()
    other = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id,
                                             CampaignSendLog.customer_phone_e164 == spelling).one()
    other.provider_message_id, other.sent_at, other.attempt_count = "w.legacy", _now(), 1
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert meta.calls == []
    assert _row(dbf, ids.campaign_id, X) == (ledger.LOG_SKIPPED_SEND_GUARD, "row_accepted")


def test_a_copys_own_recorded_recipient_places_it(dbf, fake_meta):
    """No wamid; the conversation is Y's; the event itself records X as the
    number it was sent to. X must not be sent (it is a conflict with Y)."""
    ids = _seed(dbf, phones=[X, Y, Z])
    conv_id, _ = _conv_for(dbf, ids, Y)
    db = dbf()
    db.add(MessageEvent(tenant_id=ids.tenant_id, conversation_id=conv_id, direction="outbound",
                        event_type="campaign", created_at=_now() - timedelta(days=1),
                        extra_metadata={"campaign_id": ids.campaign_id, "customer_phone": X,
                                        "phone": X}))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    _dispatch(dbf, ids)
    assert meta.calls == [Z]
    assert _row(dbf, ids.campaign_id, X)[1] == "message_events_conflict"


def test_any_error_while_reading_evidence_rolls_back_and_pauses(dbf, fake_meta, monkeypatch):
    """Not only database errors: nothing half-written (no 'sending' row
    without an attempt), no send, the run pauses."""
    def broken(db, ctx):
        raise AttributeError("'str' object has no attribute 'get'")
    monkeypatch.setattr(ledger, "PRIOR_SEND_CHECKS", (broken,))
    ids = _seed(dbf, phones=[X])
    meta = fake_meta(FakeMeta())
    asyncio.run(_run(dbf, ids, _conn()))
    assert meta.calls == []
    db = dbf()
    row = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id).one()
    assert (row.status, int(row.attempt_count or 0)) == ("queued", 0)
    assert db.query(CampaignSendAttempt).count() == 0
    assert db.get(CampaignDispatchLease, ids.campaign_id).pause_reason == \
        ledger.PAUSE_EVIDENCE_UNREADABLE
    db.close()
