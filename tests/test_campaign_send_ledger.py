"""tests/test_campaign_send_ledger.py
──────────────────────────────────
Execution-safety contract for manual marketing campaigns.

Incident this pins down (platform-wide, first observed on a large
national-day campaign): a second ``dispatch-now`` started a second
background dispatcher while the first was still sending. Both loaded the
same ``queued`` rows, both called Meta for the same recipient ~0.1–0.2 s
apart, Meta accepted both, and the second write replaced the first wamid on
the recipient's single ``provider_message_id`` — so one copy's status
webhooks became "orphans". A redeploy then killed the workers mid-flight.

Every test uses a scripted fake provider — nothing leaves the process.
Scenarios use generic merchant data (``متجر تجريبي عام``).
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

from models import (  # noqa: E402
    Base,
    Campaign,
    CampaignDispatchLease,
    CampaignSendAttempt,
    CampaignSendLog,
    CampaignStatusEventInbox,
    Customer,
    Tenant,
    WhatsAppTemplate,
)
from services import campaign_dispatcher as disp  # noqa: E402
from services import campaign_send_ledger as ledger  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────


PG_DSN = os.environ.get("NAHLA_CAMPAIGN_LEDGER_PG_DSN")


@pytest.fixture(params=["sqlite", "postgres"])
def dbf(request, tmp_path):
    """Several sessions (workers) sharing one database: file-backed SQLite
    always, and a disposable PostgreSQL schema when
    ``NAHLA_CAMPAIGN_LEDGER_PG_DSN`` is set (real row locks)."""
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("set NAHLA_CAMPAIGN_LEDGER_PG_DSN to run on PostgreSQL")
        import uuid as _uuid
        from sqlalchemy import text
        schema = f"ledger_{_uuid.uuid4().hex[:8]}"
        admin = create_engine(PG_DSN)
        with admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(PG_DSN, connect_args={"options": f"-csearch_path={schema}"})
        Base.metadata.create_all(engine)
        yield sessionmaker(bind=engine, autocommit=False, autoflush=False)
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
        return

    engine = create_engine(
        f"sqlite:///{tmp_path / 'ledger.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    # Same session settings as production (database/session.py).
    yield sessionmaker(bind=engine, autocommit=False, autoflush=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(disp, "INTER_MESSAGE_DELAY", 0)
    monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_BATCH_PAUSE_SECONDS", 0)
    monkeypatch.setattr(disp, "MARKETING_CAMPAIGN_BATCH_SIZE", 3)
    # Conversation recording needs the full app; the dispatcher already
    # treats its failure as non-fatal.
    monkeypatch.setattr(disp, "_record_campaign_message", lambda *a, **k: None)


class FakeMeta:
    """Scripted stand-in for ``provider_send_message``.

    ``script[phone]`` is a list of behaviours consumed per call:
    ``accept`` | ``reject:<code>:<message>`` | ``timeout`` | ``connect`` |
    ``no_wamid`` | ``gateway_html``. Default ``accept``.
    """

    def __init__(self, script=None, *, delay=0.0, before_return=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []
        self.delay = delay
        self.before_return = before_return
        self._n = 0
        self._lock = threading.Lock()

    async def __call__(self, db, conn, *, tenant_id, operation, phone_id, payload, **kw):
        # The real provider path commits the caller's session before the
        # HTTP call (token state persistence) — which is what released the
        # pre-fix dispatcher's uncommitted "sending" flip to a second worker.
        db.commit()
        phone = payload["to"]
        with self._lock:
            self.calls.append(phone)
            self._n += 1
            n = self._n
            steps = self.script.get(phone) or []
            step = steps.pop(0) if steps else "accept"
        if self.delay:
            await asyncio.sleep(self.delay)
        if step == "timeout":
            raise httpx.ReadTimeout("read timed out")
        if step == "connect":
            raise httpx.ConnectError("connection refused")
        if step == "no_wamid":
            return {"messaging_product": "whatsapp"}, None
        if step == "gateway_html":
            return {"error": {"type": "non_json_response", "message": "502 bad gateway"}}, None
        if step.startswith("reject:"):
            _, code, msg = step.split(":", 2)
            return {"error": {"code": int(code), "message": msg}}, None
        wamid = f"wamid.fake.{n}"
        if self.before_return:
            await self.before_return(phone, wamid)
        return {"messages": [{"id": wamid}]}, None


@pytest.fixture()
def fake_meta(monkeypatch):
    holder = {}

    def install(meta: FakeMeta) -> FakeMeta:
        import services.whatsapp_platform.service as svc
        monkeypatch.setattr(svc, "provider_send_message", meta)
        holder["m"] = meta
        return meta

    return install


def _conn(**over):
    base = dict(
        id=1, tenant_id=None, phone_number_id="PN-GENERIC-1",
        business_manager_id="BM-GENERIC-1", meta_business_account_id=None,
        whatsapp_business_account_id="WABA-1",
        meta_messaging_limit="TIER_10K",
        meta_tier_updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _seed(Session, *, phones, campaign_name="حملة عامة", tenant=None):
    db = Session()
    if tenant is None:
        n = db.query(Tenant).count()
        tenant = Tenant(name=f"متجر تجريبي عام {n}", is_active=True)
        db.add(tenant)
        db.commit()
    tpl = WhatsAppTemplate(
        tenant_id=tenant.id, name="generic_offer", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "مرحبا {{1}}"}],
    )
    db.add(tpl)
    db.commit()
    camp = Campaign(
        tenant_id=tenant.id, name=campaign_name, campaign_type="broadcast",
        template_id=str(tpl.id), template_name=tpl.name, template_language="ar",
        template_category="MARKETING", audience_type="all", status="active",
        audience_count=len(phones),
    )
    db.add(camp)
    db.commit()
    for i, ph in enumerate(phones):
        if not db.query(Customer).filter(Customer.tenant_id == tenant.id,
                                         Customer.normalized_phone == ph).first():
            db.add(Customer(tenant_id=tenant.id, phone=ph, normalized_phone=ph,
                            name=f"عميل {i}", extra_metadata={}))
        db.add(CampaignSendLog(
            tenant_id=tenant.id, campaign_id=camp.id, customer_phone_e164=ph,
            template_name=tpl.name, template_language="ar", status="queued",
        ))
    db.commit()
    ids = SimpleNamespace(tenant_id=tenant.id, campaign_id=camp.id, template_id=tpl.id)
    db.close()
    return ids


async def _run(Session, ids, conn, *, ctx=None):
    db = Session()
    try:
        camp = db.get(Campaign, ids.campaign_id)
        tpl = db.get(WhatsAppTemplate, ids.template_id)
        customers = {
            c.normalized_phone: c
            for c in db.query(Customer).filter(Customer.tenant_id == ids.tenant_id)
        }
        return await disp._dispatch_queued_rows(
            db, campaign=camp, template=tpl, wa_conn=conn, store_name="متجر تجريبي عام",
            auto_coupon=False, discount_pct=None, customers_by_phone=customers, ctx=ctx,
        )
    finally:
        db.close()


def _logs(Session, campaign_id):
    db = Session()
    try:
        return {
            r.customer_phone_e164: (r.status, r.error_code, r.provider_message_id)
            for r in db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == campaign_id)
        }
    finally:
        db.close()


def _attempts(Session, campaign_id):
    db = Session()
    try:
        return [
            (a.customer_phone_e164, a.attempt_no, a.state, a.provider_message_id)
            for a in db.query(CampaignSendAttempt)
            .filter(CampaignSendAttempt.campaign_id == campaign_id)
            .order_by(CampaignSendAttempt.id)
        ]
    finally:
        db.close()


PHONES = [f"+96650000{n:04d}" for n in range(1, 8)]


def _age(Session, model, seconds, **filters):
    db = Session()
    q = db.query(model)
    for k, v in filters.items():
        q = q.filter(getattr(model, k) == v)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)
    for r in q:
        r.updated_at = old
        if hasattr(r, "claimed_at"):
            r.claimed_at = old
    db.commit()
    db.close()


# ── 1. Concurrency ──────────────────────────────────────────────────────


def test_two_concurrent_dispatches_send_each_recipient_once(dbf, fake_meta):
    """The incident: two dispatchers for one campaign at the same time.
    Exactly one gets the lease; every recipient reaches Meta once."""
    ids = _seed(dbf, phones=PHONES)
    meta = fake_meta(FakeMeta(delay=0.01))

    async def both():
        return await asyncio.gather(_run(dbf, ids, _conn()), _run(dbf, ids, _conn()))

    r1, r2 = asyncio.run(both())
    assert sorted(meta.calls) == sorted(PHONES)
    skipped = [r for r in (r1, r2) if any("held_by_other_worker" in e for e in r[2])]
    assert len(skipped) == 1
    assert {s for s, _, _ in _logs(dbf, ids.campaign_id).values()} == {"sent"}
    assert len(_attempts(dbf, ids.campaign_id)) == len(PHONES)


def test_overlapping_workers_after_lease_expiry_never_double_claim(dbf, fake_meta):
    """A stalled worker whose lease lapsed and was taken over stops at its
    very next claim; the new holder finishes; nobody gets two requests."""
    ids = _seed(dbf, phones=PHONES)
    state = {"stolen": False}

    async def steal(phone, wamid):
        # During worker A's first request its lease expires and B takes it.
        if not state["stolen"]:
            state["stolen"] = True
            db = dbf()
            lease = db.get(CampaignDispatchLease, ids.campaign_id)
            lease.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.commit()
            assert ledger.acquire_lease(db, campaign_id=ids.campaign_id,
                                        tenant_id=ids.tenant_id, owner="worker-B").acquired
            db.close()

    meta = fake_meta(FakeMeta(before_return=steal))
    ctx_a = disp.DispatchRunContext("worker-A")
    db = dbf()
    assert ledger.acquire_lease(db, campaign_id=ids.campaign_id,
                                tenant_id=ids.tenant_id, owner="worker-A").acquired
    db.close()
    asyncio.run(_run(dbf, ids, _conn(), ctx=ctx_a))
    assert ctx_a.lease_lost is True
    assert meta.calls == [PHONES[0]]
    ctx_b = disp.DispatchRunContext("worker-B")
    asyncio.run(_run(dbf, ids, _conn(), ctx=ctx_b))
    assert sorted(meta.calls) == sorted(PHONES)


def test_claim_compare_and_set_admits_exactly_one_session(dbf):
    ids = _seed(dbf, phones=PHONES[:1])
    a, b = dbf(), dbf()
    assert ledger.acquire_lease(a, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id,
                                owner="A").acquired
    camp_a = a.get(Campaign, ids.campaign_id)
    log_id = a.query(CampaignSendLog.id).scalar()
    # B holds a stale ORM copy that still says "queued" — the old bug.
    stale = b.get(CampaignSendLog, log_id)
    assert stale.status == "queued"
    first = ledger.claim_recipient(a, log_id=log_id, campaign=camp_a, owner="A",
                                   scope_key="bm:X", phone_number_id="PN")
    assert first.reason == "claimed"
    # Same owner id so the lease heartbeat passes: only the row CAS decides.
    second = ledger.claim_recipient(b, log_id=log_id, campaign=b.get(Campaign, ids.campaign_id),
                                    owner="A", scope_key="bm:X", phone_number_id="PN")
    assert second.reason == "not_queued" and second.attempt is None
    a.close()
    b.close()


# ── 2. Crashes ──────────────────────────────────────────────────────────


def test_crash_before_request_returns_recipient_to_queue_and_sends_once(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:2])
    db = dbf()
    assert ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id,
                                owner="dead").acquired
    log_id = db.query(CampaignSendLog.id).filter(
        CampaignSendLog.customer_phone_e164 == PHONES[0]).scalar()
    claim = ledger.claim_recipient(db, log_id=log_id, campaign=db.get(Campaign, ids.campaign_id),
                                   owner="dead", scope_key=None, phone_number_id=None)
    assert claim.attempt is not None
    # Worker dies here: no request_started. Its lease and attempt go stale.
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db.commit()
    db.close()
    _age(dbf, CampaignSendAttempt, 600, campaign_id=ids.campaign_id)
    _age(dbf, CampaignSendLog, 600, campaign_id=ids.campaign_id)

    meta = fake_meta(FakeMeta())
    asyncio.run(_run(dbf, ids, _conn()))
    assert sorted(meta.calls) == sorted(PHONES[:2])
    states = [s for p, _, s, _ in _attempts(dbf, ids.campaign_id) if p == PHONES[0]]
    assert states == [ledger.ATTEMPT_ABANDONED, ledger.ATTEMPT_ACCEPTED]


def test_crash_after_request_started_is_uncertain_and_never_resent(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:2])
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="dead")
    log_id = db.query(CampaignSendLog.id).filter(
        CampaignSendLog.customer_phone_e164 == PHONES[0]).scalar()
    att = ledger.claim_recipient(db, log_id=log_id, campaign=db.get(Campaign, ids.campaign_id),
                                 owner="dead", scope_key=None, phone_number_id=None).attempt
    ledger.mark_request_started(db, att)
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db.commit()
    db.close()
    _age(dbf, CampaignSendAttempt, 600, campaign_id=ids.campaign_id)

    meta = fake_meta(FakeMeta())
    asyncio.run(_run(dbf, ids, _conn()))
    assert meta.calls == [PHONES[1]]
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][0] == ledger.LOG_UNCERTAIN
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 0
    db.close()


def test_crash_after_meta_accepts_but_before_saving_is_not_resent(dbf, fake_meta, monkeypatch):
    """Meta accepted, the worker died before persisting the wamid."""
    ids = _seed(dbf, phones=PHONES[:1])
    meta = fake_meta(FakeMeta())

    class Died(BaseException):
        pass

    def die(*a, **k):
        raise Died()

    monkeypatch.setattr(ledger, "record_accepted", die)
    with pytest.raises(Died):
        asyncio.run(_run(dbf, ids, _conn()))
    monkeypatch.undo()
    monkeypatch.setattr(disp, "INTER_MESSAGE_DELAY", 0)
    monkeypatch.setattr(disp, "_record_campaign_message", lambda *a, **k: None)
    import services.whatsapp_platform.service as svc
    monkeypatch.setattr(svc, "provider_send_message", meta)
    # Process gone: lease and in-flight attempt age out.
    db = dbf()
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    if lease is not None:
        lease.owner = None
        lease.expires_at = None
    db.commit()
    db.close()
    _age(dbf, CampaignSendAttempt, 600, campaign_id=ids.campaign_id)

    asyncio.run(_run(dbf, ids, _conn()))
    assert meta.calls == [PHONES[0]]  # only the original request
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][0] == ledger.LOG_UNCERTAIN


# ── 3. Timeouts and ambiguous answers ───────────────────────────────────


@pytest.mark.parametrize("behaviour", ["timeout", "no_wamid", "gateway_html"])
def test_unknown_external_outcome_is_uncertain_not_retried(dbf, fake_meta, behaviour):
    ids = _seed(dbf, phones=PHONES[:2])
    meta = fake_meta(FakeMeta({PHONES[0]: [behaviour]}))
    asyncio.run(_run(dbf, ids, _conn()))
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][0] == ledger.LOG_UNCERTAIN
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 0
    db.commit()
    db.close()
    asyncio.run(_run(dbf, ids, _conn()))
    assert meta.calls.count(PHONES[0]) == 1


def test_connect_error_is_provably_unsent_and_retried_once(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:1])
    meta = fake_meta(FakeMeta({PHONES[0]: ["connect"]}))
    asyncio.run(_run(dbf, ids, _conn()))
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][:2] == ("failed", "transport_not_sent")
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 1
    db.commit()
    db.close()
    asyncio.run(_run(dbf, ids, _conn()))
    assert meta.calls == [PHONES[0], PHONES[0]]
    assert [a[1:3] for a in _attempts(dbf, ids.campaign_id)] == [
        (1, ledger.ATTEMPT_NOT_SENT), (2, ledger.ATTEMPT_ACCEPTED),
    ]


def test_consecutive_uncertain_sends_pause_the_campaign(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES)
    meta = fake_meta(FakeMeta({p: ["timeout"] for p in PHONES}))
    ctx = disp.DispatchRunContext("w")
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w")
    db.close()
    asyncio.run(_run(dbf, ids, _conn(), ctx=ctx))
    assert ctx.pause_reason == ledger.PAUSE_UNCERTAIN
    assert len(meta.calls) == ledger.UNCERTAIN_BREAKER_THRESHOLD


# ── 4. Webhooks ─────────────────────────────────────────────────────────


def test_webhook_before_attempt_commit_is_applied_after(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:1])

    async def early(phone, wamid):
        s = dbf()
        res = ledger.apply_status_event(s, wamid=wamid, status="delivered",
                                        provider_timestamp=1790000000)
        assert res.matched is False  # attempt not committed yet
        s.close()

    fake_meta(FakeMeta(before_return=early))
    asyncio.run(_run(dbf, ids, _conn()))
    db = dbf()
    att = db.query(CampaignSendAttempt).one()
    row = db.query(CampaignSendLog).one()
    assert att.delivered_at is not None and att.delivery_state == "delivered"
    assert row.delivered_at is not None
    assert db.query(CampaignStatusEventInbox).filter(
        CampaignStatusEventInbox.applied_at.is_(None)).count() == 0
    db.close()


def test_duplicate_and_out_of_order_webhooks(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:1])
    fake_meta(FakeMeta())
    asyncio.run(_run(dbf, ids, _conn()))
    db = dbf()
    wamid = db.query(CampaignSendAttempt.provider_message_id).scalar()
    r1 = ledger.apply_status_event(db, wamid=wamid, status="read", provider_timestamp=1790000100)
    r2 = ledger.apply_status_event(db, wamid=wamid, status="read", provider_timestamp=1790000100)
    r3 = ledger.apply_status_event(db, wamid=wamid, status="delivered", provider_timestamp=1790000050)
    assert r1.changed and r2.duplicate and not r2.changed
    assert r3.changed  # delivered arriving after read still records its own time
    att = db.query(CampaignSendAttempt).one()
    row = db.query(CampaignSendLog).one()
    assert att.delivery_state == "read"
    assert row.read_at is not None and row.delivered_at is not None
    assert row.failed_at is None
    db.close()


def test_failed_after_accept_is_separated_and_not_retried(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:2])
    fake_meta(FakeMeta())
    asyncio.run(_run(dbf, ids, _conn()))
    db = dbf()
    wamid = (db.query(CampaignSendAttempt.provider_message_id)
             .filter(CampaignSendAttempt.customer_phone_e164 == PHONES[0]).scalar())
    ledger.apply_status_event(
        db, wamid=wamid, status="failed", provider_timestamp=1790000000,
        errors=[{"code": 131048, "title": "Spam Rate limit hit"}],
    )
    att = db.query(CampaignSendAttempt).filter(CampaignSendAttempt.provider_message_id == wamid).one()
    assert att.post_accept_error_code == "spam_rate_limit"
    assert att.post_accept_raw_error_code == "131048"
    from routers.campaigns import _campaign_canonical_stats
    stats = _campaign_canonical_stats(db, [ids.campaign_id])[ids.campaign_id]
    assert stats["meta_accepted"] == 2
    assert stats["failed_after_accept"] == 1
    assert stats["failed_before_accept"] == 0
    assert stats["failed_total"] == 1
    assert stats["recipients_failed_after_accept"] == 1
    after = [e for e in stats["error_breakdown"] if e["phase"] == "after_accept"]
    assert after == [{"phase": "after_accept", "key": "spam_rate_limit",
                      "label_ar": after[0]["label_ar"], "raw_code": "spam_rate_limit",
                      "count": 1, "retryable": False}]
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 0
    db.close()


def test_each_wamid_resolves_to_its_own_attempt(dbf):
    """Two accepted copies (the pre-fix duplicate): each webhook lands on
    the attempt that owns its wamid; neither overwrites the other."""
    ids = _seed(dbf, phones=PHONES[:1])
    db = dbf()
    log = db.query(CampaignSendLog).one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for n, w in ((1, "wamid.A"), (2, "wamid.B")):
        db.add(CampaignSendAttempt(
            tenant_id=ids.tenant_id, campaign_id=ids.campaign_id, send_log_id=log.id,
            customer_phone_e164=log.customer_phone_e164, attempt_no=n, state="accepted",
            provider_message_id=w, claimed_at=now, request_started_at=now, accepted_at=now,
        ))
    log.status, log.provider_message_id = "sent", "wamid.B"
    db.commit()
    ledger.apply_status_event(db, wamid="wamid.A", status="failed",
                              errors=[{"code": 131049, "title": "healthy ecosystem"}])
    assert ledger.recipient_outcomes(db, ids.campaign_id)[log.id] == \
        ledger.OUTCOME_ACCEPTED_MULTIPLE_UNPROVEN
    ledger.apply_status_event(db, wamid="wamid.B", status="delivered")
    a, b = db.query(CampaignSendAttempt).order_by(CampaignSendAttempt.attempt_no).all()
    assert a.delivery_state == "failed" and b.delivery_state == "delivered"
    assert ledger.recipient_outcomes(db, ids.campaign_id)[log.id] == ledger.OUTCOME_DELIVERED_ONCE
    ledger.apply_status_event(db, wamid="wamid.A", status="read")  # contradictory but possible
    assert ledger.recipient_outcomes(db, ids.campaign_id)[log.id] == \
        ledger.OUTCOME_DELIVERED_MULTIPLE
    db.close()


# ── 5. Retries across attempts ──────────────────────────────────────────


def test_rejected_then_retried_keeps_both_attempts(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:1])
    meta = fake_meta(FakeMeta({PHONES[0]: ["reject:130429:Rate limit hit"]}))
    asyncio.run(_run(dbf, ids, _conn()))
    db = dbf()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 1
    db.commit()
    db.close()
    asyncio.run(_run(dbf, ids, _conn()))
    assert meta.calls == [PHONES[0], PHONES[0]]
    atts = _attempts(dbf, ids.campaign_id)
    assert [a[2] for a in atts] == [ledger.ATTEMPT_REJECTED, ledger.ATTEMPT_ACCEPTED]
    assert _logs(dbf, ids.campaign_id)[PHONES[0]][2] == atts[1][3]


def test_delivered_recipient_is_never_requeued_even_if_marked_failed(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:1])
    fake_meta(FakeMeta())
    asyncio.run(_run(dbf, ids, _conn()))
    db = dbf()
    row = db.query(CampaignSendLog).one()
    # Corrupt state an operator might produce by hand.
    row.status, row.error_code = "failed", "rate_limit"
    db.commit()
    assert disp.reschedule_failed_for_retry(db, ids.campaign_id) == 0
    db.close()


# ── 6. Frequency cap ────────────────────────────────────────────────────


def test_frequency_cap_counts_uncertain_and_skips_proven_failures(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:3])
    fake_meta(FakeMeta({PHONES[1]: ["timeout"]}))
    asyncio.run(_run(dbf, ids, _conn()))
    db = dbf()
    w0 = (db.query(CampaignSendAttempt.provider_message_id)
          .filter(CampaignSendAttempt.customer_phone_e164 == PHONES[0]).scalar())
    ledger.apply_status_event(db, wamid=w0, status="failed",
                              errors=[{"code": 131049, "title": "ecosystem"}])
    db.close()
    ids2 = _seed(dbf, phones=PHONES[:3], campaign_name="حملة ثانية",
                 tenant=SimpleNamespace(id=ids.tenant_id))
    db = dbf()
    skipped = disp._apply_frequency_cap(db, ids.tenant_id, ids2.campaign_id)
    db.commit()
    got = {r.customer_phone_e164: r.status for r in db.query(CampaignSendLog)
           .filter(CampaignSendLog.campaign_id == ids2.campaign_id)}
    db.close()
    assert got[PHONES[0]] == "queued"              # proven not received
    assert got[PHONES[1]] == "skipped_duplicate"   # uncertain: may have it
    assert got[PHONES[2]] == "skipped_duplicate"   # accepted, no failure
    assert skipped == 2


def test_frequency_cap_keeps_legacy_failed_after_accept_rows_blocking(dbf):
    """Pre-ledger rows could have had their delivered wamid overwritten
    by a failed duplicate — their failed_at proves nothing."""
    ids = _seed(dbf, phones=PHONES[:1])
    db = dbf()
    row = db.query(CampaignSendLog).one()
    now = datetime.now(timezone.utc)
    row.status, row.provider_message_id, row.sent_at, row.failed_at = "sent", "wamid.legacy", now, now
    db.commit()
    db.close()
    ids2 = _seed(dbf, phones=PHONES[:1], campaign_name="ثانية",
                 tenant=SimpleNamespace(id=ids.tenant_id))
    db = dbf()
    assert disp._apply_frequency_cap(db, ids.tenant_id, ids2.campaign_id) == 1
    db.close()


# ── 7. Shared Meta messaging limit ──────────────────────────────────────


def _seed_scope_usage(Session, ids, scope, n, *, phone_prefix="+96655"):
    db = Session()
    log = db.query(CampaignSendLog).filter(CampaignSendLog.campaign_id == ids.campaign_id).first()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(n):
        db.add(CampaignSendAttempt(
            tenant_id=ids.tenant_id, campaign_id=ids.campaign_id, send_log_id=log.id,
            customer_phone_e164=f"{phone_prefix}{i:07d}", attempt_no=1000 + i,
            state="accepted", messaging_scope_key=scope, claimed_at=now,
            request_started_at=now, accepted_at=now, provider_message_id=f"w.{scope}.{i}",
        ))
    db.commit()
    db.close()


def test_shared_limit_across_campaigns_and_numbers_pauses_before_sending(dbf, fake_meta):
    """Two campaigns on two numbers in one business portfolio share the
    limit: once the portfolio's 24h budget is used, nothing more is sent."""
    other = _seed(dbf, phones=["+966511111111"], campaign_name="حملة الرقم الآخر")
    tier = dict(meta_messaging_limit="TIER_250")
    budget = 250 * ledger.CAMPAIGN_BUDGET_PERCENT // 100
    _seed_scope_usage(dbf, other, "bm:BM-GENERIC-1", budget)
    ids = _seed(dbf, phones=PHONES[:2], campaign_name="حملة الرقم الأول")
    meta = fake_meta(FakeMeta())
    ctx = disp.DispatchRunContext("w")
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w")
    db.close()
    asyncio.run(_run(dbf, ids, _conn(phone_number_id="PN-OTHER", **tier), ctx=ctx))
    assert meta.calls == []
    assert ctx.pause_reason == ledger.PAUSE_MESSAGING_LIMIT
    assert {s for s, _, _ in _logs(dbf, ids.campaign_id).values()} == {"queued"}


