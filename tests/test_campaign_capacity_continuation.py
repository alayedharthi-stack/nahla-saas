"""Campaigns stopped by Meta's shared messaging limit wait for capacity and
continue on their own — durably, without the merchant, and never past the
limit or into a resend.

Also pins the two inputs of that decision: the tier is read from Meta's
current business-portfolio field, and ``used_24h`` counts only recipients
a send may actually have reached.

Generic merchant data; Meta is a scripted fake (no real sends).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import test_campaign_send_ledger as _ledger_suite
from test_campaign_send_ledger import (
    PHONES, FakeMeta, _conn, _logs, _seed, _seed_scope_usage,
)
from models import (  # noqa: E402
    Campaign, CampaignDispatchLease, CampaignSendAttempt, CampaignSendLog,
)
from services import campaign_dispatcher as disp  # noqa: E402
from services import campaign_send_ledger as ledger  # noqa: E402

dbf = _ledger_suite.dbf
fake_meta = _ledger_suite.fake_meta
_fast = _ledger_suite._fast

SCOPE = "bm:BM-GENERIC-1"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def world(monkeypatch):
    """Real ``dispatch_campaign`` + ``resume_capacity_waiting`` with the
    provider pieces stubbed; ``tier`` / ``percent`` set the shared budget."""
    import core.billing as billing
    import core.wa_usage as wa_usage
    monkeypatch.setattr(billing, "has_billing_access", lambda *a, **k: True)
    monkeypatch.setattr(wa_usage, "check_limit",
                        lambda *a, **k: SimpleNamespace(allowed=True, reason=None,
                                                        used_total=0, limit=None))
    state = {"tier": "TIER_10K"}

    def conn_for(db, tenant_id):
        return _conn(meta_messaging_limit=state["tier"], meta_tier_updated_at=_now(),
                     tenant_id=tenant_id)
    monkeypatch.setattr(disp, "_get_wa_connection", conn_for)

    def budget(n):
        """A fresh Meta limit whose campaign share is exactly ``n``."""
        state["tier"] = "TIER_250"
        monkeypatch.setattr(ledger, "parse_messaging_tier", lambda raw: n if raw else None)
        monkeypatch.setattr(ledger, "CAMPAIGN_BUDGET_PERCENT", 100)

    def dispatch(Session, cid):
        db = Session()
        try:
            return asyncio.run(disp.dispatch_campaign(db, cid))
        finally:
            db.close()

    def resume(Session):
        db = Session()
        try:
            return asyncio.run(disp.resume_capacity_waiting(db))
        finally:
            db.close()

    return SimpleNamespace(state=state, budget=budget, dispatch=dispatch, resume=resume)


def _age_window(Session, hours=25):
    """Move every send in the database ``hours`` into the past — the 24h
    window rolls forward without waiting a day."""
    db = Session()
    delta = timedelta(hours=hours)
    for a in db.query(CampaignSendAttempt):
        for col in ("claimed_at", "request_started_at", "accepted_at", "completed_at"):
            v = getattr(a, col)
            if v is not None:
                setattr(a, col, v - delta)
    for r in db.query(CampaignSendLog):
        if r.sent_at is not None:
            r.sent_at = r.sent_at - delta
    for c in db.query(Campaign):
        w = ledger.capacity_wait(c)
        if w and w.get("next_eligible_at"):
            w = dict(w, next_eligible_at=(datetime.fromisoformat(w["next_eligible_at"])
                                          - delta).isoformat())
            tv = dict(c.template_variables or {})
            tv[ledger.CAPACITY_WAIT_KEY] = w
            c.template_variables = tv
    db.commit()
    db.close()


def _campaign(Session, cid):
    db = Session()
    try:
        c = db.get(Campaign, cid)
        lease = db.get(CampaignDispatchLease, cid)
        return SimpleNamespace(status=c.status, wait=ledger.capacity_wait(c),
                               pause_reason=lease.pause_reason if lease else None,
                               stop=bool(lease and lease.stop_requested_at))
    finally:
        db.close()


def _lifecycle(Session, cid):
    import routers.campaigns as rc
    db = Session()
    try:
        return rc._campaigns_payload(db, [db.get(Campaign, cid)])[0]
    finally:
        db.close()


# ── 1. capacity available → sends ───────────────────────────────────────


def test_queued_recipients_with_capacity_are_sent(dbf, fake_meta, world):
    ids = _seed(dbf, phones=PHONES[:3])
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert sorted(meta.calls) == sorted(PHONES[:3])
    c = _campaign(dbf, ids.campaign_id)
    assert c.status == "completed" and c.wait is None


# ── 2. no capacity → no provider call, waiting_for_capacity ─────────────


def test_zero_capacity_waits_without_calling_meta(dbf, fake_meta, world):
    world.budget(2)
    other = _seed(dbf, phones=["+966511111111"], campaign_name="حملة سابقة")
    _seed_scope_usage(dbf, other, SCOPE, 2)
    ids = _seed(dbf, phones=PHONES[:3])
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert meta.calls == []
    c = _campaign(dbf, ids.campaign_id)
    assert c.status == "paused" and c.pause_reason == ledger.PAUSE_MESSAGING_LIMIT
    assert c.wait["reason"] == ledger.PAUSE_MESSAGING_LIMIT
    assert c.wait["next_eligible_exact"] is True
    # Exactly when the oldest counted recipient leaves the 24h window.
    due = datetime.fromisoformat(c.wait["next_eligible_at"])
    assert timedelta(hours=23, minutes=59) < due - _now() <= timedelta(hours=24)
    payload = _lifecycle(dbf, ids.campaign_id)
    assert payload["lifecycle"] == "waiting_for_capacity"
    assert payload["capacity_wait"]["next_eligible_at"] == c.wait["next_eligible_at"]
    assert {s for s, _, _ in _logs(dbf, ids.campaign_id).values()} == {"queued"}


# ── 3. the window passes → resumes on its own ───────────────────────────


def test_capacity_returning_resumes_without_the_merchant(dbf, fake_meta, world):
    world.budget(2)
    other = _seed(dbf, phones=["+966511111111"], campaign_name="حملة سابقة")
    _seed_scope_usage(dbf, other, SCOPE, 2)
    ids = _seed(dbf, phones=PHONES[:2])
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert meta.calls == []
    # Still inside the window: the scheduler leaves it waiting.
    acts = world.resume(dbf)
    assert [a["action"] for a in acts if a["campaign_id"] == ids.campaign_id] == ["waiting"]
    assert meta.calls == []
    _age_window(dbf)
    acts = world.resume(dbf)
    assert [a["action"] for a in acts if a["campaign_id"] == ids.campaign_id] == ["resumed"]
    assert sorted(meta.calls) == sorted(PHONES[:2])
    c = _campaign(dbf, ids.campaign_id)
    assert c.status == "completed" and c.wait is None


# ── 4. several batches across several windows until completion ─────────


def test_batches_across_windows_until_completed(dbf, fake_meta, world):
    world.budget(2)
    ids = _seed(dbf, phones=PHONES[:5])
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert len(meta.calls) == 2 and _campaign(dbf, ids.campaign_id).status == "paused"
    for expected in (4, 5):
        assert world.resume(dbf)[0]["action"] == "waiting"      # window still full
        _age_window(dbf)
        world.resume(dbf)
        assert len(meta.calls) == expected
    assert sorted(meta.calls) == sorted(PHONES[:5])              # each exactly once
    assert _campaign(dbf, ids.campaign_id).status == "completed"
    assert world.resume(dbf) == []                               # nothing left to continue


# ── 5. restart between batches ──────────────────────────────────────────


def test_restart_between_batches_continues_from_the_database(dbf, fake_meta, world):
    world.budget(2)
    ids = _seed(dbf, phones=PHONES[:3])
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    # The process dies: in-memory state is gone and the worker's lease was
    # left behind (expired, owner still set).
    db = dbf()
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.owner, lease.expires_at = "dead-worker", _now() - timedelta(minutes=5)
    db.commit()
    db.close()
    _age_window(dbf)
    world.resume(dbf)                                            # a fresh process
    assert sorted(meta.calls) == sorted(PHONES[:3])
    assert _campaign(dbf, ids.campaign_id).status == "completed"


def test_a_live_worker_is_never_doubled(dbf, fake_meta, world):
    world.budget(2)
    ids = _seed(dbf, phones=PHONES[:3])
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    _age_window(dbf)
    db = dbf()
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.owner, lease.expires_at = "live-worker", _now() + timedelta(minutes=5)
    db.commit()
    db.close()
    assert world.resume(dbf)[0]["action"] == "worker_running"
    assert len(meta.calls) == 2


# ── 6. merchant stop wins ───────────────────────────────────────────────


@pytest.mark.parametrize("how", ["stop", "status_paused"])
def test_merchant_stop_blocks_auto_resume(dbf, fake_meta, world, monkeypatch, how):
    world.budget(2)
    ids = _seed(dbf, phones=PHONES[:3])
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    db = dbf()
    if how == "stop":
        ledger.request_stop(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id)
    else:
        import routers.campaigns as rc
        monkeypatch.setattr(rc, "resolve_tenant_id", lambda request, db=None: ids.tenant_id)
        asyncio.run(rc.update_campaign_status(
            ids.campaign_id, rc.UpdateCampaignStatusIn(status="paused"), request=None, db=db))
    db.close()
    _age_window(dbf)
    assert world.resume(dbf) == []
    assert len(meta.calls) == 2
    c = _campaign(dbf, ids.campaign_id)
    assert c.stop and c.pause_reason == ledger.PAUSE_MERCHANT_STOP
    assert _lifecycle(dbf, ids.campaign_id)["lifecycle"] == "paused"


# ── 7. sent / delivered / uncertain are never sent again ────────────────


def test_resume_never_resends_sent_delivered_or_uncertain(dbf, fake_meta, world):
    world.budget(2)
    ids = _seed(dbf, phones=PHONES[:5])
    db = dbf()
    now = _now()
    rows = {r.customer_phone_e164: r for r in db.query(CampaignSendLog)
            .filter(CampaignSendLog.campaign_id == ids.campaign_id)}
    rows[PHONES[0]].status, rows[PHONES[0]].sent_at = "sent", now - timedelta(hours=30)
    rows[PHONES[0]].provider_message_id = "w.sent"
    rows[PHONES[1]].status, rows[PHONES[1]].sent_at = "sent", now - timedelta(hours=30)
    rows[PHONES[1]].provider_message_id, rows[PHONES[1]].delivered_at = "w.dl", now
    rows[PHONES[2]].status, rows[PHONES[2]].error_code = "uncertain", "send_outcome_unknown"
    rows[PHONES[2]].updated_at = now - timedelta(hours=30)
    db.commit()
    db.close()
    other = _seed(dbf, phones=["+966511111111"], campaign_name="حملة سابقة")
    _seed_scope_usage(dbf, other, SCOPE, 2)
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert meta.calls == []
    _age_window(dbf)
    world.resume(dbf)
    assert sorted(meta.calls) == sorted(PHONES[3:5])
    logs = _logs(dbf, ids.campaign_id)
    assert logs[PHONES[2]][0] == "uncertain"


# ── 8. two campaigns sharing one business portfolio ─────────────────────


def test_two_campaigns_sharing_a_portfolio_never_exceed_the_limit(dbf, fake_meta, world):
    world.budget(3)
    a = _seed(dbf, phones=PHONES[:3], campaign_name="حملة أ")
    b = _seed(dbf, phones=PHONES[3:6], campaign_name="حملة ب",
              tenant=SimpleNamespace(id=a.tenant_id))
    meta = fake_meta(FakeMeta())
    world.dispatch(dbf, a.campaign_id)
    world.dispatch(dbf, b.campaign_id)
    assert len(meta.calls) == 3                                  # one window's budget
    windows = [len(meta.calls)]
    for _ in range(3):
        _age_window(dbf)
        before = len(meta.calls)
        world.resume(dbf)
        windows.append(len(meta.calls) - before)
    assert all(n <= 3 for n in windows)
    assert sorted(meta.calls) == sorted(PHONES[:6])              # everyone once, eventually
    assert {_campaign(dbf, a.campaign_id).status, _campaign(dbf, b.campaign_id).status} \
        == {"completed"}


# ── Inputs of the decision ──────────────────────────────────────────────


def test_budget_ignores_accepted_sends_meta_reported_undelivered(dbf):
    """Meta limits unique users messages are *delivered* to. An accepted
    attempt Meta later reported failed, with no delivered/read receipt,
    does not hold a slot; one with no receipt yet still does."""
    ids = _seed(dbf, phones=PHONES[:1])
    _seed_scope_usage(dbf, ids, SCOPE, 4)
    db = dbf()
    atts = db.query(CampaignSendAttempt).order_by(CampaignSendAttempt.id).all()
    atts[0].failed_at = _now()                                   # undelivered
    atts[1].failed_at, atts[1].delivered_at = _now(), _now()     # delivered, later failed copy
    db.commit()
    undelivered = atts[0].customer_phone_e164
    b = ledger.messaging_budget(db, _conn(meta_messaging_limit="TIER_250",
                                          meta_tier_updated_at=_now()))
    db.close()
    assert b.used == 3
    assert undelivered not in b.contacted_phones


def test_next_slot_is_when_enough_recipients_age_out(dbf, monkeypatch):
    monkeypatch.setattr(ledger, "parse_messaging_tier", lambda raw: 2 if raw else None)
    monkeypatch.setattr(ledger, "CAMPAIGN_BUDGET_PERCENT", 100)
    ids = _seed(dbf, phones=PHONES[:1])
    _seed_scope_usage(dbf, ids, SCOPE, 3)
    db = dbf()
    base = _now()
    for i, a in enumerate(db.query(CampaignSendAttempt).order_by(CampaignSendAttempt.id)):
        a.claimed_at = a.request_started_at = base - timedelta(hours=10 - i)
    db.commit()
    b = ledger.messaging_budget(db, _conn(meta_messaging_limit="TIER_250",
                                          meta_tier_updated_at=_now()), now=base)
    db.close()
    # used=3, budget=2 → two must age out; the second-oldest (−9h) frees it.
    assert b.used == 3 and b.remaining == 0
    assert abs((b.next_slot_at - (base - timedelta(hours=9) + timedelta(hours=24)))
               .total_seconds()) < 1


def test_tier_is_read_from_the_business_portfolio_field(monkeypatch):
    """Meta deprecated ``messaging_limit_tier``; the portfolio value wins
    and is what the shared budget then uses."""
    import services.whatsapp_platform.service as svc
    seen = {}

    async def fake_get(conn, ctx, **kw):
        seen["fields"] = kw["params"]["fields"]
        return {"whatsapp_business_manager_messaging_limit": "TIER_10K",
                "messaging_limit_tier": "TIER_250", "quality_rating": "GREEN"}

    monkeypatch.setattr(svc, "provider_get_with_context", fake_get)
    conn = SimpleNamespace(phone_number_id="PN-1", provider="meta")
    out = asyncio.run(svc.fetch_meta_phone_tier(conn, SimpleNamespace(token="t")))
    assert "whatsapp_business_manager_messaging_limit" in seen["fields"].split(",")
    assert out["messaging_limit"] == "TIER_10K"
    assert out["messaging_limit_field"] == "whatsapp_business_manager_messaging_limit"
    assert ledger.parse_messaging_tier(out["messaging_limit"]) == 10_000


@pytest.mark.parametrize("payload, expected", [
    ({"whatsapp_business_manager_messaging_limit": {"current_limit": "TIER_2K"}},
     ("TIER_2K", "whatsapp_business_manager_messaging_limit")),
    ({"messaging_limit_tier": "TIER_250"}, ("TIER_250", "messaging_limit_tier")),
    ({"quality_rating": "GREEN"}, (None, None)),
])
def test_tier_extraction_shapes(payload, expected):
    import services.whatsapp_platform.service as svc
    assert svc.extract_messaging_limit(payload) == expected
