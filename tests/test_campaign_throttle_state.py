"""Meta throttling: why a campaign stopped is reported truthfully, a
temporary per-minute limit backs off and continues on its own, a delivery
block (131049) cools down before continuing untouched recipients, and a run never ends "completed" with recipients
still queued. Real leased dispatch path, scripted Meta, SQLite and
PostgreSQL; generic merchant data, no real sends.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import test_campaign_capacity_continuation as _cap
import test_campaign_send_ledger as _ledger_suite
from test_campaign_send_ledger import FakeMeta, _logs, _seed
from models import Campaign, CampaignDispatchLease, CampaignSendAttempt, CampaignSendLog  # noqa: E402
from services import campaign_dispatcher as disp  # noqa: E402
from services import campaign_send_ledger as ledger  # noqa: E402
from services.meta_errors import classify_meta_error  # noqa: E402

dbf = _ledger_suite.dbf
fake_meta = _ledger_suite.fake_meta
_fast = _ledger_suite._fast
world = _cap.world
SCOPE = "bm:BM-GENERIC-1"
PHONES = [f"+96650{n:07d}" for n in range(100, 140)]      # 40 generic numbers


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _post_accept_failures(Session, ids, n, *, key="marketing_blocked", minutes_ago=5.0):
    """``n`` copies of an EARLIER run that Meta accepted and then reported
    failed with ``key`` — the breaker's input."""
    db = Session()
    base = _now() - timedelta(minutes=minutes_ago)
    for i in range(n):
        ph = f"+96655{i:07d}"
        row = CampaignSendLog(tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                              customer_phone_e164=ph, template_name="generic_offer",
                              template_language="ar", status="sent", attempt_count=1,
                              provider_message_id=f"w.pa.{key}.{i}", sent_at=base)
        db.add(row)
        db.flush()
        t = base + timedelta(seconds=i)
        db.add(CampaignSendAttempt(
            tenant_id=ids.tenant_id, campaign_id=ids.campaign_id, send_log_id=row.id,
            customer_phone_e164=ph, attempt_no=1, state="accepted", messaging_scope_key=SCOPE,
            provider_message_id=f"w.pa.{key}.{i}", claimed_at=t, request_started_at=t,
            accepted_at=t, failed_at=t, post_accept_error_code=key, created_at=t, updated_at=t))
    db.commit()
    db.close()


def _lease(Session, cid):
    db = Session()
    try:
        lease = db.get(CampaignDispatchLease, cid)
        return SimpleNamespace(reason=lease.pause_reason, detail=lease.pause_detail or "")
    finally:
        db.close()


def _status(Session, cid):
    db = Session()
    try:
        return db.get(Campaign, cid).status
    finally:
        db.close()


def _age_failures(Session, minutes):
    db = Session()
    for a in db.query(CampaignSendAttempt).filter(CampaignSendAttempt.failed_at.isnot(None)):
        a.failed_at = a.failed_at - timedelta(minutes=minutes)
    db.commit()
    db.close()


# ── The breaker counts the scope's window, not the run (RCA) ─────────────


def test_a_new_run_inside_the_window_stops_at_its_first_recipient(dbf, fake_meta, world):
    """What production showed at 20:53–20:58: every run started while the
    25 failures of the previous run were still inside the 15-minute window
    paused with no send. Once they age out, the next run sends."""
    ids = _seed(dbf, phones=PHONES[:3])
    _post_accept_failures(dbf, ids, 25)
    meta = fake_meta(FakeMeta())
    for _ in range(3):
        world.dispatch(dbf, ids.campaign_id)
    assert meta.calls == []
    lease = _lease(dbf, ids.campaign_id)
    assert lease.reason == ledger.PAUSE_PROVIDER_THROTTLING
    assert "marketing_blocked x25" in lease.detail and "clears_at=" in lease.detail
    _age_failures(dbf, 16)
    db = dbf()
    ledger.clear_stop(db, campaign_id=ids.campaign_id)
    db.commit()
    db.close()
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted(PHONES[:3])


