"""Closed contract for applying the commerce runtime schema to a pilot database.

Revisions ``0108`` (foundation) and ``0109`` (effect and delivery ledgers) are
merged and their reconciliation is proven on real PostgreSQL, but the normal
bootstrap target stays pinned at ``0093``: production applies nothing until an
operator runs this job deliberately. Nothing in the application can advance the
schema past the pinned target, and that is on purpose.

This contract states exactly what the job may do:

* it **adds nine new tables** and nothing else — no column is added to an
  existing table, no existing index or constraint is changed, and no row of
  existing data is read, written or backfilled;
* it refuses unless the database is at an accepted starting revision and the
  nine relations are either all absent (a fresh apply) or all present at the
  target (a no-op re-run);
* a partial state is refused rather than repaired, because a half-present
  schema is exactly what the runtime itself fails closed on.

The job is fail-closed in both directions: it asserts its preconditions before
Alembic runs and asserts the resulting state afterwards, and it reports
``RESULT=SUCCESS`` only when both hold.
"""
from __future__ import annotations

from typing import Tuple

TARGET_REVISION = "0109"
FOUNDATION_REVISION = "0108"

# The integration branch states this job accepts as a starting point. Anything
# else — including a state this repository has not seen — is refused with the
# observed value printed, so an operator decides rather than a script guessing.
ACCEPTED_START_REVISIONS: Tuple[frozenset, ...] = (
    frozenset({"0107"}),
    frozenset({"0088", "0107"}),
    frozenset({FOUNDATION_REVISION}),
    frozenset({"0088", FOUNDATION_REVISION}),
)
ALREADY_APPLIED_REVISIONS: Tuple[frozenset, ...] = (
    frozenset({TARGET_REVISION}),
    frozenset({"0088", TARGET_REVISION}),
)

FOUNDATION_RELATIONS: Tuple[str, ...] = (
    "commerce_runtime_conversations",
    "commerce_runtime_turns",
    "commerce_runtime_turn_terminals",
)
LEDGER_RELATIONS: Tuple[str, ...] = (
    "commerce_runtime_effects",
    "commerce_runtime_effect_attempts",
    "commerce_runtime_effect_results",
    "commerce_runtime_delivery_sequences",
    "commerce_runtime_delivery_attempts",
    "commerce_runtime_delivery_receipts",
)
RUNTIME_RELATIONS: Tuple[str, ...] = FOUNDATION_RELATIONS + LEDGER_RELATIONS

CONFIRMATION_ENV = "NAHLA_COMMERCE_RUNTIME_MIGRATION_CONFIRM"
CONFIRMATION_TOKEN = "RUN_COMMERCE_RUNTIME_0109"

DEFAULT_TIMEOUT_SEC = 900
MIN_TIMEOUT_SEC = 120
MAX_TIMEOUT_SEC = 3600

LOG_PREFIX = "[commerce-runtime-0109]"

RESULT_SUCCESS = "SUCCESS"
RESULT_ALREADY_APPLIED = "ALREADY_APPLIED"
RESULT_FAILED_PRECONDITION = "FAILED_PRECONDITION"
RESULT_FAILED = "FAILED"

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_FAILED = 4

# Local databases are never the pilot database. A URL pointing at one is a
# misconfiguration, not a target.
FORBIDDEN_HOST_MARKERS: Tuple[str, ...] = ("@localhost", "@127.0.0.1", "@::1", "@0.0.0.0")


def start_state_accepted(revisions: frozenset) -> bool:
    return revisions in ACCEPTED_START_REVISIONS


def already_applied(revisions: frozenset) -> bool:
    return revisions in ALREADY_APPLIED_REVISIONS


def build_upgrade_argv(*, python_executable: str) -> list:
    """The one Alembic command this job may run. Never ``head``."""
    if TARGET_REVISION == "head":
        raise ValueError("target_revision_forbidden")
    return [python_executable, "-m", "alembic", "upgrade", TARGET_REVISION]


def clamp_timeout(seconds: object) -> int:
    try:
        value = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SEC
    return max(MIN_TIMEOUT_SEC, min(value, MAX_TIMEOUT_SEC))


__all__ = [
    "ACCEPTED_START_REVISIONS", "ALREADY_APPLIED_REVISIONS", "CONFIRMATION_ENV",
    "CONFIRMATION_TOKEN", "DEFAULT_TIMEOUT_SEC", "EXIT_FAILED", "EXIT_PRECONDITION",
    "EXIT_SUCCESS", "EXIT_USAGE", "FORBIDDEN_HOST_MARKERS", "FOUNDATION_RELATIONS",
    "FOUNDATION_REVISION", "LEDGER_RELATIONS", "LOG_PREFIX", "MAX_TIMEOUT_SEC",
    "MIN_TIMEOUT_SEC", "RESULT_ALREADY_APPLIED", "RESULT_FAILED", "RESULT_FAILED_PRECONDITION",
    "RESULT_SUCCESS", "RUNTIME_RELATIONS", "TARGET_REVISION", "already_applied",
    "build_upgrade_argv", "clamp_timeout", "start_state_accepted",
]
