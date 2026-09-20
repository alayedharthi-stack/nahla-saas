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
none — but **never optional in fact**. An OrderFlowV2 scenario turn must
state `expects_address_turn` explicitly (the operator refuses a manifest
that omits it) and, when true, must declare what it expects. Signing
protects bytes after they are written; it says nothing about whether they
describe the turn the scenario asked for, so the validator checks meaning:

| Refused | Code |
|---|---|
| declared but absent, or empty | `address_turn_evidence_missing` |
| no stated field/goal expectation | `address_turn_expectations_missing` |
| a call contradicts the expected field, goal, `missing_field`, address status or maps presence | `address_turn_expectation_unmet` |
| stage `unspecified`, outcome unrecorded, or observed anywhere but the adapter | `model_bound_call_evidence_incomplete` |
| wire offer ≠ recorded offer, compared as **address identities** | `address_turn_receipt_mismatch` |
| missing payload digest, delivery id, persisted row id, or a `turn_ref` that does not match across all three | `address_turn_evidence_unbound` |
| an injection declared but never fired | `address_turn_injection_not_executed` |
| unresolved execution path, missing provenance, transport not `captured` | `address_turn_evidence_incomplete` |

A turn where compose could not be **entered** legitimately has zero model
calls; that stays reportable provided the record carries the fallback
provenance saying so, rather than simply omitting everything.

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
checks**, each with its own denominator. The denominator counts turns the
**owner** established as address collection turns (`collection_field` in
`delivery_address`/`city`); anything else is excluded and reported as
`non_address_turns_excluded`, so a customer-name turn is never filed as an
address attempt. Outcomes are distinguished rather than bucketed —
`composed`, `fallback`, `refused_claim`, `unresolved` — because healthy
ordinary composition and an ordinary provider-failure fallback are
different results. Samples and p50/p95 are reported per path **and**
outcome, with the observation window, and
`representative_of_production: false` is emitted unconditionally.

`failure_injection` is not a label. `provider_error` and
`provider_timeout` install a real fault at the orchestrator adapter,
immediately before the provider call and after the call is recorded as
attempted; `guard_boundary` installs one at the OrderFlowV2 outbound
guard, which is the failure the recovery path exists for. Both record
that they fired, and an injection that did not fire fails the turn.

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

Preparation (disposable database only; never canonical or shared). Every
block starts from the repository root — the Alembic commands run in
`database/` inside a subshell so the shell returns to the root afterwards:

