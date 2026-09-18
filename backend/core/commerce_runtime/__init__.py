"""Dormant commerce runtime foundation: durable conversation ownership and
versioned state (persistence slice only).

Status
======
Nothing in this package is registered at application startup, wired into a
webhook, a worker, a model or a provider call, or reachable from any live
request path. Importing it has no side effects. Its tables are created only
by an explicitly applied Alembic revision (``0108``) or by
:func:`core.commerce_runtime.models.create_runtime_tables`; production
startup pins ``alembic upgrade 0093`` and materialises tables through the
*application* ``Base.metadata`` only, which this package deliberately does
not join, so no production schema change happens until an owner applies the
revision.

What it provides
================
* ``contracts`` — bounded identities, closed vocabularies, result records,
  explicit conflict errors and the pure rejection classifier.
* ``models`` — a dedicated SQLAlchemy metadata (``RuntimeBase``) with three
  tables: conversations (ownership + versioned state), turns (durable inbound
  admission with per-conversation order) and immutable per-turn terminals.
* ``repositories`` — short-transaction operations: admission, claim, renew,
  release, revision-based state commit bound to the eligible turn, atomic
  terminal recording of the eligible turn and administrative ownership
  invalidation. Each operation runs one write transaction; after a database
  conflict rolled it back, admission and terminal recording open one further
  read-only transaction to report the committed truth. Lease validity uses
  the database wall clock read after the row lock; ownership tokens are
  bound to tenant, namespace and conversation.

What it does not provide (accurate boundaries)
==============================================
* No production delivery, no order mutation, no provider adapter.
* No exactly-once external effect: a transport outcome of ``unknown`` is
  recorded as unknown and never authorises a replay.
* No search replacement, callbacks, activation flags or live state writes.

Contract: ``docs/architecture/commerce-runtime-foundation-contract.md``.
"""
from __future__ import annotations

__all__ = ["contracts", "models", "repositories"]