def test_pre_ledger_sends_in_the_window_count_against_the_limit(dbf, fake_meta):
    """Sends recorded before the attempt ledger existed still use the
    portfolio's 24h budget (resuming an interrupted campaign must not
    assume a fresh budget)."""
    ids = _seed(dbf, phones=PHONES[:2])
    db = dbf()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    budget = 250 * ledger.CAMPAIGN_BUDGET_PERCENT // 100
    for i in range(budget):
        db.add(CampaignSendLog(
            tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
            customer_phone_e164=f"+9665777{i:05d}", status="sent",
            provider_message_id=f"legacy.{i}", sent_at=now, attempt_count=1,
        ))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    ctx = disp.DispatchRunContext("w")
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w")
    db.close()
    conn = _conn(meta_messaging_limit="TIER_250", tenant_id=ids.tenant_id)
    asyncio.run(_run(dbf, ids, conn, ctx=ctx))
    assert meta.calls == []
    assert ctx.pause_reason == ledger.PAUSE_MESSAGING_LIMIT


def _race_two_campaigns(dbf, fake_meta, monkeypatch, *, lock_enabled):
    """Two campaigns (two tenants, two numbers, one business portfolio) start
    at the same instant with ONE slot left in the portfolio's budget. A delay
    between counting usage and reserving widens the race window."""
    import time as _time
    budget = 250 * ledger.CAMPAIGN_BUDGET_PERCENT // 100
    first = _seed(dbf, phones=[f"+96651000{n:04d}" for n in range(3)], campaign_name="حملة أ")
    second = _seed(dbf, phones=[f"+96652000{n:04d}" for n in range(3)], campaign_name="حملة ب")
    _seed_scope_usage(dbf, first, "bm:BM-GENERIC-1", budget - 1)
    real_budget = ledger.messaging_budget

    def slow_budget(*a, **k):
        b = real_budget(*a, **k)
        _time.sleep(0.3)
        return b

    monkeypatch.setattr(ledger, "messaging_budget", slow_budget)
    if not lock_enabled:
        monkeypatch.setattr(ledger, "lock_messaging_scope", lambda *a, **k: None)
    meta = fake_meta(FakeMeta())
    barrier = threading.Barrier(2)
    ctxs = {}

    def worker(ids, number):
        ctx = disp.DispatchRunContext(f"w-{ids.campaign_id}")
        ctxs[ids.campaign_id] = ctx
        s = dbf()
        assert ledger.acquire_lease(s, campaign_id=ids.campaign_id,
                                    tenant_id=ids.tenant_id, owner=ctx.owner).acquired
        s.close()
        barrier.wait()
        asyncio.run(_run(dbf, ids, _conn(phone_number_id=number, meta_messaging_limit="TIER_250"),
                         ctx=ctx))

    threads = [threading.Thread(target=worker, args=(first, "PN-A")),
               threading.Thread(target=worker, args=(second, "PN-B"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return meta, ctxs, budget


def test_shared_limit_holds_under_truly_concurrent_campaigns(dbf, fake_meta, monkeypatch):
    meta, ctxs, _ = _race_two_campaigns(dbf, fake_meta, monkeypatch, lock_enabled=True)
    assert len(meta.calls) == 1                 # exactly the one remaining slot
    assert all(c.pause_reason == ledger.PAUSE_MESSAGING_LIMIT for c in ctxs.values())


def test_shared_limit_race_is_detected_without_the_scope_lock(dbf, fake_meta, monkeypatch, request):
    """Negative control: with the scope lock removed, the same race
    overshoots the budget on PostgreSQL — proving the test above would
    catch a regression. (SQLite serialises every writer, so it cannot show
    the race.)"""
    if request.node.callspec.params.get("dbf") != "postgres":
        pytest.skip("needs PostgreSQL row-level concurrency")
    meta, _, _ = _race_two_campaigns(dbf, fake_meta, monkeypatch, lock_enabled=False)
    assert len(meta.calls) > 1


def test_stale_or_missing_limit_falls_back_to_starting_tier(dbf):
    db = dbf()
    stale = _conn(meta_tier_updated_at=datetime(2020, 1, 1))
    assert ledger.messaging_budget(db, stale).limit == ledger.DEFAULT_MESSAGING_LIMIT
    assert ledger.messaging_budget(db, stale).limit_source == "meta_stale_fallback"
    missing = _conn(meta_messaging_limit=None)
    assert ledger.messaging_budget(db, missing).limit_source == "unknown_fallback"
    fresh = _conn(meta_messaging_limit="TIER_2K")
    assert ledger.messaging_budget(db, fresh).limit == 2000
    unlimited = _conn(meta_messaging_limit="TIER_UNLIMITED")
    assert ledger.messaging_budget(db, unlimited).budget is None
    db.close()


def test_post_accept_spam_failures_trip_the_breaker(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES[:2])
    db = dbf()
    log = db.query(CampaignSendLog).first()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(ledger.POST_ACCEPT_BREAKER_THRESHOLDS["spam_rate_limit"]):
        db.add(CampaignSendAttempt(
            tenant_id=ids.tenant_id, campaign_id=ids.campaign_id, send_log_id=log.id,
            customer_phone_e164=f"+9665999{i:05d}", attempt_no=100 + i, state="accepted",
            messaging_scope_key="bm:BM-GENERIC-1", claimed_at=now, failed_at=now,
            post_accept_error_code="spam_rate_limit",
        ))
    db.commit()
    db.close()
    meta = fake_meta(FakeMeta())
    ctx = disp.DispatchRunContext("w")
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w")
    db.close()
    asyncio.run(_run(dbf, ids, _conn(), ctx=ctx))
    assert meta.calls == []
    assert ctx.pause_reason == ledger.PAUSE_PROVIDER_THROTTLING


# ── 8. Safe stop and the dispatch-now guard ─────────────────────────────


def test_stop_request_prevents_new_sends(dbf, fake_meta):
    ids = _seed(dbf, phones=PHONES)

    async def stop_after_first(phone, wamid):
        s = dbf()
        ledger.request_stop(s, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id)
        s.close()

    meta = fake_meta(FakeMeta(before_return=stop_after_first))
    asyncio.run(_run(dbf, ids, _conn()))
    assert len(meta.calls) == 1
    statuses = [s for s, _, _ in _logs(dbf, ids.campaign_id).values()]
    assert statuses.count("sent") == 1 and statuses.count("queued") == len(PHONES) - 1
    db = dbf()
    assert ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id,
                                owner="later").reason == "stop_requested"
    db.close()


def test_dispatch_now_refuses_while_a_worker_holds_the_lease(dbf, monkeypatch):
    import routers.campaigns as rc
    ids = _seed(dbf, phones=PHONES[:1])
    spawned = []
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request: ids.tenant_id)
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", lambda cid: spawned.append(cid))
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="live")
    out = asyncio.run(rc.dispatch_campaign_now(ids.campaign_id, request=None, db=db,
                                               bypass_frequency_cap=False))
    assert out["ok"] is False and out["reason"] == "already_running"
    assert spawned == []
    # Once the worker is gone, the same call kicks exactly one dispatch.
    ledger.release_lease(db, campaign_id=ids.campaign_id, owner="live")
    out = asyncio.run(rc.dispatch_campaign_now(ids.campaign_id, request=None, db=db,
                                               bypass_frequency_cap=False))
    assert out["ok"] is True and spawned == [ids.campaign_id]
    db.close()


