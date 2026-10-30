"""Test the compatible rollback source on synthetic 0125 data and PG18."""
from __future__ import annotations

import os
from pathlib import Path
import json
from contextlib import contextmanager
import subprocess
import sys

from sqlalchemy import create_engine, text
from backend.tests import test_shopify_release_gate_pg as topology

pytestmark = topology.pytestmark

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def fresh_database():
    from sqlalchemy.engine.url import make_url

    assert topology.ADMIN_URL and topology.HOST_ALIAS, "disposable PG18 is required"
    db = topology._fresh_db("startup_comm_0125_rollback")
    try:
        assert 180000 <= int(topology._sql(db, "SHOW server_version_num")[0][0]) < 190000
        yield make_url(topology._loopback_url(db)), db
    finally:
        topology._drop(db)


def _run(args: list[str], cwd: Path, database_url: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        env.pop(name, None)
    env.update(DATABASE_URL=database_url, **extra)
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120, check=False)


def _startup_database_tasks(root: Path, database_url: str, hook_dir: Path) -> dict:
    """Exercise the source's startup DB callback with external egress blocked.

    The probe runs the actual startup DB tasks, not the health alias. Its
    provider and scheduler tasks are withheld and therefore require separate
    production checks after a real release.
    """
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "sitecustomize.py").write_text(
        "from _shopify_startup_probe import install_network_guard\ninstall_network_guard()\n",
        encoding="utf-8",
    )
    probe = root / "backend/tests/_shopify_startup_probe.py"
    assert probe.is_file()
    env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "SystemRoot", "TEMP", "TMP")
           if name in os.environ}
    env.update({"DATABASE_URL": database_url, "ENVIRONMENT": "production",
                "NAHLA_DISABLE_SCHEDULERS": "1", "NAHLA_STARTUP_ROOT": str(root),
                "NAHLA_DB_STATEMENT_TIMEOUT_MS": "15000",
                "NAHLA_BOOTSTRAP_OVERALL_TIMEOUT": "180", "NAHLA_SAFE_ALTERS_TIMEOUT": "180",
                "SALLA_COMM_WHATSAPP_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": os.pathsep.join((str(hook_dir), str(probe.parent)))})
    for flag in (
        "NAHLA_SHOPIFY_CONNECTION_ENABLED", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED",
        "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED", "NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED",
        "NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED", "NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED",
        "VITE_SHOPIFY_CONNECTION_UI", "VITE_SHOPIFY_INSTALL_FIRST_ENABLED",
        "VITE_SHOPIFY_CATALOG_IMPORT_UI",
    ):
        env[flag] = "0"
    result = subprocess.run([sys.executable, "-B", str(probe), "--root", str(root)],
                            env=env, capture_output=True, text=True, timeout=210, check=False)
    assert result.returncode == 0, (result.stderr or result.stdout)[-2000:]
    reports = [line.split("=", 1)[1] for line in result.stdout.splitlines()
               if line.startswith("STARTUP_REPORT=")]
    assert len(reports) == 1, "startup report missing"
    report = json.loads(reports[0])
    assert report["network_canary_refused"] and report["provider_network_attempts"] == 0
    assert report["provider_client_calls"] == [] and report["scheduler_registrations"] == []
    assert report["lifespan_complete"] and report["bootstrap_ready"] and report["safe_alters_ready"]
    assert report["sql_or_bootstrap_errors"] == []
    assert report["subprocesses"] == [
        {"operation": "cleanup", "returncode": 0},
        {"operation": "upgrade 0093", "returncode": 0},
    ]
    return report


def _communication_schema(conn) -> tuple:
    columns = conn.execute(text(
        "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid "
        "WHERE n.nspname='public' AND c.relname IN "
        "('webhook_events','salla_communication_function_nonces') "
        "AND a.attnum>0 AND NOT a.attisdropped "
        "AND (c.relname='salla_communication_function_nonces' OR "
        "a.attname LIKE 'communication_%') ORDER BY c.relname,a.attname"
    )).all()
    primary_key = conn.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
        "conrelid='public.salla_communication_function_nonces'::regclass AND contype='p'"
    )).scalars().all()
    return tuple(tuple(row) for row in columns), tuple(primary_key)


def test_rollback_application_can_use_retained_0125(tmp_path):
    with fresh_database() as (url, db_name):
        topology._build(db_name, "0124")
        database_url = url.render_as_string(hide_password=False)
        migrated = topology._alembic(db_name, "upgrade", "0125")
        assert migrated.returncode == 0, migrated.stderr[-1000:]

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                schema_before = _communication_schema(conn)
                assert schema_before == (
                    (
                        ("salla_communication_function_nonces", "created_at", "timestamp with time zone", True),
                        ("salla_communication_function_nonces", "nonce", "character varying(32)", True),
                        ("webhook_events", "communication_provider_message_id", "character varying(128)", False),
                        ("webhook_events", "communication_send_attempted_at", "timestamp with time zone", False),
                        ("webhook_events", "communication_send_error_code", "character varying(64)", False),
                        ("webhook_events", "communication_send_state", "character varying(16)", False),
                    ),
                    ("PRIMARY KEY (nonce)",),
                )
                relation_oids_before = conn.execute(text(
                    "SELECT c.relname,c.oid FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN "
                    "('webhook_events','salla_communication_function_nonces') ORDER BY c.relname"
                )).all()
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
                        ROOT / "database", database_url)
            assert boot.returncode == 0, (boot.stderr or boot.stdout)[-1000:]

            # A green /healthz is not evidence that the background migration
            # callback succeeded. Require its observed exit codes and reports.
            _startup_database_tasks(ROOT, database_url, tmp_path / "startup_hook_0125")

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
"""], ROOT, database_url, ROW_ID=str(row_id))
            assert old_orm.returncode == 0, (old_orm.stderr or old_orm.stdout)[-1000:]

            with engine.connect() as conn:
                assert _communication_schema(conn) == schema_before
                assert conn.execute(text(
                    "SELECT c.relname,c.oid FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN "
                    "('webhook_events','salla_communication_function_nonces') ORDER BY c.relname"
                )).all() == relation_oids_before
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
                assert {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))} == {
                    "0111", "0113", "0125"
                }
        finally:
            engine.dispose()
