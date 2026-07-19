"""Filesystem + import contract for the confined internal E2E runner image.

The confined runner Dockerfile bakes a minimal ``/app`` layout. The canonical
``generate_orchestrate_response`` adapter still imports transitional legacy
modules from ``services/ai-orchestrator`` via ``sys.path`` injection. This
contract proves those modules exist in the built layout and resolve beneath
``/app`` — stale host ``sys.modules`` must not mask absence.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Mapping

CONTRACT_VERSION = "confined_internal_e2e_artifact_v1"
ARTIFACT_KIND = "nahla_internal_e2e_confined_runner"
DEPLOYMENT_APP_ROOT = "/app"
ORCHESTRATOR_REL = Path("services") / "ai-orchestrator"
DOCKERFILE_REL = Path("ops") / "internal_e2e_runner" / "Dockerfile"
DOCKERFILE_COPY_LINE = "COPY services/ai-orchestrator ./services/ai-orchestrator"

REQUIRED_ORCHESTRATOR_FILES = frozenset(
    {
        "memory/loader.py",
        "memory/updater.py",
        "fact_guard/data_fetcher.py",
        "fact_guard/checker.py",
        "policy/guard.py",
        "commerce/permission_guard.py",
        "commerce/permissions.py",
        "execution/action_execution_guard.py",
        "engine/claude_client.py",
    }
)

# Legacy top-level imports used by adapter.generate_orchestrate_response.
REQUIRED_LEGACY_IMPORT_CHECKS: tuple[tuple[str, str], ...] = (
    ("memory.loader", "load_customer_memory"),
    ("memory.updater", "update_customer_memory"),
    ("fact_guard.data_fetcher", "fetch_grounding_data"),
    ("fact_guard.data_fetcher", "GroundingData"),
    ("fact_guard.checker", "vet_reply"),
    ("fact_guard.checker", "extract_coupon_codes_from_text"),
    ("policy.guard", "validate_actions"),
    ("commerce.permission_guard", "gate"),
    ("commerce.permission_guard", "load_permissions"),
    ("commerce.permissions", "CommercePermissionSet"),
    ("execution.action_execution_guard", "decide"),
    ("engine.claude_client", "_TOOLS"),
)

IMPORT_FAILURE_IMPORT_FAILED = "import_failed"
IMPORT_FAILURE_MISSING_SYMBOL = "missing_symbol"
IMPORT_FAILURE_PROVENANCE_UNAVAILABLE = "module_provenance_unavailable"
IMPORT_FAILURE_PROVENANCE_OUTSIDE_ORCHESTRATOR = (
    "module_provenance_outside_orchestrator_root"
)

_DOCKERFILE_COPY_RE = re.compile(
    r"^COPY\s+services/ai-orchestrator\s+\./services/ai-orchestrator\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ConfinedArtifactInventory:
    artifact_root: str
    orchestrator_root: str
    backend_root: str
    present_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    dockerfile_copy_line_present: bool
    import_checks_ok: bool
    import_failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return (
            not self.missing_files
            and self.dockerfile_copy_line_present
            and self.import_checks_ok
        )


@dataclass(frozen=True)
class ConfinedArtifactEvaluation:
    contract_version: str
    artifact_kind: str
    inventory: ConfinedArtifactInventory

    @property
    def ok(self) -> bool:
        return self.inventory.ok

    def to_dict(self) -> dict[str, object]:
        inv = self.inventory
        return {
            "artifact_kind": self.artifact_kind,
            "contract_version": self.contract_version,
            "dockerfile_copy_line_present": inv.dockerfile_copy_line_present,
            "import_checks_ok": inv.import_checks_ok,
            "import_failures": list(inv.import_failures),
            "inventory_ok": inv.ok,
            "missing_files": list(inv.missing_files),
            "orchestrator_root": inv.orchestrator_root,
            "present_files": list(inv.present_files),
        }


def resolve_artifact_roots(artifact_root: Path) -> tuple[Path, Path, Path]:
    root = artifact_root.resolve()
    backend_root = root / "backend"
    orchestrator_root = root / ORCHESTRATOR_REL
    if not backend_root.is_dir():
        raise ValueError("artifact_backend_root_missing")
    return root, backend_root, orchestrator_root


def dockerfile_declares_orchestrator_copy(artifact_root: Path) -> bool:
    dockerfile = artifact_root / DOCKERFILE_REL
    if not dockerfile.is_file():
        return False
    text = dockerfile.read_text(encoding="utf-8")
    return DOCKERFILE_COPY_LINE in text or bool(_DOCKERFILE_COPY_RE.search(text))


def _module_is_under_root(module: ModuleType, expected_root: Path) -> tuple[bool, str | None]:
    raw_file = getattr(module, "__file__", None)
    if not isinstance(raw_file, str) or not raw_file:
        return False, IMPORT_FAILURE_PROVENANCE_UNAVAILABLE
    try:
        resolved_file = Path(raw_file).resolve(strict=True)
        resolved_file.relative_to(expected_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False, IMPORT_FAILURE_PROVENANCE_OUTSIDE_ORCHESTRATOR
    return True, None


def _affected_import_roots() -> frozenset[str]:
    return frozenset(
        module_name.partition(".")[0] for module_name, _ in REQUIRED_LEGACY_IMPORT_CHECKS
    )


def _is_affected_module(module_name: str, roots: frozenset[str]) -> bool:
    return any(
        module_name == root or module_name.startswith(f"{root}.") for root in roots
    )


def _restore_module_state(snapshot: Mapping[str, ModuleType]) -> None:
    import sys

    for module_name in tuple(sys.modules):
        if module_name not in snapshot:
            sys.modules.pop(module_name, None)
    for module_name, module in snapshot.items():
        if sys.modules.get(module_name) is not module:
            sys.modules[module_name] = module


def verify_legacy_orchestrator_imports(
    *,
    repo_root: Path,
    backend_root: Path,
    orchestrator_root: Path,
) -> tuple[bool, tuple[str, ...]]:
    import sys

    isolated_paths = [str(backend_root), str(repo_root), str(orchestrator_root)]
    failures: list[str] = []
    saved_path = list(sys.path)
    module_snapshot = dict(sys.modules)
    affected_roots = _affected_import_roots()
    try:
        for module_name in tuple(sys.modules):
            if _is_affected_module(module_name, affected_roots):
                sys.modules.pop(module_name, None)
        importlib.invalidate_caches()
        sys.path[:] = [entry for entry in sys.path if entry not in isolated_paths]
        sys.path[:0] = isolated_paths
        for module_name, symbol in REQUIRED_LEGACY_IMPORT_CHECKS:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001
                failures.append(f"{module_name}:{IMPORT_FAILURE_IMPORT_FAILED}")
                continue
            provenance_ok, provenance_failure = _module_is_under_root(
                module,
                orchestrator_root,
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


def evaluate_confined_artifact_inventory(artifact_root: Path) -> ConfinedArtifactInventory:
    repo_root, backend_root, orchestrator_root = resolve_artifact_roots(artifact_root)
    present: list[str] = []
    missing: list[str] = []
    for rel in sorted(REQUIRED_ORCHESTRATOR_FILES):
        target = orchestrator_root / rel
        rel_str = str(Path(ORCHESTRATOR_REL) / rel).replace("\\", "/")
        if target.is_file():
            present.append(rel_str)
        else:
            missing.append(rel_str)

    import_ok, import_failures = (False, ("orchestrator_tree_missing",))
    if not missing:
        import_ok, import_failures = verify_legacy_orchestrator_imports(
            repo_root=repo_root,
            backend_root=backend_root,
            orchestrator_root=orchestrator_root,
        )

    return ConfinedArtifactInventory(
        artifact_root=str(repo_root),
        orchestrator_root=str(orchestrator_root),
        backend_root=str(backend_root),
        present_files=tuple(present),
        missing_files=tuple(missing),
        dockerfile_copy_line_present=dockerfile_declares_orchestrator_copy(repo_root),
        import_checks_ok=import_ok,
        import_failures=import_failures,
    )


def evaluate_confined_runner_artifact(artifact_root: Path) -> ConfinedArtifactEvaluation:
    return ConfinedArtifactEvaluation(
        contract_version=CONTRACT_VERSION,
        artifact_kind=ARTIFACT_KIND,
        inventory=evaluate_confined_artifact_inventory(artifact_root),
    )
