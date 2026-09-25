"""Owner-requested start-once campaigns: durable 131049 cooldown, no resend.

Provider is scripted; both SQLite and disposable PostgreSQL run the real
leased dispatcher and scheduler. No customer data or real provider calls.
"""
import os
from datetime import datetime, timedelta

import pytest

import test_campaign_throttle_state as base
import test_campaign_capacity_continuation as cap
import test_campaign_send_ledger as ledger_suite
from models import Campaign, CampaignDispatchLease, CampaignSendAttempt
from services import campaign_dispatcher as disp
from services import campaign_send_ledger as ledger


dbf = ledger_suite.dbf
fake_meta = ledger_suite.fake_meta
_fast = ledger_suite._fast
world = cap.world


@pytest.fixture(autouse=True)
def pg_target(monkeypatch):
    # The strict CI proof runner supplies its disposable PostgreSQL target.
    target = os.environ.get("NAHLA_CAMPAIGN_LEDGER_PG_DSN") or os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN")
    if target:
        monkeypatch.setattr(ledger_suite, "PG_DSN", target)


def paused(Session, fake_meta, world):
    ids = base._seed(Session, phones=base.PHONES[:3])
    base._post_accept_failures(Session, ids, 25)
    meta = fake_meta(base.FakeMeta())
    world.dispatch(Session, ids.campaign_id)
    return ids, meta


def due(Session, cid):
    db = Session()
    c = db.get(Campaign, cid)
    w = dict(ledger.capacity_wait(c), next_eligible_at=(base._now() - timedelta(seconds=1)).isoformat())
    ledger.store_capacity_wait(c, w)
    db.commit()
    db.close()


def test_cooldown_is_durable_and_not_just_the_breaker_window(dbf, fake_meta, world):
    ids, meta = paused(dbf, fake_meta, world)
    state = cap._campaign(dbf, ids.campaign_id)
    assert state.wait["continuation"] == "untouched_recipients_only"
    assert timedelta(minutes=59) < datetime.fromisoformat(state.wait["next_eligible_at"]) - base._now() <= timedelta(hours=1)
    base._age_failures(dbf, 16)  # old 15m breaker cleared, but cooldown remains
    assert world.resume(dbf)[0]["action"] == "waiting"
    assert meta.calls == []
    # Restart: a new session sees durable state; expired lease may be recovered.
    db = dbf()
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.owner, lease.expires_at = "dead-worker", base._now() - timedelta(minutes=5)
    db.commit()
    db.close()
    due(dbf, ids.campaign_id)
    assert world.resume(dbf)[0]["action"] == "resumed"
    assert sorted(meta.calls) == sorted(base.PHONES[:3])
    assert world.resume(dbf) == []


def test_late_marketing_receipts_extend_wait_without_any_send(dbf, fake_meta, world):
    ids, meta = paused(dbf, fake_meta, world)
    due(dbf, ids.campaign_id)
    assert world.resume(dbf)[0]["action"] == "marketing_wait"
    state = cap._campaign(dbf, ids.campaign_id)
    assert state.wait["attempt"] == 2
    assert timedelta(minutes=119) < datetime.fromisoformat(state.wait["next_eligible_at"]) - base._now() <= timedelta(hours=2)
    assert meta.calls == [] and state.status == "paused"


def test_spam_block_takes_precedence_over_marketing_and_requires_action(dbf, fake_meta, world):
    ids, meta = paused(dbf, fake_meta, world)
    other = base._seed(dbf, phones=["+966511111111"], campaign_name="حملة أخرى")
    base._post_accept_failures(dbf, other, 5, key="spam_rate_limit")
    due(dbf, ids.campaign_id)
    assert world.resume(dbf)[0]["action"] == "provider_action_required"
    state = cap._campaign(dbf, ids.campaign_id)
    assert state.wait is None and state.status == "paused"
    base._age_failures(dbf, 20)
    assert world.resume(dbf)[0]["action"] == "needs_merchant_resume"
    assert meta.calls == []


