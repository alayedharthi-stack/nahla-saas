"""Commerce runtime navigation — one durable page of an unfinished browse.

One new relation, ``commerce_runtime_navigation_snapshots``, and no change to
any existing one.

Why a relation of its own
=========================
A browse of thirty products cannot be answered in a list that shows ten. Paging
it needs a result set that outlives the turn that produced it, and the delivery
ledger's ``intent_payload`` was measured against the seven properties such a
snapshot needs and failed five: no opaque-token lookup, no expiry, no replay
rejection, no bounded retention, and — decisively — the sequence row is
deliberately mutable while a snapshot must not be. It is also bound to one turn
by ``UniqueConstraint("turn_id")``, and a snapshot exists to be read by later
turns. The measurement is written out in
``docs/engineering/commerce-runtime-product-presentation.md``.

Dormant by construction
=======================
The relation belongs to ``core.commerce_runtime.models.RuntimeBase``,
deliberately separate from the application ``models.Base``. Production startup
pins ``alembic upgrade 0093`` and materialises only ``models.Base`` through
``create_all`` (``backend/main.py``), so this revision changes no production
database until an owner applies it explicitly. Until it is applied, a browse
that does not fit keeps today's behaviour: the selector is withheld whole and
every option follows the model's sentence as a line — nothing is trimmed and
nothing is faked.

Graph
=====
Numbered ``0113`` and revising ``0112``, the shipment-tracking revision, which
is the repository's head on this line. It is a plain child, not a sibling: it
touches nothing ``0112`` touches, and nothing touches it.

Apply with ``alembic upgrade 0113`` on a database at ``0112``. Roll back **this
revision only** with ``alembic downgrade 0113@-1``; the branch-qualified
spelling matters wherever more than one head exists, for the reason ``0111``
records at length. The downgrade drops the relation, which discards unfinished
browses and nothing else: a customer mid-list simply gets a fresh answer.

Drafts
======
This revision was first drafted in PR #1143 and revised before it was ever
applied: the stored result now carries its provenance (the originating turn
and search call, the strategy, a digest of the query), whether it is
**complete**, the model's own words for the list's button and its "More" row,
and which turn spent each token; the database refuses a token that points
outside its result, a result above the cap, and a half-recorded spend; and the
partial index nothing read is gone. A database that ever ran the first draft
carries a relation of the same name and a different definition, and this
revision refuses it rather than reconciling it — as it refuses any relation
that only looks right.

One source of truth
===================
The relation is created from the package's own metadata rather than from a
second hand-written copy of it, so this revision and
``core.commerce_runtime.navigation_models`` cannot drift. A pre-existing
relation is verified against that same metadata — columns, **and the primary
key, unique constraints, check constraints, foreign keys and indexes that carry
the guarantees, by definition and not by name** — and an incompatible one stops
the upgrade rather than being quietly reconciled.
"""
from __future__ import annotations

import os
import sys

from alembic import op

from migration_inspector_helpers import has_table

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

_NAVIGATION = "commerce_runtime_navigation_snapshots"
_TABLES = (_NAVIGATION,)


def _shared():
    """The by-definition schema comparison, shared with the revision it came from."""
    for path in (
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")),
    ):
        if path not in sys.path:
            sys.path.insert(0, path)
    import runtime_schema_guarantees as guarantees  # noqa: PLC0415

    return guarantees


def _navigation_tables():
    """The package's own table objects — the single definition of this schema."""
    _shared()   # puts backend/ on the path as a side effect, as 0111 does
    from core.commerce_runtime.navigation_models import (  # noqa: PLC0415
        NAVIGATION_TABLE_OBJECTS,
    )

    return list(NAVIGATION_TABLE_OBJECTS)


def upgrade() -> None:
    shared = _shared()
    bind = op.get_bind()
    tables = _navigation_tables()

    existing = [table for table in tables if has_table(bind, table.name)]
    diffs = []
    for table in existing:
        diffs.extend(shared.differences(bind, table))
    if diffs:
        raise shared.IncompatibleSchema(
            "0113 refuses to reconcile an incompatible pre-existing schema: " + "; ".join(diffs)
        )

    missing = [table for table in tables if not has_table(bind, table.name)]
    if missing:
        tables[0].metadata.create_all(bind, tables=missing, checkfirst=False)

    remaining = []
    for table in tables:
        if not has_table(bind, table.name):
            remaining.append(f"{table.name} was not created")
        else:
            remaining.extend(shared.differences(bind, table))
    if remaining:
        raise shared.IncompatibleSchema("0113 post-condition failed: " + "; ".join(remaining))


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        if has_table(bind, name):
            op.drop_table(name)
