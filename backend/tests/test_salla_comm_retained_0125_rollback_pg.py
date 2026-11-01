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


# ── a36 as the authorized rollback source: real rollback-only bytes on PG18 ──
# The release binding names a36 as the 0125 rollback source. Render the
# existing rollback-only command from the exact a36 tree, run its actual bytes
# read-only on one synthetic database at 0124 and at 0125, and require refusal
# of an expired approval, flags that are not OFF and tampered bytes, with the
# database unchanged by every run. Identities, receipts and approvals are
# synthetic; nothing reads Railway or production. Needs full git history.
import base64  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from hashlib import sha256  # noqa: E402
import io  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import tarfile  # noqa: E402

A36_ROLLBACK_SOURCE = "a36ad7e3442bf23bfd56cdab243df006d5f11e05"
_A36_IDS = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333")
_A36_BACKUP = ("44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555")
_ROLLBACK_ONLY_MODULES = (
    "scripts/operators/salla_comm_rollback_only_contract.py",
    "scripts/operators/salla_comm_rollback_only_gate.py",
    "scripts/operators/salla_comm_rollback_only_render.py",
    "scripts/operators/salla_comm_rollback_only_source.py",
    "scripts/operators/shopify_release_gate.py",
    "scripts/operators/shopify_release_gate_contract.py",
    "scripts/operators/shopify_release_gate_render.py",
)


def _a36_root(tmp_path: Path) -> Path:
    """The exact a36 tree from git history, without the tracked .env file."""
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", A36_ROLLBACK_SOURCE, "--", ".", ":(exclude).env"],
        capture_output=True, check=True,
    ).stdout
    root = tmp_path / "a36-root"
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(root, filter="data")
    return root


