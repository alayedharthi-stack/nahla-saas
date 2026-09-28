"""Start-once campaigns: a campaign started once completes on its own.

RCA (campaign 35 / tenant 33, 24–27 September 2026): Meta's 131049 ("not
delivered to maintain healthy ecosystem engagement") is the RECIPIENT's
marketing-message limit, yet it tripped a sender-scope breaker (25 in 15
minutes) with a cooldown that grew to 24h. Every continuation sent 100–180
recipients and stopped again. This suite pins the corrected semantics:

* 131049 is a final, per-recipient outcome (never retried) and never a
  reason to stop sending to anyone else;
* the sender-scope breakers (spam 131048, per-minute rate limit) still stop
  a run and still need the merchant / a timed backoff respectively;
* a run whose worker died (deploy, crash) is continued by the scheduler
  through the same atomic claims — nobody is sent twice — with a bounded
  backoff and a review pause when it keeps dying;
* a pause the retired breaker left behind is shown as "needs review" and
  is never continued automatically (the offer may be stale);
* an expired offer stops new claims; the merchant edits the content /
  expiry (recorded as a revision) and resumes explicitly; the new content
  reaches only recipients not yet sent;
* consent is re-read at send time; button taps count once, never
  retroactively, and only where WhatsApp reports them.

Generic merchant, scripted Meta (no real sends), SQLite and PostgreSQL.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import pytest

import test_campaign_capacity_continuation as cap
import test_campaign_send_ledger as ledger_suite
import test_campaign_throttle_state as base
from models import (
    Campaign, CampaignContentRevision, CampaignDispatchLease, CampaignSendAttempt,
    CampaignSendLog, Customer, WhatsAppTemplate,
)
from services import campaign_dispatcher as disp
from services import campaign_send_ledger as ledger

dbf = ledger_suite.dbf
fake_meta = ledger_suite.fake_meta
_fast = ledger_suite._fast
world = cap.world
PHONES = base.PHONES


@pytest.fixture(autouse=True)
def pg_target(monkeypatch):
    target = os.environ.get("NAHLA_CAMPAIGN_LEDGER_PG_DSN") or os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN")
    if target:
        monkeypatch.setattr(ledger_suite, "PG_DSN", target)


def _now():
    return base._now()


def _recover(Session):
    db = Session()
    try:
        return asyncio.run(disp.resume_stalled_campaigns(db))
    finally:
        db.close()


def _accepted_phones(Session, cid):
    db = Session()
    try:
        return sorted(a.customer_phone_e164 for a in db.query(CampaignSendAttempt)
                      .filter(CampaignSendAttempt.campaign_id == cid,
                              CampaignSendAttempt.state == ledger.ATTEMPT_ACCEPTED))
    finally:
        db.close()


def _dead_worker(Session, cid, *, minutes_ago=5):
    """A worker that stopped renewing its lease (deploy / crash)."""
    db = Session()
    lease = db.get(CampaignDispatchLease, cid)
    if lease is None:
        c = db.get(Campaign, cid)
        lease = CampaignDispatchLease(campaign_id=cid, tenant_id=c.tenant_id)
        db.add(lease)
    lease.owner = "dead-worker"
    lease.acquired_at = _now() - timedelta(minutes=minutes_ago + 2)
    lease.heartbeat_at = _now() - timedelta(minutes=minutes_ago)
    lease.expires_at = _now() - timedelta(minutes=minutes_ago)
    lease.stop_requested_at = None
    db.commit()
    db.close()


def _already_sent(Session, ids, phones):
    """Recipients an earlier (dead) run accepted before it died."""
    db = Session()
    now = _now()
    for i, ph in enumerate(phones):
        row = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id,
                                               CampaignSendLog.customer_phone_e164 == ph).one()
        row.status, row.sent_at, row.attempt_count = "sent", now, 1
        row.provider_message_id = f"w.earlier.{i}"
        db.flush()
        db.add(CampaignSendAttempt(
            tenant_id=ids.tenant_id, campaign_id=ids.campaign_id, send_log_id=row.id,
            customer_phone_e164=ph, attempt_no=1, state="accepted", messaging_scope_key=base.SCOPE,
            provider_message_id=f"w.earlier.{i}", claimed_at=now, request_started_at=now,
            accepted_at=now, created_at=now, updated_at=now, worker_id="dead-worker"))
    db.commit()
    db.close()


def _router(monkeypatch, ids, Session):
    import routers.campaigns as rc
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request, db=None: ids.tenant_id)
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", lambda cid: None)
    monkeypatch.setattr(disp, "_get_wa_connection",
                        lambda db, tenant_id: ledger_suite._conn(tenant_id=tenant_id))
    return rc


# ── 1. 131049 is the recipient's limit, not ours ─────────────────────────


def test_131049_history_never_stops_a_run(dbf, fake_meta, world):
    """What production did 12 times: 25 marketing_blocked receipts inside
    15 minutes paused the run before its first recipient. Now they are
    final outcomes for those 25 people and the queue is sent in full."""
    ids = base._seed(dbf, phones=PHONES[:3])
    base._post_accept_failures(dbf, ids, 25)
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted(PHONES[:3])
    assert base._status(dbf, ids.campaign_id) == "completed"
    assert base._lease(dbf, ids.campaign_id).reason is None
    payload = cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "sent"
    assert payload["stats"]["recipients_marketing_limit"] == 25
    assert payload["stats"]["reached"] == 0                  # acceptance is not reach


def test_131049_receipts_arriving_during_the_run_do_not_stop_it(dbf, fake_meta, world):
    """Every accepted copy is reported 131049 while the run is still going:
    the run reaches every queued recipient exactly once."""
    ids = base._seed(dbf, phones=PHONES)                  # 40 recipients > old threshold 25

    async def fail_after_accept(phone, wamid):
        db = dbf()
        try:
            ledger.apply_status_event(
                db, wamid=wamid, status="failed", provider_timestamp=None,
                errors=[{"code": 131049, "title": "This message was not delivered to maintain "
                                                  "healthy ecosystem engagement."}],
                store_if_unmatched=True)
        finally:
            db.close()

    meta = fake_meta(base.FakeMeta(before_return=fail_after_accept))
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted(PHONES)
    assert base._status(dbf, ids.campaign_id) == "completed"
    db = dbf()
    limited = db.query(CampaignSendAttempt).filter(
        CampaignSendAttempt.post_accept_error_code == "marketing_blocked").count()
    db.close()
    assert limited == len(PHONES)
    # Nothing to retry: these recipients reached their own limit.
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 0
    db.close()
    assert world.resume(dbf) == []


def test_spam_block_still_stops_the_sender_and_needs_the_merchant(dbf, fake_meta, world):
    ids = base._seed(dbf, phones=PHONES[:3])
    base._post_accept_failures(dbf, ids, 5, key="spam_rate_limit")
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert meta.calls == []
    assert base._lease(dbf, ids.campaign_id).reason == ledger.PAUSE_PROVIDER_THROTTLING
    assert cap._lifecycle(dbf, ids.campaign_id)["lifecycle"] == "provider_throttled"
    base._age_failures(dbf, 20)
    assert [a["action"] for a in world.resume(dbf)] in ([], ["needs_merchant_resume"])
    assert meta.calls == []


# ── 2. The pause the retired breaker left behind (campaign 35's state) ────


def _legacy_breaker_pause(Session, ids):
    db = Session()
    c = db.get(Campaign, ids.campaign_id)
    c.status = "paused"
    ledger.store_capacity_wait(c, {
        "reason": "provider_throttling", "authority": ledger.CAPACITY_WAIT_AUTHORITY,
        "error_key": "marketing_blocked", "continuation": "untouched_recipients_only",
        "since": (_now() - timedelta(hours=30)).isoformat(),
        "next_eligible_at": (_now() - timedelta(hours=1)).isoformat(),
        "next_eligible_exact": False, "attempt": 8})
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    if lease is None:
        lease = CampaignDispatchLease(campaign_id=ids.campaign_id, tenant_id=ids.tenant_id)
        db.add(lease)
    lease.owner, lease.expires_at = None, None
    lease.pause_reason = ledger.PAUSE_PROVIDER_THROTTLING
    lease.pause_detail = "post_accept marketing_blocked x25 in 15m clears_at=…"
    lease.paused_at = _now() - timedelta(hours=25)
    lease.stop_requested_at = None
    db.commit()
    db.close()


def test_legacy_131049_pause_needs_review_and_is_never_auto_resumed(dbf, fake_meta, world, monkeypatch):
    ids = base._seed(dbf, phones=PHONES[:3])
    _legacy_breaker_pause(dbf, ids)
    meta = fake_meta(base.FakeMeta())
    for _ in range(3):
        # Not even a candidate: provider_throttling is no longer an
        # automatic reason, and a paused campaign is never "stalled".
        assert world.resume(dbf) == []
        assert _recover(dbf) == []
    assert meta.calls == []
    payload = cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "needs_review"
    assert payload["status_explanation"]["tone"] == "action"
    assert payload["status_explanation"]["auto_resume"] is False
    # The merchant's explicit resume continues it (the old 15-minute window
    # is not consulted for 131049 any more).
    rc = _router(monkeypatch, ids, dbf)
    db = dbf()
    asyncio.run(rc.update_campaign_status(
        ids.campaign_id, rc.UpdateCampaignStatusIn(status="active"), request=None, db=db))
    db.close()
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted(PHONES[:3])
    assert base._status(dbf, ids.campaign_id) == "completed"


# ── 3. A worker that died mid-run is continued automatically ─────────────


def test_stalled_run_is_recovered_without_the_merchant_and_without_duplicates(dbf, fake_meta, world):
    ids = base._seed(dbf, phones=PHONES[:6])
    _already_sent(dbf, ids, PHONES[:2])
    # A request that had left the process when the worker died: uncertain.
    db = dbf()
    row = db.query(CampaignSendLog).filter(CampaignSendLog.customer_phone_e164 == PHONES[2]).one()
    row.status, row.attempt_count = "sending", 1
    db.flush()
    t = _now() - timedelta(minutes=6)
    db.add(CampaignSendAttempt(
        tenant_id=ids.tenant_id, campaign_id=ids.campaign_id, send_log_id=row.id,
        customer_phone_e164=PHONES[2], attempt_no=1, state="request_started",
        messaging_scope_key=base.SCOPE, claimed_at=t, request_started_at=t,
        created_at=t, updated_at=t, worker_id="dead-worker"))
    db.commit()
    db.close()
    _dead_worker(dbf, ids.campaign_id)
    assert cap._lifecycle(dbf, ids.campaign_id)["lifecycle"] == "stalled"
    meta = fake_meta(base.FakeMeta())
    acts = _recover(dbf)
    assert acts[0]["action"] == "recovered" and acts[0]["attempt"] == 1
    # Only the 3 untouched recipients were sent; the 2 accepted earlier and
    # the uncertain one were not.
    assert sorted(meta.calls) == sorted(PHONES[3:6])
    assert _accepted_phones(dbf, ids.campaign_id) == sorted(PHONES[:2] + PHONES[3:6])
    logs = base._logs(dbf, ids.campaign_id)
    assert logs[PHONES[2]][0] == "uncertain"
    # Every queued recipient is done → the campaign is paused for the one
    # unknown outcome, never "completed" with a lie, and the recovery
    # record is cleared for next time.
    db = dbf()
    c = db.get(Campaign, ids.campaign_id)
    assert ledger.stall_recovery(c) is None
    db.close()
    assert base._status(dbf, ids.campaign_id) in ("completed", "paused")
    assert _recover(dbf) == []


def test_stall_recovery_respects_a_merchant_stop_and_a_live_worker(dbf, fake_meta, world):
    ids = base._seed(dbf, phones=PHONES[:3])
    _dead_worker(dbf, ids.campaign_id)
    meta = fake_meta(base.FakeMeta())
    db = dbf()
    ledger.request_stop(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id)
    db.commit()
    db.close()
    assert _recover(dbf)[0]["action"] == "stop_requested"
    db = dbf()
    ledger.clear_stop(db, campaign_id=ids.campaign_id)
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.owner, lease.expires_at = "live-worker", _now() + timedelta(minutes=5)
    db.commit()
    db.close()
    assert _recover(dbf)[0]["action"] == "worker_running"
    assert meta.calls == []


def test_a_lease_that_just_expired_is_not_yet_stalled(dbf, fake_meta, world):
    ids = base._seed(dbf, phones=PHONES[:3])
    _dead_worker(dbf, ids.campaign_id, minutes_ago=0)
    fake_meta(base.FakeMeta())
    assert _recover(dbf)[0]["action"] == "too_recent"


def test_stall_recovery_backs_off_and_finally_asks_for_review(dbf, fake_meta, world):
    ids = base._seed(dbf, phones=PHONES[:3])
    _dead_worker(dbf, ids.campaign_id)
    meta = fake_meta(base.FakeMeta())
    db = dbf()
    c = db.get(Campaign, ids.campaign_id)
    rec = ledger.record_stall_recovery(c)                   # attempt 1 just happened
    db.commit()
    db.close()
    assert _recover(dbf)[0]["action"] == "waiting"           # inside the 1-minute backoff
    assert meta.calls == []
    db = dbf()
    c = db.get(Campaign, ids.campaign_id)
    tv = dict(c.template_variables)
    tv[ledger.STALL_RECOVERY_KEY] = dict(rec, attempt=ledger.STALL_RECOVERY_MAX_ATTEMPTS,
                                         next_eligible_at=(_now() - timedelta(seconds=1)).isoformat())
    c.template_variables = tv
    db.commit()
    db.close()
    assert _recover(dbf)[0]["action"] == "gave_up"
    assert meta.calls == []
    assert base._status(dbf, ids.campaign_id) == "paused"
    assert base._lease(dbf, ids.campaign_id).reason == ledger.PAUSE_STALLED_REPEATEDLY
    payload = cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "needs_review" and payload["status_explanation"]["tone"] == "action"


def test_backoff_delays_double_and_are_capped():
    c = Campaign(id=1, tenant_id=1, name="x", status="active", template_variables={})
    delays = []
    for _ in range(12):
        w = ledger.record_stall_recovery(c)
        delays.append(datetime.fromisoformat(w["next_eligible_at"]) - datetime.fromisoformat(w["last_recovered_at"]))
    assert delays[0] == ledger.STALL_RECOVERY_BASE and delays[1] == 2 * ledger.STALL_RECOVERY_BASE
    assert max(delays) == ledger.STALL_RECOVERY_MAX


# ── 4. An offer that ended: stop, edit, continue the remainder ────────────


def test_expired_offer_pauses_new_sends_until_the_merchant_updates_it(dbf, fake_meta, world, monkeypatch):
    ids = base._seed(dbf, phones=PHONES[:4])
    _already_sent(dbf, ids, PHONES[:1])
    db = dbf()
    db.get(Campaign, ids.campaign_id).offer_expires_at = _now() - timedelta(hours=1)
    db.commit()
    db.close()
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert meta.calls == []
    assert base._status(dbf, ids.campaign_id) == "paused"
    assert base._lease(dbf, ids.campaign_id).reason == ledger.PAUSE_OFFER_EXPIRED
    payload = cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "offer_expired" and payload["status_explanation"]["tone"] == "action"
    assert world.resume(dbf) == [] and _recover(dbf) == []

    rc = _router(monkeypatch, ids, dbf)
    from fastapi import HTTPException
    db = dbf()
    with pytest.raises(HTTPException) as err:
        asyncio.run(rc.update_campaign_status(
            ids.campaign_id, rc.UpdateCampaignStatusIn(status="active"), request=None, db=db))
    assert err.value.status_code == 409 and err.value.detail["error"] == "offer_expired"
    res = asyncio.run(rc.dispatch_campaign_now(ids.campaign_id, request=None, db=db,
                                               bypass_frequency_cap=None))
    assert (res["ok"], res["reason"]) == (False, "offer_expired")
    db.close()
    assert meta.calls == []

    # The merchant extends the offer and changes the coupon: revision 2.
    db = dbf()
    out = asyncio.run(rc.update_campaign_content(
        ids.campaign_id,
        rc.UpdateCampaignContentIn(coupon_code="EID20", note="مدّدنا العرض",
                                   offer_expires_at=(_now() + timedelta(days=2)).isoformat()),
        request=None, db=db))
    db.close()
    assert out["content_revision"] == 2 and out["revision"]["revision_no"] == 2
    assert out["lifecycle"] == "paused"                     # resuming is explicit
    db = dbf()
    revs = db.query(CampaignContentRevision).filter(
        CampaignContentRevision.campaign_id == ids.campaign_id).order_by(
        CampaignContentRevision.revision_no).all()
    assert [r.revision_no for r in revs] == [1, 2]
    assert revs[0].coupon_code is None and revs[1].coupon_code == "EID20"
    asyncio.run(rc.update_campaign_status(
        ids.campaign_id, rc.UpdateCampaignStatusIn(status="active"), request=None, db=db))
    db.close()
    world.dispatch(dbf, ids.campaign_id)
    # Only the 3 not yet sent get the new content; the one sent earlier is untouched.
    assert sorted(meta.calls) == sorted(PHONES[1:4])
    assert base._status(dbf, ids.campaign_id) == "completed"
    db = dbf()
    rev_by_phone = {a.customer_phone_e164: a.content_revision for a in db.query(CampaignSendAttempt)}
    db.close()
    assert rev_by_phone[PHONES[0]] is None and all(rev_by_phone[p] == 2 for p in PHONES[1:4])
    db = dbf()
    report = asyncio.run(rc.list_campaign_revisions(ids.campaign_id, request=None, db=db))
    db.close()
    sends = {r["revision_no"]: r["sends"]["accepted"] for r in report["revisions"]}
    assert sends == {1: 1, 2: 3}


def test_content_edit_is_refused_while_a_run_is_live_or_in_flight(dbf, fake_meta, world, monkeypatch):
    ids = base._seed(dbf, phones=PHONES[:2])
    rc = _router(monkeypatch, ids, dbf)
    from fastapi import HTTPException
    db = dbf()
    with pytest.raises(HTTPException) as err:            # status active
        asyncio.run(rc.update_campaign_content(
            ids.campaign_id, rc.UpdateCampaignContentIn(coupon_code="X1"), request=None, db=db))
    assert err.value.status_code == 409 and err.value.detail["error"] == "campaign_active"
    db.get(Campaign, ids.campaign_id).status = "paused"
    db.commit()
    lease = CampaignDispatchLease(campaign_id=ids.campaign_id, tenant_id=ids.tenant_id,
                                  owner="w", expires_at=_now() + timedelta(minutes=2))
    db.add(lease)
    db.commit()
    with pytest.raises(HTTPException) as err:            # live lease
        asyncio.run(rc.update_campaign_content(
            ids.campaign_id, rc.UpdateCampaignContentIn(coupon_code="X1"), request=None, db=db))
    assert err.value.detail["error"] == "worker_running"
    with pytest.raises(HTTPException) as err:            # nothing changed
        lease.owner, lease.expires_at = None, None
        db.commit()
        asyncio.run(rc.update_campaign_content(
            ids.campaign_id, rc.UpdateCampaignContentIn(), request=None, db=db))
    assert err.value.status_code == 422
    db.close()


def test_expiry_reached_while_waiting_for_capacity_stops_instead_of_resuming(dbf, fake_meta, world):
    world.budget(1)
    ids = base._seed(dbf, phones=PHONES[:3])
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert len(meta.calls) == 1 and base._lease(dbf, ids.campaign_id).reason == ledger.PAUSE_MESSAGING_LIMIT
    db = dbf()
    db.get(Campaign, ids.campaign_id).offer_expires_at = _now() - timedelta(minutes=1)
    db.commit()
    db.close()
    cap._age_window(dbf)
    assert world.resume(dbf)[0]["action"] == "offer_expired"
    assert len(meta.calls) == 1
    assert cap._lifecycle(dbf, ids.campaign_id)["lifecycle"] == "offer_expired"


# ── 5. Consent is re-read at send time ───────────────────────────────────


def test_a_customer_who_unsubscribed_after_the_snapshot_is_not_sent(dbf, fake_meta, world):
    ids = base._seed(dbf, phones=PHONES[:3])
    db = dbf()
    cust = db.query(Customer).filter(Customer.tenant_id == ids.tenant_id,
                                     Customer.normalized_phone == PHONES[1]).one()
    cust.extra_metadata = dict(cust.extra_metadata or {}, is_unsubscribed=True)
    row = db.query(CampaignSendLog).filter(CampaignSendLog.customer_phone_e164 == PHONES[1]).one()
    row.customer_id = cust.id
    db.commit()
    db.close()
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted([PHONES[0], PHONES[2]])
    logs = base._logs(dbf, ids.campaign_id)
    assert logs[PHONES[1]][0] == disp.LOG_SKIPPED_UNSUBSCRIBED
    assert base._status(dbf, ids.campaign_id) == "completed"


def _link_customer(Session, ids, phone):
    db = Session()
    cust = db.query(Customer).filter(Customer.tenant_id == ids.tenant_id,
                                     Customer.normalized_phone == phone).one()
    row = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id,
                                           CampaignSendLog.customer_phone_e164 == phone).one()
    row.customer_id = cust.id
    db.commit()
    cid = cust.id
    db.close()
    return cid


def _attempt_phones(Session, cid):
    db = Session()
    try:
        return sorted({a.customer_phone_e164 for a in db.query(CampaignSendAttempt)
                       .filter(CampaignSendAttempt.campaign_id == cid)})
    finally:
        db.close()


def test_unreadable_consent_state_never_sends_and_pauses_for_review(dbf, fake_meta, world, monkeypatch):
    """Reading the customer's current flags fails (a database error at
    send time): that recipient gets NO request, stays queued with the
    reason, the others are still sent, and the run ends paused for the
    merchant. Once the read works again, an explicit resume sends them."""
    from sqlalchemy.orm import Session as _Session
    ids = base._seed(dbf, phones=PHONES[:3])
    _link_customer(dbf, ids, PHONES[1])
    real_refresh = _Session.refresh

    def flaky_refresh(self, instance, *a, **kw):
        if isinstance(instance, Customer) and instance.normalized_phone == PHONES[1]:
            raise RuntimeError("customer store unavailable")
        return real_refresh(self, instance, *a, **kw)

    monkeypatch.setattr(_Session, "refresh", flaky_refresh)
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    # No WhatsApp request for the unreadable recipient; the others went out.
    assert sorted(meta.calls) == sorted([PHONES[0], PHONES[2]])
    assert PHONES[1] not in _attempt_phones(dbf, ids.campaign_id)
    logs = base._logs(dbf, ids.campaign_id)
    assert logs[PHONES[1]] == ("queued", ledger.CONSENT_UNREADABLE_ERROR, None)
    assert base._status(dbf, ids.campaign_id) == "paused"
    assert base._lease(dbf, ids.campaign_id).reason == ledger.PAUSE_CONSENT_UNREADABLE
    payload = cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "needs_review"
    assert payload["status_explanation"]["tone"] == "action"
    assert payload["stats"]["queued"] == 1
    # Nothing continues it on its own while the state is unreadable.
    assert world.resume(dbf) == [] and _recover(dbf) == []
    assert sorted(meta.calls) == sorted([PHONES[0], PHONES[2]])
    # The read works again: the merchant's resume sends exactly the one left.
    monkeypatch.setattr(_Session, "refresh", real_refresh)
    rc = _router(monkeypatch, ids, dbf)
    db = dbf()
    asyncio.run(rc.update_campaign_status(
        ids.campaign_id, rc.UpdateCampaignStatusIn(status="active"), request=None, db=db))
    db.close()
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted(PHONES[:3])
    assert meta.calls.count(PHONES[1]) == 1
    logs = base._logs(dbf, ids.campaign_id)
    assert logs[PHONES[1]][0] == "sent" and logs[PHONES[1]][1] is None
    assert base._status(dbf, ids.campaign_id) == "completed"


@pytest.mark.parametrize("shape", ["oops", ["is_unsubscribed"], 7], ids=["string", "list", "int"])
def test_malformed_consent_flags_are_not_permission_to_send(dbf, fake_meta, world, shape):
    """Consent flags of a shape this code does not understand are treated as
    unreadable: no request, still queued, run paused for review. Only a
    legacy NULL means 'no flags were ever set'."""
    ids = base._seed(dbf, phones=PHONES[:3])
    _link_customer(dbf, ids, PHONES[1])
    legacy_id = _link_customer(dbf, ids, PHONES[2])
    db = dbf()
    db.query(Customer).filter(Customer.normalized_phone == PHONES[1]).one().extra_metadata = shape
    db.get(Customer, legacy_id).extra_metadata = None
    db.commit()
    db.close()
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted([PHONES[0], PHONES[2]])
    logs = base._logs(dbf, ids.campaign_id)
    assert logs[PHONES[1]] == ("queued", ledger.CONSENT_UNREADABLE_ERROR, None)
    assert logs[PHONES[2]][0] == "sent"
    assert base._lease(dbf, ids.campaign_id).reason == ledger.PAUSE_CONSENT_UNREADABLE
    assert cap._lifecycle(dbf, ids.campaign_id)["lifecycle"] == "needs_review"


def test_consent_withdrawn_between_two_runs_is_honoured(dbf, fake_meta, world):
    """The check reads the database at each attempt, not a cached copy."""
    world.budget(1)
    ids = base._seed(dbf, phones=PHONES[:2])
    _link_customer(dbf, ids, PHONES[1])
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)                  # capacity for one
    assert len(meta.calls) == 1
    db = dbf()
    c = db.query(Customer).filter(Customer.normalized_phone == PHONES[1]).one()
    c.extra_metadata = dict(c.extra_metadata or {}, pending_unsubscribe=True)
    db.commit()
    db.close()
    cap._age_window(dbf)
    world.resume(dbf)
    assert len(meta.calls) == 1 and PHONES[1] not in meta.calls
    assert base._logs(dbf, ids.campaign_id)[PHONES[1]][0] == disp.LOG_SKIPPED_UNSUBSCRIBED


# ── 6. Button taps: counted once, only where reported, never retroactively ─


def _quick_reply_template(Session, ids):
    db = Session()
    tpl = db.get(WhatsAppTemplate, ids.template_id)
    tpl.components = [{"type": "BODY", "text": "مرحبا {{1}}"},
                      {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "أريد العرض"}]}]
    db.commit()
    db.close()


def test_quick_reply_taps_count_once_and_only_for_tracked_copies(dbf, fake_meta, world):
    from services.campaign_click_tracking import record_button_tap_from_inbound
    ids = base._seed(dbf, phones=PHONES[:3])
    _quick_reply_template(dbf, ids)
    _already_sent(dbf, ids, PHONES[:1])                     # a copy from before tracking
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted(PHONES[1:3])
    db = dbf()
    tracked = {a.customer_phone_e164: a for a in db.query(CampaignSendAttempt)
               .filter(CampaignSendAttempt.click_trackable.is_(True))}
    assert set(tracked) == set(PHONES[1:3])
    wamid = tracked[PHONES[1]].provider_message_id
    db.close()

    import services.campaign_click_tracking as clicks
    import core.database as core_db
    db_factory = dbf
    class _Local:
        def __call__(self):
            return db_factory()
    # The webhook path opens its own session: point it at this database.
    orig = core_db.SessionLocal
    core_db.SessionLocal = _Local()
    try:
        tap = {"type": "button", "id": "wamid.in.1", "timestamp": str(int(_now().timestamp())),
               "context": {"id": wamid}, "button": {"text": "أريد العرض", "payload": "أريد العرض"}}
        assert record_button_tap_from_inbound(tap)["reason"] == "counted"
        assert record_button_tap_from_inbound(dict(tap, id="wamid.in.2"))["reason"] == "already_counted"
        legacy = dict(tap, id="wamid.in.3", context={"id": "w.earlier.0"})
        assert record_button_tap_from_inbound(legacy)["reason"] == "sent_before_tracking"
        assert record_button_tap_from_inbound({"type": "text", "text": {"body": "hi"}}) is None
        assert record_button_tap_from_inbound(dict(tap, context={"id": "wamid.unknown"}))["reason"] \
            == "not_a_campaign_message"
    finally:
        core_db.SessionLocal = orig
    del clicks
    payload = cap._lifecycle(dbf, ids.campaign_id)
    assert payload["stats"]["clicked"] == 1 and payload["clicked_count"] == 1
    assert payload["click_tracking"]["status"] == "available"
    assert payload["click_tracking"]["partial"] is True      # one copy predates tracking
    assert payload["click_tracking"]["trackable_messages"] == 2
    # A read receipt is never a click.
    assert payload["stats"]["read"] == 0


def test_url_button_campaigns_report_clicks_as_unavailable(dbf, fake_meta, world):
    ids = base._seed(dbf, phones=PHONES[:2])
    db = dbf()
    tpl = db.get(WhatsAppTemplate, ids.template_id)
    tpl.components = [{"type": "BODY", "text": "مرحبا {{1}}"},
                      {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "تسوق", "url": "https://x.example"}]}]
    db.commit()
    db.close()
    fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    payload = cap._lifecycle(dbf, ids.campaign_id)
    assert payload["click_tracking"] == {"status": "unavailable", "reason": "no_quick_reply_buttons",
                                         "trackable_messages": 0, "partial": False}
    assert payload["stats"]["clicked"] == 0


# ── 7. Filtered statistics ───────────────────────────────────────────────


def test_stats_endpoint_counts_by_event_time_and_scope(dbf, fake_meta, world, monkeypatch):
    ids = base._seed(dbf, phones=PHONES[:3])
    other = base._seed(dbf, phones=["+966511111111"], campaign_name="حملة متجر أحذية")
    db = dbf()                                            # same merchant, second campaign
    db.get(Campaign, other.campaign_id).tenant_id = ids.tenant_id
    for r in db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == other.campaign_id):
        r.tenant_id = ids.tenant_id
    db.commit()
    db.close()
    rc = _router(monkeypatch, ids, dbf)
    fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    db = dbf()
    # One receipt from yesterday, one today.
    atts = db.query(CampaignSendAttempt).filter(CampaignSendAttempt.campaign_id == ids.campaign_id).all()
    ledger._apply_to_attempt(atts[0], "delivered", _now() - timedelta(days=2), None)
    ledger.refresh_send_log_from_attempts(db, atts[0].send_log_id)
    ledger._apply_to_attempt(atts[1], "read", _now(), None)
    ledger.refresh_send_log_from_attempts(db, atts[1].send_log_id)
    db.commit()
    everything = asyncio.run(rc.campaign_stats(request=None, db=db, scope="all", period="all",
                                               date_from=None, date_to=None, tz_offset_minutes=180))
    today = asyncio.run(rc.campaign_stats(request=None, db=db, scope=str(ids.campaign_id), period="today",
                                          date_from=None, date_to=None, tz_offset_minutes=180))
    running = asyncio.run(rc.campaign_stats(request=None, db=db, scope="running", period="all",
                                            date_from=None, date_to=None, tz_offset_minutes=180))
    db.close()
    assert everything["recipients"]["accepted"] == 3 and everything["recipients"]["reached"] == 2
    assert everything["recipients"]["read"] == 1 and everything["period_basis"] == "event_time"
    assert today["recipients"]["reached"] == 1 and today["recipients"]["read"] == 1
    assert today["range"]["from"] is not None
    # The completed campaign is not "running"; the untouched one is.
    assert running["campaign_ids"] == [other.campaign_id] and running["remaining"] == 1
    assert everything["click_tracking"]["status"] == "unavailable"
