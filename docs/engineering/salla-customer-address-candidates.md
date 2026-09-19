# Salla profile address candidates — bounded persistence and reuse

Status: implemented in the Salla address-candidate slice. Scope is the
customer **profile** address surface only. No order/shipment address
ingestion is added, no Salla address-book API is assumed, and nothing is
written back to Salla.

## 1. What this slice does

1. Preserves the address information Salla exposes on a customer profile as
   a durable, source-labelled **address candidate** on the Nahla customer.
2. Exposes that candidate to the existing agent operational context, the
   checkout reply surface and the customer API through one shared read
   projection.
3. Records an **explicit customer selection** from a structured customer
   action, bound to the exact address revision that was presented to them.
4. Makes a selected address reusable across a state reset and a new
   conversation, from durable customer-level state — not from message text
   and not from `Conversation.extra_metadata`.
5. Ties permanent-save and adoption claims to the operation this turn
   actually performed **and** to committed customer-address evidence.
6. Keeps the complete address inventory — including selections a later
   choice superseded — visible to the agent, checkout and the customer
   API, so a customer can go back to an address they used before.

## 2. Source fields actually supported

From the Salla customer profile payload (customer sync and customer
webhook), exactly three fields are treated as address information:

| Salla field | Nahla component | Stored in |
| --- | --- | --- |
| `location` (free text, or its `description`/`address`/`street`) | `address_line` | `customer_addresses.address_text` / `.raw_address` |
| `city` | `city` | `customer_addresses.city` |
| `country` | `country` | `customer_address_provenance.source_country` |
| `updated_at` | provider revision | `customer_address_provenance.source_updated_at` |

Nothing else on the profile becomes an address component. In particular:

* `location` is free text. It is **never** written to
  `saudi_national_address` (the Saudi national **short** address) and no
  postal code is inferred from it. Short address and postal code are
  distinct and stay distinct on every read and write in this slice.
* `updated_at` is the only accepted provider revision. When it is absent,
  `source_updated_at` stays `NULL`; it is never replaced by `now()` or any
  other locally invented revision. `source_observed_at` records when Nahla
  saw the payload, which is a different fact.

### Verification limits

Salla's published documentation was not readable from the build
environment (`docs.salla.dev` is blocked by the network egress proxy), and
no live connection, grant listing, webhook subscription or masked customer
payload was read. The supported surface above was verified against the
repository's own Salla customer paths and against search-result summaries
of the official `List Customers` / `Customer Details` pages. Treat it as
"verified against the shapes this repository already handles", not as a
live-synchronisation claim or a Salla review readiness claim.

No supported Merchant API customer saved-address-book or default-address
operation was established. This slice claims no address-book
synchronisation.

## 3. Address policy

| Rule | Where it is enforced |
| --- | --- |
| Imported profile information is a candidate, never an automatically confirmed/default delivery address | `upsert_imported_address_candidate` writes `selection_state='candidate'` |
| Only supported information is persisted; city alone is not a complete delivery address | `is_sufficient_delivery_address` (city **and** one locating component) |
| A sufficient explicitly selected address is reusable without re-collecting the address | `AddressResolution.reusable` → `known_previous_address.explicitly_selected` |
| An unselected candidate may be presented for brief confirmation | checkout `field_modes` stay `confirm`; no `customer_confirmed_previous_address` |
| Multiple candidates require an explicit valid selection | `AddressResolution.reusable` is `None`; candidates are listed instead |
| Missing required fields prompt only for those fields | `missing_address_requirements` |
| A Salla refresh must not silently replace a newer customer selection | a selected row is never content-mutated; changed content becomes a new candidate |
| A selection landing between a refresh's read and its write still wins | the refresh re-reads the provenance row under a write lock with `populate_existing` and revalidates before mutating |
| A superseded selection stays visible and re-selectable | `AddressResolution.addresses` is what the agent context and customer API project, not `candidates + selected` |
| One store's import never rewrites another store's address | §5 store ownership, plus the store connection in the revision key |

