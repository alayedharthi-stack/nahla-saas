"""
tests/conftest.py
─────────────────
Shared pytest bootstrap for AI commerce confidence suites.

Ensures repo/backend/database paths are stable before any test module
imports ``core.database`` (which must not shadow the ``database`` package).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _TESTS_DIR.parent
_REPO_ROOT = _BACKEND_DIR.parent
_DATABASE_DIR = _REPO_ROOT / "database"

for _path in (_REPO_ROOT, _BACKEND_DIR, _DATABASE_DIR):
    _entry = str(_path)
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "confidence_gate: AI Commerce Confidence Gate regression scenarios",
    )


@pytest.fixture(autouse=True)
def _enable_commerce_draft_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable deterministic draft-order bridge for scenario harness tests."""
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED", "true")
