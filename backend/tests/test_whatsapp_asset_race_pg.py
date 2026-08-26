"""PostgreSQL race test: concurrent commit_connection -> one 409."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from database.models import Tenant, WhatsAppConnection  # noqa: E402
from services.whatsapp_connection_service import (  # noqa: E402
    WhatsAppConnectionConflict,
    commit_connection,
)

PHONE = "PG-RACE-PHONE-877"
WABA = "PG-RACE-WABA-877"
TOKEN = "pg-race-token-877"


def _db_url() -> str:
    return (os.getenv("A1_PG_TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or
            "postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas")


@pytest.fixture(scope="module")
def pg_engine():
    try:
        engine = create_engine(_db_url(), poolclass=NullPool, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        if (os.getenv("A1_PG_INTEGRATION_REQUIRED") or "").strip() == "1":
            pytest.fail(f"PostgreSQL unavailable: {exc}")
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    yield engine
    engine.dispose()


def test_concurrent_commit_connection_one_conflict(pg_engine, monkeypatch):
    Session = sessionmaker(bind=pg_engine)
    s1 = Session()
    s2 = Session()
    try:
        t1 = Tenant(name="race-a")
        t2 = Tenant(name="race-b")
        s1.add(t1)
        s2.add(t2)
        s1.commit()
        s2.commit()

        monkeypatch.setattr(
            "services.whatsapp_connection_service.validate_phone_waba_match",
            lambda *a, **k: (True, WABA, None),
        )
        monkeypatch.setattr(
            "services.whatsapp_connection_service.fetch_phone_metadata",
            lambda *a, **k: {},
        )
        monkeypatch.setattr(
            "services.whatsapp_connection_service.register_phone_number",
            lambda *a, **k: (True, None),
        )
        monkeypatch.setattr(
            "services.whatsapp_connection_service.subscribe_phone_webhook",
            lambda *a, **k: (True, None),
        )

        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker(tenant_id: int, session):
            try:
                barrier.wait(timeout=5)
                commit_connection(
                    session,
                    tenant_id=tenant_id,
                    phone_number_id=PHONE,
                    waba_id=WABA,
                    access_token=TOKEN,
                    connection_type="embedded",
                    skip_phone_register=True,
                )
                session.commit()
                results.append("ok")
            except WhatsAppConnectionConflict as exc:
                session.rollback()
                results.append(exc.code)
            except Exception:
                session.rollback()
                results.append("error")

        th1 = threading.Thread(target=worker, args=(t1.id, s1))
        th2 = threading.Thread(target=worker, args=(t2.id, s2))
        th1.start()
        th2.start()
        th1.join(timeout=30)
        th2.join(timeout=30)

        assert "ok" in results
        assert "CONFLICT_ASSET_RACE" in results or "CONFLICT_PHONE_CLAIMED" in results
        owners = (
            pg_engine.connect()
            .execute(
                text(
                    "SELECT tenant_id FROM whatsapp_connections "
                    "WHERE phone_number_id = :phone AND status != 'disconnected'"
                ),
                {"phone": PHONE},
            )
            .fetchall()
        )
        assert len(owners) == 1
    finally:
        s1.rollback()
        s2.rollback()
        s1.close()
        s2.close()
