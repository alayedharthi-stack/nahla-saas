"""R1 at the real connection boundary, on PostgreSQL.

The strict runner's identity suite names its target with
``LEGACY_MIG_PG_TEST_DATABASE_URL``. That URL is authoritative: when it is
unreachable the proof fails even though an alternate or default URL is
reachable, and no connection or test-database creation happens on that
alternate service. The positive control uses a reachable explicit target
while every alternate is dead.

These are runner/fixture regressions, not PostgreSQL proofs; the strict
inventory lists them under ``kind: runner_regression``. Requires
``NAHLA_RELIABILITY_REQUIRE_PG=1`` and ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
without them the module skips and the skip is reported, never counted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Set

from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "required_postgres_proofs.py"
COMMITTED_MANIFEST = REPO_ROOT / "scripts" / "required_postgres_proofs.json"
DEAD_URL = "postgresql://nahla:nahla_password@127.0.0.1:5499/nahla_saas"
CONNECTION_ENVS = ("LEGACY_MIG_PG_TEST_DATABASE_URL", "A1_PG_TEST_DATABASE_URL", "DATABASE_URL",
                   "CUSTOMER_NAME_PROVENANCE_PG_REQUIRED", "LEGACY_MIG_PG_INTEGRATION_REQUIRED")


def _identity_only_manifest(tmp_path: Path) -> Path:
    manifest = json.loads(COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    identity = next(s for s in manifest["suites"] if s["id"] == "global_customer_identity")
    path = tmp_path / "identity-only.json"
    path.write_text(json.dumps({"suites": [identity]}), encoding="utf-8")
    return path


def _run_runner(tmp_path: Path, manifest: Path, env: Dict[str, str]) -> Dict[str, Any]:
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--manifest", str(manifest), "--junit-dir", str(tmp_path / "junit"),
         "--report", str(report)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        env={**{k: v for k, v in os.environ.items() if k not in CONNECTION_ENVS}, **env},
    )
    return {"exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "report": json.loads(report.read_text(encoding="utf-8"))}


def _ephemeral_databases(admin_dsn: str) -> Set[str]:
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            return {r[0] for r in conn.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'legacy_mig_%'"))}
    finally:
        admin.dispose()


def test_unreachable_explicit_target_fails_without_fallback_to_a_reachable_service(
    pg_admin_dsn: str, tmp_path: Path,
) -> None:
    before = _ephemeral_databases(pg_admin_dsn)
    result = _run_runner(tmp_path, _identity_only_manifest(tmp_path), {
        "CUSTOMER_NAME_PROVENANCE_PG_REQUIRED": "1",
        "LEGACY_MIG_PG_TEST_DATABASE_URL": DEAD_URL,          # explicit target: nothing listens on 5499
        "A1_PG_TEST_DATABASE_URL": pg_admin_dsn,              # alternates: the live service
        "DATABASE_URL": pg_admin_dsn,
    })
    assert result["exit"] == 1, result["stdout"]
    assert result["report"]["verdict"] == "NOT PROVEN"
    suite = result["report"]["suites"][0]
    assert (suite["passed"], suite["skipped"]) == (0, 0)
    assert suite["errors"] + suite["failed"] == suite["required"]
    blockers = suite["blockers"]
    assert any("no fallback attempted" in b and "127.0.0.1:5499" in b for b in blockers), blockers
    assert all("nahla_password" not in b for b in blockers)   # target reported with the password redacted
    assert _ephemeral_databases(pg_admin_dsn) == before, "a test database was created on the alternate service"


def test_reachable_explicit_target_is_used_even_when_every_alternate_is_dead(
    pg_admin_dsn: str, tmp_path: Path,
) -> None:
    before = _ephemeral_databases(pg_admin_dsn)
    result = _run_runner(tmp_path, _identity_only_manifest(tmp_path), {
        "CUSTOMER_NAME_PROVENANCE_PG_REQUIRED": "1",
        "LEGACY_MIG_PG_TEST_DATABASE_URL": pg_admin_dsn,      # explicit target: the live service
        "A1_PG_TEST_DATABASE_URL": DEAD_URL,                  # alternates: dead
        "DATABASE_URL": DEAD_URL,
    })
    assert result["exit"] == 0, result["stdout"]
    assert result["report"]["verdict"] == "PROVEN"
    suite = result["report"]["suites"][0]
    assert (suite["passed"], suite["skipped"], suite["failed"], suite["errors"]) == (suite["required"], 0, 0, 0)
    assert _ephemeral_databases(pg_admin_dsn) == before, "the identity fixture left an ephemeral database behind"