```bash
export REPO_ROOT="$PWD"
export SANDBOX_DB="postgresql://USER:PASS@HOST:PORT/DBNAME"   # disposable

# P0 — capture the effective state, read-only
env | grep -E '^(ORDER_FLOW_V2_(ENABLED|SHADOW_ENABLED|ENFORCE_TENANTS|DISABLED_TENANTS)|LEGACY_ORDER_FLOW_DISABLED)=' || true
psql "$SANDBOX_DB" -c "select version_num from alembic_version"
psql "$SANDBOX_DB" -c "\d+ customer_address_provenance" || echo "table absent"

# P1 — prepare to 0109 by a RECORDED path (preparation, not evidence)
( cd "$REPO_ROOT/database" \
  && DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0088 \
  && DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0093 \
  && DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0109 )
psql "$SANDBOX_DB" -c "select version_num from alembic_version"   # expect 0109

# P1a — STATE A: table absent, application never started
psql "$SANDBOX_DB" -c "\d+ customer_address_provenance" > /tmp/stateA.before 2>&1
( cd "$REPO_ROOT/database" \
  && DATABASE_URL="$SANDBOX_DB" python -m alembic upgrade 0110 )
psql "$SANDBOX_DB" -c "\d+ customer_address_provenance" > /tmp/stateA.after
diff /tmp/stateA.before /tmp/stateA.after || true

# P1b — STATE B: a SECOND disposable database at 0109, where the
#        application starts once so create_all materialises the ORM shape
#        BEFORE Alembic sees it. Executable, not described:
export SANDBOX_DB_B="postgresql://USER:PASS@HOST:PORT/DBNAME_B"   # disposable
( cd "$REPO_ROOT/database" \
  && DATABASE_URL="$SANDBOX_DB_B" python -m alembic upgrade 0088 \
  && DATABASE_URL="$SANDBOX_DB_B" python -m alembic upgrade 0093 \
  && DATABASE_URL="$SANDBOX_DB_B" python -m alembic upgrade 0109 )
# Start the app once against DB B, let lifespan run create_all, then stop it.
# NOTE: main.app is the outermost raw-ASGI wrapper (backend/main.py), not the
# FastAPI instance. The lifespan lives on main._FASTAPI_APPLICATION; using
# main.app here raises AttributeError and materialises nothing. The sleep must
# outlast the bootstrap, which runs create_all in a BACKGROUND task.
( cd "$REPO_ROOT/backend" \
  && DATABASE_URL="$SANDBOX_DB_B" NAHLA_SKIP_DB_BOOTSTRAP=1 \
     timeout 240 python -c "
import asyncio, main
_fast = main._FASTAPI_APPLICATION
async def _boot():
    async with _fast.router.lifespan_context(_fast):
        await asyncio.sleep(25)
asyncio.run(_boot())
" )
# Prove the activation boundary before Alembic sees the database:
psql "$SANDBOX_DB_B" -tAc "select to_regclass('public.customer_address_provenance')"
psql "$SANDBOX_DB_B" -c "\d+ customer_address_provenance" > /tmp/stateB.before
( cd "$REPO_ROOT/database" \
  && DATABASE_URL="$SANDBOX_DB_B" python -m alembic upgrade 0110 )
psql "$SANDBOX_DB_B" -c "\d+ customer_address_provenance" > /tmp/stateB.after
diff /tmp/stateB.before /tmp/stateB.after || true
# Expect ONLY: server defaults set, NOT NULL enforced, missing
# FK/unique/index created. No column dropped, no existing value changed.
```

### 6.1 Executed result (head `62b34afd`, PostgreSQL 16, disposable databases)

This sequence has now been **run**, not only described. Both supported
states were prepared to 0109 by the recorded path and then taken to 0110.

| Check | State A (table absent) | State B (`create_all` first) |
|-------|------------------------|------------------------------|
| Table before 0110 | absent (`Did not find any relation`) | **present** — materialised by the app's lifespan at 0109, with no `alembic upgrade 0110` |
| `alembic_version` after the app start | n/a | still `0109,0088` — unmoved |
| 0110 outcome | table created with the full shape: 18 columns, PK, `ix_…_source`, `uq_…_address`, the `COALESCE`-partial `uq_…_source_revision`, and all three FKs | `diff` of `\d+` before → after is **empty**: no shape change |
| Seeded row (`selection_state='selected'`, `created_at` 2020-01-01, `updated_at` 2020-01-02, fingerprint, `source_ref`) | n/a | **byte-identical after 0110** — `selected` not normalised to `candidate`, neither timestamp rewritten |

State B is the operationally important one, and it is the claim the
deployment note rests on: **the application materialises this table by
itself**. That is now observed, not inferred from reading
`backend/main.py`.

State B was run twice — once with the app's own Alembic bootstrap left
enabled, and once with `NAHLA_SKIP_DB_BOOTSTRAP=1` as the block above
documents — with identical results. The second run is the stronger
evidence: with the bootstrap skipped the application never invokes
Alembic at all, `alembic_version` stays at `0109,0088`, and the table
still appears. Nothing but `create_all` can account for it.

**One branch of 0110 is not exercised by either supported state, and is
not claimed as verified.** The fill step at
`database/migrations/versions/0110_customer_address_provenance.py:255`
(`UPDATE … WHERE "<col>" IS NULL`) can only run where a column is already
present and nullable. In State A the table does not exist, and in State B
`create_all` emits the columns `NOT NULL` with their server defaults
already set — so the `alter_column`/fill/`NOT NULL` sequence is a no-op in
both, which is exactly why the State B diff is empty. A database holding a
NULL in `selection_state`, `created_at` or `updated_at` is a third,
partial shape, which §5 places **out of scope**: record it and stop.
Manufacturing one here to exercise the branch would be inventing a shape
the migration is not authorised to meet, so it was not done.


