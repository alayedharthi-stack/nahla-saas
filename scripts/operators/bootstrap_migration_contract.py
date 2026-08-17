"""Closed contract for normal bootstrap migration targets (multi-head aware).

Repository Alembic graph has parallel heads ``0092`` (A1-Validate branch) and
``0095`` (commerce lifecycle send_method extending ``0094`` → ``0093``), both
branching from the ``0090`` / ``0091`` siblings off ``0088`` / ``0089``.

Normal application bootstrap must never invoke bare ``head`` — that would apply
both sibling heads and advance capability to ``validated`` unintentionally.
Integration and local bootstrap pin to ``0093`` explicitly so capability remains
``expand`` until the guarded staging Validate operator runs.

After staging attaches historical ``0089`` onto validated ``0088``,
``alembic_version`` holds **two rows** (``0088`` + ``0089``). Bootstrap upgrade
to ``0093`` advances only the integration branch and yields the supported
validated-staging state ``{0088, 0093}``; it must not select ``0092``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic.script import ScriptDirectory

_REPO = Path(__file__).resolve().parents[2]


def load_repository_alembic_heads() -> frozenset[str]:
    """Derive current repository Alembic heads from the migration graph."""
    database_dir = _REPO / "database"
    if str(database_dir) not in sys.path:
        sys.path.insert(0, str(database_dir))
    prev_cwd = os.getcwd()
    try:
        os.chdir(database_dir)
        script = ScriptDirectory(str(database_dir / "migrations"))
        return frozenset(str(head) for head in script.get_heads())
    finally:
        os.chdir(prev_cwd)


# Parallel heads after 0098→0099: A1-Validate (0092) + escalation authoring (0099).
REPOSITORY_ALEMBIC_HEADS = load_repository_alembic_heads()
INTEGRATION_BOOTSTRAP_TARGET = "0093"
NORMAL_BOOTSTRAP_REVISIONS = frozenset({"0093"})
VALIDATED_STAGING_BOOTSTRAP_REVISIONS = frozenset({"0088", "0093"})
# Historical state produced by the guarded 0088→0089 attach operator before
# normal bootstrap advances the integration branch to 0093.
STAGING_VALIDATED_ATTACH_REVISIONS = frozenset({"0088", "0089"})
FORBIDDEN_BOOTSTRAP_LITERALS = frozenset({"head"})


def build_normal_bootstrap_upgrade_argv(*, python_executable: str) -> list[str]:
    if INTEGRATION_BOOTSTRAP_TARGET in FORBIDDEN_BOOTSTRAP_LITERALS:
        raise ValueError("bootstrap_target_forbidden")
    return [python_executable, "-m", "alembic", "upgrade", INTEGRATION_BOOTSTRAP_TARGET]