def test_campaign_list_says_stalled_when_no_worker_is_alive(dbf, fake_meta):
    import routers.campaigns as rc
    ids = _seed(dbf, phones=PHONES[:3])
    fake_meta(FakeMeta({PHONES[2]: ["timeout"]}))
    db = dbf()
    ledger.acquire_lease(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner="w")
    db.close()
    ctx = disp.DispatchRunContext("w")
    asyncio.run(_run(dbf, ids, _conn(), ctx=ctx))
    db = dbf()
    ledger.release_lease(db, campaign_id=ids.campaign_id, owner="w")
    camp = db.get(Campaign, ids.campaign_id)
    out = rc._campaigns_payload(db, [camp])[0]
    assert out["execution"]["worker_running"] is False
    assert out["lifecycle"] == "stalled"
    assert out["stats"]["uncertain"] == 1
    assert out["stats"]["meta_accepted"] == 2
    db.close()


# ── 10. Deploy path: boot-time create_all and a missing ledger ──────────


def _schema_snapshot(engine, schema=None):
    from sqlalchemy import inspect as _inspect
    insp = _inspect(engine)
    snap = {}
    for t in insp.get_table_names(schema=schema):
        snap[t] = {
            "columns": sorted((c["name"], str(c["type"]), bool(c["nullable"]))
                              for c in insp.get_columns(t, schema=schema)),
            "indexes": sorted((i["name"], tuple(i["column_names"]), bool(i["unique"]))
                              for i in insp.get_indexes(t, schema=schema)),
            "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"])
                          for f in insp.get_foreign_keys(t, schema=schema)),
        }
    return snap


