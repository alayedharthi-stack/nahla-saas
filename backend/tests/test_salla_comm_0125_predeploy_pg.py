"""Exact 0125 pre-deploy command on disposable PostgreSQL 18 only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, text

from backend.tests import _shopify_release_gate_harness as harness
from backend.tests import test_shopify_release_gate_pg as topology
from backend.tests import test_salla_comm_release_gate_0125_pg as release
from backend.tests import test_salla_comm_retained_0125_rollback_pg as retained
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import shopify_release_gate_render as render
from scripts.operators import salla_comm_0125_predeploy as predeploy

pytestmark = topology.pytestmark


def _command(root: Path, db: str, approval: Path) -> str:
    return render.render_command(
        root, python="python", release_sha=harness.SYNTHETIC_RELEASE_SHA,
        phase="S4_COMM_DORMANT", approval_sha256=harness.payload_sha(approval),
        project=harness.SYNTHETIC_IDENTITY["project"],
        environment=harness.SYNTHETIC_IDENTITY["environment"],
        service=harness.SYNTHETIC_IDENTITY["service"], db=topology._target(db),
        backup_service=harness.SYNTHETIC_BACKUP["service"],
        backup_volume=harness.SYNTHETIC_BACKUP["volume"], container_root=str(root),
        migrate_0125=True,
    )


def _environment(db: str, approval: Path) -> dict[str, str]:
    return harness.gate_environment(
        topology._alias_url(db), approval=approval,
        extra={name: "false" for name in k.SHOPIFY_FLAG_ENVS + (k.COMM_SEND_FLAG_ENV,)},
    )


def _heads(db: str) -> set[str]:
    return {row[0] for row in topology._sql(db, "SELECT version_num FROM alembic_version")}


def test_exact_predeploy_bytes_reject_before_write_then_migrate_once_and_retry(tmp_path):
    with retained.fresh_database() as (url, db):
        topology._build(db, "0124")
        root = harness.build_root(tmp_path / "candidate")
        approval = release._synthetic_approval(root, db)
        command = _command(root, db, approval)
        assert "salla_comm_0125_predeploy.py" in render.decode_command(command)
        env = _environment(db, approval)
        assert _heads(db) == predeploy.BEFORE

        bad_flag = dict(env, SALLA_COMM_WHATSAPP_ENABLED="true")
        rc, output = harness.run_command(command, bad_flag)
        assert rc != 0 and "CHECK shopify_flags FAIL" in output
        assert _heads(db) == predeploy.BEFORE

        bad_target = dict(env, RAILWAY_SERVICE_ID="77777777-7777-4777-8777-777777777777")
        rc, output = harness.run_command(command, bad_target)
        assert rc != 0 and "BOOTSTRAP FAIL reason=identity_mismatch" in output
        assert _heads(db) == predeploy.BEFORE

        expired = json.loads(approval.read_text(encoding="utf-8"))
        expired["approval"]["valid_until_utc"] = harness.iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        expired_file = harness.approval_file(tmp_path / "approval", expired, "expired.json")
        rc, output = harness.run_command(_command(root, db, expired_file), _environment(db, expired_file))
        assert rc != 0 and "CHECK approval FAIL" in output
        assert _heads(db) == predeploy.BEFORE

        rc, output = harness.run_command(command, env, timeout=240)
        assert rc == 0, output[-1600:]
        assert "MIGRATION heads_before=0111,0113,0124" in output
        assert "MIGRATION heads_after=0111,0113,0125" in output
        assert "RESULT=PASS phase=S4_COMM_DORMANT" in output
        assert _heads(db) == predeploy.AFTER

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                row_id = conn.execute(text(
                    "INSERT INTO webhook_events (provider,event_type,external_event_id,store_id,"
                    "raw_body,status,communication_send_state) VALUES "
                    "('salla','synthetic.event','predeploy-retry','synthetic-store',"
                    "'synthetic-only','processed','accepted') RETURNING id"
                )).scalar_one()
                nonce = "fedcba9876543210fedcba9876543210"
                conn.execute(text(
                    "INSERT INTO salla_communication_function_nonces(nonce,created_at) "
                    "VALUES (:nonce,CURRENT_TIMESTAMP)"
                ), {"nonce": nonce})
            rc, output = harness.run_command(command, env, timeout=180)
            assert rc == 0 and "MIGRATION SKIP reason=already_0125" in output, output[-1600:]
            assert "MIGRATION heads_after=" not in output
            with engine.connect() as conn:
                assert conn.execute(text(
                    "SELECT communication_send_state FROM webhook_events WHERE id=:id"
                ), {"id": row_id}).scalar_one() == "accepted"
                assert conn.execute(text(
                    "SELECT count(*) FROM salla_communication_function_nonces WHERE nonce=:nonce"
                ), {"nonce": nonce}).scalar_one() == 1
        finally:
            engine.dispose()


def test_post_migration_gate_failure_keeps_0125_and_retry_is_read_only(tmp_path, monkeypatch):
    with retained.fresh_database() as (_url, db):
        topology._build(db, "0124")
        root = harness.build_root(tmp_path / "candidate")
        approval = release._synthetic_approval(root, db)
        command = _command(root, db, approval)
        source = render.decode_command(command)
        # Use the exact bootstrap argv, with only the final gate result forced
        # to fail after a real migration. This simulates a failed pre-deploy;
        # it does not claim a provider launch or production failure mode.
        import ast
        bootstrap = ast.parse(source)
        argv = next(ast.literal_eval(node.value) for node in bootstrap.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "ARGV" for target in node.targets))
        env = _environment(db, approval)
        monkeypatch.setenv("DATABASE_URL", env["DATABASE_URL"])
        original_gate_main = gate.main
        monkeypatch.setattr(gate, "main", lambda *_args, **_kwargs: k.EXIT_FAIL)
        assert predeploy.main(argv, env) == k.EXIT_FAIL
        assert _heads(db) == predeploy.AFTER
        monkeypatch.setattr(gate, "main", original_gate_main)
        rc, output = harness.run_command(command, env, timeout=180)
        assert rc == 0 and "MIGRATION SKIP reason=already_0125" in output, output[-1600:]
        assert _heads(db) == predeploy.AFTER


def test_unexpected_prior_heads_refuse_without_migration(tmp_path):
    with retained.fresh_database() as (_url, db):
        topology._build(db, "0123")
        root = harness.build_root(tmp_path / "candidate")
        approval = release._synthetic_approval(root, db)
        rc, output = harness.run_command(_command(root, db, approval), _environment(db, approval))
        assert rc != 0 and "RESULT=FAIL reason=unexpected_heads" in output
        assert "MIGRATION heads_before=<unexpected>" in output
        assert _heads(db) == {"0111", "0113", "0123"}

