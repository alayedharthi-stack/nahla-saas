"""PostgreSQL tier — disposable database, independent processes, real runtime code.

Skips only when the tier was not requested; with
``NAHLA_RELIABILITY_REQUIRE_PG=1`` every test here must execute.
"""
from __future__ import annotations

import multiprocessing as mp
import uuid
from typing import Any, Dict, List

import pytest
from sqlalchemy import text

from tests.commerce_reliability import runtime_support as rs

rs.ensure_sys_path()

import models as M  # noqa: E402

pytestmark = pytest.mark.reliability_postgres

PHONE = "+966500000001"
NORMALIZED_PHONE = "966500000001"
WORKER_JOIN_SECONDS = 120


def _run_workers(targets: List[tuple]) -> List[Dict[str, Any]]:
    """Run workers in *separate spawned processes*; fail loudly on hangs."""
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(targets))
    out = ctx.Queue()
    procs = [ctx.Process(target=fn, args=(*args, barrier, out)) for fn, args in targets]
    for p in procs:
        p.start()
    results: List[Dict[str, Any]] = []
    try:
        for _ in procs:
            results.append(out.get(timeout=WORKER_JOIN_SECONDS))
    finally:
        for p in procs:
            p.join(timeout=WORKER_JOIN_SECONDS)
            if p.is_alive():
                p.terminate()
    exit_codes = [p.exitcode for p in procs]
    assert all(code == 0 for code in exit_codes), (exit_codes, results)
    assert len(results) == len(targets), results
    for r in results:
        assert not str(r.get("status", "")).startswith("error"), r
    return results


def test_disposable_database_is_isolated_and_utf8(disposable_pg) -> None:
    with disposable_pg.engine.connect() as conn:
        assert conn.execute(text("SHOW server_encoding")).scalar() == "UTF8"
        current = conn.execute(text("SELECT current_database()")).scalar()
    assert current == disposable_pg.name and current.startswith("nahla_reliability_")
    assert current not in ("postgres", "nahla_saas", "template0", "template1")
    db = disposable_pg.session()
    try:
        name = rs.unique_store_name("متجر تجريبي عام")
        tenant = M.Tenant(name=name, is_active=True)
        db.add(tenant)
        db.commit()
        db.expire_all()
        assert db.get(M.Tenant, tenant.id).name == name  # Arabic survives the round trip
    finally:
        db.close()


def test_webhook_claim_next_batch_never_double_claims_across_processes(disposable_pg) -> None:
    from tests.commerce_reliability import pg_workers  # noqa: PLC0415

    db = disposable_pg.session()
    try:
        tenant = M.Tenant(name=rs.unique_store_name(), is_active=True)
        db.add(tenant)
        db.commit()
        marker = uuid.uuid4().hex[:8]
        for i in range(10):
            db.add(M.WebhookEvent(
                tenant_id=tenant.id, provider="meta", event_type="message",
                external_event_id=f"evt-{marker}-{i}", raw_body="{}", status="received",
            ))
        db.commit()
        seeded = {int(r.id) for r in db.query(M.WebhookEvent).filter(
            M.WebhookEvent.external_event_id.like(f"evt-{marker}-%")).all()}
    finally:
        db.close()

    results = _run_workers([
        (pg_workers.claim_worker, (disposable_pg.dsn, 5)),
        (pg_workers.claim_worker, (disposable_pg.dsn, 5)),
    ])
    claims = [set(r["ids"]) for r in results]
    assert claims[0].isdisjoint(claims[1]), claims
    assert claims[0] | claims[1] == seeded, (claims, seeded)

    db = disposable_pg.session()
    try:
        statuses = {r.status for r in db.query(M.WebhookEvent).filter(M.WebhookEvent.id.in_(seeded)).all()}
        assert statuses == {"processing"}, statuses
    finally:
        db.close()


def test_catalog_search_singular_and_tenant_isolation_on_postgres(disposable_pg) -> None:
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    db = disposable_pg.session()
    try:
        fixture = rs.seed_catalog(db, M)
        rows = CatalogContextBuilder(db, fixture.tenant_a).search_products("تنورة", limit=10)
        ids = {int(r["id"]) for r in rows}
        assert fixture.product_ids["p-14"] in ids, rows
        assert fixture.product_ids["x-1"] not in ids, "tenant B row leaked into tenant A search"
        owners = {int(db.get(M.Product, pid).tenant_id) for pid in ids}
        assert owners == {fixture.tenant_a}
    finally:
        db.close()


def test_broken_plural_query_finds_singular_product_on_postgres(disposable_pg, baseline) -> None:
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    db = disposable_pg.session()
    try:
        fixture = rs.seed_catalog(db, M)
        rows = CatalogContextBuilder(db, fixture.tenant_a).search_products("تنانير", limit=10)
        if not rows:
            baseline.defect("RB-03-PG", "broken_plural_query_returns_no_rows", query="تنانير")
        assert fixture.product_ids["p-14"] in {int(r["id"]) for r in rows}, rows
    finally:
        db.close()


def test_concurrent_state_saves_do_not_lose_updates(disposable_pg, unimplemented) -> None:
    from tests.commerce_reliability import pg_workers  # noqa: PLC0415

    db = disposable_pg.session()
    try:
        tenant = M.Tenant(name=rs.unique_store_name(), is_active=True)
        db.add(tenant)
        db.commit()
        customer = M.Customer(tenant_id=tenant.id, phone=PHONE, normalized_phone=NORMALIZED_PHONE, name="")
        db.add(customer)
        db.commit()
        conversation = M.Conversation(
            tenant_id=tenant.id, customer_id=customer.id, status="active",
            extra_metadata={"brain_state": {"turn": 1, "stage": "browsing"}},
        )
        db.add(conversation)
        db.commit()
        tenant_id, conversation_id = int(tenant.id), int(conversation.id)
    finally:
        db.close()

    _run_workers([
        (pg_workers.state_save_worker, (
            disposable_pg.dsn, tenant_id, NORMALIZED_PHONE,
            "last_search_candidates", [{"id": 11, "title": "فستان"}],
        )),
        (pg_workers.state_save_worker, (
            disposable_pg.dsn, tenant_id, NORMALIZED_PHONE,
            "current_product_focus", {"id": 13, "title": "جاكيت"},
        )),
    ])

    db = disposable_pg.session()
    try:
        db.expire_all()
        row = db.get(M.Conversation, conversation_id)
        brain_state = (row.extra_metadata or {}).get("brain_state") or {}
    finally:
        db.close()
    kept = {
        "last_search_candidates": bool(brain_state.get("last_search_candidates")),
        "current_product_focus": bool(brain_state.get("current_product_focus")),
    }
    if not all(kept.values()):
        unimplemented.contract("UC-02", "state_store_save_lost_update", kept=kept)
    assert all(kept.values()), kept
