"""
core/review_environment.py
──────────────────────────
Isolation guard for the **catalog_management review environment**
(``nahla-catalog-review-api`` / ``nahla-catalog-review-web`` /
``postgres-catalog-review`` inside the Railway project ``desirable-growth``,
environment ``staging``).

Why
───
The review environment runs the catalog branch against Meta with a real
token. It must never be able to reach the production database, the
staging DR database (``postgres-staging``) or the production dashboard,
and the mere presence of ``DATABASE_URL`` is not evidence of isolation.

What this module proves before anything else runs
──────────────────────────────────────────────────
When ``NAHLA_CATALOG_REVIEW_ENV`` is truthy the deploy is a review
environment and **every** check below must pass, otherwise the caller
refuses to boot (preflight), refuses to run migrations and refuses to
start background workers:

  1. Environment identity: ``RAILWAY_PROJECT_NAME`` and
     ``RAILWAY_ENVIRONMENT_NAME`` equal the expected review project /
     environment, and no production marker appears in ``ENVIRONMENT``.
  2. Database binding (static, from the DSN only — never printed):
     ``DATABASE_URL`` present, PostgreSQL scheme, host equal to the
     expected review database host, host free of production markers and
     of the staging DR host, database name equal to the expected name
     when one is configured.
  3. Database identity (dynamic, one read-only statement). The operator
     marks the fresh review database **once**::

         ALTER DATABASE <review_db> SET nahla.environment = 'catalog-review';

     The guard then requires all of:

       * a **persisted, database-wide** entry ``nahla.environment=<marker>``
         in ``pg_db_role_setting`` for ``current_database()`` with
         ``setrole = 0`` (this is what ``ALTER DATABASE … SET`` writes);
       * the **effective** value ``current_setting('nahla.environment',
         true)`` equal to the same marker (a role-level override or a
         session value that differs is refused);
       * ``current_database()`` equal to the database named in the DSN
         (or ``NAHLA_CATALOG_REVIEW_DB_NAME``).

     Why not ``pg_settings``: PostgreSQL keeps an unregistered custom
     setting such as ``nahla.environment`` as a *placeholder*, and
     placeholders are not listed in ``pg_settings`` (verified on
     PostgreSQL 16.15 and 18.6: zero rows even when the database-level
     setting is applied). A session value (``options=-c …``,
     ``PGOPTIONS``, ``SET``) changes only the effective value and leaves
     no ``pg_db_role_setting`` row, so it can never satisfy the
     persisted-entry requirement. ``pg_db_role_setting`` is readable by
     ordinary roles. A database that was never marked (production,
     staging DR, a local dev database) has no entry, so a wrong binding
     is caught **before** Alembic or ``create_all`` touch the schema.
  4. Dashboard URL: ``DASHBOARD_URL`` present and not the production
     dashboard, so invite / verification / reset links and OAuth
     redirects never point at production.

Outside review mode the module is inert (``evaluate`` returns ok with
``enabled=False``) — production and local behaviour is unchanged.

Secrets: this module never logs, prints or returns the DSN, its
password, or any token. Failures are reported as stable codes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional, Tuple

REVIEW_ENV_FLAG = "NAHLA_CATALOG_REVIEW_ENV"
REVIEW_DB_HOST_ENV = "NAHLA_CATALOG_REVIEW_DB_HOST"
REVIEW_DB_NAME_ENV = "NAHLA_CATALOG_REVIEW_DB_NAME"
REVIEW_DB_MARKER_ENV = "NAHLA_CATALOG_REVIEW_DB_MARKER"
REVIEW_PROJECT_ENV = "NAHLA_CATALOG_REVIEW_PROJECT"
REVIEW_ENVIRONMENT_ENV = "NAHLA_CATALOG_REVIEW_ENVIRONMENT"

DEFAULT_REVIEW_PROJECT = "desirable-growth"
DEFAULT_REVIEW_ENVIRONMENT = "staging"
DEFAULT_REVIEW_DB_HOST = "postgres-catalog-review.railway.internal"
DEFAULT_REVIEW_DB_MARKER = "catalog-review"

#: PostgreSQL custom setting that carries the review-database marker.
DB_MARKER_SETTING = "nahla.environment"
#: One read-only statement: effective value, the persisted database-wide
#: entry written by ``ALTER DATABASE … SET`` (``pg_db_role_setting`` row with
#: ``setrole = 0`` for the current database), and the database name.
DB_MARKER_SQL = (
    "SELECT current_setting('nahla.environment', true) AS effective, "
    "(SELECT cfg FROM pg_catalog.pg_db_role_setting AS d, "
    "unnest(d.setconfig) AS cfg "
    "WHERE d.setdatabase = (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()) "
    "AND d.setrole = 0 "
    "AND split_part(cfg, '=', 1) = 'nahla.environment' "
    "LIMIT 1) AS persisted, "
    "current_database() AS database"
)

_PRODUCTION_MARKERS = ("production", "prod", "live")
_FORBIDDEN_DB_HOST_FRAGMENTS = ("postgres-staging",)
_PRODUCTION_DASHBOARD_HOSTS = ("app.nahlah.ai", "www.nahlah.ai", "nahlah.ai")
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgresql+psycopg2", "postgresql+psycopg", "postgres"})
#: DSN query parameters that may appear. Anything else (``host``, ``hostaddr``,
#: ``options``, ``service``, ``passfile``, ``dbname`` …) can redirect the connection
#: or forge the session marker and is refused.
_ALLOWED_DSN_QUERY_KEYS = frozenset({"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name"})
#: libpq environment variables that override the DSN host/options. Refused in review mode.
_FORBIDDEN_LIBPQ_ENV = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE", "PGDATABASE")

# Stable failure codes (never carry values).
F_PROJECT_MISSING = "review_project_missing"
F_PROJECT_MISMATCH = "review_project_mismatch"
F_ENVIRONMENT_MISSING = "review_environment_missing"
F_ENVIRONMENT_MISMATCH = "review_environment_mismatch"
F_PRODUCTION_MARKER = "production_marker_detected"
F_DB_URL_MISSING = "database_url_missing"
F_DB_URL_MALFORMED = "database_url_malformed"
F_DB_SCHEME = "database_scheme_rejected"
F_DB_HOST_MISSING = "database_host_missing"
F_DB_HOST_FORBIDDEN = "database_host_forbidden"
F_DB_HOST_NOT_ALLOWLISTED = "database_host_not_allowlisted"
F_DB_NAME_MISMATCH = "database_name_mismatch"
F_DB_QUERY_REJECTED = "database_url_query_rejected"
F_LIBPQ_ENV_OVERRIDE = "libpq_environment_override"
F_DB_IDENTITY_MISMATCH = "database_identity_mismatch"
F_DB_MARKER_UNREADABLE = "database_marker_unreadable"
#: No persisted database-wide marker and no effective value either.
F_DB_MARKER_MISSING = "database_marker_missing"
#: An effective value exists but no persisted database-wide entry backs it
#: (session ``SET`` / ``options=-c`` / ``PGOPTIONS`` / role-level setting).
F_DB_MARKER_NOT_PERSISTED = "database_marker_not_persisted"
#: The persisted database-wide entry carries a different marker.
F_DB_MARKER_MISMATCH = "database_marker_mismatch"
#: The effective value differs from the persisted marker (role/session override).
F_DB_MARKER_EFFECTIVE_MISMATCH = "database_marker_effective_mismatch"
F_DASHBOARD_URL_MISSING = "dashboard_url_missing"
F_DASHBOARD_URL_PRODUCTION = "dashboard_url_is_production"


class ReviewEnvironmentViolation(RuntimeError):
    """Raised by ``assert_review_environment`` when any check fails."""

    def __init__(self, failures: List[str]):
        super().__init__("catalog review environment isolation failed: " + ", ".join(failures))
        self.failures = list(failures)


@dataclass(frozen=True)
class ReviewEnvironmentCheck:
    enabled: bool
    ok: bool
    failures: Tuple[str, ...] = ()
    #: Human-readable, secret-free notes (expected host / marker names).
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "failures": list(self.failures),
            "notes": list(self.notes),
        }


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def review_env_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = env if env is not None else os.environ
    return _truthy(env.get(REVIEW_ENV_FLAG))


def expected_db_host(env: Optional[Mapping[str, str]] = None) -> str:
    env = env if env is not None else os.environ
    return (env.get(REVIEW_DB_HOST_ENV) or DEFAULT_REVIEW_DB_HOST).strip().lower()


def expected_db_marker(env: Optional[Mapping[str, str]] = None) -> str:
    env = env if env is not None else os.environ
    return (env.get(REVIEW_DB_MARKER_ENV) or DEFAULT_REVIEW_DB_MARKER).strip()


def _check_identity(env: Mapping[str, str]) -> List[str]:
    failures: List[str] = []
    want_project = (env.get(REVIEW_PROJECT_ENV) or DEFAULT_REVIEW_PROJECT).strip()
    want_environment = (env.get(REVIEW_ENVIRONMENT_ENV) or DEFAULT_REVIEW_ENVIRONMENT).strip().lower()
    project = (env.get("RAILWAY_PROJECT_NAME") or "").strip()
    environment = (env.get("RAILWAY_ENVIRONMENT_NAME") or "").strip().lower()
    generic = (env.get("ENVIRONMENT") or "").strip().lower()

    if not project:
        failures.append(F_PROJECT_MISSING)
    elif project != want_project:
        failures.append(F_PROJECT_MISMATCH)
    if not environment:
        failures.append(F_ENVIRONMENT_MISSING)
    elif environment != want_environment:
        failures.append(F_ENVIRONMENT_MISMATCH)
    if any(m in environment for m in _PRODUCTION_MARKERS) or any(m in generic for m in _PRODUCTION_MARKERS):
        failures.append(F_PRODUCTION_MARKER)
    return failures


def _parse_dsn(raw_url: str) -> Optional[Any]:
    try:
        from sqlalchemy.engine.url import make_url  # noqa: PLC0415

        return make_url(raw_url)
    except Exception:  # noqa: silent-ok — malformed DSN is reported to the caller as database_url_malformed; the DSN is never echoed
        return None


def check_database_binding(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Static DSN checks. Never returns or logs the DSN."""
    env = env if env is not None else os.environ
    failures: List[str] = []
    raw = (env.get("DATABASE_URL") or "").strip()
    if not raw:
        return [F_DB_URL_MISSING]
    parsed = _parse_dsn(raw)
    if parsed is None:
        return [F_DB_URL_MALFORMED]
    if str(parsed.drivername or "").lower() not in _POSTGRES_SCHEMES:
        failures.append(F_DB_SCHEME)
    host = str(parsed.host or "").strip().lower()
    if not host:
        failures.append(F_DB_HOST_MISSING)
        return failures
    if any(frag in host for frag in _FORBIDDEN_DB_HOST_FRAGMENTS) or any(m in host for m in _PRODUCTION_MARKERS):
        failures.append(F_DB_HOST_FORBIDDEN)
    if host != expected_db_host(env):
        failures.append(F_DB_HOST_NOT_ALLOWLISTED)
    want_name = (env.get(REVIEW_DB_NAME_ENV) or "").strip()
    if want_name and str(parsed.database or "").strip() != want_name:
        failures.append(F_DB_NAME_MISMATCH)
    query = dict(getattr(parsed, "query", {}) or {})
    if any(str(k).lower() not in _ALLOWED_DSN_QUERY_KEYS for k in query):
        failures.append(F_DB_QUERY_REJECTED)
    if any((env.get(name) or "").strip() for name in _FORBIDDEN_LIBPQ_ENV):
        failures.append(F_LIBPQ_ENV_OVERRIDE)
    return failures