Selection is bound to `content_fingerprint` — a SHA-256 over the supported
components. `record_explicit_address_selection` refuses to write when the
caller's `expected_fingerprint` no longer matches the stored content, so
the customer can never be recorded as approving something other than what
they were shown.

Resolution order is by **explicit selection**, never by row id, latest
import or latest order:

1. an address with `selection_state='selected'` whose `selected_fingerprint`
   still matches its content — the most recent `selected_at` wins;
2. otherwise a row that predates this slice and carries no provenance —
   those were only ever written on confirmed shipping evidence, so they are
   read as legacy selections and `main`'s reuse behaviour is unchanged;
3. otherwise exactly one candidate, surfaced for confirmation;
4. otherwise nothing — several unselected candidates never produce a
   default.

## 4. Schema

New table `customer_address_provenance` (one row per `customer_addresses`
row), revision **0110**, extending 0109 linearly. `customer_addresses`
itself is unchanged — no column added, no row rewritten, no backfill.

Columns: `tenant_id`, `customer_id`, `customer_address_id`, `source`,
`source_ref`, `integration_connection_id`, `source_country`,
`content_fingerprint`, `source_updated_at` (nullable),
`source_observed_at`, `selection_state`, `selected_fingerprint`,
`selected_at`, `selection_source`, `selection_operation_ref`,
`created_at`, `updated_at`.

`selection_operation_ref` records which selection OPERATION produced the
current selection. It is what lets a reply's claim be tied to the
operation *this turn* performed rather than to any selection that happens
to exist (see §6).

Constraints and indexes:

| Name | Shape | Why |
| --- | --- | --- |
| `uq_customer_address_provenance_address` | unique (`tenant_id`, `customer_address_id`) | one provenance row per address row |
| `uq_customer_address_provenance_source_revision` | unique index (`tenant_id`, `customer_id`, `COALESCE(integration_connection_id, -1)`, `source`, `source_ref`, `content_fingerprint`) | one row per store connection per source revision |
| `ix_customer_address_provenance_source` | (`tenant_id`, `customer_id`, `source`, `source_ref`) | history lookups |

The revision key carries the **store connection**: the same provider
customer reference can exist under two connections of one tenant, and
without it one store's import collided with — and was deduplicated into —
another store's row. It is an expression index over
`COALESCE(integration_connection_id, -1)` rather than a plain unique
constraint because rows written before a verified connection existed hold
`NULL` there and SQL treats `NULL`s as distinct; a bare unique key would
stop deduplicating exactly the concurrent first imports that the recovery
path depends on. Rows with no `source_ref` (confirmed-shipping provenance)
are still not deduplicated by it; that path deduplicates on address
content in `core.customer_shipping_address_writer`.

The revision handles both pre-states, exactly as 0107 does: table absent →
created; table already materialised by `Base.metadata.create_all` →
reconciled additively (missing columns, server defaults, NOT NULL only
after NULLs are filled with the same default, missing foreign keys, unique
constraint and indexes). Nothing is dropped and no row is rewritten.

### Readiness and activation boundary (explicit)

`APPLICATION_ALEMBIC_HEAD` advances `0109 → 0110`, and the normal
bootstrap target stays pinned at `0093`.

What that pin does and does not guarantee, stated precisely because an
earlier version of this runbook overstated it:

* **It does not mean production applies nothing.** `backend/main.py` calls
  `Base.metadata.create_all(engine)` at startup, so any deployment
  shipping these ORM models MATERIALIZES `customer_address_provenance` —
  table, both unique keys and the index — without `alembic upgrade 0110`
  ever being run. "No explicit Alembic run" is therefore not a guarantee
  of dormancy.
* **What the pin does guarantee** is that Alembic alters no *existing*
  table here: 0110 only creates (or additively reconciles) one new table,
  and `customer_addresses` is untouched.
* **Where the table is absent** — a database at 0109 that has not run
  `create_all` — every reader and writer degrades to the legacy
  projection: no candidate is written, no selection is recorded, and the
  optional read is isolated so it cannot abort the caller's transaction
  (§8).

No deployment, shared-database migration, backfill or flag activation is
authorised by this document.

