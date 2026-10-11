"""Shared harness for the Shopify release gate tests (DB-free and disposable PostgreSQL).

Builds a synthetic container root from the repository's inventory files, fills a
synthetic approval record bound to that root's runtime inventory, renders the
trusted command for it and runs it exactly as the platform would (``bash -c``).
Every identity value, timestamp and credential here is synthetic.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO)) if str(REPO) not in sys.path else None

from scripts.operators import shopify_release_gate as gate  # noqa: E402
from scripts.operators import shopify_release_gate_contract as k  # noqa: E402
from scripts.operators import shopify_release_gate_render as render  # noqa: E402

SYNTHETIC_RELEASE_SHA = "a" * 39 + "1"
SYNTHETIC_IDENTITY = {
    "project": "11111111-1111-4111-8111-111111111111",
    "environment": "22222222-2222-4222-8222-222222222222",
    "service": "33333333-3333-4333-8333-333333333333",
}
NOW = _dt.datetime(2026, 10, 11, 18, 0, 0, tzinfo=_dt.timezone.utc)


def iso(moment: _dt.datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def copy_inventory(root: Path, source: Path = REPO) -> None:
    """Copy everything the gate reads from a checkout into ``root`` (database tree, operator files, flag modules)."""
    shutil.copytree(source / "database", root / "database", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in list(k.LEGACY_FIXED_FILES) + list(k.RUNTIME_EXTRA_FILES) + list(k.FULL_EXTRA_FILES) + ["scripts/operators/__init__.py"]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)


def filled_approval(runtime: Tuple[int, str], *, now: _dt.datetime = NOW, phases: Sequence[str] = ("S0", "S1", "S2", "S3"),
                    restore_verified: bool = True) -> Dict[str, Any]:
    """A synthetic, fully filled record bound to ``runtime`` (count, digest); approvals valid around ``now``."""
    record: Dict[str, Any] = {
        "record_version": k.APPROVAL_RECORD_VERSION,
        "bound_runtime_inventory_digest": runtime[1],
        "bound_runtime_inventory_count": runtime[0],
        "release_label": "synthetic-release",
        "deployment_rollback_snapshot": {
            "railway_deployment_id": "44444444-4444-4444-8444-444444444444",
            "source_sha": "b" * 40,
            "verified_redeployable_at_utc": iso(now - _dt.timedelta(hours=3)),
            "verified_by": "synthetic-operator",
        },
        "db_recovery_evidence": {
            "backup": {
                "service_id": "55555555-5555-4555-8555-555555555555",
                "volume_id": "66666666-6666-4666-8666-666666666666",
                "card_timestamp_utc": iso(now - _dt.timedelta(hours=2)),
                "size_mb": 1,
                "observed_by": "synthetic-operator",
                "observed_at_utc": iso(now - _dt.timedelta(hours=2)),
            },
            "restore_verified": bool(restore_verified),
            "restore_verification_ref": "synthetic-restore-rehearsal" if restore_verified else None,
            "restore_verified_at_utc": iso(now - _dt.timedelta(hours=1)) if restore_verified else None,
        },
        "phase_approvals": {},
    }
    for phase in ("S0", "S1", "S2", "S3"):
        approved = phase in phases
        record["phase_approvals"][phase] = {
            "approved": approved,
            "reviewer": "synthetic-reviewer" if approved else None,
            "independent_review_ref": "synthetic-review" if approved else None,
            "approved_at_utc": iso(now - _dt.timedelta(hours=1)) if approved else None,
            "valid_until_utc": iso(now + _dt.timedelta(days=2)) if approved else None,
        }
    return record


def write_approval(root: Path, record: Mapping[str, Any]) -> None:
    with open(root / k.APPROVAL_RELATIVE_PATH, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1, sort_keys=True)


def build_root(root: Path, *, approval: Optional[Mapping[str, Any]] = None, source: Path = REPO, **approval_kwargs: Any) -> Path:
    """A synthetic container root with a filled approval bound to its own runtime inventory."""
    copy_inventory(root, source)
    record = approval if approval is not None else filled_approval(gate.runtime_inventory(root), **approval_kwargs)
    write_approval(root, record)
    return root


def rendered(root: Path, phase: str, db: str, *, release_sha: str = SYNTHETIC_RELEASE_SHA, identity: Mapping[str, str] = SYNTHETIC_IDENTITY) -> str:
    return render.render_command(root, release_sha=release_sha, phase=phase, project=identity["project"],
                                 environment=identity["environment"], service=identity["service"], db=db,
                                 python=sys.executable, container_root=str(root))


def gate_environment(database_url: str, *, release_sha: str = SYNTHETIC_RELEASE_SHA, identity: Mapping[str, str] = SYNTHETIC_IDENTITY,
                     extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A minimal, explicit process environment: nothing is inherited from the test runner's target."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8",
           k.RELEASE_SHA_ENV: release_sha, "DATABASE_URL": database_url}
    for key, env_name in k.IDENTITY_ENVS.items():
        env[env_name] = identity[key]
    env.update(extra or {})
    return env


def run_command(command: str, env: Mapping[str, str], *, timeout: int = 120) -> Tuple[int, str]:
    completed = subprocess.run(["bash", "-c", command], env=dict(env), capture_output=True, text=True, timeout=timeout)
    return completed.returncode, completed.stdout + completed.stderr


def run_gate(root: Path, phase: str, database_url: str, db_target: str, *, extra_env: Optional[Mapping[str, str]] = None,
             release_sha: str = SYNTHETIC_RELEASE_SHA, env_release_sha: Optional[str] = None) -> Tuple[int, str]:
    """Render the trusted command for ``root`` and run it with an explicit synthetic environment."""
    command = rendered(root, phase, db_target, release_sha=release_sha)
    env = gate_environment(database_url, release_sha=env_release_sha or release_sha, extra=extra_env)
    return run_command(command, env)


def checks(output: str) -> Dict[str, Tuple[str, Optional[str]]]:
    """``{check_name: (status, reason)}`` parsed from gate output."""
    parsed: Dict[str, Tuple[str, Optional[str]]] = {}
    for line in output.splitlines():
        if not line.startswith(f"{k.LOG_PREFIX} CHECK "):
            continue
        parts = line[len(k.LOG_PREFIX) + 7:].split(" ")
        name, status = parts[0], parts[1]
        reason = None
        for token in parts[2:]:
            if token.startswith("reason="):
                reason = token[len("reason="):]
        parsed[name] = (status, reason)
    return parsed


def result(output: str) -> Optional[str]:
    for line in output.splitlines():
        if line.startswith(f"{k.LOG_PREFIX} RESULT="):
            return line.split("RESULT=", 1)[1].split(" ", 1)[0]
    return None
