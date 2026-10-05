"""Dormant sibling revisions are accepted in every combination, and only them.

Three branches each add one dormant revision next to the payments topology
``{0092, 0111, 0114, 0115}``: OTO connections ``0117`` and catalog channel
retirements ``0118`` (both further children of ``0112``), and payments readiness
``0119`` (the child of ``0115``, so it replaces ``0115`` as a head). The
bootstrap contract enumerates every subset, so the checkout's real heads are
accepted whichever of those branches are present and in whatever order they
were merged. The list stays closed, and normal bootstrap stays pinned to 0093.

This file is identical on every branch that carries the shared contract block;
it asserts nothing that depends on which of the three revisions is present.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from alembic.script import ScriptDirectory  # noqa: E402

from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    DORMANT_SIBLING_ALEMBIC_HEADS,
    DORMANT_SIBLING_BASE_REPOSITORY_ALEMBIC_HEADS,
    DORMANT_SIBLING_REPOSITORY_ALEMBIC_HEAD_SETS,
    FORBIDDEN_BOOTSTRAP_LITERALS,
    INTEGRATION_BOOTSTRAP_TARGET,
    NORMAL_BOOTSTRAP_REVISIONS,
    build_normal_bootstrap_upgrade_argv,
    repository_heads_expected,
)

_PAYMENTS = {"0092", "0111", "0114", "0115"}

# Written out by hand rather than derived, so the enumeration is checked
# against an independent statement of the eight accepted topologies.
_ACCEPTED = (
    _PAYMENTS,
    _PAYMENTS | {"0117"},
    _PAYMENTS | {"0118"},
    _PAYMENTS | {"0117", "0118"},
    {"0092", "0111", "0114", "0119"},
    {"0092", "0111", "0114", "0117", "0119"},
    {"0092", "0111", "0114", "0118", "0119"},
    {"0092", "0111", "0114", "0117", "0118", "0119"},
)

_REFUSED = (
    # 0119 is the child of 0115: both can never be heads together.
    _PAYMENTS | {"0119"},
    _PAYMENTS | {"0117", "0118", "0119"},
    # The payments head cannot vanish without 0119 replacing it.
    {"0092", "0111", "0114", "0117"},
    {"0092", "0111", "0114", "0117", "0118"},
    # The retired id and any undeclared extra head.
    _PAYMENTS | {"0116"},
    _PAYMENTS | {"0117", "0116"},
    _PAYMENTS | {"0120"},
    {"0092", "0111", "0114", "0118", "0119", "0120"},
    # Dormant siblings never ride on another base topology.
    {"0092", "0111", "0113", "0117"},
    {"0092", "0111", "0113", "0114", "0118"},
    {"0092", "0111", "0117"},
    {"0111", "0114", "0115", "0117"},
    # A dormant sibling alone.
    {"0117"},
)


def test_every_subset_of_the_dormant_siblings_is_accepted() -> None:
    assert {frozenset(s) for s in _ACCEPTED} == set(DORMANT_SIBLING_REPOSITORY_ALEMBIC_HEAD_SETS)
    for heads in _ACCEPTED:
        assert repository_heads_expected(heads), sorted(heads)
        # Order and container type never matter.
        assert repository_heads_expected(sorted(heads, reverse=True)), sorted(heads)


def test_the_dormant_sibling_list_is_closed() -> None:
    assert DORMANT_SIBLING_BASE_REPOSITORY_ALEMBIC_HEADS == frozenset(_PAYMENTS)
    assert dict(DORMANT_SIBLING_ALEMBIC_HEADS) == {"0117": None, "0118": None, "0119": "0115"}
    assert len(DORMANT_SIBLING_REPOSITORY_ALEMBIC_HEAD_SETS) == 8
    for heads in _REFUSED:
        assert not repository_heads_expected(heads), sorted(heads)


def test_dormant_siblings_never_become_a_bootstrap_target() -> None:
    assert INTEGRATION_BOOTSTRAP_TARGET == "0093"
    assert NORMAL_BOOTSTRAP_REVISIONS == frozenset({"0093"})
    assert "head" in FORBIDDEN_BOOTSTRAP_LITERALS
    assert build_normal_bootstrap_upgrade_argv(python_executable="python") == [
        "python", "-m", "alembic", "upgrade", "0093"]
    for added, _replaced in DORMANT_SIBLING_ALEMBIC_HEADS:
        assert added not in NORMAL_BOOTSTRAP_REVISIONS


def test_this_checkout_heads_are_the_payments_topology_plus_declared_siblings() -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        script = ScriptDirectory(str(_REPO / "database" / "migrations"))
        heads = frozenset(script.get_heads())
        present = {rev.revision: rev for rev in script.walk_revisions()}
    finally:
        os.chdir(prev_cwd)
    assert repository_heads_expected(heads), sorted(heads)
    for added, replaced in DORMANT_SIBLING_ALEMBIC_HEADS:
        rev = present.get(added)
        if rev is None:
            continue
        # A sibling that exists is a head of this checkout, and one that
        # replaces a head revises it, so the replaced id is no longer a head.
        assert added in heads, added
        if replaced is not None:
            assert rev.down_revision == replaced, (added, rev.down_revision)
            assert replaced not in heads, (added, replaced)