def _a36_command(a36_root: Path, db_name: str, *, container_root: Path | None = None,
                 approved_at: datetime | None = None, expires_at: datetime | None = None) -> tuple[bytes, bytes]:
    from scripts.operators import shopify_release_gate as gate
    from scripts.operators import shopify_release_gate_contract as existing
    from scripts.operators import salla_comm_rollback_only_contract as terminal
    from scripts.operators import salla_comm_rollback_only_render as render
    from scripts.operators import salla_comm_rollback_only_source as source

    target = terminal.Target(*_A36_IDS, topology._target(db_name))
    count, digest = source.candidate_inventory(a36_root)
    template = render.command_template(
        a36_root, release_sha=A36_ROLLBACK_SOURCE, target=target,
        backup_service=_A36_BACKUP[0], backup_volume=_A36_BACKUP[1],
        container_root=str(container_root or a36_root),
    )
    now = datetime.now(timezone.utc)
    approved_at = approved_at or now - timedelta(minutes=1)
    expires_at = expires_at or now + timedelta(minutes=10)
    utc = lambda value: value.isoformat().replace("+00:00", "Z")  # noqa: E731
    record = {
        "record_version": 2, "action": terminal.ACTION,
        "target": {"project": target.project, "environment": target.environment,
                   "service": target.service, "database_identity": target.database_identity},
        "candidate_source_sha": A36_ROLLBACK_SOURCE,
        "heads_before": sorted(terminal.HEADS_BEFORE), "heads_after": sorted(terminal.HEADS_AFTER),
        "owner_approval_reference": "synthetic-owner-message",
        "backup_service_id": _A36_BACKUP[0], "backup_volume_id": _A36_BACKUP[1],
        "backup_receipt_sha256": "1" * 64, "restore_receipt_sha256": "2" * 64,
        "rollback_command_template_sha256": sha256(template).hexdigest(),
        "inventory_digest": digest, "inventory_count": count,
        "shopify_schema_sha256": gate.file_sha256(a36_root / existing.INSTALL_EXPECTATIONS_RELATIVE_PATH),
        "communication_schema_sha256": gate.file_sha256(a36_root / existing.COMM_EXPECTATIONS_RELATIVE_PATH),
        "approved_at_utc": utc(approved_at), "expires_at_utc": utc(expires_at), "approved": True,
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return payload, render.bind_command(template, sha256(payload).hexdigest())


def _run_a36_command(payload: bytes, command: bytes, db_name: str, cwd: Path,
                     env_changes: dict[str, str | None] | None = None) -> subprocess.CompletedProcess[str]:
    from scripts.operators import shopify_release_gate_contract as existing
    from scripts.operators import salla_comm_rollback_only_contract as terminal

    argv = shlex.split(command.decode("ascii"))
    assert argv[0] == "python" and argv[1:3] == ["-I", "-c"]
    argv[0] = sys.executable
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SystemRoot", "TEMP", "TMP")
           if key in os.environ}
    env.update({
        "RAILWAY_GIT_COMMIT_SHA": A36_ROLLBACK_SOURCE,
        "RAILWAY_PROJECT_ID": _A36_IDS[0], "RAILWAY_ENVIRONMENT_ID": _A36_IDS[1],
        "RAILWAY_SERVICE_ID": _A36_IDS[2],
        "DATABASE_URL": topology._alias_url(db_name),
        existing.APPROVAL_PAYLOAD_ENV: base64.b64encode(payload).decode("ascii"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.update({name: "false" for name in terminal.FLAG_NAMES})
    for key, value in (env_changes or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False)


def _database_fingerprint(url) -> tuple:
    """Heads, relations (incl. relfilenode), every table's rows and sequences."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            heads = tuple(sorted(row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))))
            relations = tuple(tuple(row) for row in conn.execute(text(
                "SELECT c.relname, c.relkind, c.oid::bigint, c.relfilenode::bigint FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname"
            )))
            tables = [row[0] for row in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            ))]
            rows = tuple(
                (name, conn.execute(text(
                    'SELECT md5(coalesce(string_agg(x::text, \'|\' ORDER BY x::text), \'\')) FROM "%s" x'
                    % name.replace('"', '""')
                )).scalar_one())
                for name in tables
            )
            sequences = tuple(tuple(row) for row in conn.execute(text(
                "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='public' ORDER BY 1"
            )))
    finally:
        engine.dispose()
    return heads, relations, rows, sequences


def test_a36_rollback_only_bytes_are_read_only_on_0124_and_0125_and_refuse_unsafe_runs(tmp_path):
    a36_root = _a36_root(tmp_path)
    # The rendering code imported here is byte-identical to a36's own copy.
    for relative in _ROLLBACK_ONLY_MODULES:
        assert (a36_root / relative).read_bytes() == (ROOT / relative).read_bytes(), relative
    # a36 keeps the Communication code dormant; it is not a Communication-free root.
    assert (a36_root / "backend/core/salla_communication_whatsapp.py").is_file()
    pass_line = "RESULT=PASS action=rollback_only_0125"

    with fresh_database() as (url, db_name):
        topology._build(db_name, "0124")
        payload, command = _a36_command(a36_root, db_name)
        before = _database_fingerprint(url)
        assert before[0] == ("0111", "0113", "0124")
        result = _run_a36_command(payload, command, db_name, a36_root)
        assert result.returncode == 0 and pass_line in result.stdout, (result.stdout, result.stderr[-1000:])
        assert _database_fingerprint(url) == before

        migrated = topology._alembic(db_name, "upgrade", "0125")  # test setup, not the command
        assert migrated.returncode == 0, migrated.stderr[-1000:]
        after_migration = _database_fingerprint(url)
        assert after_migration[0] == ("0111", "0113", "0125")
        # The identical frozen payload and command bytes, after the forward migration.
        result = _run_a36_command(payload, command, db_name, a36_root)
        assert result.returncode == 0 and pass_line in result.stdout, (result.stdout, result.stderr[-1000:])
        assert _database_fingerprint(url) == after_migration

        now = datetime.now(timezone.utc)
        expired_payload, expired_command = _a36_command(
            a36_root, db_name, approved_at=now - timedelta(minutes=20), expires_at=now - timedelta(minutes=1))
        tampered = tmp_path / "a36-tampered"
        shutil.copytree(a36_root, tampered)
        with open(tampered / "scripts/operators/salla_comm_0125_rollback.draft.json", "ab") as handle:
            handle.write(b" ")
        tampered_payload, tampered_command = _a36_command(a36_root, db_name, container_root=tampered)
        refusals = {
            "approval_expired": (expired_payload, expired_command, a36_root, None,
                                 "RESULT=FAIL action=rollback_only_0125 reason=approval_expired"),
            "flag_unset": (payload, command, a36_root, {"SALLA_COMM_WHATSAPP_ENABLED": None},
                           "RESULT=FAIL action=rollback_only_0125 reason=flags_not_off"),
            "flag_true": (payload, command, a36_root, {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true"},
                          "RESULT=FAIL action=rollback_only_0125 reason=flags_not_off"),
            "tampered_bytes": (tampered_payload, tampered_command, tampered, None,
                               "BOOTSTRAP FAIL reason=inventory_digest_mismatch"),
        }
        for case, (case_payload, case_command, cwd, changes, expected) in refusals.items():
            result = _run_a36_command(case_payload, case_command, db_name, cwd, changes)
            assert result.returncode != 0 and pass_line not in result.stdout, (case, result.stdout)
            assert expected in result.stdout, (case, result.stdout, result.stderr[-1000:])
            assert _database_fingerprint(url) == after_migration, case
