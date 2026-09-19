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

# The only dialect this job knows how to migrate. A URL naming anything else is
# refused rather than handed to Alembic to find out. The legacy ``postgres://``
# spelling is deliberately not here: SQLAlchemy 2 refuses it outright, so
# accepting it would only move the failure later and less clearly.
SUPPORTED_DIALECTS: Tuple[str, ...] = ("postgresql",)

# Hostnames that are never the pilot database, whatever they are spelled like.
# The numeric forms are matched as addresses, not as text, so the whole
# 127.0.0.0/8 range and every spelling of the IPv6 loopback are covered.
LOOPBACK_HOSTNAMES: Tuple[str, ...] = ("localhost", "localhost.localdomain", "ip6-localhost")

# The operator names the database this run is allowed to touch, as
# ``host[:port]/database``. Without it the job refuses: ``DATABASE_URL`` alone
# says which database is *configured*, never which one was *authorised*.
TARGET_ENV = "NAHLA_COMMERCE_RUNTIME_MIGRATION_TARGET"

DEFAULT_PORT = 5432

# Query parameters the driver reads as connection parameters. Any one of them
# silently moves the connection somewhere other than the URL's own authority —
# ``?host=other.internal`` on a URL naming ``approved.internal`` connects to
# ``other.internal`` — so a URL carrying one is refused outright rather than
# reconciled. The effective parameters are checked as well; this makes the
# intent explicit and closes the ones a check could model wrongly.
TARGET_OVERRIDE_QUERY_KEYS: Tuple[str, ...] = (
    "host", "hostaddr", "port", "dbname", "database", "service", "servicefile",
    "passfile", "target_session_attrs",
)


def expected_relations_at(revisions: frozenset) -> Tuple[str, ...]:
    """The relations a database at an accepted starting revision must already have.

    ``0107`` predates the runtime entirely, so none of the nine may exist.
    ``0108`` *is* the foundation revision, so exactly its three must exist and
    none of the ledger six. Without this the two rules contradicted each other:
    ``0108`` was an accepted start, and a database at ``0108`` was then refused
    as a partial schema, so that start could never proceed.
    """
    if FOUNDATION_REVISION in revisions:
        return FOUNDATION_RELATIONS
    return ()


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
    "EXIT_SUCCESS", "EXIT_USAGE", "FOUNDATION_RELATIONS",
    "FOUNDATION_REVISION", "LEDGER_RELATIONS", "LOG_PREFIX", "LOOPBACK_HOSTNAMES",
    "MAX_TIMEOUT_SEC",
    "MIN_TIMEOUT_SEC", "RESULT_ALREADY_APPLIED", "RESULT_FAILED", "RESULT_FAILED_PRECONDITION",
    "RESULT_SUCCESS", "RUNTIME_RELATIONS", "SUPPORTED_DIALECTS", "TARGET_ENV",
    "TARGET_REVISION", "already_applied",
    "build_upgrade_argv", "clamp_timeout", "expected_relations_at", "start_state_accepted",
]
