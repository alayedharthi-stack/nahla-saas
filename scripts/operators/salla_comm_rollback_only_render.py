"""Render reviewable bytes for the container rollback-only predeploy command.

The normal release renderer is unchanged. Operator-side authenticated reads
and approval provenance are required before these bytes may be installed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as existing
from scripts.operators import shopify_release_gate_render as prior_render
from scripts.operators import salla_comm_rollback_only_contract as terminal
from scripts.operators import salla_comm_rollback_only_source as source


APPROVAL_SHA_PLACEHOLDER = "__APPROVAL_SHA256__"
RUNNER_RELATIVE_PATH = "scripts/operators/salla_comm_rollback_only_gate.py"
PRIOR_RUNNER_RELATIVE_PATH = "scripts/operators/shopify_release_gate.py"


def command_template(root: Path, *, release_sha: str, target: terminal.Target,
                     backup_service: str, backup_volume: str,
                     container_root: str = existing.CONTAINER_ROOT) -> bytes:
    """Hash this template in the approval payload; no approval hash cycle."""
    terminal.require(terminal._source(release_sha), "source_invalid")
    terminal.require(existing.parse_db_target(target.database_identity) is not None,
                     "database_target_invalid")
    for value in (target.project, target.environment, target.service,
                  backup_service, backup_volume):
        terminal.require(bool(existing.UUID_RE.fullmatch(value)), "identity_invalid")
    for value in (container_root, target.database_identity):
        prior_render._check_tokens(value)
    terminal.require(not gate.unexpected_initializers(root), "initializer_unexpected")
    count, digest = source.candidate_inventory(root)
    runner_sha = gate.file_sha256(root / RUNNER_RELATIVE_PATH)
    argv = [
        "--root", container_root, "--action", terminal.ACTION,
        "--release-sha", release_sha, "--inventory-digest", digest,
        "--inventory-count", str(count), "--approval-sha256", APPROVAL_SHA_PLACEHOLDER,
        "--expect-project", target.project, "--expect-environment", target.environment,
        "--expect-service", target.service, "--expect-db", target.database_identity,
        "--expect-backup-service", backup_service,
        "--expect-backup-volume", backup_volume,
    ]
    literals = {
        "prefix": existing.LOG_PREFIX, "root": container_root, "release": release_sha,
        "identity": {
            existing.IDENTITY_ENVS["project"]: target.project,
            existing.IDENTITY_ENVS["environment"]: target.environment,
            existing.IDENTITY_ENVS["service"]: target.service,
        },
        "libpq": list(existing.LIBPQ_TARGET_ENV_VARS),
        "fixed": sorted(set(existing.LEGACY_FIXED_FILES)
                        | set(existing.RUNTIME_EXTRA_FILES) | set(source.ACTION_FILES)),
        "absent": list(existing.MUST_BE_ABSENT),
        "count": count, "digest": digest,
        "gate_sha": runner_sha, "approval_sha": APPROVAL_SHA_PLACEHOLDER,
        "argv": argv,
    }
    bootstrap = prior_render.BOOTSTRAP_TEMPLATE
    terminal.require(bootstrap.count(PRIOR_RUNNER_RELATIVE_PATH) == 1,
                     "bootstrap_runner_path_ambiguous")
    bootstrap = bootstrap.replace(PRIOR_RUNNER_RELATIVE_PATH, RUNNER_RELATIVE_PATH)
    for name, value in literals.items():
        bootstrap = bootstrap.replace("{" + name + "}", json.dumps(value, sort_keys=True))
    terminal.require(bootstrap.count(APPROVAL_SHA_PLACEHOLDER) == 2,
                     "approval_placeholder_ambiguous")
    return bootstrap.encode("utf-8")


def bind_command(template: bytes, approval_sha256: str) -> bytes:
    """Return the exact shell command bytes for later provider readback."""
    terminal.require(terminal._digest(approval_sha256), "approval_digest_invalid")
    decoded = template.decode("utf-8")
    terminal.require(decoded.count(APPROVAL_SHA_PLACEHOLDER) == 2,
                     "approval_placeholder_ambiguous")
    source_bytes = decoded.replace(APPROVAL_SHA_PLACEHOLDER, approval_sha256).encode("utf-8")
    encoded = base64.b64encode(source_bytes).decode("ascii")
    return ("python -I -c \"import base64,sys;exec(compile(base64.b64decode('"
            + encoded + "'),'release-gate-bootstrap','exec'))\"").encode("ascii")


class CandidateCommandRenderer:
    def __init__(self, approval_sha256: str):
        self.approval_sha256 = approval_sha256

    def render(self, command_template_bytes: bytes,
               approval: terminal.Approval) -> bytes:
        return bind_command(command_template_bytes, self.approval_sha256)
