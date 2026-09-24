"""The marketing frequency cap is mandatory for every merchant dispatch.

The merchant-facing "ignore frequency cap for this campaign" toggle is
gone. These tests pin the fail-closed contract on every path that used
to honour it:

1. a stale client sending ``dispatch-now?bypass_frequency_cap=true`` is
   refused (HTTP 400) before anything changes;
2. a ``_bypass_frequency_cap`` flag stored on an older campaign neither
   skips the cap nor re-queues rows the cap skipped before;
3. a normal dispatch keeps a recently-contacted customer blocked;
4. resuming a failed / stalled campaign does not open a bypass;
5. the lease still refuses a second worker (double click);
6. a legitimate retry — every attempt provably unsent — is governed by
   the ledger retry rules, not by the cap.

Generic merchant data only; Meta is a scripted fake (no real sends).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import test_campaign_send_ledger as _ledger_suite
from test_campaign_send_ledger import PHONES, FakeMeta, _conn, _logs, _seed

# Shared fixtures (several worker sessions on SQLite / PostgreSQL, the
# scripted Meta fake, zero send delays).
dbf = _ledger_suite.dbf
fake_meta = _ledger_suite.fake_meta
_fast = _ledger_suite._fast
from models import Campaign, CampaignSendLog  # noqa: E402
from services import campaign_dispatcher as disp  # noqa: E402
from services import campaign_send_ledger as ledger  # noqa: E402

CAP_SKIP = f"{disp.REASON_FREQ_CAP}:14d"


@pytest.fixture()
def full_dispatch(monkeypatch):
    """Run the real ``dispatch_campaign`` (snapshot → frequency cap →
    send loop) with the provider pieces stubbed."""
    import core.billing as billing
    import core.wa_usage as wa_usage
    monkeypatch.setattr(billing, "has_billing_access", lambda *a, **k: True)
    monkeypatch.setattr(wa_usage, "check_limit",
                        lambda *a, **k: SimpleNamespace(allowed=True, reason=None,
                                                        used_total=0, limit=None))
    monkeypatch.setattr(disp, "_get_wa_connection", lambda db, tenant_id: _conn())

    def run(Session, campaign_id):
        db = Session()
        try:
            return asyncio.run(disp.dispatch_campaign(db, campaign_id))
        finally:
            db.close()
    return run


def _sent_recently(Session, ids, phone):
    """An earlier campaign of the same merchant reached ``phone`` today."""
    prior = _seed(Session, phones=[phone], campaign_name="حملة سابقة",
                  tenant=SimpleNamespace(id=ids.tenant_id))
    db = Session()
    row = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == prior.campaign_id).one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row.status, row.provider_message_id, row.sent_at = "sent", f"wamid.prior.{phone}", now
    db.commit()
    db.close()


def _cap_skip(Session, campaign_id, phone):
    db = Session()
    row = (db.query(CampaignSendLog)
           .filter(CampaignSendLog.campaign_id == campaign_id,
                   CampaignSendLog.customer_phone_e164 == phone).one())
    row.status, row.skip_reason = disp.LOG_SKIPPED_DUPLICATE, CAP_SKIP
    db.commit()
    db.close()


def _set_legacy_flag(Session, campaign_id, **fields):
    db = Session()
    camp = db.get(Campaign, campaign_id)
    tv = dict(camp.template_variables or {})
    tv["_bypass_frequency_cap"] = "true"
    camp.template_variables = tv
    for k, v in fields.items():
        setattr(camp, k, v)
    db.commit()
    db.close()


def _dispatch_now(Session, ids, monkeypatch, **query):
    import routers.campaigns as rc
    spawned = []
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request, db=None: ids.tenant_id)
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", lambda cid: spawned.append(cid))
    db = Session()
    try:
        out = asyncio.run(rc.dispatch_campaign_now(ids.campaign_id, request=None, db=db, **query))
    finally:
        db.close()
    return out, spawned


# ── 1. stale request ────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", " true ", "on", "t", True])
def test_stale_bypass_request_is_refused_before_anything_changes(dbf, monkeypatch, value):
    ids = _seed(dbf, phones=PHONES[:2])
    _cap_skip(dbf, ids.campaign_id, PHONES[0])
    db = dbf()
    db.get(Campaign, ids.campaign_id).status = "failed"
    db.commit()
    db.close()
    before = _logs(dbf, ids.campaign_id)
    with pytest.raises(HTTPException) as exc:
        _dispatch_now(dbf, ids, monkeypatch, bypass_frequency_cap=value)
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "frequency_cap_bypass_removed"
    db = dbf()
    camp = db.get(Campaign, ids.campaign_id)
    assert "_bypass_frequency_cap" not in (camp.template_variables or {})
    assert camp.status == "failed"                       # untouched
    db.close()
    assert _logs(dbf, ids.campaign_id) == before


def test_stale_bypass_query_is_refused_over_http(dbf, monkeypatch):
    """The real route: a stale dashboard build appending the query string."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.campaigns as rc
    from core.database import get_db

    ids = _seed(dbf, phones=PHONES[:1])
    spawned = []
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request, db=None: ids.tenant_id)
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", lambda cid: spawned.append(cid))
    app = FastAPI()
    app.include_router(rc.router)

    def _db():
        db = dbf()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _db
    client = TestClient(app)
    for qs in ("bypass_frequency_cap=true", "bypass_frequency_cap=on",
               "bypass_frequency_cap=true&bypass_frequency_cap=false"):
        r = client.post(f"/campaigns/{ids.campaign_id}/dispatch-now?{qs}")
        assert r.status_code == 400, (qs, r.text)
        assert r.json()["detail"]["error"] == "frequency_cap_bypass_removed"
    assert spawned == []
    # Without the parameter the same route kicks one dispatch.
    r = client.post(f"/campaigns/{ids.campaign_id}/dispatch-now")
    assert r.status_code == 200 and r.json()["kicked"] is True
    assert "bypass_frequency_cap" not in r.json()
    assert spawned == [ids.campaign_id]


