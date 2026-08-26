"""PostgreSQL race test: concurrent commit_connection -> one 409."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from database.models import Tenant  # noqa: E402
from services.whatsapp_connection_service import (  # noqa: E402
    WhatsAppConnectionConflict,
    commit_connection,
)

PHONE = "PG-RACE-PHONE-877"
WABA = "PG-RACE-WABA-877"
TOKEN = "pg-race-token-877"


def _db_url() -> str:
    return (
        os.getenv("A1_PG_TEST_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas"
    )


def _run_alembic(engine, revision: str) -> None:
    prev = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        cfg = Config("alembic.ini")
        url = engine.url.render_as_string(hide_password=False)
        cfg.set_main_option("sqlalchemy.url", url)
        os.environ["DATABASE_URL"] = url
        command.upgrade(cfg, revision)
    finally:
        os.chdir(prev)



def _ensure_whatsapp_asset_unique_indexes(engine) -> None:
    """Mirror production startup indexes required for asset race defense."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM whatsapp_connections "
                "WHERE phone_number_id = :phone OR whatsapp_business_account_id = :waba"
            ),
            {"phone": PHONE, "waba": WABA},
        )
        conn.execute(text("DROP INDEX IF EXISTS uq_wa_conn_phone_number_id"))
        conn.execute(text("DROP INDEX IF EXISTS uq_wa_conn_waba_id"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_wa_conn_phone_number_id "
                "ON whatsapp_connections (phone_number_id) "
                "WHERE phone_number_id IS NOT NULL"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_wa_conn_waba_id "
                "ON whatsapp_connections (whatsapp_business_account_id) "
                "WHERE whatsapp_business_account_id IS NOT NULL"
            )
        )


def _validation_ok():
    return SimpleNamespace(
        is_valid=True,
        token_status="valid",
        token_source_label="system_user",
        warnings=[],
        expires_at=None,
        error_message=None,
    )


@pytest.fixture(scope="module")
def pg_engine():
    try:
        engine = create_engine(_db_url(), poolclass=NullPool, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _run_alembic(engine, "0101")
        _ensure_whatsapp_asset_unique_indexes(engine)
    except Exception as exc:  # noqa: BLE001
        if (os.getenv("A1_PG_INTEGRATION_REQUIRED") or "").strip() == "1":
            pytest.fail(f"PostgreSQL unavailable: {exc}")
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    yield engine
    engine.dispose()


def test_concurrent_commit_connection_one_conflict(pg_engine):
    SessionLocal = sessionmaker(bind=pg_engine, expire_on_commit=False)

    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM whatsapp_connections WHERE phone_number_id = :phone "
                "OR whatsapp_business_account_id = :waba"
            ),
            {"phone": PHONE, "waba": WABA},
        )

    with SessionLocal() as s:
        t1 = Tenant(name="race-a-877")
        t2 = Tenant(name="race-b-877")
        s.add_all([t1, t2])
        s.commit()
        t1_id, t2_id = t1.id, t2.id

    patches = [
        patch(
            "services.whatsapp_connection_service.validate_phone_waba_match",
            return_value=(True, WABA, None),
        ),
        patch("services.whatsapp_connection_service.fetch_phone_metadata", return_value={}),
        patch(
            "services.whatsapp_connection_service.register_phone_number",
            return_value=(True, None),
        ),
        patch(
            "services.whatsapp_connection_service.subscribe_phone_webhook",
            return_value=(True, None),
        ),
        patch("services.whatsapp_platform.wa_connection_secrets.store_access_token"),
        patch(
            "services.whatsapp_platform.wa_token_validation.validate_meta_access_token_sync",
            return_value=_validation_ok(),
        ),
        patch("services.whatsapp_platform.wa_token_validation.apply_validation_to_connection"),
        patch(
            "services.whatsapp_platform.wa_token_validation.production_sending_allowed",
            return_value=True,
        ),
    ]
    for p in patches:
        p.start()
    try:
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker(tenant_id: int) -> None:
            session = SessionLocal()
            try:
                barrier.wait(timeout=10)
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
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                results.append(f"error:{type(exc).__name__}")
            finally:
                session.close()

        th1 = threading.Thread(target=worker, args=(t1_id,))
        th2 = threading.Thread(target=worker, args=(t2_id,))
        th1.start()
        th2.start()
        th1.join(timeout=60)
        th2.join(timeout=60)

        assert "ok" in results, results
        assert any(
            code in results
            for code in ("CONFLICT_ASSET_RACE", "CONFLICT_PHONE_CLAIMED", "CONFLICT_WABA_CLAIMED")
        ), results
        assert TOKEN not in str(results)
        assert PHONE not in str(results)

        with pg_engine.connect() as conn:
            owners = conn.execute(
                text(
                    "SELECT tenant_id FROM whatsapp_connections "
                    "WHERE phone_number_id = :phone AND status != 'disconnected'"
                ),
                {"phone": PHONE},
            ).fetchall()
        assert len(owners) == 1
    finally:
        for p in patches:
            p.stop()
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM whatsapp_connections WHERE phone_number_id = :phone "
                    "OR whatsapp_business_account_id = :waba"
                ),
                {"phone": PHONE, "waba": WABA},
            )