def test_manual_stop_wins_even_after_cooldown(dbf, fake_meta, world):
    ids, meta = paused(dbf, fake_meta, world)
    db = dbf()
    ledger.request_stop(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id)
    db.commit()
    db.close()
    due(dbf, ids.campaign_id)
    base._age_failures(dbf, 20)
    assert world.resume(dbf) == []
    assert meta.calls == [] and cap._campaign(dbf, ids.campaign_id).stop


def test_legacy_or_untyped_pause_never_gains_automatic_authority(dbf, fake_meta, world):
    ids, meta = paused(dbf, fake_meta, world)
    db = dbf()
    c = db.get(Campaign, ids.campaign_id)
    w = dict(ledger.capacity_wait(c))
    w.pop("continuation")
    ledger.store_capacity_wait(c, w)
    db.commit()
    db.close()
    due(dbf, ids.campaign_id)
    base._age_failures(dbf, 20)
    assert world.resume(dbf)[0]["action"] == "needs_merchant_resume"
    assert meta.calls == []


def test_capacity_wait_preserves_marketing_backoff_and_never_resends_accepted(dbf, fake_meta, world):
    ids, meta = paused(dbf, fake_meta, world)
    due(dbf, ids.campaign_id)
    base._age_failures(dbf, 20)
    world.budget(1)
    other = base._seed(dbf, phones=["+966511111111"], campaign_name="حملة متجر أحذية")
    ledger_suite._seed_scope_usage(dbf, other, base.SCOPE, 1)
    acts = world.resume(dbf)
    assert acts[0]["action"] == "still_full" and meta.calls == []
    cap._age_window(dbf)
    world.resume(dbf)
    assert len(meta.calls) == 1  # remaining recipients wait for the next capacity slot
    cap._age_window(dbf)
    world.resume(dbf)
    cap._age_window(dbf)
    world.resume(dbf)
    assert sorted(meta.calls) == sorted(base.PHONES[:3])
    # The 25 accepted-but-failed copies are untouched, not treated as queued.
    db = dbf()
    assert db.query(CampaignSendAttempt).filter(CampaignSendAttempt.post_accept_error_code == "marketing_blocked").count() == 25
    assert db.get(Campaign, ids.campaign_id).status == "completed"
    db.close()


def test_another_live_worker_prevents_continuation(dbf, fake_meta, world):
    ids, meta = paused(dbf, fake_meta, world)
    due(dbf, ids.campaign_id)
    base._age_failures(dbf, 20)
    db = dbf()
    lease = db.get(CampaignDispatchLease, ids.campaign_id)
    lease.owner, lease.expires_at = "live-worker", base._now() + timedelta(minutes=5)
    db.commit()
    db.close()
    assert world.resume(dbf)[0]["action"] == "worker_running"
    assert meta.calls == []


def test_backoff_counter_survives_capacity_wait_and_is_bounded(dbf, fake_meta, world):
    ids, _ = paused(dbf, fake_meta, world)
    db = dbf()
    c = db.get(Campaign, ids.campaign_id)
    ledger.record_capacity_wait(c, None)
    w = ledger.record_marketing_wait(c)
    assert w["attempt"] == 2
    for _ in range(30):
        w = ledger.record_marketing_wait(c)
    assert datetime.fromisoformat(w["next_eligible_at"]) - datetime.fromisoformat(w["since"]) == timedelta(hours=24)
    db.close()


def test_stop_during_tier_refresh_is_not_overwritten(dbf, fake_meta, world, monkeypatch):
    import routers.whatsapp_connect as wa
    ids, meta = paused(dbf, fake_meta, world)
    due(dbf, ids.campaign_id)
    base._age_failures(dbf, 20)

    async def stop_during_refresh(*args, **kwargs):
        db = dbf()
        ledger.request_stop(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id)
        db.commit()
        db.close()

    monkeypatch.setattr(wa, "_maybe_refresh_meta_tier", stop_during_refresh)
    assert world.resume(dbf)[0]["action"] == "state_changed"
    assert cap._campaign(dbf, ids.campaign_id).status == "paused" and meta.calls == []


