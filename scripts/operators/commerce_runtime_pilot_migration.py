"""One-off job: apply the commerce runtime schema (``0108`` + ``0109``) to a pilot database.

Run as a dedicated Railway one-off service, the way the repository already
applies a production migration: a service built from a pinned branch, with
``restartPolicyType: NEVER``, whose only variable is the pilot database's
``DATABASE_URL``.

    NAHLA_COMMERCE_RUNTIME_MIGRATION_CONFIRM=RUN_COMMERCE_RUNTIME_0109 \
        python -m scripts.operators.commerce_runtime_pilot_migration

Fail-closed at both ends. Before Alembic runs it asserts that the database is
not a local one, that the confirmation token is present, that the current
revision is one this contract accepts, and that the nine runtime relations are
either all absent or all present. After Alembic runs it asserts the target
revision and all nine relations, and only then prints ``RESULT=SUCCESS``. A
partial schema is refused rather than repaired: the runtime itself fails closed
on a half-present schema, and so does this.

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


def database_url(environ: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(url, refusal_reason)``. A local database is never the pilot database."""
    env = environ if environ is not None else os.environ
    url = str(env.get("DATABASE_URL", "") or "").strip()
    if not url:
        return None, "DATABASE_URL_unresolved"
    for marker in k.FORBIDDEN_HOST_MARKERS:
        if marker in url:
            return None, "DATABASE_URL_is_local"
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
    """``fresh``, ``complete`` or ``partial`` for the nine runtime relations."""
    if not observation["present"]:
        return "fresh"
    if not observation["missing"]:
        return "complete"
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
    if shape == "partial":
        result(k.RESULT_FAILED_PRECONDITION, reason="partial_runtime_schema",
               present=before["present"], missing=before["missing"])
        return k.EXIT_PRECONDITION
    if not k.start_state_accepted(revisions):
        result(k.RESULT_FAILED_PRECONDITION, reason="unexpected_start_revision",
               observed=before["alembic_version"],
               accepted=[tuple(sorted(s)) for s in k.ACCEPTED_START_REVISIONS])
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
