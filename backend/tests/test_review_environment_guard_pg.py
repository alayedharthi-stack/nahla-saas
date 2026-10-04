"""Catalog review environment guard against REAL PostgreSQL servers.

Each admin DSN in ``NAHLA_REVIEW_ENV_PG_ADMIN_URLS`` (comma separated, e.g. a
PostgreSQL 16 and a PostgreSQL 18 server) gets a throw-away role and two
throw-away databases per test module:

  * ``rv_marked_<rand>``   — marked once with ``ALTER DATABASE … SET nahla.environment``
  * ``rv_plain_<rand>``    — never marked (stands in for production / staging DR)

Everything is dropped at teardown. Without the variable the module falls back
to ``NAHLA_RELIABILITY_PG_ADMIN_DSN`` — the admin DSN the required PostgreSQL
proofs runner provides on PostgreSQL 16 (``lint-and-test``) and 18
(``postgres-18-compatibility``), where this module is an inventoried suite and a
skip fails the run. With neither variable the module is skipped (reported as
skipped, never as passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and neither
variable it fails instead.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from core import review_environment as re_mod  # noqa: E402

ADMIN_URLS = [u.strip() for u in (os.environ.get("NAHLA_REVIEW_ENV_PG_ADMIN_URLS")
                                   or os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").split(",") if u.strip()]
if not ADMIN_URLS and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but no PostgreSQL admin DSN is configured")
pytestmark = pytest.mark.skipif(not ADMIN_URLS, reason="no PostgreSQL admin DSN set (no real PostgreSQL)")
# Stable ids (server0, server1, …): the required-proofs inventory must not depend on
# host, port, or whether a DSN is configured at collection time.
_SERVER_IDS = [f"server{i}" for i in range(len(ADMIN_URLS))] or ["server0"]

MARKER = "catalog-review"
APP_PASSWORD = "rv-" + secrets.token_hex(8)
_LIBPQ_ENV = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE", "PGDATABASE")


def _admin_exec(admin_url: str, *statements: str) -> None:
    eng = create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        eng.dispose()


def _scalar(url: str, sql: str):
    eng = create_engine(url, poolclass=NullPool, future=True)
    try:
        with eng.connect() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        eng.dispose()


@pytest.fixture(scope="module", params=ADMIN_URLS or [None], ids=_SERVER_IDS)
def pg(request):
    admin_url = request.param
    suffix = secrets.token_hex(4)
    role = f"rv_app_{suffix}"
    marked = f"rv_marked_{suffix}"
    plain = f"rv_plain_{suffix}"
    _admin_exec(
        admin_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{APP_PASSWORD}'",
        f"CREATE DATABASE {marked} OWNER {role}",
        f"CREATE DATABASE {plain} OWNER {role}",
        f"ALTER DATABASE {marked} SET nahla.environment = '{MARKER}'",
    )
    base = make_url(admin_url)
    version = _scalar(admin_url, "SHOW server_version")

    def app_url(db: str, query: str = "") -> str:
        url = f"postgresql+psycopg2://{role}:{quote(APP_PASSWORD)}@{base.host}:{base.port}/{db}"
        return url + (f"?{query}" if query else "")

    info = {"admin": admin_url, "role": role, "marked": marked, "plain": plain, "app_url": app_url,
            "host": base.host, "version": version}
    try:
        yield info
    finally:
        _admin_exec(
            admin_url,
            f"DROP DATABASE IF EXISTS {marked} WITH (FORCE)",
            f"DROP DATABASE IF EXISTS {plain} WITH (FORCE)",
            f"DROP ROLE IF EXISTS {role}",
        )


def review_env(pg, db: str, **overrides):
    env = {
        "NAHLA_CATALOG_REVIEW_ENV": "1",
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "ENVIRONMENT": "staging",
        "DATABASE_URL": pg["app_url"](db),
        "DASHBOARD_URL": "https://catalog-review.nahlah.ai",
        "NAHLA_CATALOG_REVIEW_DB_HOST": pg["host"],
    }
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def _clean_libpq_env(monkeypatch):
    for k in _LIBPQ_ENV:
        monkeypatch.delenv(k, raising=False)


# ── the bug this replaces: pg_settings hides the custom placeholder ─────────

def test_pg_settings_does_not_list_the_database_level_marker(pg):
    url = pg["app_url"](pg["marked"])
    assert _scalar(url, "SELECT current_setting('nahla.environment', true)") == MARKER
    assert _scalar(url, "SELECT count(*) FROM pg_settings WHERE name = 'nahla.environment'") == 0


# ── accepted ─────────────────────────────────────────────────────────────────

def test_marked_review_database_is_accepted_with_a_real_read(pg):
    reading = re_mod.read_database_marker(pg["app_url"](pg["marked"]))
    assert reading == re_mod.MarkerReading(MARKER, MARKER, pg["marked"])
    check = re_mod.evaluate_review_environment(review_env(pg, pg["marked"]))
    assert check.enabled and check.ok, (pg["version"], check.failures)


def test_explicit_db_name_variable_is_honoured(pg):
    env = review_env(pg, pg["marked"], NAHLA_CATALOG_REVIEW_DB_NAME=pg["marked"])
    assert re_mod.evaluate_review_environment(env).ok


# ── refused ──────────────────────────────────────────────────────────────────

def test_unmarked_database_is_refused(pg):
    check = re_mod.evaluate_review_environment(review_env(pg, pg["plain"]))
    assert not check.ok and re_mod.F_DB_MARKER_MISSING in check.failures


def test_session_forged_marker_via_options_is_refused_by_both_layers(pg):
    forged_dsn = pg["app_url"](pg["plain"], "options=" + quote("-c nahla.environment=catalog-review"))
    # Dynamic layer alone: the session value exists but no persisted entry backs it.
    reading = re_mod.read_database_marker(forged_dsn)
    assert reading.effective == MARKER and reading.persisted is None
    env = review_env(pg, pg["plain"])
    assert re_mod.check_database_marker(env, marker_reader=lambda _u: reading) == [re_mod.F_DB_MARKER_NOT_PERSISTED]
    # Full guard: the DSN query is refused statically and never connected to.
    calls = []
    check = re_mod.evaluate_review_environment(
        review_env(pg, pg["plain"], DATABASE_URL=forged_dsn), marker_reader=lambda u: calls.append(u))
    assert not check.ok and re_mod.F_DB_QUERY_REJECTED in check.failures and calls == []


def test_session_forged_marker_via_pgoptions_is_refused(pg, monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c nahla.environment=catalog-review")
    reading = re_mod.read_database_marker(pg["app_url"](pg["plain"]))
    assert reading.effective == MARKER and reading.persisted is None
    check = re_mod.evaluate_review_environment(review_env(pg, pg["plain"], PGOPTIONS="-c nahla.environment=catalog-review"))
    assert not check.ok and re_mod.F_LIBPQ_ENV_OVERRIDE in check.failures


def test_role_level_forged_marker_on_unmarked_database_is_refused(pg):
    _admin_exec(pg["admin"], f"ALTER ROLE {pg['role']} IN DATABASE {pg['plain']} SET nahla.environment = '{MARKER}'")
    try:
        reading = re_mod.read_database_marker(pg["app_url"](pg["plain"]))
        assert reading.effective == MARKER and reading.persisted is None
        check = re_mod.evaluate_review_environment(review_env(pg, pg["plain"]))
        assert not check.ok and re_mod.F_DB_MARKER_NOT_PERSISTED in check.failures
    finally:
        _admin_exec(pg["admin"], f"ALTER ROLE {pg['role']} IN DATABASE {pg['plain']} RESET nahla.environment")


def test_role_level_override_on_marked_database_is_refused(pg):
    _admin_exec(pg["admin"], f"ALTER ROLE {pg['role']} IN DATABASE {pg['marked']} SET nahla.environment = 'staging'")
    try:
        check = re_mod.evaluate_review_environment(review_env(pg, pg["marked"]))
        assert not check.ok and re_mod.F_DB_MARKER_EFFECTIVE_MISMATCH in check.failures
    finally:
        _admin_exec(pg["admin"], f"ALTER ROLE {pg['role']} IN DATABASE {pg['marked']} RESET nahla.environment")


def test_wrong_persisted_marker_is_refused(pg):
    _admin_exec(pg["admin"], f"ALTER DATABASE {pg['plain']} SET nahla.environment = 'staging-dr'")
    try:
        check = re_mod.evaluate_review_environment(review_env(pg, pg["plain"]))
        assert not check.ok and re_mod.F_DB_MARKER_MISMATCH in check.failures
    finally:
        _admin_exec(pg["admin"], f"ALTER DATABASE {pg['plain']} RESET nahla.environment")


def test_configured_name_differs_from_connected_database_is_refused(pg):
    check = re_mod.evaluate_review_environment(review_env(pg, pg["marked"], NAHLA_CATALOG_REVIEW_DB_NAME=pg["plain"]))
    assert not check.ok and re_mod.F_DB_NAME_MISMATCH in check.failures


def test_wrong_password_is_unreadable_not_accepted(pg):
    bad = pg["app_url"](pg["marked"]).replace(quote(APP_PASSWORD), "wrong-password")
    check = re_mod.evaluate_review_environment(review_env(pg, pg["marked"], DATABASE_URL=bad))
    assert not check.ok and re_mod.F_DB_MARKER_UNREADABLE in check.failures
    assert "wrong-password" not in "\n".join(re_mod.format_report(check))


# ── end to end: the real entrypoints refuse before migrations and workers ───

def _subprocess_env(pg, db: str, **overrides):
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    env.update(review_env(pg, db, **overrides))
    return env


def test_preflight_accepts_marked_and_refuses_unmarked_database(pg):
    script = str(_REPO / "scripts" / "preflight_check.py")
    ok = subprocess.run([sys.executable, script], env=_subprocess_env(pg, pg["marked"]), capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "isolation verified" in ok.stdout
    bad = subprocess.run([sys.executable, script], env=_subprocess_env(pg, pg["plain"]), capture_output=True, text=True, timeout=120)
    assert bad.returncode == 1 and re_mod.F_DB_MARKER_MISSING in bad.stdout
    for out in (ok.stdout + ok.stderr, bad.stdout + bad.stderr):
        assert APP_PASSWORD not in out


def test_lifespan_refuses_unmarked_database_and_creates_nothing(pg):
    """The app lifespan (on_startup) raises before create_all, repairs or workers:
    the unmarked database still has zero tables afterwards."""
    code = (
        "import asyncio, sys\n"
        "sys.path[:0] = [%r, %r, %r]\n"
        "import main\n"
        "asyncio.run(main.on_startup())\n"
    ) % (str(_REPO), str(_REPO / "backend"), str(_REPO / "database"))
    env = _subprocess_env(pg, pg["plain"], NAHLA_DISABLE_SCHEDULERS="1")
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(_REPO / "backend"),
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode != 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "catalog review environment isolation failed" in proc.stderr
    assert re_mod.F_DB_MARKER_MISSING in proc.stderr
    assert APP_PASSWORD not in proc.stdout + proc.stderr
    tables = _scalar(pg["app_url"](pg["plain"]),
                     "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
    assert tables == 0
