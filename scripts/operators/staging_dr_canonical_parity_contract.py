"""Closed contract for staging DR canonical parity verification.

Source eligibility is explicit and versioned: each approved staging pin is a
profile (revision + public table count + exact fingerprint). The verifier never
upgrades eligibility implicitly when staging advances — operators add a new
evidence-backed profile in a dedicated contract bump.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence, TypedDict

from scripts.operators.schema_fingerprint import SCHEMA_FINGERPRINT_VERSION

CONTRACT_VERSION = "staging-dr-canonical-parity-v1"
CONTRACT_JSON_FILENAME = "canonical_parity.json"

REQUIRED_CONTRACT_KEYS = frozenset(
    {
        "contract_version",
        "schema_fingerprint_version",
        "source_eligibility_profiles",
    }
)

REQUIRED_PROFILE_KEYS = frozenset(
    {
        "profile_id",
        "alembic_revision",
        "public_table_count",
        "schema_fingerprint_sha256",
    }
)


class SourceEligibilityProfile(TypedDict, total=False):
    profile_id: str
    alembic_revision: str
    public_table_count: int
    schema_fingerprint_sha256: str
    operator_note: str


SOURCE_ELIGIBILITY_PROFILES: tuple[SourceEligibilityProfile, ...] = (
    {
        "profile_id": "staging_pin_0024",
        "alembic_revision": "0024",
        "public_table_count": 96,
        "schema_fingerprint_sha256": (
            "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
        ),
        "operator_note": "Prior Phase A source attestation",
    },
    {
        "profile_id": "staging_pin_0030",
        "alembic_revision": "0030",
        "public_table_count": 96,
        "schema_fingerprint_sha256": (
            "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
        ),
        "operator_note": "Live staging source attestation after successful 0030 restore drill",
    },
    {
        "profile_id": "staging_pin_0032",
        "alembic_revision": "0032",
        "public_table_count": 96,
        "schema_fingerprint_sha256": (
            "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
        ),
        "operator_note": "Live staging source attestation after guarded Stage A (0030→0032) post-validation",
    },
    {
        "profile_id": "staging_pin_0083",
        "alembic_revision": "0083",
        "public_table_count": 96,
        "schema_fingerprint_sha256": (
            "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
        ),
        "operator_note": "Live staging source attestation after guarded Stage B (0032→0083) post-validation",
    },
)


def export_contract() -> dict[str, Any]:
    """Return the closed JSON contract baked into the DR executor image."""
    return {
        "contract_version": CONTRACT_VERSION,
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
        "source_eligibility_profiles": [
            dict(profile) for profile in SOURCE_ELIGIBILITY_PROFILES
        ],
    }


def _normalize_profile(raw: Mapping[str, Any]) -> SourceEligibilityProfile:
    if not REQUIRED_PROFILE_KEYS <= set(raw):
        raise ValueError("profile_shape_invalid")
    profile_id = raw["profile_id"]
    revision = raw["alembic_revision"]
    table_count = raw["public_table_count"]
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("profile_id_invalid")
    if not isinstance(revision, str) or not revision:
        raise ValueError("profile_revision_invalid")
    if not isinstance(table_count, int) or isinstance(table_count, bool) or table_count <= 0:
        raise ValueError("profile_table_count_invalid")
    normalized: SourceEligibilityProfile = {
        "profile_id": profile_id,
        "alembic_revision": revision,
        "public_table_count": table_count,
    }
    fingerprint = raw["schema_fingerprint_sha256"]
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError("profile_fingerprint_invalid")
    normalized["schema_fingerprint_sha256"] = fingerprint
    note = raw.get("operator_note")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("profile_note_invalid")
        normalized["operator_note"] = note
    return normalized


def load_contract(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a contract document and return normalized profiles."""
    if set(raw) != REQUIRED_CONTRACT_KEYS:
        raise ValueError("contract_shape_invalid")
    version = raw["contract_version"]
    fingerprint_version = raw["schema_fingerprint_version"]
    profiles = raw["source_eligibility_profiles"]
    if version != CONTRACT_VERSION:
        raise ValueError("contract_version_mismatch")
    if fingerprint_version != SCHEMA_FINGERPRINT_VERSION:
        raise ValueError("schema_fingerprint_version_mismatch")
    if not isinstance(profiles, Sequence) or isinstance(profiles, (str, bytes)):
        raise ValueError("profiles_invalid")
    normalized_profiles: list[SourceEligibilityProfile] = []
    seen_ids: set[str] = set()
    for entry in profiles:
        if not isinstance(entry, Mapping):
            raise ValueError("profile_entry_invalid")
        profile = _normalize_profile(entry)
        if profile["profile_id"] in seen_ids:
            raise ValueError("profile_id_duplicate")
        seen_ids.add(profile["profile_id"])
        normalized_profiles.append(profile)
    if not normalized_profiles:
        raise ValueError("profiles_empty")
    return {
        "contract_version": version,
        "schema_fingerprint_version": fingerprint_version,
        "source_eligibility_profiles": normalized_profiles,
    }


def match_source_profile(
    *,
    alembic_revision: str,
    public_table_count: int,
    schema_fingerprint_sha256: str,
    contract: Mapping[str, Any],
) -> str | None:
    """Return the matching profile_id or None when source is ineligible."""
    for profile in contract["source_eligibility_profiles"]:
        if profile["alembic_revision"] != alembic_revision:
            continue
        if profile["public_table_count"] != public_table_count:
            continue
        if profile["schema_fingerprint_sha256"] != schema_fingerprint_sha256:
            continue
        return profile["profile_id"]
    return None


def evaluate_parity(
    *,
    source_revision: str,
    restore_revision: str,
    source_table_count: int,
    restore_table_count: int,
    source_fingerprint_sha256: str,
    restore_fingerprint_sha256: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate canonical parity without database access (deterministic harness)."""
    revision_parity = source_revision == restore_revision
    table_count_parity = source_table_count == restore_table_count
    fingerprint_parity = source_fingerprint_sha256 == restore_fingerprint_sha256
    canonical_manifest_parity = revision_parity and table_count_parity and fingerprint_parity
    matched_profile = (
        match_source_profile(
            alembic_revision=source_revision,
            public_table_count=source_table_count,
            schema_fingerprint_sha256=source_fingerprint_sha256,
            contract=contract,
        )
        if canonical_manifest_parity
        else None
    )
    return {
        "contract_version": contract["contract_version"],
        "schema_fingerprint_version": contract["schema_fingerprint_version"],
        "revision_parity": revision_parity,
        "public_table_count_parity": table_count_parity,
        "canonical_full_sha_parity": fingerprint_parity,
        "canonical_manifest_parity": canonical_manifest_parity,
        "source_contract_eligible": matched_profile is not None,
        "matched_source_profile_id": matched_profile,
    }
