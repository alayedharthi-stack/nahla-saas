"""Closed revision attestation for staging operators (fail-closed, no spoofable drift).

Separates **verifier tooling revision** (the operator checkout / container that runs
gates) from **target runtime revision** (the nahla-saas artifact whose modules and
deployment identity are being signed off).

In-container attestation uses Railway build-time env vars only (``.git`` is excluded
from production images). External-runner attestation uses ``git rev-parse`` on an
explicit target checkout at the pinned revision.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Railway and platform build injects — do not accept loose operator-set GIT_SHA here.
_BUILD_ATTESTED_REVISION_ENVS: tuple[str, ...] = (
    "RAILWAY_GIT_COMMIT_SHA",
    "RAILWAY_DEPLOYMENT_COMMIT_SHA",
    "GIT_COMMIT_SHA",
    "COMMIT_SHA",
    "SOURCE_COMMIT",
)

TARGET_APP_ROOT_ENV = "NAHLA_CONSUMER_VERIFY_TARGET_APP_ROOT"

_REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")

CODE_RUNTIME_REVISION_MISMATCH = "runtime_revision_mismatch"
CODE_RUNTIME_REVISION_UNKNOWN = "runtime_revision_unknown"
CODE_TARGET_APP_ROOT_REQUIRED = "target_app_root_required"


class ExecutionMode(str, Enum):
    IN_CONTAINER = "in_container"
    EXTERNAL_RUNNER = "external_runner"


@dataclass(frozen=True)
class RuntimeRevisionAttestation:
    ok: bool
    code: str | None
    execution_mode: ExecutionMode | None
    pinned_target_revision: str | None
    attested_revision: str | None
    target_app_root: str | None
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "attested_revision": self.attested_revision,
            "blockers": list(self.blockers),
            "code": self.code,
            "execution_mode": self.execution_mode.value if self.execution_mode else None,
            "ok": self.ok,
            "pinned_target_revision": self.pinned_target_revision,
            "target_app_root": self.target_app_root,
        }


def normalize_revision_token(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value == "unknown":
        return None
    if not _REVISION_RE.fullmatch(value):
        raise ValueError("revision_token_invalid")
    return value


def revisions_equivalent(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    a = normalize_revision_token(left)
    b = normalize_revision_token(right)
    if a is None or b is None:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def read_build_attested_revision() -> str | None:
    """Read deployment revision from platform build injects (in-container)."""
    for env_name in _BUILD_ATTESTED_REVISION_ENVS:
        try:
            token = normalize_revision_token(os.environ.get(env_name))
        except ValueError:
            continue
        if token:
            return token
    return None


def read_checkout_revision(app_root: Path) -> str | None:
    """Read ``git rev-parse HEAD`` for an external-runner target checkout."""
    root = app_root.resolve()
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        )
        return normalize_revision_token(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def resolve_container_app_root() -> Path | None:
    container_root = Path("/app")
    if (container_root / "backend").is_dir():
        return container_root
    return None


def evaluate_runtime_revision_attestation(
    *,
    pinned_target_revision: str,
    target_app_root: Path | None = None,
) -> RuntimeRevisionAttestation:
    """Fail-closed gate: target artifact identity must match the closed contract pin."""
    blockers: list[str] = []
    try:
        pin = normalize_revision_token(pinned_target_revision)
    except ValueError:
        blockers.append(CODE_RUNTIME_REVISION_MISMATCH)
        pin = None

    build_attested = read_build_attested_revision()
    if build_attested is not None:
        if pin and not revisions_equivalent(build_attested, pin):
            return RuntimeRevisionAttestation(
                ok=False,
                code=CODE_RUNTIME_REVISION_MISMATCH,
                execution_mode=ExecutionMode.IN_CONTAINER,
                pinned_target_revision=pin,
                attested_revision=build_attested,
                target_app_root=str(resolve_container_app_root()) if resolve_container_app_root() else None,
                blockers=(CODE_RUNTIME_REVISION_MISMATCH,),
            )

        container_root = resolve_container_app_root()
        if container_root is None:
            blockers.append(CODE_TARGET_APP_ROOT_REQUIRED)
            root_str = None
        else:
            root_str = str(container_root)
        ok = pin is not None and not blockers
        code = blockers[0] if blockers else None
        return RuntimeRevisionAttestation(
            ok=ok,
            code=code,
            execution_mode=ExecutionMode.IN_CONTAINER,
            pinned_target_revision=pin,
            attested_revision=build_attested,
            target_app_root=root_str,
            blockers=tuple(blockers),
        )

    env_root = os.environ.get(TARGET_APP_ROOT_ENV)
    resolved_root = target_app_root
    if resolved_root is None and env_root:
        resolved_root = Path(env_root)
    if resolved_root is None:
        blockers.append(CODE_TARGET_APP_ROOT_REQUIRED)
        checkout_revision = None
        root_str = None
    else:
        root_str = str(resolved_root.resolve())
        checkout_revision = read_checkout_revision(resolved_root)
        if checkout_revision is None:
            blockers.append(CODE_RUNTIME_REVISION_UNKNOWN)
        elif pin and not revisions_equivalent(checkout_revision, pin):
            blockers.append(CODE_RUNTIME_REVISION_MISMATCH)

    ok = pin is not None and not blockers
    code = blockers[0] if blockers else None
    return RuntimeRevisionAttestation(
        ok=ok,
        code=code,
        execution_mode=ExecutionMode.EXTERNAL_RUNNER,
        pinned_target_revision=pin,
        attested_revision=checkout_revision,
        target_app_root=root_str,
        blockers=tuple(blockers),
    )


__all__ = [
    "CODE_RUNTIME_REVISION_MISMATCH",
    "CODE_RUNTIME_REVISION_UNKNOWN",
    "CODE_TARGET_APP_ROOT_REQUIRED",
    "TARGET_APP_ROOT_ENV",
    "ExecutionMode",
    "RuntimeRevisionAttestation",
    "evaluate_runtime_revision_attestation",
    "normalize_revision_token",
    "read_build_attested_revision",
    "read_checkout_revision",
    "resolve_container_app_root",
    "revisions_equivalent",
]