def test_the_breaker_says_exactly_when_it_clears(dbf):
    ids = _seed(dbf, phones=PHONES[:1])
    _post_accept_failures(dbf, ids, 30, minutes_ago=10)
    db = dbf()
    st = ledger.post_accept_throttle_status(db, SCOPE)
    first = min(a.failed_at for a in db.query(CampaignSendAttempt))
    db.close()
    # 30 counted, threshold 25: below it once the 6 oldest have left the window.
    assert (st["key"], st["count"], st["threshold"]) == ("marketing_blocked", 30, 25)
    # The window includes its lower bound: the count drops one tick later.
    assert datetime.fromisoformat(st["clears_at"]) == first + timedelta(seconds=6) + \
        ledger.POST_ACCEPT_BREAKER_WINDOW


# ── Resume refuses while tripped, with the time ─────────────────────────


def _dispatch_now(Session, ids, monkeypatch):
    import routers.campaigns as rc
    spawned = []
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", spawned.append)
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request, db=None: ids.tenant_id)
    monkeypatch.setattr(disp, "_get_wa_connection",
                        lambda db, tenant_id: _ledger_suite._conn(tenant_id=tenant_id))
    db = Session()
    try:
        res = asyncio.run(rc.dispatch_campaign_now(ids.campaign_id, request=None, db=db,
                                                   bypass_frequency_cap=None))
    finally:
        db.close()
    return res, spawned


def test_dispatch_now_refuses_while_tripped_and_changes_nothing(dbf, fake_meta, monkeypatch):
    ids = _seed(dbf, phones=PHONES[:2])
    _post_accept_failures(dbf, ids, 25)
    before = _status(dbf, ids.campaign_id)
    res, spawned = _dispatch_now(dbf, ids, monkeypatch)
    assert (res["ok"], res["reason"]) == (False, "provider_throttled")
    assert res["throttle"]["key"] == "marketing_blocked" and res["throttle"]["clears_at"]
    assert spawned == [] and _status(dbf, ids.campaign_id) == before


def test_negative_control_without_the_check_dispatch_now_starts_a_run_that_cannot_send(
        dbf, fake_meta, monkeypatch):
    import routers.campaigns as rc
    monkeypatch.setattr(rc, "_campaign_throttle", lambda db, c: None)
    ids = _seed(dbf, phones=PHONES[:2])
    _post_accept_failures(dbf, ids, 25)
    res, spawned = _dispatch_now(dbf, ids, monkeypatch)
    assert res["ok"] is True and spawned == [ids.campaign_id]


def test_resume_refuses_while_tripped(dbf, fake_meta, monkeypatch):
    import routers.campaigns as rc
    from fastapi import HTTPException
    ids = _seed(dbf, phones=PHONES[:2])
    db = dbf()
    db.get(Campaign, ids.campaign_id).status = "paused"
    db.commit()
    db.close()
    _post_accept_failures(dbf, ids, 25)
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", lambda cid: None)
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request, db=None: ids.tenant_id)
    monkeypatch.setattr(disp, "_get_wa_connection",
                        lambda db, tenant_id: _ledger_suite._conn(tenant_id=tenant_id))
    db = dbf()
    with pytest.raises(HTTPException) as err:
        asyncio.run(rc.update_campaign_status(
            ids.campaign_id, rc.UpdateCampaignStatusIn(status="active"), request=None, db=db))
    db.close()
    assert err.value.status_code == 409 and err.value.detail["reason"] == "provider_throttled"
    assert _status(dbf, ids.campaign_id) == "paused"


# ── The page says what actually happened ─────────────────────────────────


def test_a_delivery_block_is_shown_as_one_not_as_wait_a_minute(dbf, fake_meta, world):
    ids = _seed(dbf, phones=PHONES[:2])
    _post_accept_failures(dbf, ids, 25)
    fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    payload = _cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "marketing_delivery_backoff"
    assert payload["last_error_key"] != "rate_limit"
    assert "انتظر دقيقة" not in (payload["last_error_ar"] or "")
    assert payload["capacity_wait"]["next_eligible_at"]
    assert payload["capacity_wait"]["next_eligible_exact"] is False


