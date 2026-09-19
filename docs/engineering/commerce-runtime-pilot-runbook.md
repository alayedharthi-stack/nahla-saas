# Commerce runtime owner pilot — activation, verification and rollback

Operating document for the owner-only trial of the commerce runtime on
WhatsApp. It is **off** by default and reaches nothing until every variable
below is set. Contract: `docs/architecture/commerce-runtime-pilot-activation.md`.

---

## 1. Preconditions

| # | Precondition | How to check |
| --- | --- | --- |
| 1 | **All nine** runtime relations exist in the target database | the query below returns nine non-null values |
| 2 | The Anthropic key is present, and the pilot's **own** model is configured | `ANTHROPIC_API_KEY` present; `COMMERCE_RUNTIME_PILOT_MODEL` set to the approved model — the pilot never inherits `CLAUDE_MODEL` or any repository fallback |
| 3 | The test store's tenant id is known **from configuration**, not from a name | read the tenant id from the store's own connection row (step 3.1) |
| 4 | The test conversations' phone numbers are known | the owner's own test handsets, nothing else |
| 5 | The deployed revision contains the pilot | the merge commit of the pilot PR is the deployed commit |

The runtime requires all nine relations together and stays unavailable on a
partial schema, so the preflight must check all nine — a turns-plus-sequences
check passes on a foundation-only database that has no terminals table:

```sql
SELECT to_regclass('public.commerce_runtime_conversations'),
       to_regclass('public.commerce_runtime_turns'),
       to_regclass('public.commerce_runtime_turn_terminals'),
       to_regclass('public.commerce_runtime_effects'),
       to_regclass('public.commerce_runtime_effect_attempts'),
       to_regclass('public.commerce_runtime_effect_results'),
       to_regclass('public.commerce_runtime_delivery_sequences'),
       to_regclass('public.commerce_runtime_delivery_attempts'),
       to_regclass('public.commerce_runtime_delivery_receipts');
```

The service probes the same nine once per database and caches the answer, and
logs `[COMMERCE_RUNTIME] schema probe state=… missing=…` when it does. The cache
is per process and is **not** invalidated by applying the migration: after §1.2
runs, restart the service (or redeploy it) so the next probe sees the new
schema. Until it does, every turn reports `runtime_schema_unavailable` and the
legacy path answers, which is the safe direction.

Revisions `0108` and `0109` are merged and validated, but the normal bootstrap
target is pinned at `0093`, so a database that has never had them applied will
report `runtime_schema_unavailable` and the pilot will answer nothing. Applying
them is a deliberate operator step, taken with the owner's knowledge; §1.2 is
the job that does it. It creates only new tables — it alters no existing table,
changes no existing index or constraint, and reads, writes or backfills no
existing data.

### 1.2 Applying the schema (owner-approved, one-off)

`scripts/operators/commerce_runtime_pilot_migration.py` is the job, run the way
this repository already applies a production migration: a dedicated Railway
one-off service built from a pinned `ops/…` branch, `restartPolicyType: NEVER`,
whose only variables are the pilot database's `DATABASE_URL`, the confirmation
token, and the database the operator is authorising this run to touch.

```bash
NAHLA_COMMERCE_RUNTIME_MIGRATION_CONFIRM=RUN_COMMERCE_RUNTIME_0109 \
NAHLA_COMMERCE_RUNTIME_MIGRATION_TARGET=<host>[:<port>]/<database> \
  python -m scripts.operators.commerce_runtime_pilot_migration
```

`DATABASE_URL` says which database this service is *configured* for; it can
never say which one was *authorised*. The target is `host[:port]/database`
(port defaults to 5432), and the job compares it against the **effective**
connection parameters — the ones SQLAlchemy's own dialect hands the driver —
not against the URL's authority. Those are not the same thing: a PostgreSQL URL
may carry `host`, `hostaddr`, `port`, `dbname` or `service` as query parameters
and the driver honours them over the authority, so
`postgresql://u:p@approved.internal/pilot?host=other.internal` connects to
`other.internal`. Any such parameter is refused outright, loopback is matched as
an address rather than as text, the port is part of the comparison, and the
database name is compared with its case intact because PostgreSQL treats
`Pilot` and `pilot` as different databases.

It is fail-closed at both ends and refuses rather than repairs:

| Situation | Outcome |
| --- | --- |
| no confirmation token, or the wrong one | `RESULT=FAILED_PRECONDITION`, exit 2, nothing runs |
| `DATABASE_URL` missing, unparsable, not PostgreSQL, or a loopback address | `RESULT=FAILED_PRECONDITION`, exit 2, nothing runs |
| `NAHLA_COMMERCE_RUNTIME_MIGRATION_TARGET` unset, malformed, or not the effective host/port/database | `RESULT=FAILED_PRECONDITION`, exit 2, nothing runs |
| `DATABASE_URL` carries a target-changing query parameter (`host`, `hostaddr`, `port`, `dbname`, `service`, …) | `RESULT=FAILED_PRECONDITION`, exit 2, nothing runs |
| current revision is not one the contract accepts | `RESULT=FAILED_PRECONDITION`, exit 3, the observed value is printed |
| the relations present are not the ones the current revision creates | `RESULT=FAILED_PRECONDITION`, exit 3 — a schema that does not match its revision is never repaired |
| revision `0108` with exactly its three foundation relations | accepted: that is what `0108` creates, and the job upgrades it to `0109` |
| all nine exist and the revision is already `0109` | `RESULT=ALREADY_APPLIED`, exit 0, Alembic is not run |
| `alembic upgrade 0109` ran and all nine relations and the revision are verified | `RESULT=SUCCESS`, exit 0 |
| anything else after the upgrade | `RESULT=FAILED`, exit 4, with what was observed |

Every line is prefixed `[commerce-runtime-0109]`. Rolling the schema back is
`alembic downgrade 0107`, which the revision's own reversibility proofs cover;
it is only safe while the pilot is off and no runtime rows exist.

### 1.1 Reading the verified tenant and connection

```sql
SELECT id, tenant_id, phone_number_id, status
FROM whatsapp_connections
WHERE phone_number_id = '<the phone number id the webhook receives>';
```

The `tenant_id` from that row is the value for the allowlist. The guard re-checks
the same relationship on every turn, so a wrong id refuses rather than
misroutes.

---

## 2. Configuration

| Variable | Meaning | Pilot value |
| --- | --- | --- |
| `COMMERCE_RUNTIME_PILOT_ENABLED` | the switch | `true` |
| `COMMERCE_RUNTIME_PILOT_DRAINING` | hand back: no new turns, finish our own | unset during the trial; `true` during a rollback (§5) |
| `COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST` | comma-separated tenant ids | the one test store's id |
| `COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST` | comma-separated phone numbers | the owner's test handsets only |
| `COMMERCE_RUNTIME_PILOT_MODEL` | the model this pilot runs on | **required** — the approved model, named explicitly |
| `COMMERCE_RUNTIME_PILOT_MAX_STEPS` | optional, ≤ 6 | leave unset (4) |
| `COMMERCE_RUNTIME_PILOT_MAX_TOOL_CALLS` | optional, ≤ 8 | leave unset (6) |
| `COMMERCE_RUNTIME_PILOT_TOOL_TIMEOUT_SECONDS` | optional, ≤ 20 | leave unset (10) |
| `COMMERCE_RUNTIME_PILOT_PROVIDER_TIMEOUT_SECONDS` | optional, ≤ 60 | leave unset (35) |
| `COMMERCE_RUNTIME_PILOT_DEADLINE_SECONDS` | optional, ≤ 120 | leave unset (75) |

`COMMERCE_RUNTIME_PILOT_MODEL` is required and has no default. The loop is
model-neutral, and inheriting the legacy path's `CLAUDE_MODEL` or the
repository's fallback would mean activating a pilot on a model nobody chose for
it; with it unset the guard refuses every turn with `model_not_configured`
before touching the database, and the legacy path answers.

Both allowlists are required. Setting the tenant list alone enables nothing:
the guard also requires the recipient to be listed, so one allowlisted store
cannot become "every conversation in that store". Configuration can only make a
limit smaller; a value above its ceiling, or an unparsable one, falls back to
the bounded default.

---

## 3. Activation

1. Deploy the revision that contains the pilot with all three variables **unset**
   or `COMMERCE_RUNTIME_PILOT_ENABLED=false`. Confirm the service is healthy and
   that no `[COMMERCE_RUNTIME_PILOT]` line appears.
2. Set `COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST`,
   `COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST` and
   `COMMERCE_RUNTIME_PILOT_MODEL`, still with the switch off, and redeploy.
   Still nothing should route.
3. Set `COMMERCE_RUNTIME_PILOT_ENABLED=true` and redeploy. (A redeploy is also
   what refreshes the per-process schema probe; see §1.)
4. Send one message from an allowlisted handset and confirm exactly one
   `route=commerce_runtime` line with `replied=True`.
5. Send one message from a **non**-allowlisted handset in the same store and
   confirm a `route=legacy` line with `recipient_not_allowlisted`, and that the
   legacy brain answered it as before.
6. Run the handover check once, with nothing in flight, and confirm
   `RESULT=SETTLED`:

   ```bash
   python -m scripts.operators.commerce_runtime_pilot_handover
   ```

   Knowing it reports SETTLED while healthy is what makes its answer meaningful
   during a rollback.