## 5. Identity binding

Address attachment needs **both** a provider customer identity and a
verified store connection, and both are checked before any write.

* Connection **first**: a Salla `Integration` that exists, belongs to this
  tenant and is enabled. A verified connection is a **prerequisite**, not
  optional provenance: recording `integration_connection_id=None` and
  importing anyway would let a payload borrow authority its store binding
  never granted, so a missing, foreign, disabled or wrong-provider
  connection writes nothing at all.
* Customer, **bound to that connection**: `salla_customer_id` resolving to
  exactly one local `Customer` in this tenant. Name matching and phone
  fallback are explicitly not accepted — they resolve customers, they do
  not prove whose address a payload describes. Global name resolution is
  untouched.

`(tenant, salla_customer_id)` alone is not a binding. One tenant may hold
two enabled Salla connections, and the same provider customer reference
can exist under both. So where the tenant maps that reference to a store
through `ExternalCustomerProfile`, a different store is refused
(`customer_owned_by_another_connection`). Where no mapping is recorded,
nothing is asserted, so a tenant with no A1 profiles behaves exactly as
before rather than losing address import.

### Store ownership of the address

One provider customer reference under one tenant is owned by **one store
connection at a time**. An import from another connection is refused
outright (`source_owned_by_another_connection`) rather than overwriting.

Ownership does transfer — replacement and reconnection have to work — but
only when the previous owner is no longer a usable connection (removed or
disabled). That transition is logged
(`[CUSTOMER_ADDRESS] source ownership transferred …`), never a silent
rewrite, and the previous store's approved content is preserved as its own
row rather than being mutated.

Source history is read once for the whole scope to decide ownership, then
narrowed to this store's own rows **plus rows no store ever claimed**, so a
legacy row is adopted and refreshed rather than duplicated. Freshness,
idempotency and the update decision are all judged on that narrowed
history.

Refusal reasons, all writing nothing: `missing_salla_customer_id`,
`customer_not_linked`, `ambiguous_customer_identity`, `tenant_mismatch`,
`conflicting_customer_identity`, `customer_not_persisted`,
`customer_owned_by_another_connection`, `no_active_salla_connection`,
`connection_not_found_for_tenant`, `connection_tenant_mismatch`,
`connection_provider_mismatch`, `connection_disabled`,
`source_owned_by_another_connection`.

## 5a. Presentation and consent as lifecycle events

A durable address **selection** is an operational act, so it needs
operational evidence — not a reading of the customer's words.

**Presentation.** Reading a customer's addresses is not showing them to
anyone: `load_checkout_reply_context` runs on turns that never mention an
address. It therefore only PREPARES an `AddressPresentation`. The offer is
recorded by `record_presented_address_offer` at the delivery boundary
(`order_flow_v2/owner.py` `_finalize`), and only for a live reply that
actually puts the address in front of the customer — one with address
choices, or with the city / delivery-address slot in `confirm` mode.

An existing offer is never advanced by a later context read. A refresh
that lands between the presentation and the customer's answer makes that
answer refuse, which is the point: replacing the offer with the newer
revision would record approval of content nobody saw.

**Consent.** A durable selection is written only by a STRUCTURED customer
action — an interactive reply id in the `nahla_addr_select:<address_id>`
namespace (`button_id` / `list_reply_id`), handled by
`apply_structured_address_consent`. It names the exact address, it cannot
be a question, and no phrasing produces it. The selection is still bound to
the offered revision: a fingerprint that no longer matches refuses, as does
an address that was never offered, an offer for another tenant, and an
offer for another customer. Tapping the same choice twice selects once.

