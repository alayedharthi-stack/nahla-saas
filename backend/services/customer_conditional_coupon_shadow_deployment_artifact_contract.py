"""Closed deployment-artifact contract for the conditional-coupon shadow slice.

Deterministic inventory + importability checks for a purported shadow-capable
``nahla-saas`` artifact (repo checkout or ``/app`` container layout). Does not
enable runtime flags, touch Railway, or mutate coupon logic.
"""
from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

CONTRACT_VERSION = "coupon_shadow_deployment_artifact_v1"
ARTIFACT_KIND = "nahla_saas_conditional_coupon_shadow_slice"

# Container layout baked by the shared Dockerfile (WORKDIR /app, COPY . .).
DEPLOYMENT_APP_ROOT = "/app"
BACKEND_REL = "backend"

SHADOW_FLAG_ENV = "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED"
SHADOW_FLAG_ACCESSOR = "is_customer_conditional_coupon_shadow_enabled"
SHADOW_FLAG_MODULE = "modules.ai.brain.truth_surface.flags"

# Layer 0 truth-surface modules required for shadow observation.
REQUIRED_TRUTH_SURFACE_FILES = frozenset(
    {
        "customer_conditional_coupon_loader.py",
        "customer_conditional_coupon_shadow_readiness.py",
    }
)

# Post-#617 fixture operator slice (staging observation tuple harness).
REQUIRED_FIXTURE_OPERATOR_FILES = frozenset(
    {
        "services/customer_conditional_coupon_shadow_fixture.py",
        "services/customer_conditional_coupon_shadow_fixture_contract.py",
        "scripts/seed_customer_conditional_coupon_shadow_fixture.py",
    }
)

REQUIRED_IMPORT_CHECKS: tuple[tuple[str, str], ...] = (
    (SHADOW_FLAG_MODULE, SHADOW_FLAG_ACCESSOR),
    (
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader",
        "load_customer_conditional_coupon_facts",
    ),
    (
        "modules.ai.brain.truth_surface.customer_conditional_coupon_shadow_readiness",
        "evaluate_coupon_shadow_readiness",
    ),
    (
        "services.customer_conditional_coupon_shadow_fixture",
        "execute_customer_conditional_coupon_shadow_fixture_seed",
    ),
)

OBSERVATION_BLOCKER_MISSING_SLICE = "staging_deploy_missing_layer0_modules"
OBSERVATION_BLOCKER_UNPINNED_REDEPLOY = "observation_flag_toggle_without_pinned_revision"
OBSERVATION_BLOCKER_INVENTORY_NOT_VERIFIED = "observation_flag_toggle_without_module_inventory"

_PINNED_REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")

IMPORT_FAILURE_IMPORT_FAILED = "import_failed"
IMPORT_FAILURE_MISSING_SYMBOL = "missing_symbol"
IMPORT_FAILURE_PROVENANCE_UNAVAILABLE = "module_provenance_unavailable"
IMPORT_FAILURE_PROVENANCE_OUTSIDE_BACKEND = "module_provenance_outside_backend_root"


@dataclass(frozen=True)
class DeploymentArtifactInventory:
    artifact_root: str
    backend_root: str
    present_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    shadow_flag_accessor_present: bool
    import_checks_ok: bool
    import_failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return (
            not self.missing_files
            and self.shadow_flag_accessor_present
            and self.import_checks_ok
        )


@dataclass(frozen=True)
class DeploymentArtifactEvaluation:
    contract_version: str
    artifact_kind: str
    inventory: DeploymentArtifactInventory

    @property
    def ok(self) -> bool:
        return self.inventory.ok

    def to_dict(self) -> dict[str, Any]:
        inv = self.inventory
        return {
            "artifact_kind": self.artifact_kind,
            "contract_version": self.contract_version,
            "import_checks_ok": inv.import_checks_ok,
            "import_failures": list(inv.import_failures),
            "inventory_ok": inv.ok,
            "missing_files": list(inv.missing_files),
            "present_files": list(inv.present_files),
            "shadow_flag_accessor_present": inv.shadow_flag_accessor_present,
        }


@dataclass(frozen=True)
class ObservationWindowPreflight:
    ok: bool
    code: str | None
    blockers: tuple[str, ...] = field(default_factory=tuple)
    pinned_source_revision: str | None = None
    inventory: DeploymentArtifactEvaluation | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "blockers": list(self.blockers),
            "code": self.code,
            "inventory": self.inventory.to_dict() if self.inventory else None,
            "ok": self.ok,
            "pinned_source_revision": self.pinned_source_revision,
        }
        return payload