def test_negative_control_the_meta_classifier_misreads_the_internal_token():
    """Why the page said "wait a minute": the classifier maps the word
    'throttling' in Nahla's own pause token to Meta's rate_limit."""
    assert classify_meta_error(message="dispatch_paused:provider_throttling").key == "rate_limit"


# ── A temporary per-minute limit backs off and continues on its own ──────


def _rate_limited_meta(n):
    """Meta rejects the first ``n`` sends with 130429, then accepts."""
    return FakeMeta({p: ["reject:130429:Rate limit hit"] for p in PHONES[:n]})


def test_a_rate_limit_pauses_with_a_backoff_and_continues_without_the_merchant(
        dbf, fake_meta, world):
    ids = _seed(dbf, phones=PHONES)
    meta = fake_meta(_rate_limited_meta(25))
    world.dispatch(dbf, ids.campaign_id)
    assert _status(dbf, ids.campaign_id) == "paused"                   # not "completed"
    assert _lease(dbf, ids.campaign_id).reason == ledger.PAUSE_PROVIDER_RATE_LIMITED
    c = _cap._campaign(dbf, ids.campaign_id)
    due = datetime.fromisoformat(c.wait["next_eligible_at"])
    assert (c.wait["reason"], c.wait["attempt"]) == (ledger.PAUSE_PROVIDER_RATE_LIMITED, 1)
    assert timedelta(minutes=4) < due - _now() <= ledger.RATE_LIMIT_BACKOFF_BASE
    assert _cap._lifecycle(dbf, ids.campaign_id)["lifecycle"] == "rate_limit_backoff"
    assert [a["action"] for a in world.resume(dbf)] == ["waiting"]      # inside the backoff
    _cap._age_window(dbf, hours=1)
    acts = world.resume(dbf)
    assert acts[0]["action"] == "resumed" and acts[0]["requeued_rate_limited"] == 25
    # Every recipient ends with exactly one accepted copy; the rejected
    # ones were retried once, nobody was sent twice.
    db = dbf()
    accepted = [a.customer_phone_e164 for a in db.query(CampaignSendAttempt)
                .filter(CampaignSendAttempt.state == ledger.ATTEMPT_ACCEPTED)]
    db.close()
    assert sorted(accepted) == sorted(PHONES)
    assert all(meta.calls.count(p) <= 2 for p in PHONES)
    assert _status(dbf, ids.campaign_id) == "completed"


def test_consecutive_rate_limits_back_off_longer(dbf, fake_meta, world):
    ids = _seed(dbf, phones=PHONES)
    fake_meta(FakeMeta({p: ["reject:130429:Rate limit hit"] * 3 for p in PHONES}))
    world.dispatch(dbf, ids.campaign_id)
    _cap._age_window(dbf, hours=1)
    world.resume(dbf)
    c = _cap._campaign(dbf, ids.campaign_id)
    assert c.wait["attempt"] == 2
    due = datetime.fromisoformat(c.wait["next_eligible_at"])
    assert timedelta(minutes=9) < due - _now() <= 2 * ledger.RATE_LIMIT_BACKOFF_BASE


def test_marketing_cooldown_continues_only_untouched_recipients(dbf, fake_meta, world):
    """Owner-authorized continuation; never retry a previously accepted copy."""
    ids = _seed(dbf, phones=PHONES[:3])
    _post_accept_failures(dbf, ids, 25)
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    _age_failures(dbf, 16)
    _cap._age_window(dbf, hours=1)
    assert [a["action"] for a in world.resume(dbf) if a["campaign_id"] == ids.campaign_id] == ["resumed"]
    assert sorted(meta.calls) == sorted(PHONES[:3])