**Why not free text.** `هل عنواني محفوظ عندكم؟` ("is my address saved with
you?") and `نفس العنوان السابق` ("same address as before") both read as
`previous_address_confirmed` from the platform's existing intent detector.
A phrase detector cannot separate an inquiry from consent, and widening or
narrowing it would be customer-intent regex repair — forbidden by GOV-001
and wrong again on the next phrasing. So free text continues to drive the
turn's checkout exactly as it does on `main` (`shipping_source =
customer_confirmed_previous_address`), and simply never writes the durable
act; the patch carries `address_selection_durable = False`. No intent
detector, keyword router or customer regex was changed.

## 6. Save and adoption evidence

`core/customer_address_persistence_evidence.py` names the scope of what was
actually persisted:

| Scope | Supports "held on file" | Supports "adopted as your delivery address" |
| --- | --- | --- |
| `none` | no | no |
| `conversation_state` | no | no |
| `imported_candidate` | yes | no |
| `selected_delivery_address` | yes | yes |

Evidence is produced only by re-reading the customer record on an
INDEPENDENT session that cannot see the writer's open transaction. A
writer returning `True`, an uncommitted `db.add`, a bridge skip, an
unavailable capability, an ambiguous identity and a failed commit all
resolve to `none`.

### The operation, and who produces it

Evidence answers "is this address durably there". It does not answer "did
*this turn* do anything", and an address that was already on file proves
nothing about a new claim. So the reply's claim is checked against an
`AddressOperationAttempt` — operation, tenant, customer, address,
**revision** and **operation reference** — and `is_actionable` requires all
six.

The attempt is produced by the **writer**, never assembled by the boundary
that wants to make the claim:

1. `apply_structured_address_consent` → `_record_selection_for_confirmed_address`
   performs the selection and returns the attempt describing exactly what
   it wrote.
2. `record_turn_address_operation` stores it on the conversation, stamped
   with the inbound turn it belongs to.
3. `read_turn_address_operation` hands it back at the guard boundary, and
   only for the same conversation, the same inbound turn and within 10
   minutes. Last turn's operation is not this turn's.
4. `resolve_customer_address_persistence_evidence` verifies it against
   independently committed state: the revision is always checked, and for
   an adoption claim a selection committed by a *different*
   `selection_operation_ref` does not authorize it
   (`committed_operation_mismatch`).

### What the guard removes, and what it must not

`customer_address_save_claim_guard` judges **completed-action assertions
only**, clause by clause. A question and a negation contain the same words
but assert no completed action, so they are truthful LLM text and are
preserved. The two semantic classes (saved / adopted) cover the Arabic
attached-pronoun forms of "address" rather than being a phrase list to
extend.

Clause granularity matters, and punctuation alone does not give it:
`تم حفظ عنوانك هل تريد إكمال الطلب؟` is ONE punctuated sentence carrying a
completed-action assertion and a question about something else. Judged
whole, its trailing question mark exempted the false claim. A sentence is
therefore split again before an interrogative lead, and each clause is
judged on its own — so only the offending clause is removed and the honest
half survives.

Negation is **positional**: it must stand before the claim to govern it.
`لم يتم حفظ عنوانك` denies the save. `عنوانك محفوظ بدون أي مشكلة` asserts
it and then says it went smoothly — `بدون` there negates "problem", not the
save. No phrase list can repair that; only position can.

### When the claim was the whole reply

Removing it leaves nothing, and a guard may correct the AI but never mute
it. Two boundaries, two behaviours:

* **Brain pipeline** (`modules/ai/brain/pipeline.py`), which still holds
  the composer: the turn asks for ONE more natural composition
  (`invoke_authorized_address_claim_recompose`, role
  `address_save_claim_grounding_recompose`), revalidates it with
  `allow_recompose=False`, and delivers it when it is truthful. The
  existing approved emergency fallback (`EX-FALLBACK-GENERIC-001`, via
  `core.fallback_policy`) speaks only after a genuine compose failure or a
  second candidate that repeats the claim. The turn's single recompose
  budget is shared with `product_claim_grounding_guard`.
* **Post-compose boundary** (`post_compose_guard_pipeline`), which runs
  after composition and has no composer to ask: the truthful platform line
  is sent rather than silence. The send is suppressed only if even that is
  unavailable.

Fallback metadata is recorded so this is measurable in production:
`compose_source=fallback_deterministic`, `response_mode`, `chosen_path`,
`fallback_reason=address_save_claim_unsupported_after_recompose`,
`fallback_action_type=address_save_claim_failed_compose`,
`llm_candidate_present=true`, `final_text_transformed`,
`final_transform_reasons`.

The guard authors no customer-facing prose and adds no template, and no
new `DETERMINISTIC_EXCEPTIONS` entry was created for it.

## 7. Test discovery (no workflow change)

`.github/workflows/ci.yml` is in the GOV-002 scanner's `GOVERNANCE_CORE`
set, so a runtime pull request may not touch it and an owner exception may
not be created in the same pull request. This slice therefore adds **no**
workflow change and relies on discovery that already exists:

* The behavioural regressions live in `tests/`, which is `pytest.ini`'s
  only `testpaths` entry, so the existing `Run unit tests` step
  (`python -m pytest -q --maxfail=1`) collects and enforces them. They use
  in-memory SQLite only and probe no PostgreSQL, preserving the documented
  invariant that the root run never reaches the job's `127.0.0.1:5433`
  service.
* The PostgreSQL proofs stay in `backend/tests/` — where the equivalent
  identity suite lives — and run through the existing required-proofs
  step, gated on `LEGACY_MIG_PG_TEST_DATABASE_URL`, which that step
  already sets.

Known gap, pre-existing and **not** introduced here: `backend/tests` is
not in `testpaths`, and the repository's CI enumerates backend modules
individually. `test_p1b_post_compose_guard_consolidation.py` (the full
post-compose ordering contract, extended by this slice) is among the
modules CI does not collect — as it was before this change. The one fact
this slice introduces there, that the address save-claim guard is
registered between the shipment and staff guards, is additionally asserted
from `tests/test_salla_customer_address_candidates.py`, which CI does run.
Closing the wider gap needs a `ci.yml`-only governance pull request and is
out of scope here.

## 8. Activation dependencies

* **Migration.** `customer_address_provenance` exists only where
  `alembic upgrade 0110` has been applied, or where the ORM table was
  materialised by `create_all` (see §4 — the second case needs no Alembic
  run). Until then the read projection degrades to legacy behaviour and no
  candidate is written. The degradation is transaction-safe: the optional
  provenance read runs inside its own savepoint, so a missing or denied
  read leaves the caller's PostgreSQL transaction usable instead of
  aborted, and a rejected optional provenance write rolls back that row
  alone rather than taking the confirmed address down with it.
* **Structured consent channel.** A durable selection needs an interactive
  reply carrying `nahla_addr_select:<address_id>`. Where a merchant's
  surface sends no interactive replies, candidates are still imported,
  read and offered, and checkout still continues on free text — only the
  durable selection (and any claim resting on it) waits for a structured
  action.
* **Salla permissions / subscriptions.** Candidates only appear where the
  connection can read customers (`sync_customers`) and/or where the
  customer webhook is delivered. Neither is changed by this slice.
* **Source data.** A profile with no `location`, `city` or `country`
  produces no candidate, by design.
* **`ORDER_MISSING_FIELDS_ENGINE_ENABLED` (default off).** The
  missing-fields engine carries the selection distinction in its field
  evidence. Its modes stay `confirm` at the `known_previous_address`
  branch on purpose: reaching that branch means `order_prep` does not hold
  the value, so `skip` would report a city or delivery address the order
  does not have. The `skip` comes from the `order_prep` branch once the
  value is promoted.
* **`ORDER_CONTEXT_OPERATIONAL_PREFILL_ENABLED` /
  `ORDER_CONTEXT_SHIPPING_CONFIRM_ENABLED` (default off).** Promotion of a
  saved address into `order_prep` without an explicit customer reference
  remains behind these flags. This slice does not enable them. The live,
  unflagged promotion paths are the delivery-continuation and
  previous-address-confirmation paths in `order_flow_v2/checkout_context`.

## 9. Deferred

* Order and shipment addresses as candidates — order/shipment addresses
  remain order snapshots.
* A manual merchant-facing customer address editor.
* A standalone WhatsApp operation that saves arbitrary newly supplied
  address text. Salla import does not provide it and does not fix it.
* Any Salla address-book / default-address write or read. None is
  established as supported.
