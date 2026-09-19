"""One-off job: apply the commerce runtime schema (``0108`` + ``0109``) to a pilot database.

Run as a dedicated Railway one-off service, the way the repository already
applies a production migration: a service built from a pinned branch, with
``restartPolicyType: NEVER``, whose only variable is the pilot database's
``DATABASE_URL``.

    NAHLA_COMMERCE_RUNTIME_MIGRATION_CONFIRM=RUN_COMMERCE_RUNTIME_0109 \
    NAHLA_COMMERCE_RUNTIME_MIGRATION_TARGET=<host>/<database> \
        python -m scripts.operators.commerce_runtime_pilot_migration

Fail-closed at both ends. Before Alembic runs it asserts that the confirmation
token is present, that ``DATABASE_URL`` parses as a remote PostgreSQL database,
that it is **the database the operator authorised** — ``DATABASE_URL`` says
which database is configured, never which one was authorised — that the current
revision is one this contract accepts, and that the relations present are
exactly the ones that revision creates. After Alembic runs it asserts the target
revision and all nine relations, and only then prints ``RESULT=SUCCESS``.

A schema that does not match its own revision is refused rather than repaired:
the runtime itself fails closed on a half-present schema, and so does this. The
one intermediate state that *is* startable is revision ``0108``, which creates
the three foundation relations and none of the ledger six — that is the shape
that revision produces, not a half-applied one.

It adds nine tables and touches nothing else — no existing table is altered, no
data is read, written or backfilled. Every observation and the outcome are
printed with a single grep-able prefix.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.operators import commerce_runtime_pilot_migration_contract as k  # noqa: E402


def emit(message: str) -> None:
    print(f"{k.LOG_PREFIX} {message}", flush=True)


def result(marker: str, **observations: Any) -> None:
    body = " ".join(f"{name}={value!r}" for name, value in observations.items())
    emit(f"RESULT={marker} {body}".rstrip())


def _is_loopback(host: str) -> bool:
    """Whether a hostname names this machine, by name or by address.

    Addresses are compared as addresses, so the whole ``127.0.0.0/8`` range,
    ``::1`` in any spelling and the IPv4-mapped loopback are all covered, and a
    remote host whose *name* merely contains "localhost" is not.
    """
    import ipaddress  # noqa: PLC0415

    name = host.strip().strip("[]").lower()
    if not name:
        return False
    if name in k.LOOPBACK_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and (mapped.is_loopback or mapped.is_unspecified))


def parse_database_url(url: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(target, refusal_reason)`` for one database URL, parsed as a URL.

    Substring matching cannot answer any of the questions that matter here —
    which dialect this is, which host it names, whether that host is this
    machine — and gets each of them wrong in a different way: a password
    containing ``localhost`` refuses a real target, ``[::1]`` passes as a remote
    one, and ``sqlite:///x`` passes as a database this job can migrate.
    """
    from urllib.parse import unquote, urlsplit  # noqa: PLC0415

    raw = str(url or "").strip()
    if not raw:
        return None, "DATABASE_URL_unresolved"
    try:
        parts = urlsplit(raw)
    except Exception:  # noqa: BLE001 - an unparsable URL names no database
        return None, "DATABASE_URL_unparsable"
    scheme = (parts.scheme or "").lower()
    dialect = scheme.split("+", 1)[0]
    if not dialect:
        return None, "DATABASE_URL_unparsable"
    if dialect not in k.SUPPORTED_DIALECTS:
        return None, "DATABASE_URL_unsupported_dialect"
    try:
        host = parts.hostname or ""
    except ValueError:
        return None, "DATABASE_URL_unparsable"
    if not host:
        return None, "DATABASE_URL_host_missing"
    if _is_loopback(host):
        return None, "DATABASE_URL_is_local"
    database = unquote((parts.path or "").lstrip("/"))
    if not database:
        return None, "DATABASE_URL_database_missing"
    try:
        port = parts.port
    except ValueError:
        return None, "DATABASE_URL_unparsable"
    return {"dialect": dialect, "host": host.lower(), "port": port, "database": database}, None