def test_negative_control_the_old_breaker_ended_a_rate_limited_run_as_completed(
        dbf, fake_meta, world, monkeypatch):
    """Without the backoff pause and the queued rule the same run is written
    'completed' with recipients still queued."""
    monkeypatch.setattr(ledger, "PAUSE_PROVIDER_RATE_LIMITED", None)
    real_pause = disp.DispatchRunContext.pause

    def pause(self, reason, detail=""):
        if reason in (None, ledger.PAUSE_RUN_ENDED_WITH_QUEUE):
            return
        return real_pause(self, reason, detail)
    monkeypatch.setattr(disp.DispatchRunContext, "pause", pause)
    ids = _seed(dbf, phones=PHONES)
    # Some copies accepted first, then Meta's per-minute limit — the real order.
    fake_meta(FakeMeta({p: ["reject:130429:Rate limit hit"] for p in PHONES[3:28]}))
    world.dispatch(dbf, ids.campaign_id)
    assert _status(dbf, ids.campaign_id) == "completed"
    assert "queued" in {s for s, _, _ in _logs(dbf, ids.campaign_id).values()}


def test_the_same_run_with_the_fix_is_paused_not_completed(dbf, fake_meta, world):
    ids = _seed(dbf, phones=PHONES)
    fake_meta(FakeMeta({p: ["reject:130429:Rate limit hit"] for p in PHONES[3:28]}))
    world.dispatch(dbf, ids.campaign_id)
    assert _status(dbf, ids.campaign_id) == "paused"
    assert _lease(dbf, ids.campaign_id).reason == ledger.PAUSE_PROVIDER_RATE_LIMITED


# ── Review fixes: only a real rate limit auto-resumes; capacity first ────


def test_a_repeated_non_rate_limit_error_stops_for_the_merchant(dbf, fake_meta, world):
    """25 × a retryable service error (131000) is not proven temporary: the
    run pauses for the merchant and the scheduler never resumes it."""
    ids = _seed(dbf, phones=PHONES)
    meta = fake_meta(FakeMeta({p: ["reject:131000:Something went wrong"] for p in PHONES[:25]}))
    world.dispatch(dbf, ids.campaign_id)
    assert _status(dbf, ids.campaign_id) == "paused"
    assert _lease(dbf, ids.campaign_id).reason == ledger.PAUSE_PROVIDER_REPEATED_ERROR
    calls = len(meta.calls)
    _cap._age_window(dbf, hours=1)
    assert [a for a in world.resume(dbf) if a["campaign_id"] == ids.campaign_id] == []
    assert len(meta.calls) == calls
    payload = _cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "paused"
    assert payload["last_error_ar"] and "same_code_circuit_breaker" not in payload["last_error_ar"]
    assert payload["pause_reason_ar"] == rc_labels()["provider_repeated_error"]


def rc_labels():
    import routers.campaigns as rc
    return rc.PAUSE_REASON_LABELS_AR


@pytest.mark.parametrize("bucket,reason", [
    ("rate_limit", "provider_rate_limited"),
    ("spam_rate_limit", "provider_throttling"),
    ("service_unavailable", "provider_repeated_error"),
])
def test_a_breaker_token_is_labelled_by_its_bucket_never_shown_raw(bucket, reason):
    import routers.campaigns as rc
    c = Campaign(id=1, tenant_id=1, name="x", status="paused", template_variables={
        "_dispatch_errors": f"dispatch_aborted:same_code_circuit_breaker:{bucket}@25"})
    out = rc._campaign_to_dict(c, execution={"worker_running": False})
    assert out["last_error_ar"] == rc.PAUSE_REASON_LABELS_AR[reason]


def test_negative_control_every_retryable_code_as_a_rate_limit_auto_resumes(
        dbf, fake_meta, world, monkeypatch):
    """What the first version did: any retryable code earned a timed backoff."""
    monkeypatch.setattr(ledger, "RATE_LIMIT_BUCKETS",
                        frozenset({"rate_limit", "service_unavailable"}))
    ids = _seed(dbf, phones=PHONES)
    fake_meta(FakeMeta({p: ["reject:131000:Something went wrong"] for p in PHONES[:25]}))
    world.dispatch(dbf, ids.campaign_id)
    assert _lease(dbf, ids.campaign_id).reason == ledger.PAUSE_PROVIDER_RATE_LIMITED


