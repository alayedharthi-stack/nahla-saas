"""Shared harness for the Shopify release gate tests (DB-free and disposable PostgreSQL).

Builds a synthetic container root from the repository's inventory files, fills
a synthetic external approval payload bound to that root, the synthetic release
and target, renders the trusted bootstrap command and runs it exactly as the
platform would (``bash -c``) with an explicit environment. Every identity
value, timestamp and credential here is synthetic.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.operators import shopify_release_gate as gate  # noqa: E402
from scripts.operators import shopify_release_gate_contract as k  # noqa: E402
from scripts.operators import shopify_release_gate_render as render  # noqa: E402

SYNTHETIC_RELEASE_SHA = "a" * 39 + "1"
SYNTHETIC_IDENTITY = {
    "project": "11111111-1111-4111-8111-111111111111",
    "environment": "22222222-2222-4222-8222-222222222222",
    "service": "33333333-3333-4333-8333-333333333333",
}
SYNTHETIC_BACKUP = {"service": "55555555-5555-4555-8555-555555555555", "volume": "66666666-6666-4666-8666-666666666666"}
NOW = _dt.datetime(2026, 10, 11, 18, 0, 0, tzinfo=_dt.timezone.utc)


def iso(moment: _dt.datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def copy_inventory(root: Path, source: Path = REPO) -> None:
    """Copy everything the gate reads from a checkout into ``root``: the database tree and every pinned file."""
    shutil.copytree(source / "database", root / "database", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in list(k.LEGACY_FIXED_FILES) + list(k.RUNTIME_EXTRA_FILES):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)


def build_root(root: Path, source: Path = REPO) -> Path:
    copy_inventory(root, source)
    return root


def target_for(db: str, identity: Mapping[str, str] = SYNTHETIC_IDENTITY) -> Dict[str, str]:
    return {"project": identity["project"], "environment": identity["environment"], "service": identity["service"],
            "db": k.target_label(k.parse_db_target(db))}


def filled_approval(*, release_sha: str, inventory: Tuple[int, str], target: Mapping[str, str], phase: str,
                    now: Optional[_dt.datetime] = None, restore_verified: bool = True,
                    backup: Mapping[str, str] = SYNTHETIC_BACKUP) -> Dict[str, Any]:
    """A synthetic, fully filled v2 payload valid around ``now`` (the real clock unless given)."""
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    return {
        "record_version": k.APPROVAL_RECORD_VERSION,
        "release_sha": release_sha,
        "runtime_inventory_digest": inventory[1],
        "runtime_inventory_count": inventory[0],
        "target": dict(target),
        "phase": phase,
        "approval": {"approved": True, "reviewer": "synthetic-reviewer", "independent_review_ref": "synthetic-review",
                     "approved_at_utc": iso(moment - _dt.timedelta(hours=1)), "valid_until_utc": iso(moment + _dt.timedelta(days=2))},
        "deployment_rollback_snapshot": {"railway_deployment_id": "44444444-4444-4444-8444-444444444444", "source_sha": "b" * 40,
                                         "verified_redeployable_at_utc": iso(moment - _dt.timedelta(hours=3)), "verified_by": "synthetic-operator"},
        "db_recovery_evidence": {
            "backup": {"service_id": backup["service"], "volume_id": backup["volume"],
                       "card_timestamp_utc": iso(moment - _dt.timedelta(hours=2)), "size_mb": 1, "completed": True, "restore_available": True,
                       "observed_by": "synthetic-operator", "observed_at_utc": iso(moment - _dt.timedelta(hours=1, minutes=30))},
            "restore_verified": bool(restore_verified),
            "restore_verification_ref": "synthetic-restore-rehearsal" if restore_verified else None,
            "restore_verified_at_utc": iso(moment - _dt.timedelta(hours=1, minutes=10)) if restore_verified else None,
        },
    }


def approval_file(directory: Path, record: Mapping[str, Any], name: str = "approval.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(json.dumps(record, sort_keys=True).encode("utf-8"))
    return path


def payload_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def payload_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approval_for(root: Path, phase: str, db: str, *, release_sha: str = SYNTHETIC_RELEASE_SHA, **kwargs: Any) -> Path:
    """A filled payload bound to ``root``'s inventory, written next to (never inside) the root."""
    record = filled_approval(release_sha=release_sha, inventory=gate.inventory(root), target=target_for(db), phase=phase, **kwargs)
    if phase in k.POST_INSTALL_PHASES:
        snapshot = record["deployment_rollback_snapshot"]
        snapshot["source_sha"] = release_sha
        verified = k.parse_utc(snapshot["verified_redeployable_at_utc"])
        bindings = gate.schema_rollback_bindings(root)
        record["schema_rollback"] = {
            "record_version": 1, "phase": "S4_DORMANT", "source_sha": release_sha,
            "railway_deployment_id": snapshot["railway_deployment_id"], "target": record["target"],
            "database_heads": ["0111", "0113", "0124"], "table_names": sorted(k.INSTALL_TABLES),
            "runtime_inventory_count": record["runtime_inventory_count"],
            "runtime_inventory_digest": record["runtime_inventory_digest"],
            "artifact_sha256": bindings["artifact_sha256"],
            "schema_expectations_sha256": bindings["schema_expectations_sha256"],
            "verified_at_utc": iso(verified), "valid_until_utc": iso(verified + _dt.timedelta(hours=k.ROLLBACK_MAX_AGE_HOURS)),
            "independent_review_ref": "synthetic-0124-compatible-source-review",
        }
    return approval_file(root.parent / "approval", record, f"{root.name}_{phase}.json")


