"""Regression checks for the normal migration path with parallel heads."""
from __future__ import annotations

from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]


def test_normal_bootstrap_and_compose_pin_0089_not_head() -> None:
    main_source = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
    compose_source = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")

    assert '[sys.executable, "-m", "alembic", "upgrade", "0089"]' in main_source
    assert "alembic upgrade head" not in main_source
    assert 'alembic upgrade 0089' in compose_source
    assert "alembic upgrade head" not in compose_source


def test_admin_migration_endpoint_pins_0089_not_head() -> None:
    source = (_REPO / "backend" / "routers" / "admin_debug.py").read_text(encoding="utf-8")

    assert '[_sys.executable, "-m", "alembic", "upgrade", "0089"]' in source
