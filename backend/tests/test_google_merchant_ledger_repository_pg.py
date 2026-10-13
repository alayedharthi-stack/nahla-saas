"""Dormant adapter proofs against revision 0125 in disposable loopback databases."""
from __future__ import annotations

import importlib.util
import hashlib
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from services import google_merchant_ledger_repository as repo
from services.google_merchant_lifecycle_v1 import LifecyclePlan, PlannedAction, OfferScope, action_snapshot
from services.google_merchant_protocol_v1 import (
    MerchantApiBinding, TransportResponse, insert_request, patch_request,
    delete_request, get_product_request, RESP_UNCERTAIN_WRITE,
)

DSN = os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN", "")
if not DSN and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("required disposable PostgreSQL DSN missing")
if DSN:
    url = make_url(DSN)
    if (url.host not in {"127.0.0.1", "localhost", "::1"}
            or {"host", "hostaddr", "service"} & set(url.query)
            or os.environ.get("PGHOSTADDR") or os.environ.get("PGSERVICE")):
        raise RuntimeError("disposable loopback PostgreSQL required")
pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL unavailable")


@pytest.fixture
def pg():
    name = "gmc_repo_" + uuid.uuid4().hex
    admin = create_engine(DSN, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE {name}"))
    engine = create_engine(make_url(DSN).set(database=name), poolclass=NullPool)
    try:
        with engine.begin() as c:
            c.execute(text("CREATE TABLE tenants (id integer PRIMARY KEY)"))
            c.execute(text("INSERT INTO tenants VALUES (1), (2)"))
            from alembic.migration import MigrationContext
            from alembic.operations import Operations
            spec = importlib.util.spec_from_file_location("ledger_0125", ROOT / "database/migrations/versions/0125_google_merchant_ledger.py")
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            with Operations.context(MigrationContext.configure(c)):
                migration.upgrade()
            c.execute(text("INSERT INTO google_merchant_connections (tenant_id, account_id, content_language, feed_label, status) VALUES (1,'700100','en','US','authorized')"))
            c.execute(text("UPDATE google_merchant_connections SET data_source_id='880011', status='bound', binding_verified_at=clock_timestamp(), version=version+1 WHERE tenant_id=1"))
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


ATTRS = {"title": "Blue cotton shirt", "description": "A cotton shirt", "link": "https://example.test/shirt", "imageLink": "https://example.test/shirt.jpg", "price": {"amountMicros": "25000000", "currencyCode": "USD"}, "availability": "IN_STOCK"}


def plan(c, *, offer="SHIRT-1", action="INSERT", attrs=None, removed=(), tenant=1, account="700100", source="880011"):
    now = repo._now(c)
    b = MerchantApiBinding(tenant, account, source, "API", "en", "US", now)
    desired = dict(ATTRS) if attrs is None else attrs
    if action == "INSERT":
        request = insert_request(b, {"offerId": offer, "contentLanguage": "en", "feedLabel": "US", "productAttributes": desired}, offer_id=offer)
    elif action == "PATCH":
        request = patch_request(b, offer, desired, {})
    else:
        desired = None
        request = delete_request(b, offer)
    a = PlannedAction(offer, action, (), request, desired, removed, action_snapshot(offer, action, request, desired, removed))
    return LifecyclePlan(tenant, b, now, (), (a,)), a


def admit(c, *, token="one", generation=1, key=None, connection_id=1, **kw):
    p, a = plan(c, **kw)
    b = p.binding
    expected = repo.idempotency_key_for(OfferScope(p.tenant_id, b.account_id, b.data_source_id, b.content_language, b.feed_label, a.offer_id), generation)
    return repo.admit(c, connection_id, p, a, op_id=hashlib.sha256(token.encode()).hexdigest(), idempotency_key=expected if key is None else key, generation=generation, max_age_s=60)


def dispatch(c, ref):
    return repo.claim(c, ref, owner="worker-a", attempt_id=uuid.uuid4().hex, lease_seconds=30)


def row(c, ref):
    return c.execute(text("SELECT * FROM google_merchant_offer_ledger WHERE id=:id"), {"id": ref.ledger_id}).mappings().one()


def readback(c, ref, *, status=200, attrs=None, source="880011", offer="SHIRT-1", error=None):
    b = plan(c)[0].binding
    body = {"name": "accounts/700100/products/en~US~" + offer, "offerId": offer, "contentLanguage": "en", "feedLabel": "US", "dataSource": "accounts/700100/dataSources/" + source, "productAttributes": dict(ATTRS) if attrs is None else attrs}
    return repo.record_readback(c, ref, get_product_request(b, row(c, ref)["offer_id"]), TransportResponse(status, body=body, error=error))


def owned(c):
    ref = admit(c)
    d = dispatch(c, ref)
    awaiting = repo.record_outcome(c, d.ref, TransportResponse(200), retry_delay_s=1)
    return readback(c, awaiting)


def test_admission_replay_and_generation_are_retained(pg):
    with pg.begin() as c:
        first = admit(c)
        assert admit(c) == first
        with pytest.raises(repo.LedgerConflict):
            admit(c, token="other")
        done = readback(c, repo.record_outcome(c, dispatch(c, first).ref, TransportResponse(200), retry_delay_s=1))
        assert row(c, done)["op_state"] == "idle"
        with pytest.raises(repo.LedgerConflict, match="completed_key_retained"):
            admit(c)
        with pytest.raises(repo.LedgerConflict):
            admit(c, token="new", generation=1, action="PATCH")


@pytest.mark.parametrize("mode", ["same", "different", "casefold"])
def test_concurrent_admissions_serialize_identity(pg, mode):
    gate = threading.Barrier(2)
    def run(i):
        with pg.begin() as c:
            gate.wait(timeout=10)
            try:
                return admit(c, token="one" if mode == "same" else str(i), offer=("Straße" if i == 0 else "STRASSE") if mode == "casefold" else "SHIRT-1")
            except repo.LedgerConflict:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    if mode == "same":
        assert results[0] == results[1]
    else:
        assert sum(r is not None for r in results) == 1
    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM google_merchant_offer_ledger")).scalar_one() == 1


def test_two_claimers_have_exactly_one_dispatch(pg):
    with pg.begin() as c:
        ref = admit(c)
    gate = threading.Barrier(2)
    def run(i):
        with pg.begin() as c:
            gate.wait(timeout=10)
            return repo.claim(c, ref, owner="worker-" + str(i), attempt_id=uuid.uuid4().hex, lease_seconds=30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    assert sum(r is not None for r in results) == 1
    with pg.connect() as c:
        assert row(c, ref)["attempts"] == 1


@pytest.mark.parametrize("status", [200, 408, 500, None])
def test_write_success_or_uncertainty_requires_readback(pg, status):
    with pg.begin() as c:
        d = dispatch(c, admit(c))
        ref = repo.record_outcome(c, d.ref, TransportResponse(status), retry_delay_s=1)
        assert row(c, ref)["op_state"] == "readback_required"
        assert repo.claim(c, ref, owner="b", attempt_id="b", lease_seconds=30) is None
        done = readback(c, ref)
        assert row(c, done)["ownership"] == "owned"
        assert row(c, done)["publication_state"] == "unknown"
        assert repo.record_outcome(c, d.ref, TransportResponse(429), retry_delay_s=1) is None


@pytest.mark.parametrize("status", [401, 403, 400, 404])
def test_definitive_rejection_releases_only_claim(pg, status):
    with pg.begin() as c:
        d = dispatch(c, admit(c))
        rejected = repo.record_outcome(c, d.ref, TransportResponse(status), retry_delay_s=1)
        assert row(c, rejected)["op_state"] == "rejected"
        closed = repo.close_rejected(c, rejected)
        assert row(c, closed)["ownership"] == "released"
        assert row(c, closed)["applied_generation"] is None
        assert repo.close_rejected(c, rejected) is None
        with pytest.raises(repo.LedgerConflict):
            admit(c, token="new", generation=1)


def test_429_due_time_and_stale_attempt_fence(pg):
    with pg.begin() as c:
        d = dispatch(c, admit(c))
        retry = repo.record_outcome(c, d.ref, TransportResponse(429, {"Retry-After": "1"}), retry_delay_s=1)
        assert row(c, retry)["op_state"] == "pending"
        assert dispatch(c, retry) is None
        c.execute(text("SELECT pg_sleep(1.05)"))
        second = dispatch(c, retry)
        assert second.ref.lease_fence == d.ref.lease_fence + 1
        assert repo.record_outcome(c, d.ref, TransportResponse(200), retry_delay_s=1) is None


@pytest.mark.parametrize("late_status", [429, 401, 200])
def test_expired_attempt_never_grants_resend(pg, late_status):
    with pg.begin() as c:
        ref = admit(c)
        d = repo.claim(c, ref, owner="worker", attempt_id=uuid.uuid4().hex, lease_seconds=1)
        c.execute(text("SELECT pg_sleep(1.05)"))
        ref = repo.record_outcome(c, d.ref, TransportResponse(late_status), retry_delay_s=1)
        assert row(c, ref)["op_state"] == "readback_required"
        assert dispatch(c, ref) is None


def test_crash_reconciliation_preserves_fence(pg):
    with pg.begin() as c:
        d = repo.claim(c, admit(c), owner="worker", attempt_id=uuid.uuid4().hex, lease_seconds=1)
        assert repo.reconcile_expired(c, d.ref) is None
        c.execute(text("SELECT pg_sleep(1.05)"))
        restored = repo.operation_ref(c, tenant_id=1, connection_id=1, ledger_id=d.ref.ledger_id)
        assert restored == d.ref
        assert repo.operation_ref(c, tenant_id=1, connection_id=1, ledger_id=d.ref.ledger_id+1) is None
        ref = repo.reconcile_expired(c, restored)
        assert ref.lease_fence == d.ref.lease_fence
        assert row(c, ref)["op_state"] == "readback_required"


@pytest.mark.parametrize("override", [{"source": "880022"}, {"offer": "shirt-1"}, {"attrs": {}}, {"status": 404}, {"status": 200.0}, {"error": "timeout"}])
def test_unconfirmed_readback_never_creates_ownership(pg, override):
    with pg.begin() as c:
        d = dispatch(c, admit(c))
        ref = repo.record_outcome(c, d.ref, TransportResponse(200), retry_delay_s=1)
        unresolved = readback(c, ref, **override)
        r = row(c, unresolved)
        assert (r["op_state"], r["ownership"], r["observed_at"]) == ("unresolved", "claiming", None)
        assert repo.planner_records(c, tenant_id=1, connection_id=1)[0] == ()


def test_tenant_version_and_disabled_connection_fences(pg):
    with pg.begin() as c:
        ref = admit(c)
        assert dispatch(c, replace(ref, version=ref.version+1)) is None
        with pytest.raises(repo.LedgerConflict):
            dispatch(c, replace(ref, tenant_id=2))
        c.execute(text("UPDATE google_merchant_connections SET status='disabled',version=version+1"))
        assert dispatch(c, ref) is None
        with pytest.raises(repo.LedgerConflict):
            admit(c)


def test_patch_confirmation_and_delete_tombstone(pg):
    with pg.begin() as c:
        first = owned(c)
        changed = dict(ATTRS, title="Green cotton shirt")
        ref = admit(c, token="patch", generation=2, action="PATCH", attrs=changed)
        ref = repo.record_outcome(c, dispatch(c, ref).ref, TransportResponse(200), retry_delay_s=1)
        old_stamp = row(c, first)["observed_at"]
        ref = readback(c, ref)
        assert row(c, ref)["observed_at"] == old_stamp
        ref = readback(c, ref, attrs=changed)
        assert row(c, ref)["applied_attributes"]["title"] == "Green cotton shirt"
        ref = admit(c, token="delete", generation=3, action="DELETE")
        ref = repo.record_outcome(c, dispatch(c, ref).ref, TransportResponse(200), retry_delay_s=1)
        ref = readback(c, ref, status=200)
        assert row(c, ref)["op_state"] == "unresolved"
        ref = readback(c, ref, status=404)
        assert row(c, ref)["ownership"] == "released"
        assert row(c, ref)["admitted_generation"] == 3
        assert repo.planner_records(c, tenant_id=1, connection_id=1) == ((), ())


def test_action_mutation_and_old_evidence_refused(pg):
    with pg.begin() as c:
        p, a = plan(c)
        a.desired_attributes["title"] = "tampered"
        with pytest.raises(repo.LedgerConflict, match="integrity"):
            repo.admit(c, 1, p, a, op_id="a", idempotency_key="a", generation=1, max_age_s=60)
        p, a = plan(c)
        p = replace(p, planned_at=p.planned_at-61)
        with pytest.raises(repo.LedgerConflict, match="fresh"):
            repo.admit(c, 1, p, a, op_id="a", idempotency_key="a", generation=1, max_age_s=60)


def test_readback_wrong_request_and_cross_tenant_completion_refused(pg):
    with pg.begin() as c:
        d = dispatch(c, admit(c))
        assert repo.record_outcome(c, replace(d.ref, tenant_id=2), TransportResponse(200), retry_delay_s=1) is None
        ref = repo.record_outcome(c, d.ref, TransportResponse(200), retry_delay_s=1)
        request = get_product_request(plan(c)[0].binding, "OTHER-SHIRT")
        with pytest.raises(repo.LedgerConflict, match="scope"):
            repo.record_readback(c, ref, request, TransportResponse(404))
        assert row(c, ref)["op_state"] == "readback_required"


def test_owned_patch_rejection_keeps_prior_evidence(pg):
    with pg.begin() as c:
        first = owned(c)
        before = row(c, first)
        ref = admit(c, token="patch", generation=2, action="PATCH", attrs=dict(ATTRS, title="Green cotton shirt"))
        ref = repo.record_outcome(c, dispatch(c, ref).ref, TransportResponse(403), retry_delay_s=1)
        ref = repo.close_rejected(c, ref)
        after = row(c, ref)
        assert after["ownership"] == "owned"
        for key in ("applied_attributes", "applied_fingerprint", "applied_generation", "applied_idempotency_key", "observed_at"):
            assert after[key] == before[key]
        assert after["admitted_generation"] == 2


def test_caller_rollback_and_planner_records_are_scoped(pg):
    with pg.connect() as c:
        tx = c.begin()
        admit(c)
        tx.rollback()
        assert c.execute(text("SELECT count(*) FROM google_merchant_offer_ledger")).scalar_one() == 0
    with pg.begin() as c:
        # Sequence IDs survive rollback; connection identity stays unchanged.
        ref = admit(c)
        owned_records, pending = repo.planner_records(c, tenant_id=1, connection_id=1)
        assert owned_records == () and len(pending) == 1
        assert pending[0].scope.offer_id == "SHIRT-1"
        with pytest.raises(repo.LedgerConflict, match="scope"):
            repo.planner_records(c, tenant_id=2, connection_id=1)


def test_autocommit_and_missing_transaction_refused(pg):
    with pg.connect() as c:
        with pytest.raises(repo.LedgerConflict, match="transaction"):
            repo.planner_records(c, tenant_id=1, connection_id=1)
    with pg.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("SELECT 1"))
        with pytest.raises(repo.LedgerConflict, match="transaction"):
            repo.planner_records(c, tenant_id=1, connection_id=1)


@pytest.mark.parametrize("target", ["offer", "account"])
def test_completed_key_cannot_transfer_to_other_identity(pg, target):
    with pg.begin() as c:
        first = owned(c)
        completed_key = row(c, first)["applied_idempotency_key"]
        kw = {"offer": "SHIRT-2"}
        if target == "account":
            c.execute(text("INSERT INTO google_merchant_connections (tenant_id,account_id,content_language,feed_label,status) VALUES (2,'700200','en','US','authorized')"))
            c.execute(text("UPDATE google_merchant_connections SET data_source_id='880022',status='bound',binding_verified_at=clock_timestamp(),version=version+1 WHERE tenant_id=2"))
            kw = {"tenant": 2, "account": "700200", "source": "880022", "connection_id": 2}
        with pytest.raises(repo.LedgerConflict, match="scope_generation_mismatch"):
            admit(c, token="other", key=completed_key, **kw)
        assert c.execute(text("SELECT count(*) FROM google_merchant_offer_ledger")).scalar_one() == 1
        # A distinct identity has its own slot key and can be admitted normally.
        second = admit(c, token="other", **kw)
        assert row(c, second)["op_idempotency_key"] != completed_key


def test_historical_key_cannot_replay_at_old_or_new_generation(pg):
    with pg.begin() as c:
        first = owned(c)
        key_one = row(c, first)["applied_idempotency_key"]
        changed = dict(ATTRS, title="Green cotton shirt")
        second = admit(c, token="second", generation=2, action="PATCH", attrs=changed)
        second = repo.record_outcome(c, dispatch(c, second).ref, TransportResponse(200), retry_delay_s=1)
        second = readback(c, second, attrs=changed)
        key_two = row(c, second)["applied_idempotency_key"]
        assert key_two != key_one
        with pytest.raises(repo.LedgerConflict, match="stale_generation"):
            admit(c, token="old", generation=1, key=key_one, action="PATCH")
        with pytest.raises(repo.LedgerConflict, match="scope_generation_mismatch"):
            admit(c, token="new", generation=3, key=key_one, action="PATCH")
        third = admit(c, token="third", generation=3, action="PATCH")
        assert row(c, third)["op_idempotency_key"] not in {key_one, key_two}


@pytest.mark.parametrize("status", [429, 400, 401, 403, 404])
def test_error_bearing_write_status_never_grants_retry_or_rejection(pg, status):
    with pg.begin() as c:
        d = dispatch(c, admit(c))
        ref = repo.record_outcome(c, d.ref, TransportResponse(status, {"Retry-After": "1"}, error="timeout"), retry_delay_s=1)
        r = row(c, ref)
        assert r["op_state"] == "readback_required"
        assert r["ownership"] == "claiming"
        assert r["last_outcome"] == RESP_UNCERTAIN_WRITE
        assert r["last_error_code"] is None and r["next_attempt_at"] is None
        assert dispatch(c, ref) is None
        assert repo.close_rejected(c, ref) is None