def authorized_target(environ: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``("<host>/<database>", None)`` — the one database this run may touch.

    The operator states it explicitly. ``DATABASE_URL`` says which database is
    *configured* in this service; it can never say which one was *authorised*,
    and this job exists precisely because those two must be checked against each
    other before any schema changes.
    """
    env = environ if environ is not None else os.environ
    declared = str(env.get(k.TARGET_ENV, "") or "").strip()
    if not declared:
        return None, "authorized_target_not_declared"
    if declared.count("/") != 1 or declared.startswith("/") or declared.endswith("/"):
        return None, "authorized_target_malformed"
    return declared.lower(), None


def database_url(environ: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(url, refusal_reason)``. The URL is parsed, and it must be the target."""
    env = environ if environ is not None else os.environ
    url = str(env.get("DATABASE_URL", "") or "").strip()
    target, refusal = parse_database_url(url)
    if target is None:
        return None, refusal
    declared, refusal = authorized_target(env)
    if declared is None:
        return None, refusal
    observed = f"{target['host']}/{target['database']}".lower()
    if observed != declared:
        emit(f"authorized_target={declared!r} observed_target={observed!r}")
        return None, "DATABASE_URL_is_not_the_authorized_target"
    return url, None


def confirmed(environ: Optional[Dict[str, str]] = None) -> bool:
    env = environ if environ is not None else os.environ
    return str(env.get(k.CONFIRMATION_ENV, "") or "").strip() == k.CONFIRMATION_TOKEN


def observe(url: str) -> Dict[str, Any]:
    """The database's own account of where it stands. Reads nothing else."""
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    engine = sa.create_engine(url, poolclass=NullPool, future=True)
    try:
        with engine.connect() as conn:
            has_version = conn.execute(
                sa.text("SELECT to_regclass(:t)"), {"t": "public.alembic_version"}
            ).scalar()
            revisions = (
                frozenset(
                    str(row[0])
                    for row in conn.execute(sa.text("SELECT version_num FROM alembic_version"))
                )
                if has_version
                else frozenset()
            )
            present = tuple(
                name
                for name in k.RUNTIME_RELATIONS
                if conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": f"public.{name}"}).scalar()
            )
    finally:
        engine.dispose()
    missing = tuple(name for name in k.RUNTIME_RELATIONS if name not in present)
    return {"alembic_version": tuple(sorted(revisions)), "present": present, "missing": missing}


def classify(observation: Dict[str, Any]) -> str:
    """``fresh``, ``foundation``, ``complete`` or ``partial``.

    ``foundation`` is the shape of a database at revision ``0108``: the three
    foundation relations and none of the ledger six. It is a known revision on
    the way to the target, not a half-applied schema, and the observed relations
    are checked against what that revision actually creates rather than against
    "some are missing".
    """
    present = tuple(observation["present"])
    if not observation["missing"]:
        return "complete"
    if not present:
        return "fresh"
    if present == k.FOUNDATION_RELATIONS:
        return "foundation"
    return "partial"


def run_alembic(*, timeout_seconds: int, cwd: str) -> int:
    command = k.build_upgrade_argv(python_executable=sys.executable)
    emit(f"running: {' '.join(command)} (cwd={cwd}, timeout={timeout_seconds}s)")
    try:
        completed = subprocess.run(command, cwd=cwd, check=False, env=os.environ.copy(),
                                   timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        emit(f"alembic upgrade {k.TARGET_REVISION} TIMED OUT after {timeout_seconds}s")
        return 124
    emit(f"alembic upgrade {k.TARGET_REVISION} exited rc={completed.returncode}")
    return int(completed.returncode)


def database_directory() -> Optional[str]:
    for root in ("/app", _REPO_ROOT):
        candidate = os.path.join(root, "database")
        if os.path.isdir(candidate):
            return candidate
    return None


def main(argv: Optional[list] = None) -> int:
    _ = argv
    emit(
        f"commit={os.environ.get('RAILWAY_GIT_COMMIT_SHA', 'unknown')} "
        f"service={os.environ.get('RAILWAY_SERVICE_NAME', 'unknown')} "
        f"env={os.environ.get('RAILWAY_ENVIRONMENT_NAME', 'unknown')}"
    )
    if not confirmed():
        result(k.RESULT_FAILED_PRECONDITION,
               reason=f"set {k.CONFIRMATION_ENV}={k.CONFIRMATION_TOKEN} to run this job")
        return k.EXIT_USAGE
    url, refusal = database_url()
    if url is None:
        result(k.RESULT_FAILED_PRECONDITION, reason=refusal)
        return k.EXIT_USAGE
    directory = database_directory()
    if directory is None:
        result(k.RESULT_FAILED_PRECONDITION, reason="database_directory_not_found")
        return k.EXIT_USAGE
    timeout_seconds = k.clamp_timeout(os.environ.get("NAHLA_COMMERCE_RUNTIME_MIGRATION_TIMEOUT"))

    before = observe(url)
    shape = classify(before)
    emit(f"BEFORE alembic_version={before['alembic_version']} shape={shape} "
         f"present={len(before['present'])}/{len(k.RUNTIME_RELATIONS)}")

    revisions = frozenset(before["alembic_version"])
    if shape == "complete" and k.already_applied(revisions):
        result(k.RESULT_ALREADY_APPLIED, alembic_version=before["alembic_version"])
        return k.EXIT_SUCCESS
    if not k.start_state_accepted(revisions):
        result(k.RESULT_FAILED_PRECONDITION, reason="unexpected_start_revision",
               observed=before["alembic_version"],
               accepted=[tuple(sorted(s)) for s in k.ACCEPTED_START_REVISIONS])
        return k.EXIT_PRECONDITION
    # The relations a database at this revision must already have, which is what
    # makes ``0108`` a startable state instead of a permanently refused one.
    expected = k.expected_relations_at(revisions)
    if tuple(before["present"]) != expected:
        result(k.RESULT_FAILED_PRECONDITION, reason="unexpected_runtime_schema_for_revision",
               alembic_version=before["alembic_version"], shape=shape,
               present=before["present"], expected=list(expected))
        return k.EXIT_PRECONDITION

    rc = run_alembic(timeout_seconds=timeout_seconds, cwd=directory)

    after = observe(url)
    emit(f"AFTER alembic_version={after['alembic_version']} shape={classify(after)} "
         f"present={len(after['present'])}/{len(k.RUNTIME_RELATIONS)}")
    if rc != 0 or k.TARGET_REVISION not in after["alembic_version"] or after["missing"]:
        result(k.RESULT_FAILED, upgrade_rc=rc, alembic_version=after["alembic_version"],
               missing=after["missing"])
        return k.EXIT_FAILED
    result(k.RESULT_SUCCESS, alembic_version=after["alembic_version"],
           relations=len(after["present"]))
    return k.EXIT_SUCCESS


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