# ── 2. stale stored flag ────────────────────────────────────────────────


def test_stored_legacy_flag_neither_skips_the_cap_nor_revives_rows(dbf, fake_meta, full_dispatch):
    ids = _seed(dbf, phones=PHONES[:3])
    _cap_skip(dbf, ids.campaign_id, PHONES[0])            # skipped by an earlier run
    _sent_recently(dbf, ids, PHONES[1])                   # would be capped this run
    _set_legacy_flag(dbf, ids.campaign_id)
    meta = fake_meta(FakeMeta())
    full_dispatch(dbf, ids.campaign_id)
    logs = _logs(dbf, ids.campaign_id)
    assert logs[PHONES[0]][0] == "skipped_duplicate"      # not revived
    assert logs[PHONES[1]][0] == "skipped_duplicate"      # cap applied
    assert meta.calls == [PHONES[2]]
    db = dbf()
    assert "_bypass_frequency_cap" not in (db.get(Campaign, ids.campaign_id).template_variables or {})
    db.close()


def test_the_removed_bypass_surface_is_gone():
    import inspect
    assert not hasattr(disp, "_revive_frequency_cap_skipped")
    assert "bypass" not in inspect.signature(disp._apply_frequency_cap).parameters


# ── 3. normal merchant dispatch ─────────────────────────────────────────


def test_recently_contacted_customer_stays_blocked(dbf, fake_meta, full_dispatch):
    ids = _seed(dbf, phones=PHONES[:2])
    _sent_recently(dbf, ids, PHONES[0])
    meta = fake_meta(FakeMeta())
    full_dispatch(dbf, ids.campaign_id)
    logs = _logs(dbf, ids.campaign_id)
    assert logs[PHONES[0]][0] == "skipped_duplicate"
    assert meta.calls == [PHONES[1]]