Execution, from the repository root:

Scenario manifest (`internal_conversational_e2e_scenarios_v2`). A complete
OrderFlowV2 turn — every field below is required and the loader refuses
the manifest without them:

```json
{
  "text": "الرياض",
  "mode": "of2",
  "expected_status": "evaluated",
  "failure_injection": "none",
  "expects_address_turn": true,
  "expected_operational_result": "address_reply",
  "expectations": {
    "collection_field": "city",
    "response_goal": "collect_delivery_city",
    "missing_field": "city",
    "delivery_address_status": "accepted",
    "requires_accepted_maps_reference": true,
    "expected_execution_path": "ordinary"
  },
  "expected_denials": []
}
```

`failure_injection` is one of `none`, `provider_error`,
`provider_timeout`, `guard_boundary`. `expected_operational_result` is
one of `address_reply` (a new collection reply was captured),
`structured_selection` (a captured choice was consumed and moved state)
or `refusal` (an expected gate, nothing sent). `expectations` must state
`collection_field`, `response_goal` **and** `missing_field` — any one of
them alone asserts almost nothing. A turn declaring
`expects_address_turn` outside `mode: "of2"` is rejected, as is an
OrderFlowV2 turn in a v1 manifest.

A continuation turn replays an action id from the previous turn's
**captured** payload:

```json
{
  "text": "اختيار العنوان",
  "mode": "of2",
  "expected_status": "evaluated",
  "failure_injection": "none",
  "expects_address_turn": false,
  "expected_operational_result": "structured_selection",
  "expected_state_delta_keys": ["conversation_metadata_fingerprint"],
  "captured_action_index": 0,
  "inbound_metadata": {"button_id": "__captured_action_id__"},
  "expected_denials": []
}
```

An index that names no captured action, or one out of range, **refuses
before the turn runs** (`captured_action_reference_unresolved` /
`…_out_of_range`) rather than replaying an unresolved marker.

## 6a. Per-scenario fixture state — still to be supplied

The scenarios describe incompatible customer states (one saved address,
several, none, an accepted address with the city missing). The operator
creates **one** pristine conversation and carries it through the session,
so the manifest's expectations cannot be satisfied by a single seeded
state. Before a sandbox run, each scenario needs its fixture prepared and
isolated — shared state preserved only for the intentional continuation
pair. That preparation is **not** in this branch; it is the remaining
handoff item, and a run started without it will fail on the first
scenario whose state does not match, which is the correct outcome.

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

### Coverage boundary, stated

The runner is driven end-to-end against the **real** OrderFlowV2 send
block — real owner result, real composer, real guards and recovery, real
serializer, real `_post_wa` — with the provider substituted *below* the
adapter so the observation hook still runs. It does **not** yet enter
`_handle_merchant_message` itself: in an offline SQLite sandbox that
handler returns without producing an address turn, because the owner
needs a cart, a verified identity and a missing-address checkout state
that the seeded fixture does not yet establish. Patching the owner's
internals and calling the result "the real owner path" would be worse
than saying so. Closing that gap belongs with the fixture work in §6a.

`tests/test_of2_validation_harness.py` covers valid capture, capture
failure, invalid context, isolation across turns and asyncio tasks,
boundary observation, path/surface separation, evidence meaning and
binding, signing and tamper rejection, v2 compatibility, runner identity
refusal, denial serialization, injection firing at each seam, and the
captured-send dedup lifecycle. Several drive the **real** `_post_wa` and
the **real** composer/adapter chain with a recording stub in place of the
provider — never a real provider — so "never reaches dispatch" and
"observations survive the thread boundary" are observed rather than
argued.

`tests/test_salla_customer_address_candidates.py` gains two regressions
for the repaired save sites; both fail on `8c9cb670` with
`KeyError: 'address_turn_ref'`.
