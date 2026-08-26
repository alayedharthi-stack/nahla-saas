"""Closed contract for normal bootstrap migration targets (multi-head aware).

Repository Alembic graph has parallel heads ``0092`` (A1-Validate branch) and
``0095`` (commerce lifecycle send_method extending ``0094`` â†’ ``0093``), both
branching from the ``0090`` / ``0091`` siblings off ``0088`` / ``0089``.

Normal application bootstrap must never invoke bare ``head`` â€” that would apply
both sibling heads and advance capability to ``validated`` unintentionally.
Integration and local bootstrap pin to ``0093`` explicitly so capability remains
``expand`` until the guarded staging Validate operator runs.

After staging attaches historical ``0089`` onto validated ``0088``,
``alembic_version`` holds **two rows** (``0088`` + ``0089``). Bootstrap upgrade
to ``0093`` advances only the integration branch and yields the supported
validated-staging state ``{0088, 0093}``; it must not select ``0092``.
"""
from __future__ import annotations

REPOSITORY_ALEMBIC_HEADS = frozenset({"0092", "0101"})
INTEGRATION_BOOTSTRAP_TARGET = "0093"
NORMAL_BOOTSTRAP_REVISIONS = frozenset({"0093"})
VALIDATED_STAGING_BOOTSTRAP_REVISIONS = frozenset({"0088", "0093"})
# Historical state produced by the guarded 0088â†’0089 attach operator before
# normal bootstrap advances the integration branch to 0093.
STAGING_VALIDATED_ATTACH_REVISIONS = frozenset({"0088", "0089"})
FORBIDDEN_BOOTSTRAP_LITERALS = frozenset({"head"})


def build_normal_bootstrap_upgrade_argv(*, python_executable: str) -> list[str]:
    if INTEGRATION_BOOTSTRAP_TARGET in FORBIDDEN_BOOTSTRAP_LITERALS:
        raise ValueError("bootstrap_target_forbidden")
    return [python_executable, "-m", "alembic", "upgrade", INTEGRATION_BOOTSTRAP_TARGET]

COEXISTENCE_NONCE_MIGRATION_TARGET = "0101"
COEXISTENCE_NONCE_TABLE = "whatsapp_oauth_nonces"


def build_coexistence_nonce_upgrade_argv(*, python_executable: str) -> list[str]:
    if COEXISTENCE_NONCE_MIGRATION_TARGET in FORBIDDEN_BOOTSTRAP_LITERALS:
        raise ValueError("bootstrap_target_forbidden")
    return [python_executable, "-m", "alembic", "upgrade", COEXISTENCE_NONCE_MIGRATION_TARGET]


def assert_coexistence_nonce_migration_applied(bind) -> None:
    from sqlalchemy import inspect

    if COEXISTENCE_NONCE_TABLE not in inspect(bind).get_table_names():
        raise RuntimeError(f"missing_table:{COEXISTENCE_NONCE_TABLE}")


def assert_coexistence_nonce_migration_missing(bind) -> None:
    from sqlalchemy import inspect

    if COEXISTENCE_NONCE_TABLE in inspect(bind).get_table_names():
        raise RuntimeError(f"unexpected_table:{COEXISTENCE_NONCE_TABLE}")