# ── 4. resuming a failed / stalled campaign ─────────────────────────────


@pytest.mark.parametrize("status", ["failed", "paused", "active"])
def test_resuming_does_not_open_a_bypass(dbf, fake_meta, full_dispatch, monkeypatch, status):
    ids = _seed(dbf, phones=PHONES[:3])
    _cap_skip(dbf, ids.campaign_id, PHONES[0])
    _sent_recently(dbf, ids, PHONES[1])
    _set_legacy_flag(dbf, ids.campaign_id, status=status)
    out, spawned = _dispatch_now(dbf, ids, monkeypatch)    # the merchant's resume
    assert out["ok"] is True and spawned == [ids.campaign_id]
    meta = fake_meta(FakeMeta())
    full_dispatch(dbf, ids.campaign_id)                   # what the spawn runs
    logs = _logs(dbf, ids.campaign_id)
    assert logs[PHONES[0]][0] == "skipped_duplicate"
    assert logs[PHONES[1]][0] == "skipped_duplicate"
    assert meta.calls == [PHONES[2]]


# ── 5. double click / lease ─────────────────────────────────────────────


def test_second_click_while_a_worker_runs_is_still_refused(dbf, monkeypatch):
    ids = _seed(dbf, phones=PHONES[:1])
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="live")
    db.close()
    out, spawned = _dispatch_now(dbf, ids, monkeypatch)
    assert out["ok"] is False and out["reason"] == "already_running" and spawned == []
    with pytest.raises(HTTPException) as exc:
        _dispatch_now(dbf, ids, monkeypatch, bypass_frequency_cap="true")
    assert exc.value.status_code == 400


# ── 6. legitimate retry is the ledger's business ────────────────────────


def test_provably_unsent_recipient_is_retried_by_ledger_rules(dbf, fake_meta, full_dispatch):
    ids = _seed(dbf, phones=PHONES[:1])
    meta = fake_meta(FakeMeta({PHONES[0]: ["reject:130429:Rate limit hit"]}))
    full_dispatch(dbf, ids.campaign_id)
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][0] == "failed"
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 1
    db.commit()
    db.close()
    full_dispatch(dbf, ids.campaign_id)
    assert meta.calls == [PHONES[0], PHONES[0]]
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][0] == "sent"


def test_retry_is_still_capped_when_another_campaign_reached_the_customer(
        dbf, fake_meta, full_dispatch):
    """A retry is not a way around the cap: if another campaign reached the
    customer between the rejection and the retry, the retry is skipped."""
    ids = _seed(dbf, phones=PHONES[:1])
    meta = fake_meta(FakeMeta({PHONES[0]: ["reject:130429:Rate limit hit"]}))
    full_dispatch(dbf, ids.campaign_id)
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 1
    db.commit()
    db.close()
    _sent_recently(dbf, ids, PHONES[0])
    full_dispatch(dbf, ids.campaign_id)
    assert meta.calls == [PHONES[0]]                       # the rejection only
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][0] == "skipped_duplicate"


def test_resume_through_the_status_endpoint_keeps_the_cap(dbf, fake_meta, full_dispatch,
                                                         monkeypatch):
    import routers.campaigns as rc
    ids = _seed(dbf, phones=PHONES[:2])
    _cap_skip(dbf, ids.campaign_id, PHONES[0])
    _set_legacy_flag(dbf, ids.campaign_id, status="paused")
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request, db=None: ids.tenant_id)
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", lambda cid: None)
    db = dbf()
    asyncio.run(rc.update_campaign_status(
        ids.campaign_id, rc.UpdateCampaignStatusIn(status="active"), request=None, db=db))
    db.close()
    meta = fake_meta(FakeMeta())
    full_dispatch(dbf, ids.campaign_id)
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][0] == "skipped_duplicate"
    assert meta.calls == [PHONES[1]]