def _boot_proof(engine, schema=None):
    """Pre-PR schema → the boot's ``Base.metadata.create_all`` → only the
    ledger tables (with their indexes) appear; nothing else changes; a
    second boot is a no-op."""
    pre_tables = [t for t in Base.metadata.sorted_tables if t.name not in ledger.LEDGER_TABLES]
    Base.metadata.create_all(engine, tables=pre_tables)
    before = _schema_snapshot(engine, schema)
    Base.metadata.create_all(engine)          # what backend/main.py does at boot
    after = _schema_snapshot(engine, schema)
    assert set(after) - set(before) == set(ledger.LEDGER_TABLES)
    assert {t: after[t] for t in before} == before   # existing tables untouched
    idx = {name: (cols, uniq) for name, cols, uniq in after["campaign_send_attempts"]["indexes"]}
    assert idx["uq_campaign_send_attempt_wamid"] == (("provider_message_id",), True)
    assert idx["uq_campaign_send_attempt_log_no"] == (("send_log_id", "attempt_no"), True)
    inbox = {n: u for n, _, u in after["campaign_status_event_inbox"]["indexes"]}
    assert inbox["uq_campaign_status_event_wamid_status"] is True
    Base.metadata.create_all(engine)
    assert _schema_snapshot(engine, schema) == after


