# OrderFlowV2 address validation harness

Bounded validation apparatus for the OrderFlowV2 address path. It exists
because that path is **not reachable** from the existing Brain harness:
`run_sandbox_turn` evaluates `evaluate_live_merchant_brain_turn`, and
`backend/services/merchant_brain_turn.py` contains no `order_flow_v2`
reference at all. The owner, the ordinary compose, the guards, the
recovery and the outbound serialization all live in
`backend/routers/whatsapp_webhook.py`, ahead of the Brain.

Depends on PR #1096 (`8c9cb670`). This document authorises no merge,
deployment, shared migration, backfill, activation, live provider call or
customer message.

## 1. What it does and does not prove

**Proves** — with the real owner, real composer, real guards, real
recovery and real serializer, and a real model when the sandbox allows
one: which collection field and goal each model-bound call carried, what
the payload would have been, that the receipt matches it, and what the
runtime recorded about its own decision.

**Does not prove** — anything about WhatsApp. The transport is
**captured**, never dispatched. A synthetic `captured.<hex>` delivery id
proves local processing and nothing else. Meta's rendering, list/button
acceptance, provider-side dedup and receipt by a handset stay untested;
every record says `transport="captured"` and
`is_actual_provider_telemetry: false`.

## 2. Capture is fail-closed

`_post_wa` consults `capture_outbound_payload` immediately before
`provider_send_message`. Outside an acceptance context it returns `""`
and the ordinary dispatch happens, unchanged. Inside one, the send either
reaches a valid sink or **raises**:

| Condition | Result |
|---|---|
| no sink installed | `capture_sink_absent` — turn stops |
| sink not callable | `capture_sink_absent` — turn stops |
| sink raises | `capture_sink_failed` — turn stops |
| tenant differs from the context's | `tenant_mismatch` — turn stops |
| tenant not a positive int | `requested_tenant_invalid` — turn stops |
| valid sink | payload captured, synthetic delivery id returned |

There is no path from a failed capture to `provider_send_message`. Every
other `deny_external_egress` site is untouched: `salla_integration`,
`shipping`, `financial`, `automation`, `campaign` and `external_tool`
still deny. Sink and captures are `ContextVar`-scoped, so turns and
concurrent tasks never observe each other's payloads.

## 3. Evidence

Schema `internal_conversational_e2e_evidence_v3`. The signature envelope
(`internal_conversational_e2e_signature_v1`) is unchanged and covers the
whole canonical payload, so the new fields are signed by construction and
tamper-evident — and because `verify_session_evidence` never reads
`evidence_schema_version`, **v2 artifacts keep verifying**.

The address record is **harness-specific**. `TextProvenance` and
`PROVENANCE_FIELDS` are untouched: widening the shared reply provenance to
carry an address concern would turn a validation need into a platform
contract change with re-verification of every stored artifact behind it.

`address_turn` is optional in the schema — a Brain turn legitimately has
none — but **never optional in fact**. A scenario turn declaring
`expects_address_turn` and producing no record, an incomplete one, an
observation not taken at the orchestrator adapter, or three artifacts that
cannot be tied to one `turn_ref`, all fail the run
(`address_turn_evidence_missing`, `…_incomplete`, `…_unbound`,
`model_bound_call_evidence_incomplete`).

Per model-bound call, observed at the boundary and never reconstructed:
`call_index`, `stage` (declared by the branch that ran), `collection_field`,
`turn_ref`, `observed_at`, `response_goal`, `missing_field`,
`delivery_address_status`, `has_accepted_maps_reference` (**presence
only** — the URL is customer data and never enters an artifact),
`candidate_present`, `compose_source`, `fallback_reason`.

## 4. Classification and reporting

`execution_path` comes from what the run recorded about its own execution
(`address_reply_recovered` / `address_claim_send_suppressed` → recovery;
`address_reply_composed` / `address_claim_compose_attempted` → ordinary),
never from the payload. `delivered_surface`
(`buttons|list|text|none`) is a **separate dimension** — an ordinary turn
for a customer with no saved addresses carries no choices and is still
ordinary.

`of2_path_report` splits **natural** outcomes from **injected mechanism
checks**, each with its own denominator, and carries the observation
window and per-path sample counts with p50/p95. Injected turns never
contribute to a natural rate. `representative_of_production: false` is
emitted unconditionally: a small synthetic session on one sandbox tenant
is not a production rate.

## 5. Migration prerequisite — bounded to 0109 → 0110

`alembic upgrade 0110` **from 0093 traverses 17 revisions**, 8 of which
alter existing tables (0094, 0095, 0096, 0097, 0102, 0105, 0106, 0107),
plus 0101/0104 which change constraints and indexes. That run is database
**preparation**, not evidence, and its failures are preparation failures.

Never `alembic stamp` a `create_all`-built database to skip the traversal:
a stamp asserts a schema state nobody verified, while 0110 reasons only
about `customer_address_provenance`.

Verification is `0109 → 0110` only, in two prepared states:

* **A — table absent.** Database at 0109, application never started. 0110
  creates the table, both unique keys and the index.
