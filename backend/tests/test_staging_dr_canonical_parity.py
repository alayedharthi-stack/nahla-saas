"""Unit tests for staging DR canonical parity contract and evaluator."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators import staging_dr_canonical_parity as parity  # noqa: E402
from scripts.operators import staging_dr_canonical_parity_contract as contract  # noqa: E402


PIN_0024_FP = "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
PIN_0030_FP = "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
PIN_0032_FP = "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"


def _loaded_contract() -> dict:
    return contract.load_contract(contract.export_contract())


def test_exported_contract_matches_baked_json() -> None:
    exported = contract.export_contract()
    baked = json.loads(
        Path("ops/staging_dr_executor/contracts/canonical_parity.json").read_text(encoding="utf-8")
    )
    assert exported == baked
    assert contract.load_contract(baked)["contract_version"] == contract.CONTRACT_VERSION


def test_all_concrete_profiles_require_exact_fingerprint_pins() -> None:
    assert all(
        len(profile["schema_fingerprint_sha256"]) == 64
        for profile in contract.SOURCE_ELIGIBILITY_PROFILES
    )
    malformed = contract.export_contract()
    del malformed["source_eligibility_profiles"][0]["schema_fingerprint_sha256"]
    with pytest.raises(ValueError, match="profile_shape_invalid"):
        contract.load_contract(malformed)


def test_staging_pin_0032_requires_exact_attested_values() -> None:
    loaded = _loaded_contract()
    assert (
        contract.match_source_profile(
            alembic_revision="0032",
            public_table_count=96,
            schema_fingerprint_sha256=PIN_0032_FP,
            contract=loaded,
        )
        == "staging_pin_0032"
    )
    assert (
        contract.match_source_profile(
            alembic_revision="0032",
            public_table_count=99,
            schema_fingerprint_sha256=PIN_0032_FP,
            contract=loaded,
        )
        is None
    )
    assert (
        contract.match_source_profile(
            alembic_revision="0032",
            public_table_count=96,
            schema_fingerprint_sha256="b" * 64,
            contract=loaded,
        )
        is None
    )


def test_staging_pin_0030_requires_exact_attested_values() -> None:
    loaded = _loaded_contract()
    assert (
        contract.match_source_profile(
            alembic_revision="0030",
            public_table_count=96,
            schema_fingerprint_sha256=PIN_0030_FP,
            contract=loaded,
        )
        == "staging_pin_0030"
    )
    assert (
        contract.match_source_profile(
            alembic_revision="0030",
            public_table_count=99,
            schema_fingerprint_sha256=PIN_0030_FP,
            contract=loaded,
        )
        is None
    )
    assert (
        contract.match_source_profile(
            alembic_revision="0030",
            public_table_count=96,
            schema_fingerprint_sha256="a" * 64,
            contract=loaded,
        )
        is None
    )


def test_pinned_fingerprint_profile_rejects_drift() -> None:
    loaded = _loaded_contract()
    assert (
        contract.match_source_profile(
            alembic_revision="0024",
            public_table_count=96,
            schema_fingerprint_sha256=PIN_0024_FP,
            contract=loaded,
        )
        == "staging_pin_0024"
    )
    assert (
        contract.match_source_profile(
            alembic_revision="0024",
            public_table_count=96,
            schema_fingerprint_sha256="f" * 64,
            contract=loaded,
        )
        is None
    )


def test_evaluate_parity_fail_closed_when_manifest_mismatch() -> None:
    loaded = _loaded_contract()
    result = contract.evaluate_parity(
        source_revision="0030",
        restore_revision="0024",
        source_table_count=96,
        restore_table_count=96,
        source_fingerprint_sha256=PIN_0030_FP,
        restore_fingerprint_sha256=PIN_0030_FP,
        contract=loaded,
    )
    assert result["canonical_manifest_parity"] is False
    assert result["source_contract_eligible"] is False


def test_evaluate_parity_passes_for_matched_0032_profile() -> None:
    loaded = _loaded_contract()
    result = contract.evaluate_parity(
        source_revision="0032",
        restore_revision="0032",
        source_table_count=96,
        restore_table_count=96,
        source_fingerprint_sha256=PIN_0032_FP,
        restore_fingerprint_sha256=PIN_0032_FP,
        contract=loaded,
    )
    assert result["canonical_manifest_parity"] is True
    assert result["source_contract_eligible"] is True
    assert result["matched_source_profile_id"] == "staging_pin_0032"


def test_evaluate_parity_passes_for_matched_0030_profile() -> None:
    loaded = _loaded_contract()
    result = contract.evaluate_parity(
        source_revision="0030",
        restore_revision="0030",
        source_table_count=96,
        restore_table_count=96,
        source_fingerprint_sha256=PIN_0030_FP,
        restore_fingerprint_sha256=PIN_0030_FP,
        contract=loaded,
    )
    assert result["canonical_manifest_parity"] is True
    assert result["source_contract_eligible"] is True
    assert result["matched_source_profile_id"] == "staging_pin_0030"


def test_evaluate_observation_rejects_equal_but_unpinned_0030_schema() -> None:
    observation = {
        "source_revision": "0030",
        "restore_revision": "0030",
        "source_table_count": 96,
        "restore_table_count": 96,
        "source_fingerprint_sha256": "a" * 64,
        "restore_fingerprint_sha256": "a" * 64,
    }
    with pytest.raises(parity.ParityFailure, match="source_contract_ineligible"):
        parity.evaluate_observation(observation)


def test_verify_script_uses_versioned_contract_not_hardcoded_revision() -> None:
    script = Path("ops/staging_dr_executor/scripts/verify_canonical_parity.sh").read_text(encoding="utf-8")
    assert "0016" not in script
    assert "jq" in script
    assert "source_eligibility_profiles" in script
    assert "matched_source_profile_id" in script


def test_executor_image_includes_contract_and_jq() -> None:
    dockerfile = Path("ops/staging_dr_executor/Dockerfile").read_text(encoding="utf-8")
    assert "jq" in dockerfile
    assert "COPY contracts/" in dockerfile
    assert "verify_canonical_parity.sh" in Path(
        "ops/staging_dr_executor/scripts/verify_canonical_parity.sh"
    ).name
