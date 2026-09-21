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

APPLICATION_ALEMBIC_HEAD = "0111"
# The customer-address provenance revision is a sibling of the application
# head: both revise ``0109``. It lives on its own branch until that merges, so
# the repository's heads are ``{0092, 0111}`` here and ``{0092, 0110, 0111}``
# once it has. Both are expected; anything else is not.
ADDRESS_ALEMBIC_HEAD = "0110"
REPOSITORY_ALEMBIC_HEADS = frozenset({"0092", APPLICATION_ALEMBIC_HEAD})
TOLERATED_REPOSITORY_ALEMBIC_HEADS = REPOSITORY_ALEMBIC_HEADS | {ADDRESS_ALEMBIC_HEAD}


def repository_heads_expected(heads) -> bool:
    """Whether the script directory's heads are the ones this repository knows.

    ``0092`` and ``0111`` must both be present; ``0110`` may be — it is the
    address sibling, present once its branch has merged — and nothing else may.
    """
    found = frozenset(str(h) for h in heads)
    return REPOSITORY_ALEMBIC_HEADS <= found <= TOLERATED_REPOSITORY_ALEMBIC_HEADS
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