def _backoff_due_now(Session, cid):
    """The rate-limit backoff has passed, without moving any send."""
    db = Session()
    c = db.get(Campaign, cid)
    w = dict(ledger.capacity_wait(c), next_eligible_at=(_now() - timedelta(minutes=1)).isoformat())
    ledger.store_capacity_wait(c, w)
    db.commit()
    db.close()


def test_a_backoff_that_finds_the_limit_full_waits_for_capacity_then_requeues(
        dbf, fake_meta, world):
    ids = _seed(dbf, phones=PHONES)
    meta = fake_meta(FakeMeta({p: ["reject:130429:Rate limit hit"] for p in PHONES[3:28]}))
    world.dispatch(dbf, ids.campaign_id)
    assert _lease(dbf, ids.campaign_id).reason == ledger.PAUSE_PROVIDER_RATE_LIMITED
    world.budget(3)                                      # the 3 accepted fill the share
    _backoff_due_now(dbf, ids.campaign_id)
    acts = world.resume(dbf)
    assert acts[0]["action"] == "still_full" and "requeued_rate_limited" not in acts[0]
    c = _cap._campaign(dbf, ids.campaign_id)
    assert c.pause_reason == ledger.PAUSE_MESSAGING_LIMIT            # lease and record agree
    assert c.wait["reason"] == ledger.PAUSE_MESSAGING_LIMIT and c.wait["requeue_rate_limited"]
    assert _cap._lifecycle(dbf, ids.campaign_id)["lifecycle"] == "waiting_for_capacity"
    _cap._age_window(dbf, hours=25)                      # the window rolls: room again
    acts = world.resume(dbf)
    assert acts[0]["action"] == "resumed" and acts[0]["requeued_rate_limited"] == 25
    db = dbf()
    accepted = [a.customer_phone_e164 for a in db.query(CampaignSendAttempt)
                .filter(CampaignSendAttempt.state == ledger.ATTEMPT_ACCEPTED)]
    db.close()
    assert len(accepted) == len(set(accepted)) == 6      # 3 earlier + this window's 3
    assert all(meta.calls.count(p) <= 2 for p in PHONES)


def test_negative_control_a_capacity_record_under_a_rate_limit_lease_is_stranded(
        dbf, fake_meta, world):
    """The first version wrote a capacity record but left the lease
    ``provider_rate_limited``: neither reason authorises the other."""
    ids = _seed(dbf, phones=PHONES)
    fake_meta(FakeMeta({p: ["reject:130429:Rate limit hit"] for p in PHONES[3:28]}))
    world.dispatch(dbf, ids.campaign_id)
    db = dbf()
    ledger.record_capacity_wait(db.get(Campaign, ids.campaign_id), None,
                                now=_now() - timedelta(hours=1))
    db.commit()
    db.close()
    assert world.resume(dbf)[0]["action"] == "needs_merchant_resume"


def test_a_per_run_spam_breaker_is_never_shown_as_cleared(dbf, fake_meta, world):
    """131048 at send time trips a per-run breaker with no sliding window:
    the page must show the reason, not "cleared"."""
    ids = _seed(dbf, phones=PHONES[:8])
    fake_meta(FakeMeta({p: ["reject:131048:Spam rate limit hit"] for p in PHONES[:8]}))
    world.dispatch(dbf, ids.campaign_id)
    assert _lease(dbf, ids.campaign_id).reason == ledger.PAUSE_PROVIDER_THROTTLING
    payload = _cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "provider_throttled"
    assert payload["throttle"] is None and payload["throttle_checked"] is False


def test_without_a_connection_the_block_is_never_shown_as_cleared(dbf, fake_meta, world, monkeypatch):
    ids = _seed(dbf, phones=PHONES[:2])
    _post_accept_failures(dbf, ids, 25)
    fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    _age_failures(dbf, 16)                               # the window has in fact cleared
    monkeypatch.setattr(disp, "_get_wa_connection", lambda db, tenant_id: None)
    payload = _cap._lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "marketing_delivery_backoff"
    assert payload["throttle"] is None and payload["throttle_checked"] is False