def test_tier_upgrade_is_checked_without_waiting_for_old_24h_slot(dbf, fake_meta, world, monkeypatch):
    import routers.whatsapp_connect as wa
    world.budget(1)
    ids = base._seed(dbf, phones=base.PHONES[:3])
    meta = fake_meta(base.FakeMeta())
    world.dispatch(dbf, ids.campaign_id)
    assert len(meta.calls) == 1
    db = dbf()
    c = db.get(Campaign, ids.campaign_id)
    w = dict(ledger.capacity_wait(c), since=(base._now() - timedelta(minutes=16)).isoformat())
    ledger.store_capacity_wait(c, w)
    db.commit()
    db.close()
    checks = []

    async def upgraded(db, tenant_id, *, max_age_seconds=None):
        checks.append(max_age_seconds)
        world.budget(3)

    monkeypatch.setattr(wa, "_maybe_refresh_meta_tier", upgraded)
    assert world.resume(dbf)[0]["action"] == "resumed"
    assert checks and all(value == 900 for value in checks)
    assert sorted(meta.calls) == sorted(base.PHONES[:3])


def test_unreadable_breaker_never_becomes_permission_to_send(dbf, fake_meta, world, monkeypatch):
    ids, meta = paused(dbf, fake_meta, world)
    due(dbf, ids.campaign_id)

    def unreadable(*args, **kwargs):
        raise RuntimeError("receipt store unavailable")

    monkeypatch.setattr(ledger, "post_accept_throttle", unreadable)
    with pytest.raises(RuntimeError, match="receipt store unavailable"):
        world.resume(dbf)
    assert cap._campaign(dbf, ids.campaign_id).status == "paused" and meta.calls == []


def test_two_scheduler_workers_do_not_duplicate_recipients(dbf, fake_meta, world):
    from concurrent.futures import ThreadPoolExecutor
    ids, meta = paused(dbf, fake_meta, world)
    due(dbf, ids.campaign_id)
    base._age_failures(dbf, 20)
    meta.delay = 0.05
    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = [workers.submit(world.resume, dbf) for _ in range(2)]
        for job in jobs:
            job.result(timeout=30)
    assert sorted(meta.calls) == sorted(base.PHONES[:3])
    assert cap._campaign(dbf, ids.campaign_id).status == "completed"


def test_legacy_adoption_is_scoped_idempotent_and_preserves_queue(dbf, fake_meta, world):
    from scripts.operators.campaign_adopt_marketing_wait import adopt
    ids, meta = paused(dbf, fake_meta, world)
    db = dbf()
    c = db.get(Campaign, ids.campaign_id)
    ledger.clear_capacity_wait(c)
    db.commit()
    stamp = db.get(CampaignDispatchLease, ids.campaign_id).paused_at
    assert adopt(db, tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                 expected_paused_at=stamp)["action"] == "would_adopt"
    assert ledger.capacity_wait(db.get(Campaign, ids.campaign_id)) is None
    report = adopt(db, tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                   expected_paused_at=stamp, apply=True)
    assert report["action"] == "adopted"
    assert adopt(db, tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
                 expected_paused_at=stamp, apply=True)["action"] == "already_adopted"
    with pytest.raises(ValueError, match="not_found"):
        adopt(db, tenant_id=ids.tenant_id + 1, campaign_id=ids.campaign_id,
              expected_paused_at=stamp, apply=True)
    db.close()
    assert meta.calls == [] and cap._campaign(dbf, ids.campaign_id).status == "paused"


def test_legacy_adoption_refuses_changed_pause_or_manual_stop(dbf, fake_meta, world):
    from scripts.operators.campaign_adopt_marketing_wait import adopt
    ids, meta = paused(dbf, fake_meta, world)
    db = dbf()
    ledger.clear_capacity_wait(db.get(Campaign, ids.campaign_id))
    db.commit()
    stamp = db.get(CampaignDispatchLease, ids.campaign_id).paused_at
    with pytest.raises(ValueError, match="not_eligible"):
        adopt(db, tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
              expected_paused_at=stamp - timedelta(seconds=1), apply=True)
    db.rollback()
    ledger.request_stop(db, campaign_id=ids.campaign_id, tenant_id=ids.tenant_id)
    db.commit()
    with pytest.raises(ValueError, match="not_eligible"):
        adopt(db, tenant_id=ids.tenant_id, campaign_id=ids.campaign_id,
              expected_paused_at=stamp, apply=True)
    db.close()
    assert meta.calls == []
