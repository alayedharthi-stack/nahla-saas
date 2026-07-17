"""Regression checks for the normal migration bootstrap contract."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    FORBIDDEN_BOOTSTRAP_LITERALS,
    INTEGRATION_BOOTSTRAP_TARGET,
    REPOSITORY_ALEMBIC_HEADS,
    STAGING_VALIDATED_ATTACH_REVISIONS,
    build_normal_bootstrap_upgrade_argv,
)


def test_repository_parallel_heads_0088_and_0089() -> None:
    assert REPOSITORY_ALEMBIC_HEADS == frozenset({"0088", "0089"})


def test_normal_bootstrap_pins_0089_not_head() -> None:
    main_source = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
    compose_source = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")

    assert INTEGRATION_BOOTSTRAP_TARGET == "0089"
    assert "head" in FORBIDDEN_BOOTSTRAP_LITERALS
    assert build_normal_bootstrap_upgrade_argv(python_executable="python") == [
        "python",
        "-m",
        "alembic",
        "upgrade",
        "0089",
    ]
    assert "build_normal_bootstrap_upgrade_argv" in main_source
    assert "alembic upgrade head" not in main_source
    assert "alembic upgrade 0089" in compose_source
    assert "alembic upgrade head" not in compose_source


def test_staging_post_attach_multi_head_contract() -> None:
    assert STAGING_VALIDATED_ATTACH_REVISIONS == frozenset({"0088", "0089"})


def test_admin_migration_endpoint_pins_0089_not_head() -> None:
    source = (_REPO / "backend" / "routers" / "admin_debug.py").read_text(encoding="utf-8")

    assert '[_sys.executable, "-m", "alembic", "upgrade", "0089"]' in source
