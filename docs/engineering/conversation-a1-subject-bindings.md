# Conversation A1-subject bindings (PR1)

Platform-owned persisted bridge from a **proofable WhatsApp conversation** to an
**authoritative Nahla-internal A1 subject**. This slice is write-only substrate
for a future read bridge (PR2) and AI conditional-coupon facts.

## Scope (PR1)

- Table `conversation_a1_subject_bindings` (migration `0089`)
- `write_authoritative_internal_binding_from_verified_order` service
- Hook in `nahla_order_bridge` after `apply_nahla_internal_order_identity`
- Closed enums, privacy-safe logs, PostgreSQL + unit tests

**Not in PR1:** read bridge API, AI resolver changes, feature flags, external
profile bindings, coupon issuance.

## Association evidence

The WA order bridge receives a **concrete `conversation` object** with
`conversation.id` set by upstream webhook routing — not inferred from phone or
`external_id` parsing alone.

Authoritative internal link evidence comes from the **order row** after
`apply_nahla_internal_order_identity`:

| Field | Required value |
|-------|----------------|
| `order_source_kind` | `nahla_internal` |
| `customer_link_state` | `verified` |
| `customer_link_evidence_class` | `authoritative` |
| `identity_namespace` | `nahla_internal_order_v1` |
| `customer_id` | non-null (subject source) |

`conversation.customer_id` is **never** used to derive the bound subject.

## Binding semantics

| State | Meaning |
|-------|---------|
| `active` | Current authoritative binding for `(tenant_id, conversation_id)` |
| `superseded` | Replaced by a conflicting authoritative rebind in one transaction |
| `revoked` | Reserved for explicit revocation writers (future) |

At most one `active` row per `(tenant_id, conversation_id)` (partial unique index).
`active` rows must have `revoked_at IS NULL`; `revoked` and `superseded` rows
must have a non-null `revoked_at`. PR1 implements `superseded`; it creates no
standalone revoke writer.

## Closed `binding_source` (writers)

| Source | PR1 writer |
|--------|------------|
| `wa_order_bridge_authoritative_internal` | Yes — `nahla_order_bridge` |
| `salla_order_conversation_attestation` | Reserved (PR5) |
| `provider_oauth_session` | Reserved (PR5) |

## Privacy

Binding logs emit tenant + outcome enums only. No conversation/customer/order
IDs, phone numbers, or raw provenance refs in log lines.

## Activation

PR1 validates **source state** only. AI consumption requires PR2 read bridge +
separate flag gate (see platform identity bridge audit).

## Alembic topology and bootstrap safety

`0088` (A1-Validate) and `0089` (these conversation bindings) are sibling
heads with `down_revision = "0087"`.

- Normal application bootstrap, Compose migration, and the admin migration
  endpoint explicitly target **`0089`**. They never use bare `head` and never
  invoke `0088`.
- `0088` is a deferred validation maintenance path. It runs only from an
  environment pinned exactly at `0087`, through
  `scripts/operators/staging_migration_0087_to_0088.py`, after the G4 gate and
  its read-only violation preflight pass.
- The guarded `0087 → 0088` operation rejects `0089`; applying conversation
  bindings and A1 validation in one bootstrap operation is unsupported.
