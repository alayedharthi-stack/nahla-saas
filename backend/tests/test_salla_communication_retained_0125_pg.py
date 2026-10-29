"""Test retained 0125 against the exact previous application on disposable PG18."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text

from backend.tests._salla_comm_pg_harness import fresh_database


OLD_SHA = "0334e867c408f12067410fd8e0a9f5a6355e37d8"
NEW_SHA = "64bf7f112b649b33d14f7308c0f4aea8534e427a"
ROOT = Path(__file__).resolve().parents[2]
OLD_ROOT = ROOT / "old_app"
CANDIDATE_ROOT = ROOT / "candidate_app"


def _run(args: list[str], cwd: Path, database_url: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        env.pop(name, None)
    env.update(DATABASE_URL=database_url, **extra)
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120, check=False)


def test_old_application_can_use_retained_0125():
    from backend.tests import test_shopify_release_gate_pg as topology

    assert _run(["git", "rev-parse", "HEAD"], OLD_ROOT, "").stdout.strip() == OLD_SHA
    candidate = _run(["git", "rev-parse", "HEAD"], CANDIDATE_ROOT, "").stdout.strip()
    assert candidate == NEW_SHA

    with fresh_database() as (url, db_name):
        topology._build(db_name, "0124")
        database_url = url.render_as_string(hide_password=False)
        migrated = topology._alembic(db_name, "upgrade", "0125")
        assert migrated.returncode == 0, migrated.stderr[-1000:]

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                row_id = conn.execute(text(
                    "INSERT INTO webhook_events "
                    "(provider,event_type,external_event_id,store_id,raw_body,status,"
                    "communication_send_state,communication_provider_message_id,"
                    "communication_send_error_code,communication_send_attempted_at) "
                    "VALUES ('salla','synthetic.event','retained-0125',"
                    "'synthetic-store','synthetic-only','processed','accepted',"
                    "'synthetic-provider-id','synthetic-code',CURRENT_TIMESTAMP) RETURNING id"
                )).scalar_one()
                conn.execute(text(
                    "INSERT INTO salla_communication_function_nonces(nonce,created_at) "
                    "VALUES ('0123456789abcdef0123456789abcdef',CURRENT_TIMESTAMP)"
                ))
                before = conn.execute(text(
                    "SELECT communication_send_state,communication_provider_message_id,"
                    "communication_send_error_code,communication_send_attempted_at "
                    "FROM webhook_events WHERE id=:id"
                ), {"id": row_id}).one()

            # Run the previous application's real normal migration bootstrap.
            # An unknown 0125 revision must fail this release gate, not be
            # ignored merely because the old ORM can ignore additive columns.
            boot = _run([sys.executable, "-m", "alembic", "upgrade", "0093"],
                        OLD_ROOT / "database", database_url)
            assert boot.returncode == 0, (boot.stderr or boot.stdout)[-1000:]

            old_orm = _run([sys.executable, "-c", """
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from database.models import WebhookEvent
import os
e = create_engine(os.environ['DATABASE_URL'])
with Session(e) as session:
    row = session.execute(select(WebhookEvent).where(WebhookEvent.id == int(os.environ['ROW_ID']))).scalar_one()
    assert row.external_event_id == 'retained-0125'
    row.attempts = 1
    session.commit()
e.dispose()
"""], OLD_ROOT, database_url, ROW_ID=str(row_id))
            assert old_orm.returncode == 0, (old_orm.stderr or old_orm.stdout)[-1000:]

            with engine.connect() as conn:
                after = conn.execute(text(
                    "SELECT communication_send_state,communication_provider_message_id,"
                    "communication_send_error_code,communication_send_attempted_at "
                    "FROM webhook_events WHERE id=:id"
                ), {"id": row_id}).one()
                assert after == before
                assert conn.execute(text(
                    "SELECT COUNT(*) FROM salla_communication_function_nonces "
                    "WHERE nonce='0123456789abcdef0123456789abcdef'"
                )).scalar_one() == 1
                assert conn.execute(text(
                    "SELECT COUNT(*) FROM webhook_events WHERE external_event_id='retained-0125'"
                )).scalar_one() == 1
                assert "0125" in {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))}
        finally:
            engine.dispose()