def test_boot_create_all_adds_only_the_ledger_tables(request):
    if not PG_DSN:
        pytest.skip("set NAHLA_CAMPAIGN_LEDGER_PG_DSN to run on PostgreSQL")
    import uuid as _uuid
    from sqlalchemy import text
    schema = f"boot_{_uuid.uuid4().hex[:8]}"
    admin = create_engine(PG_DSN)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(PG_DSN, connect_args={"options": f"-csearch_path={schema}"})
    try:
        _boot_proof(engine, schema)
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


PRODLIKE_DSN = os.environ.get("NAHLA_CAMPAIGN_LEDGER_PRODLIKE_DSN")


@pytest.mark.skipif(not PRODLIKE_DSN, reason="set NAHLA_CAMPAIGN_LEDGER_PRODLIKE_DSN "
                    "to a DISPOSABLE database migrated with `alembic upgrade 0093`")
def test_boot_create_all_on_a_production_like_database():
    """Same proof on a database built the way production is: the pinned
    bootstrap chain (``alembic upgrade 0093``) plus the pre-PR create_all."""
    from sqlalchemy import inspect as _inspect
    engine = create_engine(PRODLIKE_DSN)
    if set(ledger.LEDGER_TABLES) & set(_inspect(engine).get_table_names()):
        engine.dispose()
        pytest.skip("ledger tables already exist — point this at a freshly migrated database")
    try:
        _boot_proof(engine)
    finally:
        engine.dispose()


