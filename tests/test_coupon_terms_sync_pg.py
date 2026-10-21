"""Real PostgreSQL integration tests for coupon_terms_sync dispatch and reconcile.

Uses a disposable schema per test; never falls back to SQLite.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "database")]

from database.models import Coupon
from services.coupon_terms_sync import (
    dispatch_terms_change,
    provider_expiry_utc,
    queue_terms_change,
    reconcile_imported_terms,
)
from services.store_sync import StoreSyncService

# ---------------------------------------------------------------------------
# Schema DDL — minimal: integrations + coupons (6 Integration model columns)
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE "{s}".tenants (
    id INTEGER PRIMARY KEY
);
CREATE TABLE "{s}".integrations (
    id               SERIAL PRIMARY KEY,
    provider         TEXT NOT NULL,
    external_store_id TEXT,
    config           JSONB,
    enabled          BOOLEAN NOT NULL DEFAULT true,
    tenant_id        INTEGER NOT NULL,
    UNIQUE (provider, external_store_id)
);
CREATE TABLE "{s}".coupons (
    id               SERIAL PRIMARY KEY,
    tenant_id        INTEGER NOT NULL,
    code             TEXT NOT NULL,
    description      TEXT,
    discount_type    TEXT,
    discount_value   TEXT,
    metadata         JSONB,
    expires_at       TIMESTAMP,
    source_type      TEXT NOT NULL DEFAULT 'manual',
    coupon_level     TEXT,
    allocation_channel TEXT,
    UNIQUE (tenant_id, code)
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pg_url():
    url = os.getenv("A1_PG_TEST_DATABASE_URL")
    if not url:
        if os.getenv("A1_PG_INTEGRATION_REQUIRED") == "1":
            pytest.fail("A1_PG_TEST_DATABASE_URL required but not set")
        pytest.skip("Set A1_PG_TEST_DATABASE_URL to run PostgreSQL integration tests")
    return url


def _create_schema(conn, schema):
    conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    for stmt in _DDL.format(s=schema).split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(text(stmt))


def _make_intg(id, tenant_id, store_id, *, enabled=True):
    return SimpleNamespace(
        id=id, tenant_id=tenant_id,
        external_store_id=store_id, provider="salla", enabled=enabled,
        config={
            "store_id": store_id,
            "api_sync_enabled": True,
            "refresh_token": "fake-refresh",
        },
    )


def _insert_intg(db, id, store_id, tenant_id=1):
    cfg = (
        f'{{"store_id":"{store_id}",'
        f'"api_sync_enabled":true,"refresh_token":"x"}}'
    )
    db.execute(text(
        "INSERT INTO integrations (id, provider, external_store_id, config, enabled, tenant_id) "
        "VALUES (:id, 'salla', :sid, CAST(:cfg AS JSONB), true, :tid)"
    ), {"id": id, "sid": store_id, "cfg": cfg, "tid": tenant_id})


def _make_adapter(tenant_id, intg_id, store_id, cpn_id, code, discount_type,
                  initial_amount, initial_expiry, *, updated_at=None):
    ns = SimpleNamespace(
        _tenant_id=tenant_id, _integration_id=intg_id, store_id=store_id,
        gets=0, puts=0,
        amount=initial_amount, expiry=initial_expiry,
        discount_type=discount_type, cpn_id=str(cpn_id), code=code,
        fixed_updated_at=updated_at,
    )

    async def get_coupon_by_id(pid):
        ns.gets += 1
        ts = ns.fixed_updated_at if ns.fixed_updated_at is not None else datetime.now(timezone.utc).isoformat()
        return {
            "id": int(ns.cpn_id), "coupon_id": int(ns.cpn_id),
            "code": ns.code, "type": ns.discount_type,
            "amount": ns.amount, "expiry_date": ns.expiry,
            "updated_at": ts,
        }

    async def update_coupon_terms(pid, **kwargs):
        ns.puts += 1
        if "amount" in kwargs:
            ns.amount = kwargs["amount"]
        if "expiry_date" in kwargs:
            ns.expiry = kwargs["expiry_date"] + " 21:00:00"  # non-midnight Riyadh

    ns.get_coupon_by_id = get_coupon_by_id
    ns.update_coupon_terms = update_coupon_terms
    return ns


# ---------------------------------------------------------------------------
# Test 1 — concurrent workers: SKIP_LOCKED yields; exactly one PUT
# ---------------------------------------------------------------------------

def test_row_lock_skip_locked_second_worker_yields():
    url = _pg_url()
    schema = "coupon_terms_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        _create_schema(conn, schema)
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine)
    intg = _make_intg(10, 1, "STORE1")
    try:
        with Session() as db:
            _insert_intg(db, 10, "STORE1")
            row = Coupon(
                tenant_id=1, code="PROMO25",
                discount_type="percentage", discount_value="20.00",
                source_type="imported", allocation_channel=None,
                extra_metadata={
                    "salla_coupon_id": "9001",
                    "salla_identity": {"tenant_id": 1, "integration_id": 10, "store_id": "STORE1"},
                },
            )
            db.add(row)
            db.flush()
            queue_terms_change(row, intg, {"amount": "25.00"})
            db.commit()
            coupon_id = row.id
            original_expires = row.expires_at

        async def race():
            entered, release = asyncio.Event(), asyncio.Event()
            adapter = _make_adapter(1, 10, "STORE1", 9001, "PROMO25", "percentage",
                                    "20.00", "2027-12-31")

            orig_get = adapter.get_coupon_by_id

            async def slow_get(pid):
                result = await orig_get(pid)
                if adapter.gets == 1:
                    entered.set()
                    await release.wait()
                return result

            adapter.get_coupon_by_id = slow_get

            with Session() as first, Session() as second:
                task = asyncio.create_task(
                    dispatch_terms_change(first, 1, coupon_id, intg, adapter)
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                # Second worker: lock held by first → SKIP_LOCKED returns None → immediate exit
                await asyncio.wait_for(
                    dispatch_terms_change(second, 1, coupon_id, intg, adapter),
                    timeout=5,
                )
                assert adapter.gets == 1, "second worker must not issue GET while lock held"
                assert adapter.puts == 0, "second worker must not PUT"
                release.set()
                await asyncio.wait_for(task, timeout=5)

            assert adapter.puts == 1, "first worker must issue exactly one PUT"
            assert adapter.gets == 2, "GET-before-PUT + GET-after-PUT"

            with Session() as db:
                r = db.query(Coupon).filter_by(id=coupon_id).one()
                assert r.discount_value == "25.00", "confirmed amount must equal provider readback"
                assert r.source_type == "imported", "source_type must be unchanged"
                assert r.allocation_channel is None, "allocation_channel must be unchanged"
                assert r.expires_at == original_expires, "expiry must be unchanged for amount-only change"
                assert r.extra_metadata.get("terms_sync_status") == "synced"
                assert "terms_sync_pending" not in r.extra_metadata, "pending must be cleared on success"

        asyncio.run(race())
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


# ---------------------------------------------------------------------------
# Test 2 — PUT timeout persists pending; retry converges without second PUT
# ---------------------------------------------------------------------------

def test_timeout_after_put_then_retry_converges_no_second_put():
    url = _pg_url()
    schema = "coupon_terms_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        _create_schema(conn, schema)
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine)
    intg = _make_intg(10, 1, "STORE1")
    try:
        with Session() as db:
            _insert_intg(db, 10, "STORE1")
            row = Coupon(
                tenant_id=1, code="PROMO25",
                discount_type="percentage", discount_value="20.00",
                source_type="imported",
                extra_metadata={
                    "salla_coupon_id": "9001",
                    "salla_identity": {"tenant_id": 1, "integration_id": 10, "store_id": "STORE1"},
                },
            )
            db.add(row)
            db.flush()
            queue_terms_change(row, intg, {"amount": "30.00", "expiry_date": "2027-04-20"})
            db.commit()
            coupon_id = row.id

        adapter = _make_adapter(1, 10, "STORE1", 9001, "PROMO25", "percentage",
                                "20.00", "2026-12-31")

        async def flaky_put(pid, **kwargs):
            # Side-effect: update provider state, then raise — simulates timeout after write
            adapter.puts += 1
            if "amount" in kwargs:
                adapter.amount = kwargs["amount"]
            if "expiry_date" in kwargs:
                # Store full datetime string so GET-after-PUT sees updated non-midnight value
                adapter.expiry = kwargs["expiry_date"] + " 21:00:00"
            raise TimeoutError("salla network timeout")

        adapter.update_coupon_terms = flaky_put

        async def run():
            # First dispatch: PUT raises; row persisted as failed/pending
            with Session() as db:
                await dispatch_terms_change(db, 1, coupon_id, intg, adapter)

            with Session() as db:
                r = db.query(Coupon).filter_by(id=coupon_id).one()
                assert r.extra_metadata.get("terms_sync_status") == "failed"
                assert r.extra_metadata.get("terms_sync_pending") is not None, "pending must survive PUT failure"

            # Backdate next_attempt_at so the retry is immediately due
            with Session() as db:
                r = db.query(Coupon).filter_by(id=coupon_id).one()
                meta = dict(r.extra_metadata)
                meta["terms_sync_pending"]["next_attempt_at"] = "2020-01-01T00:00:00+00:00"
                r.extra_metadata = meta
                flag_modified(r, "extra_metadata")
                db.commit()

            puts_before_retry = adapter.puts

            # Retry: provider already has desired values → _terms_match returns True → no PUT
            with Session() as db:
                await dispatch_terms_change(db, 1, coupon_id, intg, adapter)

            assert adapter.puts == puts_before_retry, "retry must not issue a second PUT when terms already match"

            with Session() as db:
                r = db.query(Coupon).filter_by(id=coupon_id).one()
                assert r.extra_metadata.get("terms_sync_status") == "synced"
                assert "terms_sync_pending" not in r.extra_metadata
                assert r.discount_value == "30.00"
                # expiry_date "2027-04-20" in Riyadh = UTC 2027-04-20 21:00:00 - 3h = 18:00 UTC
                assert r.expires_at is not None
                exp_utc = r.expires_at.replace(tzinfo=timezone.utc)
                # Riyadh 2027-04-20 21:00 = UTC 2027-04-20 18:00 (UTC+3 → subtract 3 h)
                assert exp_utc == datetime(2027, 4, 20, 18, 0, tzinfo=timezone.utc), (
                    f"expiry must be 2027-04-20T18:00:00Z (Riyadh 21:00 − 3 h UTC), got {exp_utc}"
                )

        asyncio.run(run())
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


# ---------------------------------------------------------------------------
# Test 3 — wrong integration binding: no GET/PUT; sibling row unchanged
# ---------------------------------------------------------------------------

def test_wrong_integration_binding_rejects_no_get_put():
    url = _pg_url()
    schema = "coupon_terms_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        _create_schema(conn, schema)
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine)
    intg_A = _make_intg(10, 1, "STORE1")   # correct integration
    intg_B = _make_intg(20, 1, "STORE2")   # wrong integration for this coupon
    try:
        with Session() as db:
            _insert_intg(db, 10, "STORE1")
            _insert_intg(db, 20, "STORE2")
            # Target coupon: bound to intg_A / STORE1, pending queued with intg_A
            row1 = Coupon(
                tenant_id=1, code="PROMO25",
                discount_type="percentage", discount_value="20.00",
                source_type="imported",
                extra_metadata={
                    "salla_coupon_id": "9001",
                    "salla_identity": {"tenant_id": 1, "integration_id": 10, "store_id": "STORE1"},
                },
            )
            # Sibling coupon: different code, same tenant — must remain untouched
            row2 = Coupon(
                tenant_id=1, code="OTHER10",
                discount_type="fixed", discount_value="10.00",
                source_type="imported",
                extra_metadata={
                    "salla_coupon_id": "9002",
                    "salla_identity": {"tenant_id": 1, "integration_id": 10, "store_id": "STORE1"},
                },
            )
            db.add_all([row1, row2])
            db.flush()
            queue_terms_change(row1, intg_A, {"amount": "25.00"})
            db.commit()
            coupon_id = row1.id
            sibling_id = row2.id
            sibling_val = row2.discount_value

        # Dispatch with intg_B: pending["integration_id"]==10 but intg_B.id==20
        # → _assert_integration_ok raises before any provider call
        adapter_B = _make_adapter(1, 20, "STORE2", 9001, "PROMO25", "percentage",
                                  "20.00", "2026-12-31")

        async def run():
            with Session() as db:
                await dispatch_terms_change(db, 1, coupon_id, intg_B, adapter_B)

        asyncio.run(run())

        assert adapter_B.gets == 0, "GET must not fire when integration binding mismatches"
        assert adapter_B.puts == 0, "PUT must not fire when integration binding mismatches"

        with Session() as db:
            r = db.query(Coupon).filter_by(id=coupon_id).one()
            assert r.extra_metadata.get("terms_sync_status") == "failed"
            assert r.extra_metadata.get("terms_sync_error") == "integration_binding_unavailable"

        with Session() as db:
            r2 = db.query(Coupon).filter_by(id=sibling_id).one()
            assert r2.discount_value == sibling_val, "sibling coupon must be completely unchanged"
            assert r2.extra_metadata.get("terms_sync_pending") is None

    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


# ---------------------------------------------------------------------------
# Test 4 — reconcile_imported_terms: newer T2 wins; T1 replay rejected;
#           equal-watermark conflict triggers detail GET; T3 accepted
# ---------------------------------------------------------------------------

def test_reconcile_watermark_ordering_and_conflict_resolution():
    url = _pg_url()
    schema = "coupon_terms_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        _create_schema(conn, schema)
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine)
    try:
        with Session() as db:
            db.execute(text("INSERT INTO tenants (id) VALUES (1)"))
            _insert_intg(db, 10, "STORE1")
            T2 = "2027-01-10T12:00:00+00:00"
            row = Coupon(
                tenant_id=1, code="PROMO25",
                discount_type="percentage", discount_value="25.00",
                source_type="imported",
                extra_metadata={
                    "salla_coupon_id": "9001",
                    "salla_identity": {"tenant_id": 1, "integration_id": 10, "store_id": "STORE1"},
                    "terms_source_updated_at": T2,
                    "terms_applied_at": T2,
                    "confirmed_value": "25.00",
                },
            )
            db.add(row)
            db.commit()
            row_id = row.id

        binding = {"tenant_id": 1, "integration_id": 10, "store_id": "STORE1"}

        T1_str = "2027-01-05T08:00:00+00:00"
        T2_str = "2027-01-10T12:00:00+00:00"
        T3_str = "2027-02-01T10:00:00+00:00"
        T1_dt = datetime.fromisoformat(T1_str)
        T2_dt = datetime.fromisoformat(T2_str)
        T3_dt = datetime.fromisoformat(T3_str)

        def _raw(updated_at, amount, expiry="2027-06-30"):
            return {
                "id": 9001, "coupon_id": 9001,
                "code": "PROMO25", "type": "percentage",
                "amount": amount, "expiry_date": expiry,
                "updated_at": updated_at,
            }

        # Adapter used for detail GET path; returns T2 values to resolve conflict.
        # fixed updated_at=T2_str prevents wall-clock (2026) from being treated as
        # older than the T2 watermark (2027-01-10) and causing spurious rejection.
        adapter = _make_adapter(1, 10, "STORE1", 9001, "PROMO25", "percentage",
                                "25.00", "2027-06-30", updated_at=T2_str)

        async def run():
            gets_before = adapter.gets

            # 1. Replay older T1 → watermark regression → rejected (returns None)
            with Session() as db:
                r = db.query(Coupon).filter_by(id=row_id).one()
                result_raw, delta = await reconcile_imported_terms(
                    db, r, _raw(T1_str, "20.00"), T1_dt, adapter, binding
                )
            assert result_raw is None, "older observation must be rejected (watermark regression)"
            assert adapter.gets == gets_before, "no detail GET for stale T1 observation"

            # 2. Equal T2 watermark with conflicting amount → detail GET triggered
            with Session() as db:
                r = db.query(Coupon).filter_by(id=row_id).one()
                result_conflict, delta2 = await reconcile_imported_terms(
                    db, r, _raw(T2_str, "99.00"), T2_dt, adapter, binding
                )
            # adapter.gets should have incremented (detail GET fired)
            assert adapter.gets > gets_before, "equal-watermark conflict must trigger detail GET"
            # Detail returns 25.00 (matching stored) → reconcile returns detail dict
            assert result_conflict is not None, "detail resolution must return data when detail matches"
            assert str(result_conflict.get("amount")) == "25.00"

            # 3. Newer T3 → straight accept, no detail GET, returns raw with updated amount
            gets_before_t3 = adapter.gets
            with Session() as db:
                r = db.query(Coupon).filter_by(id=row_id).one()
                result_T3, delta_T3 = await reconcile_imported_terms(
                    db, r, _raw(T3_str, "28.00"), T3_dt, adapter, binding
                )
            assert result_T3 is not None, "newer T3 must be accepted"
            assert str(result_T3.get("amount")) == "28.00"
            assert adapter.gets == gets_before_t3, "straight-newer path must not trigger detail GET"

            # Persist T3 result via actual importer
            with Session() as db:
                svc = StoreSyncService(db, 1, integration_connection_id=10, adapter=adapter)
                await svc.sync_coupons(
                    raw_list=[_raw(T3_str, "28.00")],
                    record_sync_meta=False,
                    observed_at=T3_dt,
                )
                db.commit()

            with Session() as db:
                r = db.query(Coupon).filter_by(id=row_id).one()
                assert Decimal(r.discount_value) == Decimal("28.00")
                assert r.expires_at == provider_expiry_utc(_raw(T3_str, "28.00")["expiry_date"]).replace(tzinfo=None)
                assert (
                    datetime.fromisoformat(r.extra_metadata["terms_source_updated_at"]).astimezone(timezone.utc)
                    == T3_dt
                )
                assert r.extra_metadata.get("salla_identity") == {
                    "tenant_id": 1, "integration_id": 10, "store_id": "STORE1"
                }
                assert db.query(Coupon).filter_by(tenant_id=1).count() == 1

            # 3b. Replay T1 after T3 is committed → watermark still blocks it; DB row unchanged
            gets_before_replay = adapter.gets
            with Session() as db:
                svc = StoreSyncService(db, 1, integration_connection_id=10, adapter=adapter)
                await svc.sync_coupons(
                    raw_list=[_raw(T1_str, "20.00")],
                    record_sync_meta=False,
                    observed_at=T3_dt + timedelta(seconds=1),
                )
                db.commit()
            assert adapter.gets == gets_before_replay, "no detail GET for stale T1 replay"
            with Session() as db:
                r = db.query(Coupon).filter_by(id=row_id).one()
                assert Decimal(r.discount_value) == Decimal("28.00"), (
                    "T3 persisted value must be unchanged after T1 replay attempt"
                )
                assert r.expires_at == provider_expiry_utc(_raw(T3_str, "28.00")["expiry_date"]).replace(tzinfo=None), (
                    "T3 expiry must be unchanged after T1 replay attempt"
                )
                assert (
                    datetime.fromisoformat(r.extra_metadata["terms_source_updated_at"]).astimezone(timezone.utc)
                    == T3_dt
                ), "T3 watermark must be unchanged after T1 replay attempt"

            # 4. Amount-only: expiry must NOT change when expiry_date absent from desired
            #    (validate via queue_terms_change + dispatch round-trip)
            from services.coupon_terms_sync import queue_terms_change as _qtc
            intg = _make_intg(10, 1, "STORE1")
            with Session() as db:
                r = db.query(Coupon).filter_by(id=row_id).one()
                _qtc(r, intg, {"amount": "35.00"})
                db.commit()
                original_expires = r.expires_at

            adapter2 = _make_adapter(1, 10, "STORE1", 9001, "PROMO25", "percentage",
                                     "28.00", "2027-06-30", updated_at=T2_str)
            with Session() as db:
                await dispatch_terms_change(db, 1, row_id, intg, adapter2)

            with Session() as db:
                r = db.query(Coupon).filter_by(id=row_id).one()
                assert r.discount_value == "35.00"
                assert r.expires_at == original_expires, "amount-only sync must not mutate expires_at"
                # adapter2 GET returns fixed T2 (2027-01-10) which is earlier than T3 (2027-02-01);
                # watermark must stay at T3 — max(T3, T2) = T3.
                wm_stored = r.extra_metadata.get("terms_source_updated_at")
                assert wm_stored is not None, "terms_source_updated_at must be present after dispatch"
                wm_utc = datetime.fromisoformat(wm_stored).astimezone(timezone.utc)
                assert wm_utc == T3_dt, (
                    f"terms_source_updated_at must stay T3 ({T3_dt.isoformat()!r}) "
                    f"when readback timestamp (2026) is older; got {wm_utc.isoformat()!r}"
                )

        asyncio.run(run())
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
