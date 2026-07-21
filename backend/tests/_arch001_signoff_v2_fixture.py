"""Test helper — build/install valid ARCH-001 preprod signoff v2 artifacts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.operators import product_availability_preprod_synthetic_signoff_v2 as signoff_v2
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (
    SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV,
)

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_HMAC_KEY = "test-arch001-preprod-signoff-v2-hmac"


def write_valid_v2_bundle(tmp_path: Path, *, hmac_key: str = _DEFAULT_HMAC_KEY) -> Path:
    result = signoff_v2.execute_full_probe(app_root=_REPO, hmac_key=hmac_key)
    if result.get("ok") is not True:
        raise AssertionError(result)
    artifact = tmp_path / "arch001-preprod-signoff-v2.json"
    artifact.write_text(json.dumps(result["bundle"], indent=2), encoding="utf-8")
    return artifact


def install_valid_v2_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hmac_key: str = _DEFAULT_HMAC_KEY,
) -> Path:
    artifact = write_valid_v2_bundle(tmp_path, hmac_key=hmac_key)
    monkeypatch.setenv(SIGNOFF_ARTIFACT_ENV, str(artifact))
    monkeypatch.setenv(SIGNOFF_HMAC_KEY_ENV, hmac_key)
    return artifact


def v2_env_overlay(tmp_path: Path, *, hmac_key: str = _DEFAULT_HMAC_KEY) -> dict[str, str]:
    artifact = write_valid_v2_bundle(tmp_path, hmac_key=hmac_key)
    return {
        SIGNOFF_ARTIFACT_ENV: str(artifact),
        SIGNOFF_HMAC_KEY_ENV: hmac_key,
    }
