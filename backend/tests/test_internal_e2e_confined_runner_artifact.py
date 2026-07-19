"""Regression tests for confined-runner orchestrator artifact parity."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from ops.internal_e2e_runner.lib import artifact_contract as contract  # noqa: E402


def test_repo_root_passes_confined_orchestrator_artifact_contract() -> None:
    result = contract.evaluate_confined_runner_artifact(Path(_REPO))
    assert result.contract_version == contract.CONTRACT_VERSION
    assert result.ok is True
    assert result.inventory.missing_files == ()
    assert result.inventory.dockerfile_copy_line_present is True
    assert result.inventory.import_checks_ok is True


def test_dockerfile_declares_ai_orchestrator_copy() -> None:
    dockerfile = (
        Path(_REPO) / "ops/internal_e2e_runner/Dockerfile"
    ).read_text(encoding="utf-8")
    assert contract.DOCKERFILE_COPY_LINE in dockerfile


def test_dockerignore_does_not_exclude_services_tree() -> None:
    dockerignore = (Path(_REPO) / ".dockerignore").read_text(encoding="utf-8")
    for line in dockerignore.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert stripped not in {"services/", "services", "services/**"}


def test_missing_orchestrator_tree_fails_contract(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "modules").mkdir(parents=True)
    dockerfile = tmp_path / contract.DOCKERFILE_REL
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        f"{contract.DOCKERFILE_COPY_LINE}\n",
        encoding="utf-8",
    )

    result = contract.evaluate_confined_runner_artifact(tmp_path)

    assert result.ok is False
    assert result.inventory.missing_files
    assert "services/ai-orchestrator/memory/loader.py" in result.inventory.missing_files


def test_importability_evicts_stale_module_and_restores_process_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    backend_root = repo_root / "backend"
    orch_root = repo_root / contract.ORCHESTRATOR_REL
    backend_root.mkdir(parents=True)
    orch_root.mkdir(parents=True)
    for rel in contract.REQUIRED_ORCHESTRATOR_FILES:
        target = orch_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n", encoding="utf-8")
    dockerfile = repo_root / contract.DOCKERFILE_REL
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(f"{contract.DOCKERFILE_COPY_LINE}\n", encoding="utf-8")

    stale = ModuleType("memory")
    stale.loader = object()
    stale.__file__ = str(tmp_path / "other_checkout" / "memory" / "__init__.py")
    monkeypatch.setitem(sys.modules, "memory", stale)
    before_modules = dict(sys.modules)

    ok, failures = contract.verify_legacy_orchestrator_imports(
        repo_root=repo_root,
        backend_root=backend_root,
        orchestrator_root=orch_root,
    )

    assert ok is False
    assert failures
    assert sys.modules.get("memory") is stale
    assert dict(sys.modules) == before_modules


def test_importability_rejects_module_resolved_outside_orchestrator_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    backend_root = repo_root / "backend"
    orch_root = repo_root / contract.ORCHESTRATOR_REL
    backend_root.mkdir(parents=True)
    orch_root.mkdir(parents=True)
    external = tmp_path / "external_orchestrator"
    external.mkdir()
    (external / "memory").mkdir(parents=True)
    (external / "memory" / "__init__.py").write_text("", encoding="utf-8")
    (external / "memory" / "loader.py").write_text(
        "def load_customer_memory(*_a, **_k):\n    return {}\n",
        encoding="utf-8",
    )
    for rel in contract.REQUIRED_ORCHESTRATOR_FILES:
        if rel == "memory/loader.py":
            continue
        target = orch_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n", encoding="utf-8")
    dockerfile = repo_root / contract.DOCKERFILE_REL
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(f"{contract.DOCKERFILE_COPY_LINE}\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(external))
    for module_name in ("memory", "memory.loader"):
        sys.modules.pop(module_name, None)
    before_modules = dict(sys.modules)

    ok, failures = contract.verify_legacy_orchestrator_imports(
        repo_root=repo_root,
        backend_root=backend_root,
        orchestrator_root=orch_root,
    )

    assert ok is False
    assert any(
        failure.startswith("memory.loader:")
        and contract.IMPORT_FAILURE_PROVENANCE_OUTSIDE_ORCHESTRATOR in failure
        for failure in failures
    )
    assert "memory.loader" not in sys.modules
    assert dict(sys.modules) == before_modules


def test_live_orchestrator_loader_path_is_under_services_tree() -> None:
    repo_root = Path(_REPO)
    _, _, orch_root = contract.resolve_artifact_roots(repo_root)
    loader_path = (orch_root / "memory" / "loader.py").resolve()
    assert loader_path.is_file()
    assert loader_path.is_relative_to((repo_root / "services" / "ai-orchestrator").resolve())
