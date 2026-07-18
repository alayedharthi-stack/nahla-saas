"""Tests for deployment revision attestation (verifier vs target runtime separation)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators.customer_conditional_coupon_consumer_verify_contract import (  # noqa: E402
    PINNED_TARGET_RUNTIME_REVISION,
    PINNED_TARGET_RUNTIME_REVISION_SHORT,
)
from scripts.operators.deployment_revision_attestation_contract import (  # noqa: E402
    CODE_RUNTIME_REVISION_MISMATCH,
    CODE_RUNTIME_REVISION_UNKNOWN,
    CODE_TARGET_APP_ROOT_REQUIRED,
    ExecutionMode,
    evaluate_runtime_revision_attestation,
    read_build_attested_revision,
    revisions_equivalent,
)


def test_revisions_equivalent_accepts_prefix_match() -> None:
    assert revisions_equivalent(
        PINNED_TARGET_RUNTIME_REVISION,
        PINNED_TARGET_RUNTIME_REVISION_SHORT,
    )


def test_external_runner_requires_target_app_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("NAHLA_CONSUMER_VERIFY_TARGET_APP_ROOT", raising=False)
    result = evaluate_runtime_revision_attestation(
        pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        target_app_root=None,
    )
    assert result.ok is False
    assert result.execution_mode == ExecutionMode.EXTERNAL_RUNNER
    assert result.code == CODE_TARGET_APP_ROOT_REQUIRED


def test_external_runner_rejects_checkout_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    (checkout / "marker.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )

    result = evaluate_runtime_revision_attestation(
        pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        target_app_root=checkout,
    )
    assert result.ok is False
    assert result.code == CODE_RUNTIME_REVISION_MISMATCH
    assert result.attested_revision is not None


def test_external_runner_accepts_matching_checkout_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    checkout = tmp_path / "target-checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()

    monkeypatch.setattr(
        "scripts.operators.deployment_revision_attestation_contract.read_checkout_revision",
        lambda root: PINNED_TARGET_RUNTIME_REVISION if root == checkout else None,
    )
    result = evaluate_runtime_revision_attestation(
        pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        target_app_root=checkout,
    )
    assert result.ok is True
    assert result.execution_mode == ExecutionMode.EXTERNAL_RUNNER
    assert revisions_equivalent(
        result.attested_revision,
        PINNED_TARGET_RUNTIME_REVISION,
    )


def test_in_container_attestation_rejects_newer_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "722502c0bf5fcccc89a589d21c3f5bb6cf2ab38d")
    result = evaluate_runtime_revision_attestation(
        pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        target_app_root=Path("/app"),
    )
    assert result.ok is False
    assert result.execution_mode == ExecutionMode.IN_CONTAINER
    assert result.code == CODE_RUNTIME_REVISION_MISMATCH


def test_in_container_attestation_accepts_pinned_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", PINNED_TARGET_RUNTIME_REVISION)
    result = evaluate_runtime_revision_attestation(
        pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        target_app_root=None,
    )
    if Path("/app/backend").is_dir():
        assert result.ok is True
        assert result.execution_mode == ExecutionMode.IN_CONTAINER
    else:
        assert result.ok is False
        assert result.code == CODE_TARGET_APP_ROOT_REQUIRED


def test_read_build_attested_revision_ignores_spoofable_git_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setenv("GIT_SHA", PINNED_TARGET_RUNTIME_REVISION)
    assert read_build_attested_revision() is None


def test_unknown_checkout_revision_fails_closed(tmp_path: Path) -> None:
    result = evaluate_runtime_revision_attestation(
        pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        target_app_root=tmp_path,
    )
    assert result.ok is False
    assert result.code == CODE_RUNTIME_REVISION_UNKNOWN