def resolve_backend_root(artifact_root: Path) -> Path:
    """Resolve ``backend/`` for repo checkout or ``/app`` container layout."""
    root = artifact_root.resolve()
    direct = root / BACKEND_REL
    if direct.is_dir():
        return direct
    if root.name == BACKEND_REL and root.is_dir():
        return root
    raise ValueError("artifact_root_invalid")


def required_relative_paths() -> frozenset[str]:
    truth_surface = frozenset(
        f"modules/ai/brain/truth_surface/{name}"
        for name in REQUIRED_TRUTH_SURFACE_FILES
    )
    return truth_surface | REQUIRED_FIXTURE_OPERATOR_FILES


def _flags_source_path(backend_root: Path) -> Path:
    return backend_root / "modules/ai/brain/truth_surface/flags.py"


def _shadow_flag_accessor_in_source(flags_path: Path) -> bool:
    if not flags_path.is_file():
        return False
    source = flags_path.read_text(encoding="utf-8")
    return f"def {SHADOW_FLAG_ACCESSOR}(" in source


def _module_is_under_backend(module: ModuleType, backend_root: Path) -> tuple[bool, str | None]:
    raw_file = getattr(module, "__file__", None)
    if not isinstance(raw_file, str) or not raw_file:
        return False, IMPORT_FAILURE_PROVENANCE_UNAVAILABLE
    try:
        resolved_file = Path(raw_file).resolve(strict=True)
        resolved_file.relative_to(backend_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False, IMPORT_FAILURE_PROVENANCE_OUTSIDE_BACKEND
    return True, None


def _affected_import_roots() -> frozenset[str]:
    return frozenset(module_name.partition(".")[0] for module_name, _ in REQUIRED_IMPORT_CHECKS)


def _is_affected_module(module_name: str, roots: frozenset[str]) -> bool:
    return any(module_name == root or module_name.startswith(f"{root}.") for root in roots)


def _restore_module_state(snapshot: Mapping[str, ModuleType]) -> None:
    """Restore the exact pre-check module mapping after isolated imports."""
    for module_name in tuple(sys.modules):
        if module_name not in snapshot:
            sys.modules.pop(module_name, None)
    for module_name, module in snapshot.items():
        if sys.modules.get(module_name) is not module:
            sys.modules[module_name] = module


def _verify_importability(backend_root: Path) -> tuple[bool, tuple[str, ...]]:
    repo_root = backend_root.parent
    isolated_paths = [str(backend_root), str(repo_root)]
    failures: list[str] = []
    saved_path = list(sys.path)
    module_snapshot = dict(sys.modules)
    affected_roots = _affected_import_roots()
    try:
        for module_name in tuple(sys.modules):
            if _is_affected_module(module_name, affected_roots):
                sys.modules.pop(module_name, None)
        importlib.invalidate_caches()
        sys.path[:] = [
            entry for entry in sys.path if entry not in isolated_paths
        ]
        sys.path[:0] = isolated_paths
        for module_name, symbol in REQUIRED_IMPORT_CHECKS:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001
                failures.append(f"{module_name}:{IMPORT_FAILURE_IMPORT_FAILED}")
                continue
            provenance_ok, provenance_failure = _module_is_under_backend(
                module,
                backend_root,
            )
            if not provenance_ok:
                failures.append(f"{module_name}:{provenance_failure}")
                continue
            if not hasattr(module, symbol):
                failures.append(
                    f"{module_name}:{IMPORT_FAILURE_MISSING_SYMBOL}:{symbol}"
                )
    finally:
        sys.path[:] = saved_path
        _restore_module_state(module_snapshot)
        importlib.invalidate_caches()
    return len(failures) == 0, tuple(failures)


def evaluate_deployment_artifact_inventory(artifact_root: Path) -> DeploymentArtifactInventory:
    backend_root = resolve_backend_root(artifact_root)
    rel_paths = sorted(required_relative_paths())
    present: list[str] = []
    missing: list[str] = []
    for rel in rel_paths:
        if (backend_root / rel).is_file():
            present.append(rel)
        else:
            missing.append(rel)

    shadow_flag_ok = _shadow_flag_accessor_in_source(_flags_source_path(backend_root))
    import_ok, import_failures = _verify_importability(backend_root)

    return DeploymentArtifactInventory(
        artifact_root=str(artifact_root.resolve()),
        backend_root=str(backend_root),
        present_files=tuple(present),
        missing_files=tuple(missing),
        shadow_flag_accessor_present=shadow_flag_ok,
        import_checks_ok=import_ok,
        import_failures=import_failures,
    )


def evaluate_shadow_deployment_artifact(artifact_root: Path) -> DeploymentArtifactEvaluation:
    return DeploymentArtifactEvaluation(
        contract_version=CONTRACT_VERSION,
        artifact_kind=ARTIFACT_KIND,
        inventory=evaluate_deployment_artifact_inventory(artifact_root),
    )


def _normalize_pinned_revision(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if not _PINNED_REVISION_RE.fullmatch(value):
        raise ValueError("pinned_source_revision_invalid")
    return value


def evaluate_observation_window_preflight(
    *,
    pinned_source_revision: str | None,
    inventory: DeploymentArtifactEvaluation | Mapping[str, Any],
    observation_flag_change_requested: bool,
) -> ObservationWindowPreflight:
    """Operator/programmatic gate before toggling the shadow observation flag.

    Toggling ``NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED``
    can trigger an unpinned redeploy on Railway. Observation must not start until
    operators record a pinned source revision **and** verify the deployed module
    inventory against this contract.
    """
    if isinstance(inventory, DeploymentArtifactEvaluation):
        evaluation = inventory
    else:
        evaluation = DeploymentArtifactEvaluation(
            contract_version=str(inventory.get("contract_version", CONTRACT_VERSION)),
            artifact_kind=str(inventory.get("artifact_kind", ARTIFACT_KIND)),
            inventory=DeploymentArtifactInventory(
                artifact_root="",
                backend_root="",
                present_files=tuple(inventory.get("present_files", ())),
                missing_files=tuple(inventory.get("missing_files", ())),
                shadow_flag_accessor_present=bool(
                    inventory.get("shadow_flag_accessor_present")
                ),
                import_checks_ok=bool(inventory.get("import_checks_ok")),
                import_failures=tuple(inventory.get("import_failures", ())),
            ),
        )

    blockers: list[str] = []
    pin: str | None
    try:
        pin = _normalize_pinned_revision(pinned_source_revision)
    except ValueError:
        blockers.append("pinned_source_revision_invalid")
        pin = None

    if observation_flag_change_requested:
        if pin is None:
            blockers.append(OBSERVATION_BLOCKER_UNPINNED_REDEPLOY)
        if not evaluation.ok:
            blockers.append(OBSERVATION_BLOCKER_INVENTORY_NOT_VERIFIED)
            if evaluation.inventory.missing_files or not evaluation.inventory.shadow_flag_accessor_present:
                blockers.append(OBSERVATION_BLOCKER_MISSING_SLICE)

    code: str | None = None
    if blockers:
        if OBSERVATION_BLOCKER_UNPINNED_REDEPLOY in blockers:
            code = OBSERVATION_BLOCKER_UNPINNED_REDEPLOY
        elif OBSERVATION_BLOCKER_MISSING_SLICE in blockers:
            code = OBSERVATION_BLOCKER_MISSING_SLICE
        elif OBSERVATION_BLOCKER_INVENTORY_NOT_VERIFIED in blockers:
            code = OBSERVATION_BLOCKER_INVENTORY_NOT_VERIFIED
        else:
            code = blockers[0]

    return ObservationWindowPreflight(
        ok=len(blockers) == 0,
        code=code,
        blockers=tuple(blockers),
        pinned_source_revision=pin,
        inventory=evaluation,
    )


def missing_module_labels(inventory: DeploymentArtifactInventory) -> list[str]:
    """Human/operator labels aligned with the staging observation abort report."""
    labels: list[str] = []
    for rel in inventory.missing_files:
        labels.append(Path(rel).name)
    if not inventory.shadow_flag_accessor_present:
        labels.append(SHADOW_FLAG_ACCESSOR)
    for failure in inventory.import_failures:
        if failure.endswith(f":missing_symbol:{SHADOW_FLAG_ACCESSOR}"):
            if SHADOW_FLAG_ACCESSOR not in labels:
                labels.append(SHADOW_FLAG_ACCESSOR)
    return sorted(set(labels))


__all__ = [
    "ARTIFACT_KIND",
    "CONTRACT_VERSION",
    "DEPLOYMENT_APP_ROOT",
    "DeploymentArtifactEvaluation",
    "DeploymentArtifactInventory",
    "OBSERVATION_BLOCKER_INVENTORY_NOT_VERIFIED",
    "OBSERVATION_BLOCKER_MISSING_SLICE",
    "OBSERVATION_BLOCKER_UNPINNED_REDEPLOY",
    "ObservationWindowPreflight",
    "REQUIRED_FIXTURE_OPERATOR_FILES",
    "REQUIRED_IMPORT_CHECKS",
    "REQUIRED_TRUTH_SURFACE_FILES",
    "SHADOW_FLAG_ACCESSOR",
    "SHADOW_FLAG_ENV",
    "evaluate_deployment_artifact_inventory",
    "evaluate_observation_window_preflight",
    "evaluate_shadow_deployment_artifact",
    "missing_module_labels",
    "required_relative_paths",
    "resolve_backend_root",
]
