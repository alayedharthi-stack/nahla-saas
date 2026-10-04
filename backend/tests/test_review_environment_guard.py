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
    return "catalog-review"


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
    assert check.failures == ()
    report = "\n".join(re_mod.format_report(check))
    assert "isolation verified" in report
    assert "s3cret-dsn-password" not in report and "review_user" not in report


def test_custom_host_marker_and_db_name_are_honoured():
    env = good_env(
        DATABASE_URL="postgresql://u:p@review-db.internal:5432/catalog_review",
        NAHLA_CATALOG_REVIEW_DB_HOST="review-db.internal",
        NAHLA_CATALOG_REVIEW_DB_NAME="catalog_review",
        NAHLA_CATALOG_REVIEW_DB_MARKER="catalog-review-2",
    )
    check = re_mod.evaluate_review_environment(env, marker_reader=lambda u: "catalog-review-2")
    assert check.ok, check.failures


# ── refused configurations ───────────────────────────────────────────────────

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
    assert "s3cret-dsn-password" not in text and "pw@" not in text


def test_database_url_presence_alone_is_not_isolation():
    """A DSN that merely exists (wrong host, no marker) must not pass."""
    env = good_env(DATABASE_URL="postgresql://u:p@postgres.railway.internal:5432/railway")
    check = re_mod.evaluate_review_environment(env, marker_reader=lambda u: None)
    assert not check.ok
    assert re_mod.F_DB_HOST_NOT_ALLOWLISTED in check.failures


def test_forbidden_host_is_never_connected_to():
    calls = []

    def spy(url):
        calls.append(url)
        return "catalog-review"

    env = good_env(DATABASE_URL=STAGING_DSN)
    check = re_mod.evaluate_review_environment(env, marker_reader=spy)
    assert not check.ok and calls == []


# ── database identity marker ─────────────────────────────────────────────────

def test_marker_missing_or_wrong_or_unreadable_is_refused():
    env = good_env()
    assert re_mod.F_DB_MARKER_MISSING in re_mod.evaluate_review_environment(env, marker_reader=lambda u: None).failures
    assert re_mod.F_DB_MARKER_MISSING in re_mod.evaluate_review_environment(env, marker_reader=lambda u: "").failures
    assert re_mod.F_DB_MARKER_MISMATCH in re_mod.evaluate_review_environment(env, marker_reader=lambda u: "staging").failures

    def boom(url):
        raise ConnectionError("refused host=" + url)

    check = re_mod.evaluate_review_environment(env, marker_reader=boom)
    assert re_mod.F_DB_MARKER_UNREADABLE in check.failures
    assert "s3cret-dsn-password" not in "\n".join(re_mod.format_report(check))


def test_marker_check_can_be_skipped_for_static_gates():
    check = re_mod.evaluate_review_environment(good_env(), with_database_marker=False, marker_reader=lambda u: None)
    assert check.ok


def test_marker_sql_is_a_read_only_current_setting_lookup():
    assert re_mod.DB_MARKER_SQL.strip().upper().startswith("SELECT CURRENT_SETTING('NAHLA.ENVIRONMENT', TRUE)")


def test_read_database_marker_against_sqlite_memory_reports_unreadable():
    """SQLite has no current_setting(): the reader raises → mapped to a code (no crash, no DSN leak)."""
    env = good_env(DATABASE_URL="sqlite://", NAHLA_CATALOG_REVIEW_DB_HOST="")
    failures = re_mod.check_database_marker(env)
    assert failures == [re_mod.F_DB_MARKER_UNREADABLE]


# ── preflight integration ────────────────────────────────────────────────────

def test_preflight_refuses_review_boot_when_isolation_fails(monkeypatch):
    for k, v in good_env(DATABASE_URL=STAGING_DSN).items():
        monkeypatch.setenv(k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = preflight_check.main()
    out = buf.getvalue()
    assert rc == 1
    assert re_mod.F_DB_HOST_FORBIDDEN in out
    assert "pw@" not in out and STAGING_DSN not in out


def test_preflight_passes_review_boot_when_isolated(monkeypatch):
    for k, v in good_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(re_mod, "read_database_marker", lambda url: "catalog-review")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = preflight_check.main()
    out = buf.getvalue()
    assert rc == 0, out
    assert "isolation verified" in out
    assert "s3cret-dsn-password" not in out


def test_preflight_unchanged_outside_review_mode(monkeypatch):
    monkeypatch.delenv("NAHLA_CATALOG_REVIEW_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = preflight_check.main()
    assert rc == 0
    assert "skipping production checks" in buf.getvalue()


def test_preflight_subprocess_refuses_review_without_marker():
    """End to end through the real entrypoint: wrong DSN never boots."""
    env = good_env(DATABASE_URL=PROD_LIKE_DSN)
    env["PATH"] = "/usr/bin:/bin"
    proc = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "preflight_check.py")],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing to boot" in proc.stdout
    assert "pw@" not in proc.stdout + proc.stderr


# ── bootstrap / scheduler wiring (source-level contract) ─────────────────────

def test_main_bootstrap_checks_isolation_before_any_migration_step():
    src = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
    i_guard = src.index("Step 0: catalog review environment isolation")
    i_step_a = src.index("Step A: cleanup_salla_duplicates")
    i_step_c = src.index("Step C: alembic upgrade")
    assert i_guard < i_step_a < i_step_c
    assert "no migration or create_all will run" in src
    assert "_review_workers_blocked" in src and "NOT queued — review environment isolation not proven" in src


def test_report_never_contains_values_only_codes():
    check = re_mod.evaluate_review_environment(good_env(), marker_reader=lambda u: "wrong")
    dumped = json.dumps(check.as_dict())
    for secret in ("s3cret-dsn-password", "review_user", REVIEW_DSN):
        assert secret not in dumped
