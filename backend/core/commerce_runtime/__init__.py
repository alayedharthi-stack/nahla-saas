"""Commerce runtime: durable conversation ownership, versioned state, effect
and delivery ledgers, and the agent loop that runs one turn over them.

Status
======
Nothing here runs at application startup and importing the package has no
side effects. One live entry point now exists — ``runtime_entry`` — and the
WhatsApp webhook asks ``pilot_guard`` once, before the Merchant Brain,
whether this runtime owns the inbound turn. That guard is **fail-closed**:
it permits nothing until the pilot switch is on *and* the tenant appears in
a non-empty tenant allowlist *and* the recipient appears in a non-empty
recipient allowlist *and* the database agrees the WhatsApp connection is
that tenant's. With no configuration, every turn takes the legacy path
exactly as before.

Its tables are created only by explicitly applied Alembic revisions
(``0108`` foundation, ``0109`` ledgers) or by
:func:`core.commerce_runtime.models.create_runtime_tables` and
:func:`core.commerce_runtime.ledger_models.create_ledger_tables`; production
startup pins ``alembic upgrade 0093`` and materialises tables through the
*application* ``Base.metadata`` only, which this package deliberately does
not join, so no production schema change happens until an owner applies the
revision. Where the tables are absent the runtime refuses the turn instead
of running half-present.

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

* ``agent_contracts`` / ``agent_tools`` / ``agent_scripted`` / ``agent_loop``
  — the typed single-step provider boundary, the read-only tool registry and
  the loop that runs one turn to an accepted reply or an explicit stop.
* ``agent_provider`` — one Anthropic inference step translated into the
  loop's closed result union. It starts no loop of its own, composes no
  customer-facing sentence, and imports its instructions verbatim from the
  existing AI modules.
* ``agent_live_tools`` — the merchant's real catalogue, knowledge, order and
  shipment reads, over the same implementations Commerce Agent V2 calls,
  scoped by a trusted context the model cannot influence.
* ``delivery_dispatch`` / ``runtime_entry`` / ``pilot_guard`` — one reserved
  delivery intent sent exactly once through the established transport, the
  admit→claim→reason→dispatch→complete→release sequence, and the
  fail-closed routing decision that gates all of it.

What it does not provide (accurate boundaries)
==============================================
* **No commerce write of any kind**: no order created, updated or cancelled,
  no payment, no coupon. Every registered tool is read-only and the registry
  refuses to register one that is not.
* No exactly-once external effect: local uniqueness proves that at most one
  dispatch was *reserved*, never that the provider executed it once; an
  ``unknown`` outcome is recorded as unknown and never authorises a replay.
* No reconciliation worker and no delivery/read receipt source: an uncertain
  send stays uncertain, and customer reach stays ``unknown`` even after an
  accepted send.
* No rich, interactive or template delivery: the pilot sends one text reply.
* No broad rollout: the guard reaches only explicitly allowlisted tenants and
  recipients, and an empty allowlist permits nothing.
* Verification of a reply is structural — cited evidence must have been
  observed in the turn — and establishes no semantic grounding.

Contracts: ``docs/architecture/commerce-runtime-foundation-contract.md``,
``docs/architecture/commerce-runtime-effect-and-delivery-ledgers.md``,
``docs/architecture/commerce-runtime-agent-loop.md``,
``docs/architecture/commerce-runtime-live-integration.md`` and
``docs/architecture/commerce-runtime-pilot-activation.md``.
Operating the pilot: ``docs/engineering/commerce-runtime-pilot-runbook.md``.
"""
from __future__ import annotations

__all__ = ["agent_contracts", "agent_live_tools", "agent_loop", "agent_provider", "agent_scripted",
           "agent_tools", "contracts", "delivery_dispatch", "ledger_contracts", "ledger_models",
           "ledgers", "models", "pilot_guard", "repositories", "runtime_entry"]
