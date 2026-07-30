"""Regression checks for the normal migration bootstrap contract."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from alembic.script import ScriptDirectory  # noqa: E402

from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    FORBIDDEN_BOOTSTRAP_LITERALS,
    INTEGRATION_BOOTSTRAP_TARGET,
    NORMAL_BOOTSTRAP_REVISIONS,
    REPOSITORY_ALEMBIC_HEADS,
    STAGING_VALIDATED_ATTACH_REVISIONS,
    VALIDATED_STAGING_BOOTSTRAP_REVISIONS,
    build_normal_bootstrap_upgrade_argv,
)


def test_repository_parallel_heads_0092_and_0094() -> None:
    assert REPOSITORY_ALEMBIC_HEADS == frozenset({"0092", "0094"})


def test_migration_0094_extends_integration_branch_from_0093() -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        script = ScriptDirectory(str(_REPO / "database" / "migrations"))
    finally:
        os.chdir(prev_cwd)
    rev_0094 = script.get_revision("0094")
    assert rev_0094 is not None
    assert rev_0094.down_revision == "0093"
    assert set(script.get_heads()) == frozenset({"0092", "0094"})


def test_supported_deployment_revision_states_are_explicit() -> None:
    assert NORMAL_BOOTSTRAP_REVISIONS == frozenset({"0093"})
    assert VALIDATED_STAGING_BOOTSTRAP_REVISIONS == frozenset({"0088", "0093"})
    assert REPOSITORY_ALEMBIC_HEADS == frozenset({"0092", "0094"})


def test_normal_bootstrap_pins_0093_not_head() -> None:
    main_source = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
    compose_source = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")

    assert INTEGRATION_BOOTSTRAP_TARGET == "0093"
    assert "head" in FORBIDDEN_BOOTSTRAP_LITERALS
    assert build_normal_bootstrap_upgrade_argv(python_executable="python") == [
        "python",
        "-m",
        "alembic",
        "upgrade",
        "0093",
    ]
    assert "build_normal_bootstrap_upgrade_argv" in main_source
    assert "alembic upgrade head" not in main_source
    assert "alembic upgrade 0093" in compose_source
    assert "alembic upgrade head" not in compose_source


def test_staging_post_attach_multi_head_contract() -> None:
    # Historical guarded attach state remains explicit and is not confused
    # with the post-bootstrap validated staging state.
    assert STAGING_VALIDATED_ATTACH_REVISIONS == frozenset({"0088", "0089"})
    assert VALIDATED_STAGING_BOOTSTRAP_REVISIONS == frozenset({"0088", "0093"})


def test_admin_migration_endpoint_pins_0093_not_head() -> None:
    source = (_REPO / "backend" / "routers" / "admin_debug.py").read_text(encoding="utf-8")

    assert "build_normal_bootstrap_upgrade_argv" in source
    assert "INTEGRATION_BOOTSTRAP_TARGET" in source
