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
# head: both revise ``0109``, and neither is an ancestor of the other.
ADDRESS_ALEMBIC_HEAD = "0110"
# Shipment tracking extends the address branch. It replaces 0110 as a head;
# it does not merge the 0092 validation or 0111 application siblings.
SHIPMENT_ALEMBIC_HEAD = "0112"
# The navigation snapshot extends the same branch one further. It replaces 0112
# as a head for exactly the same reason, and merges nothing: the 0092 validation
# and 0111 application siblings are untouched, so bootstrap stays pinned to 0093
# and bare ``head`` stays ambiguous — which is the point of naming every target.
NAVIGATION_ALEMBIC_HEAD = "0113"

# These are the only script-directory topologies accepted by this contract.
# They describe source checkouts, not bootstrap targets: normal bootstrap
# remains pinned to 0093 and must never use bare ``head``.
BASE_REPOSITORY_ALEMBIC_HEADS = frozenset({"0092", APPLICATION_ALEMBIC_HEAD})
ADDRESS_REPOSITORY_ALEMBIC_HEADS = BASE_REPOSITORY_ALEMBIC_HEADS | {ADDRESS_ALEMBIC_HEAD}
SHIPMENT_REPOSITORY_ALEMBIC_HEADS = frozenset(
    {"0092", APPLICATION_ALEMBIC_HEAD, SHIPMENT_ALEMBIC_HEAD})
REPOSITORY_ALEMBIC_HEADS = frozenset(
    {"0092", APPLICATION_ALEMBIC_HEAD, NAVIGATION_ALEMBIC_HEAD})
SUPPORTED_REPOSITORY_ALEMBIC_HEAD_SETS = frozenset({
    BASE_REPOSITORY_ALEMBIC_HEADS,
    ADDRESS_REPOSITORY_ALEMBIC_HEADS,
    # A checkout that predates the navigation snapshot is still a supported
    # topology, so both are accepted rather than one replacing the other.
    SHIPMENT_REPOSITORY_ALEMBIC_HEADS,
    REPOSITORY_ALEMBIC_HEADS,
})


def repository_heads_expected(heads) -> bool:
    """Whether the script directory's heads are the ones this repository knows.

    Accepted source checkouts are exactly ``{0092, 0111}``, the intermediate
    address checkout ``{0092, 0110, 0111}``, and the current shipment checkout
    ``{0092, 0111, 0112}``. No arbitrary extra head is accepted.
    """
    found = frozenset(str(h) for h in heads)
    return found in SUPPORTED_REPOSITORY_ALEMBIC_HEAD_SETS
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
