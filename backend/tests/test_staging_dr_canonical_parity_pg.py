"""PostgreSQL proof for staging DR canonical parity contract shape."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators.schema_fingerprint import compute_public_schema_fingerprint  # noqa: E402
from scripts.operators.staging_dr_canonical_parity_contract import (  # noqa: E402
    SOURCE_ELIGIBILITY_PROFILES,
    export_contract,
    load_contract,
    match_source_profile,
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


def test_upgrade_0024_to_0030_adds_three_public_tables(
    ephemeral_legacy_migration_engine_0024,
) -> None:
    """Migration-chain delta is not evidence for the live staging pin count."""
    engine = ephemeral_legacy_migration_engine_0024
    with engine.connect() as conn:
        before = compute_public_schema_fingerprint(conn)

    run_alembic(engine, TARGET_REVISION)
    assert_revision(engine, TARGET_REVISION)

    with engine.connect() as conn:
        after = compute_public_schema_fingerprint(conn)

    assert after["public_table_count"] == before["public_table_count"] + 3
    assert after["schema_fingerprint"] != before["schema_fingerprint"]


def test_staging_pin_0030_matches_live_attested_values() -> None:
    """Profile matches the recorded live staging source attestation."""
    contract = load_contract(export_contract())
    pin = _profile("staging_pin_0030")
    matched = match_source_profile(
        alembic_revision="0030",
        public_table_count=96,
        schema_fingerprint_sha256=(
            "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
        ),
        contract=contract,
    )
    assert matched == "staging_pin_0030"
    assert pin["public_table_count"] == 96
    assert pin["schema_fingerprint_sha256"] == (
        "1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae"
    )


def test_staging_pin_0024_requires_pinned_fingerprint() -> None:
    contract = load_contract(export_contract())
    pin = _profile("staging_pin_0024")
    matched = match_source_profile(
        alembic_revision="0024",
        public_table_count=pin["public_table_count"],
        schema_fingerprint_sha256=pin["schema_fingerprint_sha256"],
        contract=contract,
    )
    assert matched == "staging_pin_0024"