def prior_rollback_record(root: Path, record: Mapping[str, Any]) -> Dict[str, Any]:
    """``record`` with its rollback switched to the prior source pinned in ``root``'s manifest:
    snapshot and evidence name that full SHA, the evidence its pinned inventory and every
    flag ``"false"``. Everything else (target, deployment id, times, review) is unchanged."""
    changed = json.loads(json.dumps(record))
    prior = gate.schema_rollback_bindings(root)["artifact"]["dormant_prior_rollback"]
    changed["deployment_rollback_snapshot"]["source_sha"] = prior["source_sha"]
    rollback = changed["schema_rollback"]
    rollback.update(source_sha=prior["source_sha"], runtime_inventory_digest=prior["runtime_inventory_digest"],
                    runtime_inventory_count=prior["runtime_inventory_count"],
                    flags={name: "false" for name in k.SHOPIFY_FLAG_ENVS})
    return changed


def prior_rollback_approval_for(root: Path, phase: str, db: str, **kwargs: Any) -> Path:
    """A filled payload like ``approval_for`` whose rollback is the manifest-pinned prior source."""
    base = json.loads(approval_for(root, phase, db, **kwargs).read_text(encoding="utf-8"))
    return approval_file(root.parent / "approval", prior_rollback_record(root, base), f"{root.name}_{phase}_prior.json")


def rendered(root: Path, phase: str, db: str, approval: Path, *, release_sha: str = SYNTHETIC_RELEASE_SHA,
             identity: Mapping[str, str] = SYNTHETIC_IDENTITY, backup: Mapping[str, str] = SYNTHETIC_BACKUP) -> str:
    return render.render_command(root, python=sys.executable, release_sha=release_sha, phase=phase, approval_sha256=payload_sha(approval),
                                 project=identity["project"], environment=identity["environment"], service=identity["service"], db=db,
                                 backup_service=backup["service"], backup_volume=backup["volume"], container_root=str(root))


def gate_environment(database_url: str, *, release_sha: str = SYNTHETIC_RELEASE_SHA, identity: Mapping[str, str] = SYNTHETIC_IDENTITY,
                     approval: Optional[Path] = None, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A minimal, explicit process environment: nothing is inherited from the test runner's target."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8",
           k.RELEASE_SHA_ENV: release_sha, "DATABASE_URL": database_url}
    for key, env_name in k.IDENTITY_ENVS.items():
        env[env_name] = identity[key]
    if approval is not None:
        env[k.APPROVAL_PAYLOAD_ENV] = payload_b64(approval)
    env.update(extra or {})
    return env


def run_command(command: str, env: Mapping[str, str], *, timeout: int = 120) -> Tuple[int, str]:
    completed = subprocess.run(["bash", "-c", command], env=dict(env), capture_output=True, text=True, timeout=timeout)
    return completed.returncode, completed.stdout + completed.stderr


def run_gate(root: Path, phase: str, database_url: str, db_target: str, *, approval: Optional[Path] = None,
             extra_env: Optional[Mapping[str, str]] = None, release_sha: str = SYNTHETIC_RELEASE_SHA,
             env_release_sha: Optional[str] = None, **approval_kwargs: Any) -> Tuple[int, str]:
    """Render the trusted command for ``root`` (with a payload bound to it) and run it with an explicit synthetic environment."""
    payload = approval or approval_for(root, phase, db_target, release_sha=release_sha, **approval_kwargs)
    command = rendered(root, phase, db_target, payload, release_sha=release_sha)
    env = gate_environment(database_url, release_sha=env_release_sha or release_sha, approval=payload, extra=extra_env)
    return run_command(command, env)


def checks(output: str) -> Dict[str, Tuple[str, Optional[str]]]:
    """``{check_name: (status, reason)}`` parsed from gate output."""
    parsed: Dict[str, Tuple[str, Optional[str]]] = {}
    for line in output.splitlines():
        if not line.startswith(f"{k.LOG_PREFIX} CHECK "):
            continue
        parts = line[len(k.LOG_PREFIX) + 7:].split(" ")
        reason = None
        for token in parts[2:]:
            if token.startswith("reason="):
                reason = token[len("reason="):]
        parsed[parts[0]] = (parts[1], reason)
    return parsed


def result(output: str) -> Optional[str]:
    for line in output.splitlines():
        if line.startswith(f"{k.LOG_PREFIX} RESULT="):
            return line.split("RESULT=", 1)[1].split(" ", 1)[0]
    return None


def bootstrap(output: str) -> Optional[str]:
    """``"PASS"`` or the bootstrap's refusal token, or None if the bootstrap never reported."""
    for line in output.splitlines():
        if line.startswith(f"{k.LOG_PREFIX} BOOTSTRAP "):
            rest = line.split("BOOTSTRAP ", 1)[1]
            if rest.startswith("PASS"):
                return "PASS"
            if "reason=" in rest:
                return rest.split("reason=", 1)[1].split(" ", 1)[0]
    return None


def plant_marker(path: Path, marker: Path) -> bytes:
    """Prepend an import-time side effect to a module; returns the original bytes for restoration."""
    original = path.read_bytes()
    path.write_bytes(f"open({str(marker)!r}, 'w').close()\n".encode("utf-8") + original)
    return original
