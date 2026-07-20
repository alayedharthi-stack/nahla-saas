"""Closed contract for normal bootstrap migration targets (multi-head aware).

Repository Alembic graph has parallel heads ``0090`` (A1-Validate branch) and
``0091`` (conversation A1-subject bindings branch), both branching from the
``0088`` / ``0089`` siblings off ``0087``.

Normal application bootstrap must never invoke bare ``head`` — that would apply
both sibling heads and advance capability to ``validated`` unintentionally.
Integration and local bootstrap pin to ``0091`` explicitly so capability remains
``expand`` until the guarded staging Validate operator runs.

After staging attaches ``0089`` onto validated ``0088``, ``alembic_version`` holds
**two rows** (``0088`` + ``0089``). Bootstrap upgrade to ``0091`` is idempotent and
must not downgrade or re-run ``0088`` / ``0090``.
"""
from __future__ import annotations

REPOSITORY_ALEMBIC_HEADS = frozenset({"0090", "0091"})
INTEGRATION_BOOTSTRAP_TARGET = "0091"
STAGING_VALIDATED_ATTACH_REVISIONS = frozenset({"0088", "0089"})
FORBIDDEN_BOOTSTRAP_LITERALS = frozenset({"head"})


def build_normal_bootstrap_upgrade_argv(*, python_executable: str) -> list[str]:
    if INTEGRATION_BOOTSTRAP_TARGET in FORBIDDEN_BOOTSTRAP_LITERALS:
        raise ValueError("bootstrap_target_forbidden")
    return [python_executable, "-m", "alembic", "upgrade", INTEGRATION_BOOTSTRAP_TARGET]