---

## 4. What to watch

One line per routed turn:

```text
[COMMERCE_RUNTIME_PILOT] route=commerce_runtime {'turn_id': …, 'loop_status': …,
  'stop_reason': …, 'steps_used': …, 'tool_calls_used': …, 'tools_called': …,
  'evidence_refs': …, 'delivery_sequence_id': …, 'reused_delivery': …,
  'dispatch_status': …, 'provider_message_id': …, 'processing_outcome': …,
  'transport_outcome': …, 'customer_reach': …, 'input_tokens': …,
  'output_tokens': …, 'model': …, 'latency_ms': …, 'replied': …, 'reply_chars': …}
```

| Signal | Reading |
| --- | --- |
| `replied=True` with a `provider_message_id` | the only shape that means a message reached WhatsApp |
| `dispatch_status=unknown` | the send is uncertain; it is **not** retried, and the turn is `failed` |
| `dispatch_status=rejected` | the provider refused; nothing was sent |
| `loop_status=stopped` | no reply was accepted; `stop_reason` says why and nothing was sent |
| `reused_delivery=True` | a re-entry dispatched an intent reserved by an earlier invocation |
| `reused_dispatch=True` | this call sent nothing: it reported an outcome an earlier attempt established |
| `requested_model` vs `model` | what the platform asked for vs what the provider reported answering with; they should match |
| `reason=runtime_schema_unavailable` | one or more of the nine relations is missing on this database, **or** the process's cached probe predates the migration (§1) |
| `reason=model_not_configured` | `COMMERCE_RUNTIME_PILOT_MODEL` is unset; nothing routed |
| `reason=conversation_link_unverified` | the runtime conversation is not the one this application conversation owns; nothing ran |
| `reason=ownership_unavailable` | another invocation holds the turn, or an earlier turn in that conversation is still open |
| `reason=pilot_draining` | handing back (§5): new turns go to the legacy path |
| `reason=unfinished_…` | the guard refused a turn this runtime has **not finished**; it was kept from the legacy path and answered nothing |
| `reason=owned_…` | the guard refused a turn this runtime admitted and already finished; it was kept from the legacy path, which must not answer it a second time |
| `route=commerce_runtime_claim` | ownership was claimed at the dispatcher, so none of the dispatcher's own short circuits ran for this turn |

The customer's text never appears in the log line; only `reply_chars`.

Two further lines matter:

| Line | Reading |
| --- | --- |
| `[Idempotency] ALLOW duplicate inbound for commerce-runtime recovery` | a provider retry was let through because it carries an unfinished turn; nothing is resent, the ledger still decides |
| `[COMMERCE_RUNTIME] tool session handed to the reaper` | a tool call was abandoned on its timeout and its database session is being closed by the reaper once that call returns |

What was persisted for an accepted send says where its text came from:
`final_text_transformed` with `final_transform_reasons`, plus
`commerce_runtime_intent_sha256` — the digest of the text the ledger reserved —
so a divergence between what was reserved and what was transmitted is visible
rather than silently resolved. `wire_text_unobserved` means the send path's
transmitted text could not be read back; it is recorded as *unverified*, never
as unchanged.

Durable evidence for one turn:

```sql
SELECT * FROM commerce_runtime_turn_terminals WHERE turn_id = :turn_id;
SELECT * FROM commerce_runtime_delivery_sequences WHERE turn_id = :turn_id;
SELECT r.* FROM commerce_runtime_delivery_receipts r
  JOIN commerce_runtime_delivery_attempts a ON a.id = r.attempt_id
  WHERE a.sequence_id = :sequence_id ORDER BY r.receipt_no;
```

---

## 5. Handover and rollback

Switching the pilot off decides who takes **new** turns. It says nothing about
the turns the runtime already admitted, and flipping it while work is in flight
abandons three things at once: an admitted turn with no terminal (a customer
owed an answer or an honest record), a reply the loop reserved that nothing
dispatched, and a send with no receipt. So the supported rollback is a handover,
in three steps.

**Step 1 — drain.** Set `COMMERCE_RUNTIME_PILOT_DRAINING=true` and leave
`COMMERCE_RUNTIME_PILOT_ENABLED=true`. From that moment every **new** inbound
turn goes to the legacy path (`reason=pilot_draining`), and the turns this
runtime already admitted stay reachable and are finished as their inbound
messages are redelivered. Draining waives only the "no new turns" rule: every
other condition — the allowlists, the verified connection, the configured model
— still has to pass.

**Step 2 — quiesce ingress, and check that it happened.** Both flags are read
per process. Setting them in the service's configuration changes nothing in a
replica that is already running, so redeploy or restart every replica and
confirm each one actually came back on the new configuration. Nothing in the
codebase can verify this for you: the handover check reads one database and
cannot see another process about to admit a turn.

