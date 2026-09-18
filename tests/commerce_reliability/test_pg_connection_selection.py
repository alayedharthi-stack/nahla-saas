"""Connection selection of the shared PostgreSQL helper (no database).

When ``LEGACY_MIG_PG_TEST_DATABASE_URL`` is set it is the only candidate: a
failure to reach it fails (integration required) or skips, and never falls
through to ``A1_PG_TEST_DATABASE_URL``, ``DATABASE_URL`` or the default
service URL. Without it the historical fallback order is unchanged.
"""
from __future__ import annotations

from typing import List

import pytest

from tests import legacy_migration_drift_postgres_fixtures as helper

EXPLICIT = "postgresql://nahla:secret-pw@127.0.0.1:5499/explicit_db"
A1 = "postgresql://nahla:nahla_password@127.0.0.1:5433/a1_db"
AMBIENT = "postgresql://nahla:nahla_password@127.0.0.1:5433/ambient_db"


class _RecordingEngine:
    def __init__(self, url: str, reachable: bool) -> None:
        self.url, self.reachable = url, reachable

    def connect(self):
        if not self.reachable:
            raise ConnectionRefusedError(f"connection refused: {self.url}")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args, **_kwargs):
        return None


def _fake_create_engine(attempted: List[str], reachable: set):
    def create_engine(url, **_kwargs):
        attempted.append(url)
        return _RecordingEngine(url, url in reachable)
    return create_engine


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (helper.EXPLICIT_URL_ENV, "A1_PG_TEST_DATABASE_URL", "DATABASE_URL", "LEGACY_MIG_PG_INTEGRATION_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_explicit_target_is_the_only_candidate(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(helper.EXPLICIT_URL_ENV, EXPLICIT)
    clean_env.setenv("A1_PG_TEST_DATABASE_URL", A1)
    clean_env.setenv("DATABASE_URL", AMBIENT)
    assert helper._candidate_database_urls() == [EXPLICIT]


def test_without_an_explicit_target_the_historical_order_is_unchanged(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("A1_PG_TEST_DATABASE_URL", A1)
    clean_env.setenv("DATABASE_URL", AMBIENT)
    assert helper._candidate_database_urls() == [A1, AMBIENT, helper.DEFAULT_SERVICE_URL]
    clean_env.setenv("DATABASE_URL", A1)  # duplicates collapse
    assert helper._candidate_database_urls() == [A1, helper.DEFAULT_SERVICE_URL]
    clean_env.delenv("A1_PG_TEST_DATABASE_URL")
    clean_env.delenv("DATABASE_URL")
    assert helper._candidate_database_urls() == [helper.DEFAULT_SERVICE_URL]


@pytest.mark.parametrize("required", [True, False])
def test_unreachable_explicit_target_never_falls_back_to_a_reachable_alternate(
    clean_env: pytest.MonkeyPatch, required: bool,
) -> None:
    clean_env.setenv(helper.EXPLICIT_URL_ENV, EXPLICIT)
    clean_env.setenv("A1_PG_TEST_DATABASE_URL", A1)
    clean_env.setenv("DATABASE_URL", AMBIENT)
    if required:
        clean_env.setenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED", "1")
    attempted: List[str] = []
    # Every alternate would succeed; only the explicit target is refused.
    clean_env.setattr(helper, "create_engine", _fake_create_engine(attempted, {A1, AMBIENT, helper.DEFAULT_SERVICE_URL}))
    expected = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(expected) as outcome:
        helper.connect_engine()
    assert attempted == [EXPLICIT], attempted
    message = str(outcome.value)
    assert message.startswith("no fallback attempted")
    assert "5499/explicit_db" in message and "secret-pw" not in message   # target named, password redacted


def test_reachable_explicit_target_is_used_without_touching_alternates(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(helper.EXPLICIT_URL_ENV, EXPLICIT)
    clean_env.setenv("A1_PG_TEST_DATABASE_URL", A1)
    attempted: List[str] = []
    clean_env.setattr(helper, "create_engine", _fake_create_engine(attempted, {EXPLICIT, A1}))
    engine = helper.connect_engine()
    assert engine.url == EXPLICIT and attempted == [EXPLICIT]


def test_historical_fallback_still_reaches_a_later_candidate_when_no_explicit_target(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("A1_PG_TEST_DATABASE_URL", A1)
    clean_env.setenv("DATABASE_URL", AMBIENT)
    attempted: List[str] = []
    clean_env.setattr(helper, "create_engine", _fake_create_engine(attempted, {AMBIENT}))
    engine = helper.connect_engine()
    assert engine.url == AMBIENT and attempted == [A1, AMBIENT]
