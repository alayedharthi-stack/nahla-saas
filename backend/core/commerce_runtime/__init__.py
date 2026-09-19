"""Dormant commerce runtime foundation: durable conversation ownership and
versioned state (persistence slice only).

Status
======
Nothing in this package is registered at application startup, wired into a
webhook, a worker, a model or a provider call, or reachable from any live
request path. Importing it has no side effects. Its tables are created only
by explicitly applied Alembic revisions (``0108`` foundation, ``0109``
ledgers) or by :func:`core.commerce_runtime.models.create_runtime_tables` and
:func:`core.commerce_runtime.ledger_models.create_ledger_tables`; production
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
* ``ledger_contracts`` / ``ledger_models`` / ``ledgers`` — the dormant effect
  and delivery ledgers: reserved external mutations bound to a business
  idempotency key, durable dispatch attempts, append-only results and
  receipts, one logical delivery sequence per turn with one bounded
  rich-to-text recovery, honest ``unknown`` outcomes that block redispatch,
  scope-bound late evidence, and an atomic decision commit plus a terminal
  whose transport outcome and customer reach are derived from the ledgers.
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
* No exactly-once external effect: local uniqueness proves that at most one
  dispatch was *reserved*, never that the provider executed it once; an
  ``unknown`` outcome is recorded as unknown and never authorises a replay.
* No dispatch worker, no reconciliation source, no upstream idempotency
  guarantee: the ledgers record intents, attempts and evidence; nothing here
  performs an external call.
* No search replacement, callbacks, activation flags or live state writes.
* No live reasoning provider: the agent loop core (``agent_contracts``,
  ``agent_tools``, ``agent_scripted``, ``agent_loop``) orchestrates one turn
  against a typed single-step provider interface and read-only fixture tools.
  It calls no model, sends no message and is wired into nothing.

Contracts: ``docs/architecture/commerce-runtime-foundation-contract.md`` and
``docs/architecture/commerce-runtime-effect-and-delivery-ledgers.md``.
"""
from __future__ import annotations

__all__ = ["agent_contracts", "agent_loop", "agent_scripted", "agent_tools", "contracts",
           "ledger_contracts", "ledger_models", "ledgers", "models", "repositories"]