* **B — ORM-created.** Database at 0109, application started once so
  `Base.metadata.create_all` materialises the ORM shape: same columns,
  same unique constraint, same index, **without** the server-side
  defaults (the ORM's are Python-side). 0110 then reconciles that exact
  shape additively.

State B precisely, because "fill missing values" and "no row rewritten"
must not be promised together — they are different operations on
different things:

| Operation | What is permitted |
|---|---|
| Missing **column** | added |
| Missing **server default** | set on the column |
| Pre-existing **NULL** in `selection_state` / `created_at` / `updated_at` | initialised to that column's default (`'candidate'`, `now()`, `now()`) — this writes a value only where there is none |
| Existing **non-NULL** value | never touched, never normalised, never overwritten |
| `NOT NULL` | enforced only after the NULLs above are initialised |
| Missing FK / unique / index | created |
| Anything else | not permitted — nothing dropped, no row rewritten |

A partial legacy shape that is neither A nor B — a table with extra
columns, a differently-named constraint, or a column of a different type —
is **out of scope for this verification**. Record it and stop; do not
adapt the migration to it. The migration implementation is not to be
modified unless this bounded verification exposes a concrete defect.

## 6. Ready-to-run sequence

Preparation (disposable database only; never canonical or shared):

```bash
# P0 — capture the effective state, read-only
env | grep -E '^(ORDER_FLOW_V2_(ENABLED|SHADOW_ENABLED|ENFORCE_TENANTS|DISABLED_TENANTS)|LEGACY_ORDER_FLOW_DISABLED)='
psql "$SANDBOX_DB" -c "select version_num from alembic_version"
psql "$SANDBOX_DB" -c "\d+ customer_address_provenance"

# P1 — prepare to 0109 by a RECORDED path (preparation, not evidence)
cd database && DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0088
              DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0093
              DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0109

# P1a — State A: capture the shape, upgrade, diff
psql "$SANDBOX_DB" -c "\d+ customer_address_provenance" > /tmp/A.before
DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0110
psql "$SANDBOX_DB" -c "\d+ customer_address_provenance" > /tmp/A.after

# P1b — State B: fresh database at 0109, start the app once, then upgrade
#        (create_all materialises the ORM shape before Alembic sees it)
```

Execution:

```bash
export NAHLA_INTERNAL_E2E_ENABLED=1 NAHLA_INTERNAL_E2E_CONFIRM=1
export NAHLA_INTERNAL_E2E_DATABASE_URL="$SANDBOX_DB"
export NAHLA_INTERNAL_E2E_TENANT_ALLOWLIST="$SANDBOX_TENANT"
export NAHLA_INTERNAL_E2E_TEST_PHONE="$SYNTHETIC_PHONE"
export NAHLA_INTERNAL_E2E_PHONE_ALLOWLIST="$SYNTHETIC_PHONE"
export NAHLA_INTERNAL_E2E_PINNED_REVISION="$DEPLOYED_REVISION"
export NAHLA_INTERNAL_E2E_EVIDENCE_HMAC_KEY=...
export NAHLA_INTERNAL_E2E_ATTESTATION_HMAC_KEY=...
export NAHLA_INTERNAL_E2E_ATTESTATION_JSON=... NAHLA_INTERNAL_E2E_ATTESTATION_SIGNATURE=...
export NAHLA_INTERNAL_E2E_NETWORK_FIREWALL_CONFIRM=1
export NAHLA_INTERNAL_E2E_LLM_ENABLED=1 NAHLA_INTERNAL_E2E_LLM_HOST_ALLOWLIST=...
export NAHLA_INTERNAL_E2E_SESSION_DIR=/var/tmp/of2-validation
export ORDER_FLOW_V2_ENFORCE_TENANTS="$SANDBOX_TENANT"

python scripts/operators/internal_conversational_e2e_session.py \
  preflight --tenant-id "$SANDBOX_TENANT"

python scripts/operators/internal_conversational_e2e_session.py \
  run --tenant-id "$SANDBOX_TENANT" --scenarios docs/engineering/of2-address-scenarios.json
```

Scenario manifest (`internal_conversational_e2e_scenarios_v2`), one turn:

```json
{
  "text": "الرياض",
  "mode": "of2",
  "expected_status": "evaluated",
  "failure_injection": "none",
  "expects_address_turn": true,
  "expected_denials": []
}
```

`failure_injection` is one of `none`, `provider_error`,
`provider_timeout`, `guard_boundary`. A turn declaring
`expects_address_turn` outside `mode: "of2"` is rejected, as is an OF2
turn in a v1 manifest — a v1 manifest is never silently demoted to a
Brain turn, which would measure a different path than the scenario asked
for.

## 7. Stop conditions and rollback

Stop on: any durable write outside the sandbox database; any egress denial
that was not expected; `provenance_incomplete`; any
`address_turn_evidence_*` blocker; a save/adoption claim in a captured
payload with no evidence row; a receipt that does not match the recorded
showing; a tenant or phone that is not allowlisted; a runtime-revision
mismatch.

Rollback: dispose the sandbox database/service named in the attestation —
cleanup is deliberately outside the application — unset the harness
environment, and remove the tenant from `ORDER_FLOW_V2_ENFORCE_TENANTS`.
Never run `alembic downgrade 0109` against a shared database: it drops a
table `create_all` may have been populating since deploy.

## 8. Offline coverage

`tests/test_of2_validation_harness.py` covers valid capture, capture
failure, invalid context, context isolation across turns and asyncio
tasks, boundary observation, path/surface separation, evidence
completeness and binding, signing and tamper rejection, and v2
compatibility. Three of them drive the **real** `_post_wa` with a
recording stub in place of the provider — never a real provider — so
"never reaches dispatch" is observed rather than argued.

`tests/test_salla_customer_address_candidates.py` gains two regressions
for the repaired save sites; both fail on `8c9cb670` with
`KeyError: 'address_turn_ref'`.
