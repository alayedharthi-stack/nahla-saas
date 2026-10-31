"""Proposed PG18 check for the restart window after 0125, before forward.

Uses the existing disposable PG18 topology and synthetic rows only. It proves
command behavior, not authenticated Railway or owner evidence or live rollback.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import shlex
import subprocess
import sys

from sqlalchemy import create_engine, text

from backend.tests import _shopify_release_gate_harness as harness
from backend.tests import test_shopify_release_gate_pg as topology
from backend.tests import test_salla_comm_retained_0125_rollback_pg as retained
from backend.tests import test_salla_comm_release_gate_0125_pg as release
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as existing
from scripts.operators import salla_comm_rollback_only_contract as terminal
from scripts.operators import salla_comm_rollback_only_gate as runner
from scripts.operators import salla_comm_rollback_only_render as render
from scripts.operators import salla_comm_rollback_only_source as source


pytestmark = topology.pytestmark


def _actual_terminal_command_on_isolated_pg18(db_name: str) -> None:
    """Run actual rendered bootstrap bytes, not a mocked renderer/entrypoint.

    The operator evidence here is synthetic. This proves the real container
    command and DB checks, not an authenticated owner or Railway read.
    """
    root = retained.ROOT
    release_sha = "a" * 40
    target = terminal.Target(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        topology._target(db_name),
    )
    count, digest = source.candidate_inventory(root)
    template = render.command_template(
        root, release_sha=release_sha, target=target,
        backup_service="44444444-4444-4444-8444-444444444444",
        backup_volume="55555555-5555-4555-8555-555555555555",
        container_root=str(root),
    )
    now = datetime.now(timezone.utc)
    record = {
        "record_version": 1, "action": terminal.ACTION,
        "target": {"project": target.project, "environment": target.environment,
                   "service": target.service,
                   "database_identity": target.database_identity},
        "candidate_source_sha": release_sha,
        "reference_deployment_id": "synthetic-reference-only",
        "reference_source_sha": "9" * 40,
        "reference_image_digest": "b" * 64,
        "reference_predeploy_command_sha256": "c" * 64,
        "rollback_artifact_deployment_id": "synthetic-artifact-only",
        "rollback_artifact_source_sha": release_sha,
        "rollback_artifact_image_digest": "d" * 64,
        "rollback_artifact_predeploy_command_sha256": "e" * 64,
        "owner_approval_reference": "synthetic-owner-message",
        "backup_service_id": "44444444-4444-4444-8444-444444444444",
        "backup_volume_id": "55555555-5555-4555-8555-555555555555",
        "backup_receipt_sha256": "1" * 64,
        "restore_receipt_sha256": "2" * 64,
        "rollback_command_template_sha256": sha256(template).hexdigest(),
        "inventory_digest": digest, "inventory_count": count,
        "shopify_schema_sha256": gate.file_sha256(
            root / existing.INSTALL_EXPECTATIONS_RELATIVE_PATH),
        "communication_schema_sha256": gate.file_sha256(
            root / existing.COMM_EXPECTATIONS_RELATIVE_PATH),
        "approved_at_utc": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "approved": True,
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    command = render.bind_command(template, sha256(payload).hexdigest())
    argv = shlex.split(command.decode("ascii"))
    assert argv[0] == "python" and argv[1:3] == ["-I", "-c"]
    argv[0] = sys.executable
    env = {key: os.environ[key] for key in
           ("PATH", "HOME", "LANG", "SystemRoot", "TEMP", "TMP")
           if key in os.environ}
    env.update({
        "RAILWAY_GIT_COMMIT_SHA": release_sha,
        "RAILWAY_PROJECT_ID": target.project,
        "RAILWAY_ENVIRONMENT_ID": target.environment,
        "RAILWAY_SERVICE_ID": target.service,
        "DATABASE_URL": topology._alias_url(db_name),
        existing.APPROVAL_PAYLOAD_ENV: base64.b64encode(payload).decode("ascii"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.update({name: "false" for name in terminal.FLAG_NAMES})
    # No Railway credentials, Salla credentials, or production database.
    result = subprocess.run(argv, cwd=root, env=env, capture_output=True,
                            text=True, timeout=120, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr[-1000:])
    assert "RESULT=PASS action=rollback_only_0125" in result.stdout


def _synthetic_positive_policy_on_real_pg18(db_name: str) -> None:
    """Exercise candidate policy and DB reader; provider/approval are fakes.

    This is intentionally not a successful Railway command or provider proof.
    The separate subprocess test above verifies the real command refuses.
    """
    root = retained.ROOT
    release_sha = "a" * 40
    target = terminal.Target(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        topology._target(db_name),
    )
    count, digest = source.candidate_inventory(root)
    backup_service = "44444444-4444-4444-8444-444444444444"
    backup_volume = "55555555-5555-4555-8555-555555555555"
    template = render.command_template(
        root, release_sha=release_sha, target=target,
        backup_service=backup_service, backup_volume=backup_volume,
        container_root=str(root),
    )
    approval_pin = "f" * 64
    now = datetime.now(timezone.utc)
    approval = terminal.Approval(
        action=terminal.ACTION, target=target,
        candidate_source_sha=release_sha,
        reference_deployment_id="synthetic-reference-only",
        reference_source_sha="9" * 40,
        reference_image_digest="b" * 64,
        reference_predeploy_command_sha256="c" * 64,
        rollback_artifact_deployment_id="synthetic-artifact-only",
        rollback_artifact_source_sha=release_sha,
        rollback_artifact_image_digest="d" * 64,
        rollback_artifact_predeploy_command_sha256="e" * 64,
        owner_approval_reference="synthetic-owner-message",
        backup_service_id=backup_service,
        backup_volume_id=backup_volume,
        backup_receipt_sha256="1" * 64,
        restore_receipt_sha256="2" * 64,
        rollback_command_template_sha256=sha256(template).hexdigest(),
        inventory_digest=digest, inventory_count=count,
        shopify_schema_sha256=gate.file_sha256(
            root / existing.INSTALL_EXPECTATIONS_RELATIVE_PATH),
        communication_schema_sha256=gate.file_sha256(
            root / existing.COMM_EXPECTATIONS_RELATIVE_PATH),
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5), approved=True,
    )
    reference = terminal.Deployment(
        "synthetic-reference-only", target, "9" * 40, "b" * 64,
        "c" * 64, "SUCCESS", True,
    )

    class SyntheticProvider:
        def __init__(self):
            self.items = {reference.deployment_id: reference}
            self.current_id = reference.deployment_id

        def deployment(self, deployment_id):
            return self.items[deployment_id]

        def current_deployment_id(self, requested_target):
            assert requested_target == target
            return self.current_id

    class SyntheticApproval:
        def approved_action(self):
            return approval

    provider = SyntheticProvider()
    environ = {
        "RAILWAY_GIT_COMMIT_SHA": release_sha,
        "RAILWAY_PROJECT_ID": target.project,
        "RAILWAY_ENVIRONMENT_ID": target.environment,
        "RAILWAY_SERVICE_ID": target.service,
        "DATABASE_URL": topology._alias_url(db_name),
        **{name: "false" for name in terminal.FLAG_NAMES},
    }
    argv = [
        "--root", str(root), "--action", terminal.ACTION,
        "--release-sha", release_sha, "--inventory-digest", digest,
        "--inventory-count", str(count), "--approval-sha256", approval_pin,
        "--expect-project", target.project,
        "--expect-environment", target.environment,
        "--expect-service", target.service,
        "--expect-db", target.database_identity,
        "--expect-backup-service", backup_service,
        "--expect-backup-volume", backup_volume,
    ]
    authorized = runner.evaluate(
        argv, environ, now, approvals=SyntheticApproval(),
        database=source.GateDatabaseReadPort(root, environ, target.database_identity),
        renderer=render.CandidateCommandRenderer(approval_pin),
    )
    execution_hash = sha256(render.bind_command(template, approval_pin)).hexdigest()
    assert authorized.execution_command_sha256 == execution_hash

    # This result is a test fixture. The deployed result ID must be distinct
    # and independently read even in the synthetic positive policy path.
    result = terminal.Deployment(
        "synthetic-result-only", target, release_sha, "d" * 64,
        execution_hash, "SUCCESS", True,
    )
    provider.items[result.deployment_id] = result
    provider.current_id = result.deployment_id
    assert terminal.validate_result(
        provider=provider, authorized=authorized,
        result_deployment_id=result.deployment_id, now=now,
    ) == result


def test_restart_window_retains_audit_and_nonce_before_forward(tmp_path):
    with retained.fresh_database() as (url, db_name):
        topology._build(db_name, "0124")
        assert {row[0] for row in topology._sql(
            db_name, "SELECT version_num FROM alembic_version"
        )} == {"0111", "0113", "0124"}
        migrated = topology._alembic(db_name, "upgrade", "0125")
        assert migrated.returncode == 0, migrated.stderr[-1000:]

        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                assert {row[0] for row in conn.execute(text(
                    "SELECT version_num FROM alembic_version"
                ))} == {"0111", "0113", "0125"}
                schema = retained._communication_schema(conn)
                oids = tuple(conn.execute(text(
                    "SELECT c.relname,c.oid FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname='public' AND "
                    "c.relname IN ('webhook_events',"
                    "'salla_communication_function_nonces') ORDER BY c.relname"
                )).all())
                row_id = conn.execute(text(
                    "INSERT INTO webhook_events "
                    "(provider,event_type,external_event_id,store_id,raw_body,status,"
                    "communication_send_state,communication_provider_message_id) "
                    "VALUES ('salla','synthetic.event','pre-forward-restart',"
                    "'synthetic-store','synthetic-only','processed','accepted',"
                    "'synthetic-provider-id') RETURNING id"
                )).scalar_one()
                nonce = "abcdef0123456789abcdef0123456789"
                conn.execute(text(
                    "INSERT INTO salla_communication_function_nonces(nonce,created_at) "
                    "VALUES (:nonce,CURRENT_TIMESTAMP)"
                ), {"nonce": nonce})
                audit = conn.execute(text(
                    "SELECT communication_send_state,communication_provider_message_id "
                    "FROM webhook_events WHERE id=:id"
                ), {"id": row_id}).one()

            # Exercise the real startup DB callback, with provider egress and
            # schedulers blocked by the existing synthetic startup probe.
            retained._startup_database_tasks(
                retained.ROOT, url.render_as_string(hide_password=False),
                tmp_path / "startup_hook_before_forward",
            )
            release._isolated_healthz_smoke(db_name, tmp_path)
            _actual_terminal_command_on_isolated_pg18(db_name)
            _synthetic_positive_policy_on_real_pg18(db_name)

            # Exercise both real command entrypoints on the disposable DB.
            # These synthetic approvals prove the gates, not a Railway launch.
            forward_root = harness.build_root(tmp_path / "forward_gate")
            forward_approval = release._synthetic_approval(forward_root, db_name)
            forward_rc, forward_output = harness.run_gate(
                forward_root, "S4_COMM_DORMANT", topology._alias_url(db_name),
                topology._target(db_name), approval=forward_approval,
            )
            assert forward_rc == 0 and "RESULT=PASS" in forward_output, forward_output[-1200:]
            _actual_terminal_command_on_isolated_pg18(db_name)
            retained._startup_database_tasks(
                retained.ROOT, url.render_as_string(hide_password=False),
                tmp_path / "startup_hook_after_synthetic_forward",
            )
            after_forward = tmp_path / "after_synthetic_forward"
            after_forward.mkdir()
            release._isolated_healthz_smoke(db_name, after_forward)

            with engine.connect() as conn:
                assert {row[0] for row in conn.execute(text(
                    "SELECT version_num FROM alembic_version"
                ))} == {"0111", "0113", "0125"}
                assert retained._communication_schema(conn) == schema
                assert tuple(conn.execute(text(
                    "SELECT c.relname,c.oid FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname='public' AND "
                    "c.relname IN ('webhook_events',"
                    "'salla_communication_function_nonces') ORDER BY c.relname"
                )).all()) == oids
                assert conn.execute(text(
                    "SELECT communication_send_state,communication_provider_message_id "
                    "FROM webhook_events WHERE id=:id"
                ), {"id": row_id}).one() == audit
                assert conn.execute(text(
                    "SELECT COUNT(*) FROM salla_communication_function_nonces "
                    "WHERE nonce=:nonce"
                ), {"nonce": nonce}).scalar_one() == 1
        finally:
            engine.dispose()
