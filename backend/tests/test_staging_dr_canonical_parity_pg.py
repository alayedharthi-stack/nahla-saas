"""PostgreSQL proof that staging_pin_0030 contract matches migrated schema."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators.schema_fingerprint import compute_public_schema_fingerprint  # noqa: E402
from scripts.operators.staging_dr_canonical_parity_contract import (  # noqa: E402
    SOURCE_ELIGIBILITY_PROFILES,
    load_contract,
    match_source_profile,
    export_contract,
)
from tests.legacy_migration_drift_0025_0030_postgres_fixtures import (  # noqa: E402
    TARGET_REVISION,
    assert_revision,
    ephemeral_legacy_migration_engine_0024,
    run_alembic,
)


def _profile(profile_id: str) -> dict:
    for profile in SOURCE_ELIGIBILITY_PROFILES:
        if profile["profile_id"] == profile_id:
            return dict(profile)
    raise AssertionError(f"missing profile {profile_id}")


def test_staging_pin_0030_profile_matches_ephemeral_schema(
    ephemeral_legacy_migration_engine_0024,
) -> None:
    engine = ephemeral_legacy_migration_engine_0024
    run_alembic(engine, TARGET_REVISION)
    assert_revision(engine, TARGET_REVISION)

    with engine.connect() as conn:
        fingerprint = compute_public_schema_fingerprint(conn)

    contract = load_contract(export_contract())
    matched = match_source_profile(
        alembic_revision=TARGET_REVISION,
        public_table_count=fingerprint["public_table_count"],
        schema_fingerprint_sha256=fingerprint["schema_fingerprint"],
        contract=contract,
    )
    assert matched == "staging_pin_0030"

    expected = _profile("staging_pin_0030")
    assert fingerprint["public_table_count"] == expected["public_table_count"]


def test_staging_pin_0024_profile_matches_ephemeral_schema(
    ephemeral_legacy_migration_engine_0024,
) -> None:
    engine = ephemeral_legacy_migration_engine_0024
    with engine.connect() as conn:
        fingerprint = compute_public_schema_fingerprint(conn)

    contract = load_contract(export_contract())
    matched = match_source_profile(
        alembic_revision="0024",
        public_table_count=fingerprint["public_table_count"],
        schema_fingerprint_sha256=fingerprint["schema_fingerprint"],
        contract=contract,
    )
    assert matched == "staging_pin_0024"

    expected = _profile("staging_pin_0024")
    assert fingerprint["public_table_count"] == expected["public_table_count"]
    assert fingerprint["schema_fingerprint"] == expected["schema_fingerprint_sha256"]
