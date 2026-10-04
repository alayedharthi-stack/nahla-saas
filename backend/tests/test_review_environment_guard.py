"""Catalog review environment isolation guard — dangerous configurations are
refused, the intended review configuration is accepted, nothing is printed
that could leak a DSN or a token, and the guard is inert outside review mode.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from core import review_environment as re_mod  # noqa: E402
from scripts import preflight_check  # noqa: E402

REVIEW_DSN = "postgresql+psycopg2://review_user:s3cret-dsn-password@postgres-catalog-review.railway.internal:5432/railway"
STAGING_DSN = "postgresql+psycopg2://operator:pw@postgres-staging.railway.internal:5432/nahla"
PROD_LIKE_DSN = "postgresql://nahla:pw@postgres-production.railway.internal:5432/nahla_saas"
OK_READING = re_mod.MarkerReading(effective="catalog-review", persisted="catalog-review", database="railway")


def good_env(**overrides):
    env = {
        "NAHLA_CATALOG_REVIEW_ENV": "1",
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "ENVIRONMENT": "staging",
        "DATABASE_URL": REVIEW_DSN,
        "DASHBOARD_URL": "https://catalog-review.nahlah.ai",
    }
    env.update(overrides)
    return env


def marker_ok(_url: str):
    return OK_READING


# ── inert outside review mode ────────────────────────────────────────────────

def test_guard_is_inert_when_flag_unset():
    env = {"DATABASE_URL": PROD_LIKE_DSN, "ENVIRONMENT": "production"}
    check = re_mod.evaluate_review_environment(env, marker_reader=lambda u: None)
    assert check.enabled is False and check.ok is True
    assert re_mod.assert_review_environment(env, marker_reader=lambda u: None).enabled is False


# ── accepted review configuration ────────────────────────────────────────────

def test_review_configuration_is_accepted():
    check = re_mod.evaluate_review_environment(good_env(), marker_reader=marker_ok)
    assert check.enabled and check.ok, check.failures
    report = "\n".join(re_mod.format_report(check))
    assert "isolation verified" in report
    assert "s3cret-dsn-password" not in report and "review_user" not in report


def test_sslmode_query_is_allowed():
    env = good_env(DATABASE_URL=REVIEW_DSN + "?sslmode=require")
    assert re_mod.evaluate_review_environment(env, marker_reader=marker_ok).ok


def test_custom_host_marker_and_db_name_are_honoured():
    env = good_env(
        DATABASE_URL="postgresql://u:p@review-db.internal:5432/catalog_review",
        NAHLA_CATALOG_REVIEW_DB_HOST="review-db.internal",
        NAHLA_CATALOG_REVIEW_DB_NAME="catalog_review",
        NAHLA_CATALOG_REVIEW_DB_MARKER="catalog-review-2",
    )
    reading = re_mod.MarkerReading("catalog-review-2", "catalog-review-2", "catalog_review")
    assert re_mod.evaluate_review_environment(env, marker_reader=lambda u: reading).ok


# ── refused configurations (static) ──────────────────────────────────────────

@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"DATABASE_URL": ""}, re_mod.F_DB_URL_MISSING),
        ({"DATABASE_URL": "not a url"}, re_mod.F_DB_URL_MALFORMED),
        ({"DATABASE_URL": STAGING_DSN}, re_mod.F_DB_HOST_FORBIDDEN),
        ({"DATABASE_URL": PROD_LIKE_DSN}, re_mod.F_DB_HOST_FORBIDDEN),
        ({"DATABASE_URL": "postgresql://u:p@postgres.railway.internal:5432/railway"}, re_mod.F_DB_HOST_NOT_ALLOWLISTED),
        ({"DATABASE_URL": "sqlite:///./x.db"}, re_mod.F_DB_SCHEME),
        ({"RAILWAY_PROJECT_NAME": ""}, re_mod.F_PROJECT_MISSING),
        ({"RAILWAY_PROJECT_NAME": "nahla-production"}, re_mod.F_PROJECT_MISMATCH),
        ({"RAILWAY_ENVIRONMENT_NAME": ""}, re_mod.F_ENVIRONMENT_MISSING),
        ({"RAILWAY_ENVIRONMENT_NAME": "production"}, re_mod.F_PRODUCTION_MARKER),
        ({"ENVIRONMENT": "production"}, re_mod.F_PRODUCTION_MARKER),
        ({"DASHBOARD_URL": ""}, re_mod.F_DASHBOARD_URL_MISSING),
        ({"DASHBOARD_URL": "https://app.nahlah.ai"}, re_mod.F_DASHBOARD_URL_PRODUCTION),
        ({"DASHBOARD_URL": "https://app.nahlah.ai/"}, re_mod.F_DASHBOARD_URL_PRODUCTION),
        ({"DASHBOARD_URL": "https://APP.nahlah.ai.:443/x"}, re_mod.F_DASHBOARD_URL_PRODUCTION),
        # libpq redirection through DSN query parameters or environment
        ({"DATABASE_URL": REVIEW_DSN + "?host=prod-db.internal"}, re_mod.F_DB_QUERY_REJECTED),
        ({"DATABASE_URL": REVIEW_DSN + "?hostaddr=10.0.0.9"}, re_mod.F_DB_QUERY_REJECTED),
        ({"DATABASE_URL": REVIEW_DSN + "?options=-c%20nahla.environment%3Dcatalog-review"}, re_mod.F_DB_QUERY_REJECTED),
        ({"DATABASE_URL": REVIEW_DSN + "?service=prod"}, re_mod.F_DB_QUERY_REJECTED),
        ({"PGHOST": "prod-db.internal"}, re_mod.F_LIBPQ_ENV_OVERRIDE),
        ({"PGOPTIONS": "-c nahla.environment=catalog-review"}, re_mod.F_LIBPQ_ENV_OVERRIDE),
        ({"PGSERVICE": "prod"}, re_mod.F_LIBPQ_ENV_OVERRIDE),
    ],
)
def test_dangerous_configuration_is_refused(overrides, expected):
    env = good_env(**overrides)
    check = re_mod.evaluate_review_environment(env, marker_reader=marker_ok)
    assert check.enabled and not check.ok
    assert expected in check.failures, check.failures
    with pytest.raises(re_mod.ReviewEnvironmentViolation) as exc:
        re_mod.assert_review_environment(env, marker_reader=marker_ok)
    assert expected in exc.value.failures
    text = str(exc.value) + "\n".join(re_mod.format_report(check))
    assert "s3cret-dsn-password" not in text and "pw@" not in text and "prod-db.internal" not in text


def test_dsn_with_redirecting_query_never_reaches_the_marker_read():
    """The ``?host=`` / ``?options=`` bypass: psycopg2 would connect elsewhere and
    forge the session marker; the static layer must refuse before any connection."""
    calls = []

    def spy(url):
        calls.append(url)
        return OK_READING

    env = good_env(DATABASE_URL=REVIEW_DSN + "?host=prod-db.internal&options=-c%20nahla.environment%3Dcatalog-review")
    check = re_mod.evaluate_review_environment(env, marker_reader=spy)
    assert not check.ok and re_mod.F_DB_QUERY_REJECTED in check.failures
    assert calls == []


def test_database_url_presence_alone_is_not_isolation():
    env = good_env(DATABASE_URL="postgresql://u:p@postgres.railway.internal:5432/railway")
    check = re_mod.evaluate_review_environment(env, marker_reader=lambda u: None)
    assert not check.ok and re_mod.F_DB_HOST_NOT_ALLOWLISTED in check.failures


def test_forbidden_host_is_never_connected_to():
    calls = []

    def spy(url):
        calls.append(url)
        return OK_READING

    check = re_mod.evaluate_review_environment(good_env(DATABASE_URL=STAGING_DSN), marker_reader=spy)
    assert not check.ok and calls == []


# ── database identity marker ─────────────────────────────────────────────────

def test_marker_missing_or_wrong_or_unreadable_is_refused():
    env = good_env()
    assert re_mod.F_DB_MARKER_MISSING in re_mod.evaluate_review_environment(env, marker_reader=lambda u: None).failures
    assert re_mod.F_DB_MARKER_MISSING in re_mod.evaluate_review_environment(
        env, marker_reader=lambda u: re_mod.MarkerReading(None, None, "railway")).failures
    assert re_mod.F_DB_MARKER_MISMATCH in re_mod.evaluate_review_environment(
        env, marker_reader=lambda u: re_mod.MarkerReading("staging", "staging", "railway")).failures

    def boom(url):
        raise ConnectionError("refused host=" + url)

    check = re_mod.evaluate_review_environment(env, marker_reader=boom)
    assert re_mod.F_DB_MARKER_UNREADABLE in check.failures
    assert "s3cret-dsn-password" not in "\n".join(re_mod.format_report(check))


def test_marker_must_be_persisted_database_wide_not_only_effective():
    """A marker that only exists as the effective value (session SET, options=-c,
    PGOPTIONS, a role-level setting) has no database-wide pg_db_role_setting entry."""
    env = good_env()
    check = re_mod.evaluate_review_environment(
        env, marker_reader=lambda u: re_mod.MarkerReading("catalog-review", None, "railway"))
    assert re_mod.F_DB_MARKER_NOT_PERSISTED in check.failures and not check.ok


def test_effective_value_must_match_the_persisted_marker():
    """A role or session override on a marked database is refused too."""
    env = good_env()
    for effective in ("staging", None, ""):
        check = re_mod.evaluate_review_environment(
            env, marker_reader=lambda u, e=effective: re_mod.MarkerReading(e, "catalog-review", "railway"))
        assert re_mod.F_DB_MARKER_EFFECTIVE_MISMATCH in check.failures, effective


def test_non_reading_reader_results_carry_no_proof():
    """Only a MarkerReading counts; a bare string or a dict never passes."""
    env = good_env()
    for value in ("catalog-review", {"effective": "catalog-review", "persisted": "catalog-review", "database": "railway"}):
        check = re_mod.evaluate_review_environment(env, marker_reader=lambda u, v=value: v)
        assert re_mod.F_DB_MARKER_MISSING in check.failures, value


def test_missing_current_database_is_refused():
    check = re_mod.evaluate_review_environment(
        good_env(), marker_reader=lambda u: re_mod.MarkerReading("catalog-review", "catalog-review", None))
    assert re_mod.F_DB_IDENTITY_MISMATCH in check.failures


def test_persisted_value_parser():
    assert re_mod._persisted_value("nahla.environment=catalog-review") == "catalog-review"
    assert re_mod._persisted_value("nahla.environment=") is None
    assert re_mod._persisted_value("other.setting=catalog-review") is None
    assert re_mod._persisted_value(None) is None


def test_connected_database_name_must_match_the_dsn_or_configured_name():
    env = good_env()
    check = re_mod.evaluate_review_environment(
        env, marker_reader=lambda u: re_mod.MarkerReading("catalog-review", "catalog-review", "nahla_saas"))
    assert re_mod.F_DB_IDENTITY_MISMATCH in check.failures
    # A configured name that disagrees with the DSN fails statically (no connection is made).
    env2 = good_env(NAHLA_CATALOG_REVIEW_DB_NAME="catalog_review")
    calls = []
    check2 = re_mod.evaluate_review_environment(env2, marker_reader=lambda u: calls.append(u) or OK_READING)
    assert re_mod.F_DB_NAME_MISMATCH in check2.failures and calls == []
    # A configured name that matches the DSN but not the connected database fails dynamically.
    env3 = good_env(NAHLA_CATALOG_REVIEW_DB_NAME="railway")
    check3 = re_mod.evaluate_review_environment(
        env3, marker_reader=lambda u: re_mod.MarkerReading("catalog-review", "catalog-review", "nahla_saas"))
    assert re_mod.F_DB_IDENTITY_MISMATCH in check3.failures


def test_marker_check_can_be_skipped_for_static_gates():
    check = re_mod.evaluate_review_environment(good_env(), with_database_marker=False, marker_reader=lambda u: None)
    assert check.ok


def test_marker_sql_reads_effective_persisted_and_database_read_only():
    sql = re_mod.DB_MARKER_SQL.upper()
    assert sql.startswith("SELECT CURRENT_SETTING('NAHLA.ENVIRONMENT', TRUE)")
    assert "PG_DB_ROLE_SETTING" in sql and "SETROLE = 0" in sql and "CURRENT_DATABASE()" in sql
    assert "PG_SETTINGS" not in sql  # placeholders are not listed there (proven on PG 16/18)
    for verb in ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP", "SET "):
        assert verb not in sql


def test_read_database_marker_against_sqlite_memory_reports_unreadable():
    env = good_env(DATABASE_URL="sqlite://", NAHLA_CATALOG_REVIEW_DB_HOST="")
    assert re_mod.check_database_marker(env) == [re_mod.F_DB_MARKER_UNREADABLE]


# ── preflight integration ────────────────────────────────────────────────────

def _run_preflight(monkeypatch, env, reader=None):
    for k in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE", "PGDATABASE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if reader is not None:
        # preflight reuses an already-registered guard module; register the imported one
        # and patch its reader so no real connection is attempted.
        monkeypatch.setitem(sys.modules, "nahla_review_environment_guard", re_mod)
        monkeypatch.setattr(re_mod, "read_database_marker", reader)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = preflight_check.main()
    return rc, buf.getvalue()


def test_preflight_refuses_review_boot_when_host_is_forbidden(monkeypatch):
    rc, out = _run_preflight(monkeypatch, good_env(DATABASE_URL=STAGING_DSN))
    assert rc == 1 and re_mod.F_DB_HOST_FORBIDDEN in out
    assert "pw@" not in out and STAGING_DSN not in out


def test_preflight_refuses_allowlisted_host_without_marker(monkeypatch):
    rc, out = _run_preflight(monkeypatch, good_env(), reader=lambda url: re_mod.MarkerReading(None, None, "railway"))
    assert rc == 1 and re_mod.F_DB_MARKER_MISSING in out
    assert "s3cret-dsn-password" not in out


def test_preflight_passes_review_boot_when_isolated(monkeypatch):
    rc, out = _run_preflight(monkeypatch, good_env(), reader=lambda url: OK_READING)
    assert rc == 0, out
    assert "isolation verified" in out and "s3cret-dsn-password" not in out


def test_preflight_unchanged_outside_review_mode(monkeypatch):
    monkeypatch.delenv("NAHLA_CATALOG_REVIEW_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = preflight_check.main()
    assert rc == 0 and "skipping production checks" in buf.getvalue()


def test_preflight_subprocess_refuses_forbidden_host_end_to_end():
    env = good_env(DATABASE_URL=PROD_LIKE_DSN)
    env["PATH"] = "/usr/bin:/bin"
    proc = subprocess.run([sys.executable, str(_REPO / "scripts" / "preflight_check.py")], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing to boot" in proc.stdout and "pw@" not in proc.stdout + proc.stderr


def test_start_sh_ignores_preflight_bypass_in_review_mode():
    src = (_REPO / "start.sh").read_text(encoding="utf-8")
    assert '[ -n "${NAHLA_CATALOG_REVIEW_ENV:-}" ]' in src
    assert 'if [ "${NAHLA_SKIP_PREFLIGHT:-}" != "1" ] || [ -n "${NAHLA_CATALOG_REVIEW_ENV:-}" ]; then' in src


# ── lifespan / bootstrap / scheduler wiring ───────────────────────────────────

def test_main_lifespan_guard_precedes_every_db_phase_and_blocks_workers():
    src = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
    i_def = src.index("async def on_startup() -> None:")
    i_guard = src.index("Catalog review environment: single choke point", i_def)
    i_repair = src.index("_coexistence_client_id_repair_bg", i_def)
    i_step_a = src.index("Step A: cleanup_salla_duplicates", i_def)
    i_run_mig = src.index("def _run_migrations():", i_def)
    assert i_def < i_guard < i_repair < i_step_a < i_run_mig
    assert "raise RuntimeError(" in src[i_guard:i_repair]
    assert "_review_workers_blocked" in src and "NOT queued — review environment isolation not proven" in src


def test_report_never_contains_values_only_codes():
    check = re_mod.evaluate_review_environment(
        good_env(), marker_reader=lambda u: re_mod.MarkerReading("wrong", "wrong", "railway"))
    dumped = json.dumps(check.as_dict())
    for secret in ("s3cret-dsn-password", "review_user", REVIEW_DSN):
        assert secret not in dumped
