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
3. Records an **explicit customer selection** through the existing
   address-confirmation path, bound to the exact address revision the
   customer reviewed.
4. Makes a selected address reusable across a state reset and a new
   conversation, from durable customer-level state — not from message text
   and not from `Conversation.extra_metadata`.
5. Ties permanent-save and adoption claims to committed customer-address
   evidence.

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
`selected_at`, `selection_source`, `created_at`, `updated_at`.

Constraints: `uq_customer_address_provenance_address`
(`tenant_id`, `customer_address_id`) and
`ix_customer_address_provenance_source`
(`tenant_id`, `customer_id`, `source`, `source_ref`).

The revision handles both pre-states, exactly as 0107 does: table absent →
created; table already materialised by `Base.metadata.create_all` →
reconciled additively (missing columns, server defaults, NOT NULL only
after NULLs are filled with the same default, missing foreign keys, unique
constraint and index). Nothing is dropped and no row is rewritten.

`APPLICATION_ALEMBIC_HEAD` advances `0109 → 0110`. The normal bootstrap
target stays pinned at `0093`, so production applies nothing from this
revision until an owner runs `alembic upgrade 0110` deliberately.

## 5. Identity binding

Address attachment needs **both** a provider customer identity and a
verified store connection, and both are checked before any write.

* Customer: `salla_customer_id` resolving to exactly one local `Customer`
  in this tenant. Name matching and phone fallback are explicitly not
  accepted — they resolve customers, they do not prove whose address a
  payload describes. Global name resolution is untouched.
* Connection: a Salla `Integration` that exists, belongs to this tenant and
  is enabled. A verified connection is a **prerequisite**, not optional
  provenance: recording `integration_connection_id=None` and importing
  anyway would let a payload borrow authority its store binding never
  granted, so a missing, foreign, disabled or wrong-provider connection
  writes nothing at all.

Refusal reasons, all writing nothing: `missing_salla_customer_id`,
`customer_not_linked`, `ambiguous_customer_identity`, `tenant_mismatch`,
`conflicting_customer_identity`, `customer_not_persisted`,
`no_active_salla_connection`, `connection_not_found_for_tenant`,
`connection_tenant_mismatch`, `connection_provider_mismatch`,
`connection_disabled`.

### Offer-bound confirmation

Confirmation means "yes, *that* address". The address surfaced to the
customer is recorded as an offer on the conversation; a later confirmation
is accepted only when an offer exists for this conversation and customer,
names the same address, and its fingerprint still matches the stored row.
A refresh between the offer and the reply therefore refuses rather than
recording approval of content the customer never saw, and an inquiry with
no prior offer selects nothing. No intent detection, keyword router or
customer regex was changed to achieve this.

## 6. Save and adoption evidence

`core/customer_address_persistence_evidence.py` names the scope of what was
actually persisted:

| Scope | Supports "held on file" | Supports "adopted as your delivery address" |
| --- | --- | --- |
| `none` | no | no |
| `conversation_state` | no | no |
| `imported_candidate` | yes | no |
| `selected_delivery_address` | yes | yes |

Evidence is produced only by re-reading the customer record after the
transaction. A writer returning `True`, an uncommitted `db.add`, a bridge
skip, an unavailable capability, an ambiguous identity and a failed commit
all resolve to `none`.

`customer_address_save_claim_guard` runs in the post-compose truth-guard
pipeline (between `shipment_truth_guard` and
`staff_escalation_truth_guard`). It resolves the evidence from the database
— never from the turn's own state — and removes any save/adoption claim the
evidence cannot carry, leaving the rest of the reply intact. It authors no
customer-facing prose and adds no template.

It judges **completed-action assertions only**, sentence by sentence. A
question ("shall we adopt your address as the default?") and a negation
("your address was *not* saved") contain the same words but assert no
completed action, so they are truthful LLM text and are preserved — the
earlier version deleted both. The two semantic classes (saved / adopted)
cover the Arabic attached-pronoun forms of "address" rather than being a
phrase list to extend.

When removing the unsupported claim leaves nothing usable, the send is
**suppressed and audited** (`suppressed_send=True`, reason
`…:scrubbed_empty`) — the same mechanic `shipment_truth_guard` uses via
`resolve_outbound_after_shipment_scrub`. Neither the false claim nor an
empty message is delivered.

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
  materialised by `create_all`. Until then the read projection degrades to
  legacy behaviour and no candidate is written.
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