**Step 3 — verify.** Run the handover check, attesting step 2:

```bash
NAHLA_COMMERCE_RUNTIME_HANDOVER_INGRESS_QUIESCED=INGRESS_DRAINED_ALL_REPLICAS \
  python -m scripts.operators.commerce_runtime_pilot_handover
```

| Result | Meaning |
| --- | --- |
| `RESULT=SETTLED`, exit 0 | every count is zero **and** ingress was attested; it is safe to switch off |
| `RESULT=IN_FLIGHT`, exit 1 | `open_turns` / `reserved_undispatched` / `unresolved_attempts` / `unknown_outcomes` say what remains; keep draining and run it again |
| `RESULT=UNVERIFIED_INGRESS`, exit 1 | every count is zero, but nobody has attested step 2 — the counts alone cannot say it is safe |
| `RESULT=FAILED`, exit 3 | the database could not be read — never read as "settled" |
| `RESULT=FAILED_PRECONDITION`, exit 2 | not draining, no tenant allowlist, or more tenants configured than it will inspect |

`unknown_outcomes` counts sends whose recorded outcome is `unknown` and which no
later acceptance resolved. An unknown send may still be with the provider and
may still deliver, so it is **not** settled: establish what actually happened
and record it on that attempt (an accepted receipt with the provider's own
message id) before switching off.

The job is read-only: it sends nothing, writes nothing and changes no
configuration. It runs only while draining — in `on` the counts are a moving
target, and in `off` recovery has already stopped — and it refuses rather than
inspecting a subset when more tenants are configured than it will look at.

**Step 4 — stop.** Set `COMMERCE_RUNTIME_PILOT_ENABLED=false`. The rollback is
complete when a message from a previously allowlisted handset produces a
`route=legacy` line and the legacy brain's own trace source.

**Emergency stop.** `COMMERCE_RUNTIME_PILOT_ENABLED=false` stops **this process**
from taking new turns and from recovering. It is the right move when the pilot
is actively misbehaving, and it is worth being exact about what it is not: it is
not instantaneous across a fleet — each replica stops when it picks the change
up — and it does not stop an HTTP send already in flight. Afterwards, drain,
quiesce and run the handover check to see exactly what it left behind, and
finish those turns deliberately (below) before considering the rollback
complete.

**Narrower:** remove one number from
`COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST`, or empty it entirely. An empty
list permits nothing, so this is equivalent to switching the pilot off for that
store while leaving the tenant configured. A turn that runtime has **not
finished** is still not handed to the legacy path — the seam keeps it and
answers nothing (`reason=unfinished_recipient_not_allowlisted`) rather than let
a second runtime answer a message this one may already have answered.

**Full:** redeploy the previous revision. Nothing in the pilot changes a legacy
code path, so the legacy behaviour on the previous revision is unchanged.

### If a turn is stuck

A turn whose delivery was reserved but never dispatched stays the eligible turn,
and the conversation reports `ownership_unavailable` for later messages. This is
deliberate: the reply is reserved and must not be composed twice.

The next redelivery of the **same** provider message id resumes it. That is why
deduplication lets such a retry through: a duplicate carrying unfinished runtime
work reaches the handler (`[Idempotency] ALLOW duplicate inbound for
commerce-runtime recovery`), while a duplicate of *finished* work is dropped
exactly as before. Letting it through resends nothing — the ledger still refuses
to dispatch an attempt whose outcome is pending, accepted or unknown, so the
re-entry records what is already established rather than sending again.

If no retry arrives, finish the turn deliberately: dispatch or complete it
through the ledger. Do not delete the sequence, and never send its text by hand
as well as through the ledger.

---

## 6. What the pilot does not do

* It sends one text reply per turn — no rich, interactive or template message.
* It performs **no** commerce write: no order, payment, cancellation or coupon.
* It records no delivery or read receipt, so `customer_reach` stays `unknown`
  even for an accepted send.
* It has no reconciliation worker: an uncertain send is left uncertain rather
  than resolved automatically.
* It claims nothing about a saved or adopted address; that work is separate and
  separately approved.
* While it owns a conversation it owns **every** inbound turn in it, including
  ones the dispatcher's payment-receipt, payment-evidence, map-image and
  payment-claim short circuits would otherwise take. Those are skipped for an
  allowlisted recipient rather than racing the runtime; the pilot's read-only
  tools include no payment evidence, so it answers such a turn from what it can
  actually observe.
* A recovered send whose transmitted text could not be read back is kept in the
  store for the operator and is **not** shown to the model as a prior assistant
  turn: its body is the reserved intent, which the send path may have rewritten.