def _check_dashboard_url(env: Mapping[str, str]) -> List[str]:
    from urllib.parse import urlparse  # noqa: PLC0415

    raw = (env.get("DASHBOARD_URL") or "").strip()
    if not raw:
        return [F_DASHBOARD_URL_MISSING]
    try:
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    if not host or host in _PRODUCTION_DASHBOARD_HOSTS:
        return [F_DASHBOARD_URL_PRODUCTION]
    return []


@dataclass(frozen=True)
class MarkerReading:
    """What the connected database says about itself (no secrets).

    ``effective``: ``current_setting('nahla.environment', true)``.
    ``persisted``: value of the database-wide ``pg_db_role_setting`` entry
    (``setrole = 0``) for ``current_database()``, or None when absent.
    ``database``: ``current_database()``.
    """

    effective: Optional[str]
    persisted: Optional[str]
    database: Optional[str]


def _persisted_value(entry: Any) -> Optional[str]:
    """``'nahla.environment=catalog-review'`` → ``'catalog-review'``."""
    text_value = str(entry or "").strip()
    if not text_value:
        return None
    name, sep, value = text_value.partition("=")
    if not sep or name.strip() != DB_MARKER_SETTING:
        return None
    return value.strip() or None


def read_database_marker(database_url: str) -> MarkerReading:
    """Read the effective marker, the persisted database-wide entry and ``current_database()``.

    One read-only statement on a throw-away connection. Raises on connection
    failure (the caller maps that to ``database_marker_unreadable``).
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    engine = create_engine(database_url, poolclass=NullPool, future=True, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as conn:
            row = conn.execute(text(DB_MARKER_SQL)).first()
    finally:
        engine.dispose()
    if row is None:
        return MarkerReading(None, None, None)
    effective = str(row[0] or "").strip() or None
    persisted = _persisted_value(row[1])
    database = str(row[2] or "").strip() or None
    return MarkerReading(effective, persisted, database)


def _coerce_reading(value: Any) -> MarkerReading:
    """Only a ``MarkerReading`` carries proof. Anything else counts as no marker."""
    if isinstance(value, MarkerReading):
        return value
    return MarkerReading(None, None, None)


def check_database_marker(
    env: Optional[Mapping[str, str]] = None,
    *,
    marker_reader: Optional[Callable[[str], Any]] = None,
) -> List[str]:
    """Dynamic identity check against the connected database (fail-closed)."""
    env = env if env is not None else os.environ
    raw = (env.get("DATABASE_URL") or "").strip()
    if not raw:
        return [F_DB_URL_MISSING]
    reader = marker_reader or read_database_marker
    try:
        reading = _coerce_reading(reader(raw))
    except Exception:  # noqa: BLE001 — connection errors are reported as a code; the DSN is never echoed
        return [F_DB_MARKER_UNREADABLE]
    expected = expected_db_marker(env)
    failures: List[str] = []
    if not reading.persisted:
        # A value that only lives in the session (or a role setting) is not proof.
        failures.append(F_DB_MARKER_NOT_PERSISTED if reading.effective else F_DB_MARKER_MISSING)
    elif reading.persisted != expected:
        failures.append(F_DB_MARKER_MISMATCH)
    if reading.persisted and reading.effective != reading.persisted:
        failures.append(F_DB_MARKER_EFFECTIVE_MISMATCH)
    want_name = (env.get(REVIEW_DB_NAME_ENV) or "").strip()
    parsed = _parse_dsn(raw)
    dsn_name = str(getattr(parsed, "database", "") or "").strip() if parsed is not None else ""
    expected_name = want_name or dsn_name
    if not reading.database or not expected_name or reading.database != expected_name:
        failures.append(F_DB_IDENTITY_MISMATCH)
    return failures


def evaluate_review_environment(
    env: Optional[Mapping[str, str]] = None,
    *,
    with_database_marker: bool = True,
    marker_reader: Optional[Callable[[str], Any]] = None,
) -> ReviewEnvironmentCheck:
    """Run every check. Inert (ok, enabled=False) when review mode is off."""
    env = env if env is not None else os.environ
    if not review_env_enabled(env):
        return ReviewEnvironmentCheck(enabled=False, ok=True)

    failures: List[str] = []
    failures += _check_identity(env)
    binding = check_database_binding(env)
    failures += binding
    # Only touch the database when the static binding is acceptable; a
    # forbidden host must never even be connected to.
    if with_database_marker and not binding:
        failures += check_database_marker(env, marker_reader=marker_reader)
    failures += _check_dashboard_url(env)

    notes = (
        f"expected_db_host={expected_db_host(env)}",
        f"expected_db_marker={DB_MARKER_SETTING}={expected_db_marker(env)}",
        f"expected_identity={(env.get(REVIEW_PROJECT_ENV) or DEFAULT_REVIEW_PROJECT)}/"
        f"{(env.get(REVIEW_ENVIRONMENT_ENV) or DEFAULT_REVIEW_ENVIRONMENT)}",
    )
    # Deduplicate while keeping order.
    seen: set = set()
    ordered = [f for f in failures if not (f in seen or seen.add(f))]
    return ReviewEnvironmentCheck(enabled=True, ok=not ordered, failures=tuple(ordered), notes=notes)


def assert_review_environment(
    env: Optional[Mapping[str, str]] = None,
    *,
    with_database_marker: bool = True,
    marker_reader: Optional[Callable[[str], Any]] = None,
) -> ReviewEnvironmentCheck:
    """Raise ``ReviewEnvironmentViolation`` unless the environment is isolated.

    Returns the check (enabled=False outside review mode) so callers can log it.
    """
    check = evaluate_review_environment(env, with_database_marker=with_database_marker, marker_reader=marker_reader)
    if check.enabled and not check.ok:
        raise ReviewEnvironmentViolation(list(check.failures))
    return check


def format_report(check: ReviewEnvironmentCheck) -> List[str]:
    """Operator-facing lines. Contains codes and expectations only."""
    if not check.enabled:
        return ["[review-env] not a catalog review environment (flag unset) — guard inert."]
    lines = [f"[review-env] {n}" for n in check.notes]
    if check.ok:
        lines.append("[review-env] isolation verified: identity, database binding, persisted database marker (pg_db_role_setting), dashboard URL.")
    else:
        for code in check.failures:
            lines.append(f"[review-env][FAIL] {code}")
        lines.append(
            "[review-env] refusing to proceed. Fix the variables on the review service only; "
            "mark the review database once with: "
            "ALTER DATABASE <review_db> SET nahla.environment = 'catalog-review';"
        )
    return lines


__all__ = [
    "DB_MARKER_SETTING",
    "DB_MARKER_SQL",
    "MarkerReading",
    "DEFAULT_REVIEW_DB_HOST",
    "DEFAULT_REVIEW_DB_MARKER",
    "DEFAULT_REVIEW_ENVIRONMENT",
    "DEFAULT_REVIEW_PROJECT",
    "REVIEW_DB_HOST_ENV",
    "REVIEW_DB_MARKER_ENV",
    "REVIEW_DB_NAME_ENV",
    "REVIEW_ENV_FLAG",
    "ReviewEnvironmentCheck",
    "ReviewEnvironmentViolation",
    "assert_review_environment",
    "check_database_binding",
    "check_database_marker",
    "evaluate_review_environment",
    "expected_db_host",
    "expected_db_marker",
    "format_report",
    "read_database_marker",
    "review_env_enabled",
]