def _drop_ledger(Session):
    from sqlalchemy import text
    engine = Session.kw["bind"]
    with engine.begin() as c:
        for t in ledger.LEDGER_TABLES:
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))


def test_missing_ledger_tables_fail_closed(dbf, fake_meta, monkeypatch, request):
    """Boot has not created (or failed to create) the ledger tables: nothing
    is sent, the campaign is not falsely marked failed, and read paths keep
    working. The next tick after the tables exist proceeds normally."""
    import routers.campaigns as rc
    import core.billing as billing
    ids = _seed(dbf, phones=PHONES[:2])
    _drop_ledger(dbf)
    meta = fake_meta(FakeMeta())
    monkeypatch.setattr(billing, "has_billing_access", lambda *a, **k: True)

    assert asyncio.run(_run(dbf, ids, _conn()))[2] == ["dispatch_skipped:ledger_unavailable"]
    db = dbf()
    out = asyncio.run(disp.dispatch_campaign(db, ids.campaign_id))
    assert out["status"] == "ledger_unavailable"
    db.close()
    spawned = []
    monkeypatch.setattr(rc, "resolve_tenant_id", lambda request: ids.tenant_id)
    monkeypatch.setattr(rc, "_spawn_dispatch_in_background", lambda cid: spawned.append(cid))
    db = dbf()
    now = asyncio.run(rc.dispatch_campaign_now(ids.campaign_id, request=None, db=db,
                                               bypass_frequency_cap=False))
    assert now["reason"] == "ledger_unavailable" and spawned == []
    payload = rc._campaigns_payload(db, [db.get(Campaign, ids.campaign_id)])[0]
    assert payload["stats"]["queued"] == 2 and payload["execution"]["worker_running"] is False
    paused = asyncio.run(rc.update_campaign_status(
        ids.campaign_id, rc.UpdateCampaignStatusIn(status="paused"), request=None, db=db))
    assert paused["status"] == "paused"
    db.close()
    assert meta.calls == []
    # Tables appear (boot create_all finished): the same campaign proceeds.
    engine = dbf.kw["bind"]
    tables = [t for t in Base.metadata.sorted_tables if t.name in ledger.LEDGER_TABLES]
    saved = []
    if engine.dialect.name == "sqlite":
        for t in tables:
            for col in t.columns:
                if isinstance(col.type, JSONB):
                    saved.append((col, col.type))
                    col.type = JSON()
    Base.metadata.create_all(engine, tables=tables)
    for col, orig in saved:
        col.type = orig
    asyncio.run(_run(dbf, ids, _conn()))
    assert sorted(meta.calls) == sorted(PHONES[:2])


