"""Regression tests for conditional-coupon shadow deployment artifact contract."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.customer_conditional_coupon_shadow_deployment_artifact_contract import (  # noqa: E402
    CONTRACT_VERSION,
    DeploymentArtifactEvaluation,
    DeploymentArtifactInventory,
    OBSERVATION_BLOCKER_INVENTORY_NOT_VERIFIED,
    OBSERVATION_BLOCKER_MISSING_SLICE,
    OBSERVATION_BLOCKER_UNPINNED_REDEPLOY,
    SHADOW_FLAG_ACCESSOR,
    evaluate_observation_window_preflight,
    evaluate_shadow_deployment_artifact,
    missing_module_labels,
    required_relative_paths,
)


def test_repo_root_passes_shadow_deployment_artifact_contract() -> None:
    result = evaluate_shadow_deployment_artifact(Path(_REPO))
    assert result.contract_version == CONTRACT_VERSION
    assert result.ok is True
    assert result.inventory.missing_files == ()
    assert result.inventory.shadow_flag_accessor_present is True
    assert result.inventory.import_checks_ok is True


def test_contract_requires_layer0_modules_fixture_operator_and_flag_accessor() -> None:
    rels = required_relative_paths()
    assert "modules/ai/brain/truth_surface/customer_conditional_coupon_loader.py" in rels
    assert (
        "modules/ai/brain/truth_surface/customer_conditional_coupon_shadow_readiness.py"
        in rels
    )
    assert "services/customer_conditional_coupon_shadow_fixture.py" in rels
    assert "scripts/seed_customer_conditional_coupon_shadow_fixture.py" in rels


def test_missing_loader_fails_contract(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    truth = backend / "modules/ai/brain/truth_surface"
    truth.mkdir(parents=True)
    (truth / "customer_conditional_coupon_shadow_readiness.py").write_text(
        "def evaluate_coupon_shadow_readiness():\n    pass\n",
        encoding="utf-8",
    )
    (truth / "flags.py").write_text(
        f"def {SHADOW_FLAG_ACCESSOR}():\n    return False\n",
        encoding="utf-8",
    )
    for rel in required_relative_paths():
        if rel.startswith("modules/"):
            continue
        target = backend / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n", encoding="utf-8")

    result = evaluate_shadow_deployment_artifact(tmp_path)
    assert result.ok is False
    assert "customer_conditional_coupon_loader.py" in missing_module_labels(result.inventory)


def test_missing_shadow_flag_accessor_fails_contract(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    truth = backend / "modules/ai/brain/truth_surface"
    truth.mkdir(parents=True)
    (truth / "customer_conditional_coupon_loader.py").write_text("# stub\n", encoding="utf-8")
    (truth / "customer_conditional_coupon_shadow_readiness.py").write_text(
        "# stub\n",
        encoding="utf-8",
    )
    (truth / "flags.py").write_text("def other_flag():\n    return False\n", encoding="utf-8")
    for rel in required_relative_paths():
        if rel.startswith("modules/"):
            continue
        target = backend / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n", encoding="utf-8")

    result = evaluate_shadow_deployment_artifact(tmp_path)
    assert result.ok is False
    assert result.inventory.shadow_flag_accessor_present is False
    assert SHADOW_FLAG_ACCESSOR in missing_module_labels(result.inventory)


def test_observation_flag_toggle_without_pinned_revision_is_forbidden() -> None:
    inventory = evaluate_shadow_deployment_artifact(Path(_REPO))
    preflight = evaluate_observation_window_preflight(
        pinned_source_revision=None,
        inventory=inventory,
        observation_flag_change_requested=True,
    )
    assert preflight.ok is False
    assert preflight.code == OBSERVATION_BLOCKER_UNPINNED_REDEPLOY
    assert OBSERVATION_BLOCKER_UNPINNED_REDEPLOY in preflight.blockers


def test_observation_flag_toggle_with_pin_but_missing_slice_is_forbidden(
    tmp_path: Path,
) -> None:
    backend = tmp_path / "backend"
    truth = backend / "modules/ai/brain/truth_surface"
    truth.mkdir(parents=True)
    (truth / "flags.py").write_text("def other_flag():\n    pass\n", encoding="utf-8")
    inventory = evaluate_shadow_deployment_artifact(tmp_path)

    preflight = evaluate_observation_window_preflight(
        pinned_source_revision="3d5c90d8",
        inventory=inventory,
        observation_flag_change_requested=True,
    )
    assert preflight.ok is False
    assert OBSERVATION_BLOCKER_INVENTORY_NOT_VERIFIED in preflight.blockers
    assert OBSERVATION_BLOCKER_MISSING_SLICE in preflight.blockers
    assert preflight.code == OBSERVATION_BLOCKER_MISSING_SLICE


def test_observation_preflight_passes_with_pin_and_verified_inventory() -> None:
    inventory = evaluate_shadow_deployment_artifact(Path(_REPO))
    preflight = evaluate_observation_window_preflight(
        pinned_source_revision="3d5c90d8",
        inventory=inventory,
        observation_flag_change_requested=True,
    )
    assert preflight.ok is True
    assert preflight.code is None
    assert preflight.blockers == ()


def test_observation_preflight_skips_pin_when_flag_not_changing() -> None:
    inventory = evaluate_shadow_deployment_artifact(Path(_REPO))
    preflight = evaluate_observation_window_preflight(
        pinned_source_revision=None,
        inventory=inventory,
        observation_flag_change_requested=False,
    )
    assert preflight.ok is True


def test_regression_staging_b4f11547_missing_slice_shape() -> None:
    """Mirrors aborted_staging_missing_layer0_slice discovery (deploy b4f11547)."""
    broken_inv = DeploymentArtifactInventory(
        artifact_root="/app",
        backend_root="/app/backend",
        present_files=(),
        missing_files=(
            "modules/ai/brain/truth_surface/customer_conditional_coupon_loader.py",
            "modules/ai/brain/truth_surface/customer_conditional_coupon_shadow_readiness.py",
        ),
        shadow_flag_accessor_present=False,
        import_checks_ok=False,
        import_failures=(
            "modules.ai.brain.truth_surface.flags:import_error:ModuleNotFoundError",
        ),
    )
    broken = DeploymentArtifactEvaluation(
        contract_version=CONTRACT_VERSION,
        artifact_kind="nahla_saas_conditional_coupon_shadow_slice",
        inventory=broken_inv,
    )

    labels = missing_module_labels(broken_inv)
    assert "customer_conditional_coupon_loader.py" in labels
    assert "customer_conditional_coupon_shadow_readiness.py" in labels
    assert SHADOW_FLAG_ACCESSOR in labels

    preflight = evaluate_observation_window_preflight(
        pinned_source_revision="b4f11547",
        inventory=broken,
        observation_flag_change_requested=True,
    )
    assert preflight.ok is False
    assert preflight.code == OBSERVATION_BLOCKER_MISSING_SLICE
