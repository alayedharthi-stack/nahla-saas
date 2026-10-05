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
import socket
import subprocess
import sys
import time
import urllib.request
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


# ── end to end: the real server (uvicorn main:app) ───────────────────────────
#
# These boot the production entrypoint the way start.sh does (uvicorn main:app),
# with schedulers disabled and every outbound HTTP(S) proxy pointed at a closed
# loopback port, so nothing can leave the machine. Only loopback reaches the
# throw-away PostgreSQL server.

_NO_EGRESS = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
              "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
              "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(port: int) -> int | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/alive", timeout=2) as resp:
            return resp.status
    except OSError:  # URLError, refused, reset, timeout: not listening (yet / any more)
        return None


def _run_server(pg, db: str, log_path: Path, *, until: str, timeout: float = 240):
    """Start ``uvicorn main:app``; return once ``until`` is logged or the process
    exits. Returns (returncode or None while running, output, port, process)."""
    port = _free_port()
    env = _subprocess_env(pg, db, NAHLA_DISABLE_SCHEDULERS="1", PYTHONUNBUFFERED="1",
                          PYTHONPATH=os.pathsep.join([str(_REPO), str(_REPO / "backend"), str(_REPO / "database")]),
                          **_NO_EGRESS)
    log = open(log_path, "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(_REPO / "backend"), env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = log_path.read_text(errors="replace")
        if until in out or proc.poll() is not None:
            break
        time.sleep(0.25)
    log.flush()
    return proc.poll(), log_path.read_text(errors="replace"), port, proc


def _stop(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)


@pytest.fixture()
def boot_db(pg):
    """A fresh database marked like the review database, owned by the test role."""
    name = f"rv_boot_{secrets.token_hex(4)}"
    _admin_exec(pg["admin"], f"CREATE DATABASE {name} OWNER {pg['role']}",
                f"ALTER DATABASE {name} SET nahla.environment = '{MARKER}'")
    try:
        yield name
    finally:
        _admin_exec(pg["admin"], f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _public_tables(pg, db: str) -> set[str]:
    eng = create_engine(pg["app_url"](db), poolclass=NullPool, future=True)
    try:
        with eng.connect() as conn:
            return set(conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")).scalars())
    finally:
        eng.dispose()


def test_review_provisioning_premigration_then_boot_serves_with_the_complete_schema(pg, boot_db, tmp_path):
    """The review provisioning path the runbook prescribes, end to end on a fresh
    marked database: ``preflight_check.py`` proves isolation, the schema step
    upgrades to the application heads (``0111`` then ``0113`` from the bootstrap
    contract — the head set production carries), then ``uvicorn main:app``
    boots. The lifespan guard and bootstrap Step 0 both verify isolation, the
    pinned bootstrap upgrade (0093) is a no-op with rc=0, create_all completes,
    /alive answers, and every column the ORM models declare exists.

    The schema step is required and 0093 alone is not enough: on an EMPTY
    database booting never reaches 0093 (``test_review_environment_fresh_boot_pg``,
    the same on ``main``), and a database migrated only to 0093 boots but lacks
    23 model columns that later revisions add to existing tables."""
    from scripts.operators.bootstrap_migration_contract import (
        APPLICATION_ALEMBIC_HEAD,
        NAVIGATION_ALEMBIC_HEAD,
        build_normal_bootstrap_upgrade_argv,
    )
    from database.models import Base

    bootstrap_target = build_normal_bootstrap_upgrade_argv(python_executable=sys.executable)[-1]
    assert bootstrap_target == "0093"
    env = _subprocess_env(pg, boot_db, **_NO_EGRESS)
    preflight = subprocess.run([sys.executable, str(_REPO / "scripts" / "preflight_check.py")], env=env,
                               capture_output=True, text=True, timeout=120)
    assert preflight.returncode == 0 and "isolation verified" in preflight.stdout, preflight.stdout + preflight.stderr
    for target in (APPLICATION_ALEMBIC_HEAD, NAVIGATION_ALEMBIC_HEAD):
        migrate = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=str(_REPO / "database"),
                                 env=env, capture_output=True, text=True, timeout=600)
        assert migrate.returncode == 0, migrate.stderr[-3000:]
    heads = {APPLICATION_ALEMBIC_HEAD, NAVIGATION_ALEMBIC_HEAD}

    done = "[BOOT/db] Bootstrap completed cleanly."
    ready = "[BOOT/safe_alters] Database tables ready."
    rc, out, port, proc = _run_server(pg, boot_db, tmp_path / "boot.log", until=done)
    try:
        assert rc is None, f"server exited rc={rc}:\n{out[-3000:]}"
        assert done in out, out[-3000:]
        assert _alive(port) == 200
        for _ in range(240):
            if ready in (tmp_path / "boot.log").read_text(errors="replace"):
                break
            time.sleep(0.25)
        out = (tmp_path / "boot.log").read_text(errors="replace")
    finally:
        _stop(proc)
    assert out.count("isolation verified") >= 2  # lifespan guard and bootstrap Step 0
    assert "[BOOT/db] Step B: no stamp needed (has_alembic=True" in out
    assert f"Step C: alembic upgrade {bootstrap_target} OK rc=0" in out, out[-4000:]
    assert "Step C FAILED" not in out
    assert ready in out
    assert "Application startup complete" in out and "Application startup failed" not in out
    assert "NAHLA_DISABLE_SCHEDULERS=1" in out
    assert APP_PASSWORD not in out + preflight.stdout + preflight.stderr

    eng = create_engine(pg["app_url"](boot_db), poolclass=NullPool, future=True)
    try:
        with eng.connect() as conn:
            assert set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) == heads
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(eng)
        present = set(insp.get_table_names())
        missing = [t.name for t in Base.metadata.sorted_tables if t.name not in present]
        missing += [f"{t.name}.{c.name}" for t in Base.metadata.sorted_tables if t.name in present
                    for c in t.columns if c.name not in {col["name"] for col in insp.get_columns(t.name)}]
    finally:
        eng.dispose()
    assert missing == [], missing


def test_server_refuses_an_unmarked_database_before_any_bootstrap_or_worker(pg, tmp_path):
    """The failure path through the real server: uvicorn exits with "Application
    startup failed", never answers /alive, never dispatches the bootstrap,
    create_all or a scheduler, and the database still has no table."""
    rc, out, port, proc = _run_server(pg, pg["plain"], tmp_path / "refused.log", until="Application startup failed",
                                      timeout=120)
    try:
        if rc is None:  # the line is printed just before exit
            proc.wait(timeout=30)
            rc = proc.returncode
            out = (tmp_path / "refused.log").read_text(errors="replace")
    finally:
        _stop(proc)
    assert rc not in (None, 0), out[-3000:]
    assert "Application startup failed" in out
    assert "catalog review environment isolation failed" in out and re_mod.F_DB_MARKER_MISSING in out
    for started in ("[BOOT/db] Bootstrap dispatched", "[BOOT/db] Step", "[BOOT/safe_alters]",
                    "[Startup] NAHLA_DISABLE_SCHEDULERS", "Application startup complete"):
        assert started not in out, started
    assert _alive(port) is None
    assert APP_PASSWORD not in out
    assert _public_tables(pg, pg["plain"]) == set()