# ── 9. The real status webhook handler (PostgreSQL: JSONB lookups) ─────


def test_webhook_handler_resolves_each_copy_and_tolerates_redelivery(dbf, fake_meta, monkeypatch, request):
    if request.node.callspec.params.get("dbf") != "postgres":
        pytest.skip("the handler's MessageEvent lookup uses PostgreSQL JSONB operators")
    import routers.whatsapp_webhook as wh
    ids = _seed(dbf, phones=PHONES[:1])
    fake_meta(FakeMeta())
    asyncio.run(_run(dbf, ids, _conn()))

    def _get_db():
        s = dbf()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(wh, "get_db", _get_db)
    db = dbf()
    wamid = db.query(CampaignSendAttempt.provider_message_id).scalar()
    db.close()
    failed = {"id": wamid, "status": "failed", "timestamp": "1790162131",
              "recipient_id": PHONES[0].lstrip("+"),
              "errors": [{"code": 131048, "title": "Spam Rate limit hit"}]}
    asyncio.run(wh._handle_message_status(dict(failed)))
    asyncio.run(wh._handle_message_status(dict(failed)))   # Meta redelivery
    # An event for a wamid no attempt owns yet is parked, not lost.
    asyncio.run(wh._handle_message_status({"id": "wamid.not.yet", "status": "delivered"}))
    db = dbf()
    att = db.query(CampaignSendAttempt).one()
    row = db.query(CampaignSendLog).one()
    assert att.post_accept_error_code == "spam_rate_limit"
    assert row.failed_at is not None and row.status == "sent"
    assert db.query(CampaignStatusEventInbox).filter(
        CampaignStatusEventInbox.provider_message_id == wamid).count() == 1
    assert db.query(CampaignStatusEventInbox).filter(
        CampaignStatusEventInbox.provider_message_id == "wamid.not.yet",
        CampaignStatusEventInbox.applied_at.is_(None)).count() == 1
    db.close()


