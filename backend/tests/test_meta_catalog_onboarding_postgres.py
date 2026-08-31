"""PostgreSQL advisory-lock concurrency for auto catalog onboarding.

Uses isolated Postgres at WA_CATALOG_SYNC_PG_TEST_DATABASE_URL only.
Never DATABASE_URL. Skips when the DSN is absent unless required.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

_TEST_TENANT = 880_994


def _is_postgres_url(url: str) -> bool:
    return url.split(":", 1)[0].lower().startswith("postgres")


@pytest.fixture
def postgres_engine() -> Engine:
    explicit = (os.getenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL") or "").strip()
    if not explicit or not _is_postgres_url(explicit):
        message = (
            "WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is required for isolated "
            "onboarding concurrency tests. DATABASE_URL is ignored."
        )
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)
    try:
        engine = create_engine(explicit, poolclass=NullPool, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as exc:  # noqa: BLE001
        message = (
            "PostgreSQL unavailable for catalog onboarding tests "
            f"at WA_CATALOG_SYNC_PG_TEST_DATABASE_URL: {exc}"
        )
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)


def test_advisory_lock_serializes_two_sessions(postgres_engine: Engine, monkeypatch) -> None:
    from services.meta_catalog_onboarding import _acquire_tenant_onboard_lock

    monkeypatch.setenv("NAHLA_AUTO_CATALOG_ONBOARDING", "1")
    SessionLocal = sessionmaker(bind=postgres_engine)
    order: list[str] = []
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def worker(name: str) -> None:
        db = SessionLocal()
        try:
            barrier.wait(timeout=5)
            _acquire_tenant_onboard_lock(db, _TEST_TENANT)
            order.append(f"{name}-enter")
            time.sleep(0.25)
            order.append(f"{name}-exit")
            db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            db.rollback()
        finally:
            db.close()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert errors == []
    assert len(order) == 4
    assert order[0].endswith("-enter")
    assert order[1].endswith("-exit")
    assert order[2].endswith("-enter")
    assert order[3].endswith("-exit")
    assert order[0].split("-")[0] == order[1].split("-")[0]
    assert order[2].split("-")[0] == order[3].split("-")[0]


def test_concurrent_ensure_creates_exactly_one_catalog(
    postgres_engine: Engine, monkeypatch,
) -> None:
    from models import Tenant, WhatsAppConnection
    from services.meta_catalog_onboarding import ensure_waba_catalog_for_tenant

    monkeypatch.setenv("NAHLA_AUTO_CATALOG_ONBOARDING", "1")
    Tenant.__table__.create(bind=postgres_engine, checkfirst=True)
    WhatsAppConnection.__table__.create(bind=postgres_engine, checkfirst=True)

    SessionLocal = sessionmaker(bind=postgres_engine)
    setup = SessionLocal()
    try:
        setup.execute(text("DELETE FROM whatsapp_connections WHERE tenant_id = :tid"), {"tid": _TEST_TENANT})
        setup.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": _TEST_TENANT})
        setup.add(Tenant(id=_TEST_TENANT, name=f"generic-commerce-{_TEST_TENANT}"))
        setup.flush()
        setup.add(WhatsAppConnection(
            tenant_id=_TEST_TENANT,
            status="connected",
            whatsapp_business_account_id="WABA-GENERIC-001",
            access_token="EAAB-merchant",
            extra_metadata={},
        ))
        setup.commit()
    finally:
        setup.close()

    created_ids: list[str] = []
    create_lock = threading.Lock()
    barrier = threading.Barrier(2)
    errors: list[str] = []
    results: list[dict] = []

    def _create(business_id, token, name, *, client=None):
        with create_lock:
            n = len(created_ids) + 1
            cid = f"CAT-NEW-{n}"
            created_ids.append(cid)
        time.sleep(0.2)
        return cid, None

    from unittest.mock import patch

    def worker() -> None:
        db = SessionLocal()
        try:
            barrier.wait(timeout=5)
            out = ensure_waba_catalog_for_tenant(db, _TEST_TENANT, confirm=True)
            results.append(out)
            if not out.get("ok"):
                errors.append(str(out.get("error")))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            db.rollback()
        finally:
            db.close()

    with patch(
        "services.meta_catalog_onboarding._select_graph_token",
        return_value={"token": "EAAB-merchant"},
    ), patch(
        "services.meta_catalog_onboarding.fetch_waba_owner_business_id",
        return_value={"ok": True, "business_id": "BM-MERCHANT"},
    ), patch(
        "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
        return_value=([], 200, None),
    ), patch(
        "services.meta_catalog_onboarding._list_owned_catalog_ids",
        side_effect=lambda *a, **k: (list(created_ids), None, False),
    ), patch(
        "services.meta_catalog_onboarding._create_owned_catalog",
        side_effect=_create,
    ), patch(
        "services.meta_catalog_onboarding.link_waba_to_catalog",
        return_value={"ok": True, "already_linked": False, "action": "link"},
    ), patch(
        "services.meta_catalog_onboarding.probe_catalog_readable",
        return_value={"ok": True, "business_id": "BM-MERCHANT"},
    ), patch(
        "services.meta_catalog_onboarding.get_entitlements",
    ) as ent:
        ent.return_value.has_feature.return_value = True
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
    assert errors == [], results
    assert len(created_ids) == 1

    verify = SessionLocal()
    try:
        row = (
            verify.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == _TEST_TENANT)
            .one()
        )
        assert row.meta_catalog_id == "CAT-NEW-1"
    finally:
        verify.execute(text("DELETE FROM whatsapp_connections WHERE tenant_id = :tid"), {"tid": _TEST_TENANT})
        verify.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": _TEST_TENANT})
        verify.commit()
        verify.close()