# ── 9. PostgreSQL row-lock proof (runs when a DSN is provided) ──────────


@pytest.mark.skipif(not PG_DSN, reason="set NAHLA_CAMPAIGN_LEDGER_PG_DSN to run")
def test_postgres_concurrent_claims_and_leases_admit_one_winner():
    import uuid as _uuid
    from sqlalchemy import text
    schema = f"ledger_{_uuid.uuid4().hex[:8]}"
    admin = create_engine(PG_DSN)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(PG_DSN, connect_args={"options": f"-csearch_path={schema}"},
                           pool_size=20)
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        ids = _seed(Session, phones=PHONES[:1])
        db = Session()
        log_id = db.query(CampaignSendLog.id).scalar()
        db.close()
        barrier = threading.Barrier(8)
        leases, claims = [], []

        def lease_worker(i):
            s = Session()
            barrier.wait()
            leases.append(ledger.acquire_lease(
                s, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id, owner=f"w{i}"))
            s.close()

        threads = [threading.Thread(target=lease_worker, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [r for r in leases if r.acquired]
        assert len(winners) == 1
        owner = winners[0].owner
        barrier2 = threading.Barrier(8)

        def claim_worker():
            s = Session()
            camp = s.get(Campaign, ids.campaign_id)
            stale = s.get(CampaignSendLog, log_id)  # every worker saw "queued"
            assert stale.status == "queued"
            barrier2.wait()
            claims.append(ledger.claim_recipient(
                s, log_id=log_id, campaign=camp, owner=owner,
                scope_key=None, phone_number_id=None).reason)
            s.close()

        threads = [threading.Thread(target=claim_worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert claims.count("claimed") == 1
        db = Session()
        assert db.query(CampaignSendAttempt).count() == 1
        db.close()
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
