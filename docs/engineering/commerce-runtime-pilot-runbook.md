# Commerce runtime owner pilot — activation, verification and rollback

Operating document for the owner-only trial of the commerce runtime on
WhatsApp. It is **off** by default and reaches nothing until every variable
below is set. Contract: `docs/architecture/commerce-runtime-pilot-activation.md`.

---

## 1. Preconditions

| # | Precondition | How to check |
| --- | --- | --- |
| 1 | **All twelve** runtime relations exist in the target database | the query below returns twelve non-null values |
| 2 | The Anthropic key is present, and the pilot's **own** model is configured | `ANTHROPIC_API_KEY` present; `COMMERCE_RUNTIME_PILOT_MODEL` set to the approved model — the pilot never inherits `CLAUDE_MODEL` or any repository fallback |
| 3 | The test store's tenant id is known **from configuration**, not from a name | read the tenant id from the store's own connection row (step 3.1) |
| 4 | The test conversations' phone numbers are known | the owner's own test handsets, nothing else |
| 5 | The deployed revision contains the pilot | the merge commit of the pilot PR is the deployed commit |
| 6 | **`META_APP_SECRET` is set and matches the Meta app** | the signature audit shows `valid` for live traffic. A pilot obligation is taken only from a request whose `X-Hub-Signature-256` **verified** — whatever `META_WEBHOOK_ENFORCE_SIGNATURE` says for the legacy path. With the secret missing or wrong, every pilot-scoped inbound is answered `503 pilot_scope_unauthenticated`, nothing is recorded and nothing is processed, and Meta retries until the secret is fixed |

Choosing the model closes one precondition, not the activation. Every row above
is a separate prerequisite, and the owner's own authorisation of the tenant, the
verified connection, the recipient handsets and — where §1.2 applies — the exact
shared migration target are separate again. A pilot with an approved model and
any one of those missing is not ready to activate.

The provider integration is **Anthropic-specific**. The agent loop is
model-neutral in its contracts, but the adapter, the tool-call shape and the
error handling the pilot ships are written against Anthropic's API; running this
pilot on another provider is not a configuration change.

The runtime requires all **twelve** relations together and stays unavailable on
a partial schema, so the preflight must check all twelve — a turns-plus-sequences
check passes on a foundation-only database that has no terminals table, and a
nine-relation check passes on a database that can admit turns but has nowhere to
record an acceptance:

```sql
SELECT to_regclass('public.commerce_runtime_conversations'),
       to_regclass('public.commerce_runtime_turns'),
       to_regclass('public.commerce_runtime_turn_terminals'),
       to_regclass('public.commerce_runtime_effects'),
       to_regclass('public.commerce_runtime_effect_attempts'),
       to_regclass('public.commerce_runtime_effect_results'),
       to_regclass('public.commerce_runtime_delivery_sequences'),
       to_regclass('public.commerce_runtime_delivery_attempts'),
       to_regclass('public.commerce_runtime_delivery_receipts'),
       to_regclass('public.commerce_runtime_handover_barrier'),
       to_regclass('public.commerce_runtime_handover_workers'),
       to_regclass('public.commerce_runtime_deferred_inbound');
```

The first nine are the runtime's own state (revisions `0108` and `0109`); the
last three are the handover's (revision `0111`). **The pilot does not run
without the handover three**, and `runtime_schema_available` requires all twelve
— there is one readiness question, not two.

What a missing schema does **not** do is release work the runtime has already
established as its own. Fresh HTTP traffic on a database at `0109` is not
pilot-scoped, so it behaves exactly as it does today and the legacy path answers
it. But once the guard has said this tenant, recipient and connection are the
pilot's — or a durable acceptance record exists for the message — a barrier or
ledger that cannot be read is **not** evidence the runtime does not own the
turn: the claim is held (`basis=ownership_unavailable`), the COD route and the
legacy brain are both refused, and nothing answers. That is silent, so check for
all twelve rather than assuming the pilot is live because the flag is on, and
grep for `ownership state unreadable` when it is not.

The service probes all twelve once per database and caches the answer, and
logs `[COMMERCE_RUNTIME] schema probe state=… missing=…` when it does. The cache
is per process and is **not** invalidated by applying the migration: after §1.2
runs, restart the service (or redeploy it) so the next probe sees the new
schema. Until it does, every turn reports `runtime_schema_unavailable` and the
legacy path answers, which is the safe direction.

Revisions `0108`, `0109` and `0111` are merged and validated, but the normal
bootstrap target is pinned at `0093`, so a database that has never had them
applied will report `runtime_schema_unavailable` and the pilot will answer
nothing. Applying them is a deliberate operator step, taken with the owner's
knowledge; §1.2 is the job that does it. It creates only new tables — it alters
no existing table, changes no existing index or constraint, and reads, writes or
backfills no existing data.

**Rollout dependency.** `0111` is required before activation, not optional. It
adds the three handover relations and nothing else, it is reversible
(`alembic downgrade 0111@-1` drops exactly what it created — see §1.3 for why
that spelling and no other), and it is refused rather than reconciled when a
relation of that name already exists with a different definition. It has been
applied and reversed in isolated databases only; applying it to the shared
pilot database is a separate authorisation.

### 1.2 Applying the schema (owner-approved, one-off)

`scripts/operators/commerce_runtime_pilot_migration.py` is the job, run the way
this repository already applies a production migration: a dedicated Railway
one-off service built from a pinned `ops/…` branch, `restartPolicyType: NEVER`,
whose only variables are the pilot database's `DATABASE_URL`, the confirmation
token, and the database the operator is authorising this run to touch.

```bash
NAHLA_COMMERCE_RUNTIME_MIGRATION_CONFIRM=RUN_COMMERCE_RUNTIME_0111 \
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
| revision `0108` with exactly its three foundation relations, or `0109` **or `0110`** with those plus the ledger six | accepted: that is what each revision creates, and the job upgrades to `0111` |
| all twelve exist and the revision set is `{0111}` or `{0110, 0111}` (with or without `0088`) | `RESULT=ALREADY_APPLIED`, exit 0, Alembic is not run |
| `alembic upgrade 0111` ran and all twelve relations and the revision are verified | `RESULT=SUCCESS`, exit 0 |
| anything else after the upgrade | `RESULT=FAILED`, exit 4, with what was observed |

Every line is prefixed `[commerce-runtime-0111]`. The confirmation token names
the revision it authorises, `RUN_COMMERCE_RUNTIME_0111`, and nothing else is a
confirmation.

### 1.3 The address sibling, and rolling back only the runtime

`0110` (customer-address provenance, PR #1096) and `0111` (this runtime's
handover) are **siblings**: both revise `0109`, neither depends on the other, and
neither PR waits for the other. Every one of these applied states is valid and
the job accepts or recognises each:

| Applied | `alembic_version` | Meaning |
| --- | --- | --- |
| neither | `{0109}` | the ledger revision; a start state for `0111` |
| address first | `{0110}` | still a start state for `0111` — the runtime upgrade never requires or applies the address one |
| runtime first | `{0111}` | already applied; `0110` may be applied afterwards by its own job |
| both, either order | `{0110, 0111}` | already applied; two heads is how Alembic represents two branches |

`alembic upgrade head` was already forbidden here; with two application heads
it is ambiguous as well.

**Rolling back the runtime revision alone is `alembic downgrade 0111@-1` and
nothing else.** This was proved on real PostgreSQL with both siblings applied
rather than read off the documentation: `alembic downgrade 0109` *and*
`alembic downgrade 0111-1` both resolve to the common ancestor and remove
`0110` too — the `customer_address_provenance` table with it — whereas
`0111@-1` steps one revision back along this branch only and leaves `0110` and
its table in place. `scripts.operators.commerce_runtime_pilot_migration_contract.build_downgrade_argv`
is the one spelling, and `test_commerce_runtime_handover_migration_pg` holds it
to that on a combined schema. A rollback is only safe while the pilot is off
and no runtime rows exist.

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
| `COMMERCE_RUNTIME_PILOT_DRAINING` | take **this process** out of rotation: no new turns, finish our own. Not the handover mechanism (§5) | unset; a handover uses the shared barrier |
| `COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST` | comma-separated tenant ids | the one test store's id |
| `COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST` | comma-separated phone numbers | the owner's test handsets only |
| `COMMERCE_RUNTIME_PILOT_MODEL` | the model this pilot runs on | **required** — the approved model, named explicitly |
| `COMMERCE_RUNTIME_PILOT_MAX_STEPS` | optional, ≤ 6 | leave unset (4) |
| `COMMERCE_RUNTIME_PILOT_MAX_TOOL_CALLS` | optional, ≤ 8 | leave unset (6). Also the most tool requests one provider step may carry: the API never tells the model a per-step ceiling, so a bundle the budget can pay for is run whole rather than refused as `provider_invalid` |
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
6. Run the handover **status** command once, with nothing in flight, and read
   what it reports. This is the state §3 has just produced — the pilot on, the
   barrier open — so `status` is the command that runs here; `settle` is a step
   of the handover itself and refuses while the barrier is open (§5).

   ```bash
   COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST=<the pilot's tenant id> \
     python -m scripts.operators.commerce_runtime_pilot_handover status
   ```

   Expect, for each configured tenant:

   ```text
   [COMMERCE_RUNTIME_HANDOVER] tenant=<id> barrier=open generation=0 converged=False …
   [COMMERCE_RUNTIME_HANDOVER] tenant_id=<id> open_turns=0 reserved_undispatched=0
     unresolved_attempts=0 unknown_outcomes=0 settled=True
   [COMMERCE_RUNTIME_HANDOVER] tenant=<id> blockers=['barrier_is_open_not_draining']
   [COMMERCE_RUNTIME_HANDOVER] RESULT=REPORTED tenants=1
   ```

   All four counts zero while healthy is what makes a non-zero count meaningful
   later. `barrier_is_open_not_draining` is the expected — and only — blocker
   here: a tenant that is serving has not been drained, which is correct.

---

## 4. What to watch

One line per routed turn:

```text
[COMMERCE_RUNTIME_PILOT] route=commerce_runtime {'turn_id': …, 'loop_status': …,
  'stop_reason': …, 'stop_detail': …, 'steps_used': …, 'tool_calls_used': …, 'tools_called': …,
  'evidence_refs': …, 'delivery_sequence_id': …, 'reused_delivery': …,
  'dispatch_status': …, 'provider_message_id': …, 'processing_outcome': …,
  'transport_outcome': …, 'customer_reach': …, 'input_tokens': …,
  'output_tokens': …, 'model': …, 'latency_ms': …, 'replied': …, 'reply_chars': …}
```

| Signal | Reading |
| --- | --- |
| `replied=True` with a `provider_message_id` | the only shape that means the provider **accepted** the send. Acceptance is not confirmed delivery to the customer: no delivery or read receipt is recorded, so `customer_reach` stays `unknown` |
| `dispatch_status=unknown` | the send is uncertain; it is **not** retried, and the turn is `failed` |
| `dispatch_status=rejected` | the provider refused; nothing was sent |
| `loop_status=stopped` | no reply was accepted; `stop_reason` says why and nothing was sent |
| `stop_detail=…` | the loop's own account of that stop, as `key=value;…`: the provider's stated reason (`provider_reason=tool_requests_exceed_declared_maximum:4>3`), the validation message, the limit that was exceeded (`limit=max_tool_calls;remaining=…;requested=…`), the tool that was repeated. The same detail is stored under `stop_detail` in the failed turn's terminal. Never the customer's text |
| `reused_delivery=True` | a re-entry dispatched an intent reserved by an earlier invocation |
| `reused_dispatch=True` | this call sent nothing: it reported an outcome an earlier attempt established |
| `requested_model` vs `model` | what the platform asked for vs what the provider reported answering with; they should match |
| `reason=runtime_schema_unavailable` | one or more of the twelve relations is missing on this database, **or** the process's cached probe predates the migration (§1) |
| `reason=model_not_configured` | `COMMERCE_RUNTIME_PILOT_MODEL` is unset; nothing routed |
| `reason=conversation_link_unverified` | the runtime conversation is not the one this application conversation owns; nothing ran |
| `reason=ownership_unavailable` | another invocation holds the turn, or an earlier turn in that conversation is still open |
| `reason=pilot_draining` | this process is out of rotation: no new turn, and the conversation is released to nobody (§5) |
| `reason=handover_barrier_closed` | the tenant's shared barrier is draining; no turn was admitted and nothing was written |
| `reason=unfinished_…` | the guard refused a turn this runtime has **not finished**; it was kept from the legacy path and answered nothing |
| `reason=owned_…` | the guard refused a turn this runtime admitted and already finished; it was kept from the legacy path, which must not answer it a second time |
| `route=commerce_runtime_claim` | ownership was claimed at the dispatcher, so none of the dispatcher's own short circuits ran for this turn — the COD button routes included |
| `cod branches skipped` | the merchant handler saw a claim for this inbound, so COD classification, the order transition and its follow-up send did not run |
| `deferred during handover … entry=N` | the inbound was recorded for the operator and answered by nobody; `N` is the id `dispose` takes |

The customer's text never appears in the log line; only `reply_chars`.

Two further lines matter:

| Line | Reading |
| --- | --- |
| `[Idempotency] ALLOW duplicate inbound for commerce-runtime recovery` | a provider retry was let through because it carries an unfinished turn; nothing is resent, the ledger still decides |
| `[COMMERCE_RUNTIME_ACCEPT] recorded=N scoped=N` | N pilot-scoped inbounds were made durable **before** the webhook answered 200 |
| `[COMMERCE_RUNTIME_ACCEPT] not acknowledging: reason=…` | a pilot-scoped inbound could not be recorded, so the request was answered `503` and nothing in that batch was processed; the provider will redeliver it |
| `[COMMERCE_RUNTIME] tool session handed to the reaper` | a tool call was abandoned on its timeout and its database session is being closed by the reaper once that call returns |

**The model's reply and the text on the wire are two different things.** The
LLM composes the reply intent; the platform's own send path may postprocess it,
so the text WhatsApp receives is not always the text the model produced. What
was persisted for an accepted send records that distinction rather than hiding
it: `commerce_runtime_intent_sha256` is the digest of the text the ledger
*reserved*, and `final_text_transformed` with `final_transform_reasons` says
whether — and why — what was *transmitted* differs from it. A divergence is
therefore visible in the row rather than silently resolved.
`wire_text_unobserved` means the send path's transmitted text could not be read
back at all; it is recorded as *unverified*, never as unchanged.

Durable evidence for one turn:

```sql
SELECT * FROM commerce_runtime_turn_terminals WHERE turn_id = :turn_id;
SELECT * FROM commerce_runtime_delivery_sequences WHERE turn_id = :turn_id;
SELECT r.* FROM commerce_runtime_delivery_receipts r
  JOIN commerce_runtime_delivery_attempts a ON a.id = r.attempt_id
  WHERE a.sequence_id = :sequence_id ORDER BY r.receipt_no;
```

### 4.1 Reading a whole trial

Those three queries answer for one turn. After a trial — a set of scenarios
across several handsets — the same rows have to be read for every turn at once
and judged, which is what this job does:

```bash
COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST=<the pilot's tenant id> \
  python -m scripts.operators.commerce_runtime_trial_evidence \
    --since 2026-09-21T06:00:00Z
```

It reads only the allowlisted tenants' live namespace, using one repeatable-read,
read-only transaction. The window selects admitted turns and effect/deferred-row
creation times; related outcomes are their current state in that snapshot,
not their historical state at the end of the window. Shadow work is excluded.
It masks recipients and message ids so the report
can leave the machine. Per turn it prints the terminal, how many reply intents
were reserved, how many sends were accepted and which receipt kinds exist.
Then it judges a closed set of claims, each as `proven`, `refused` — with the
offending turns named — or `not_observed`:

| Claim | Refused when |
| --- | --- |
| `every_admitted_turn_reached_a_terminal` | a turn has no terminal: a customer owed an answer or an honest record |
| `at_most_one_reply_intent_per_turn` | one inbound reserved a second reply |
| `at_most_one_accepted_send_per_reply_intent` | one intent was accepted twice |
| `no_unknown_send_was_reported_completed` | an `unknown` send was recorded as a completed turn |
| `customer_reach_is_never_claimed_without_a_receipt` | `customer_reach=reached` with no `delivered`/`read` receipt behind it |
| `no_commerce_write_was_reserved` | an effect was reserved; the pilot has no commerce-write tool |
| `every_deferred_inbound_is_accounted_for` | an inbound is still pending or its resolution/disposition state is inconsistent |

Normal runtime handling writes `state=resolved` with a null `disposition`;
only operator handling writes `state=disposed` with a supported disposition.
The report accepts both forms and does not equate accounting (including an
`unanswered` disposition) with customer delivery. It checks each reply intent's
recorded acceptances separately; an unsent intent cannot offset a duplicate
acceptance on another intent. Failure to establish the read-only transaction
stops the job before any trial query runs.

`not_observed` is a real answer, not a pass: a window with no turns proves
nothing, and the report says so rather than reading clean. The exception is a
claim *about absence* — no commerce write was reserved — which a read that
found no row does establish. `RESULT=REFUSED` (exit 1) means at least one claim
was contradicted by the rows.

---

## 5. Handover and rollback

Switching the pilot off decides who takes **new** turns. It says nothing about
the turns the runtime already admitted, and flipping it while work is in flight
abandons four things at once: an admitted turn with no terminal (a customer owed
an answer or an honest record), a reply the loop reserved that nothing
dispatched, a send with no receipt, and a send whose recorded outcome is
`unknown` — which is not evidence it did not arrive.

So the supported rollback is a **handover**, performed against a shared barrier
in the database that every replica reads and this job writes. A per-process
environment flag cannot be the mechanism: it cannot say the same word to every
replica at the same moment, it cannot be observed from outside the process that
holds it, and it changes at a moment nobody can name.

The barrier lives in the runtime's **own** tables — `commerce_runtime_handover_barrier`,
`commerce_runtime_handover_workers` and `commerce_runtime_deferred_inbound`,
created by revision `0111`. It used to live under a namespaced key in
`tenant_settings.metadata`; that document has several unrelated writers, each of
them read-modify-write over the whole JSON, and any of them could put back a
copy taken before a drain and silently reopen it. `tenant_settings` is **not**
the handover's authority and no longer holds any part of it. Its states are
`open → draining → settled → released → open` (a reopen is also allowed from
`settled`; a re-drain from any state).

**Draining is not "new turns go to legacy."** While a tenant is draining, an
inbound for an affected recipient is *buffered*: recorded durably, answered by
nobody, and left for the operator to dispose of. Releasing it to the legacy path
is the one thing a handover must not do — it would answer, on a second runtime,
a conversation whose first runtime may still have a send in flight. Traffic the
pilot would not have owned anyway (a recipient outside the allowlist, another
tenant) is untouched and behaves exactly as it does today.

### 5.1 The procedure

Every step is a command, and every refusal names what is blocking it.

**Step 1 — drain.**

```bash
COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST=<tenant ids> \
  python -m scripts.operators.commerce_runtime_pilot_handover drain
```

Closes the barrier for every allowlisted tenant, fleet-wide, from that instant,
and bumps the generation so a replica still running the previous one is visibly
behind rather than indistinguishable from a converged fleet. Exits 0 with
`RESULT=DRAINING`.

From this moment:

* no replica admits a new turn for those tenants — the barrier is read on the
  admission transaction's own connection, under a tenant-scoped advisory lock,
  so an invocation that selected the runtime *before* the drain either commits
  its turn before the drain does (and is therefore visible to every count taken
  afterwards) or is refused (`reason=handover_barrier_closed`, nothing written);
* affected inbounds are buffered, not released;
* a redelivery of a turn this runtime already admitted still re-enters and is
  finished — that is what makes this a handover rather than an abandonment.

**Step 2 — converge.** Let every replica read the new barrier. A worker records
**the generation it observed**, the first time it evaluates a route after the
change — its own reading, not a value re-derived when the row is written, so a
heartbeat that arrives after the drain still says what that worker saw. No
restart is required; a replica reports as soon as it sees one inbound for that
tenant.

`status` names every worker in the expected set and how it stands:
`on_generation`, `behind`, or `stale`. **Silence is not retirement.** A worker
that has stopped reporting blocks settlement until an operator says, on the
record, that it is gone:

```bash
# 1. find the worker's deployment. A worker reports as
#    <RAILWAY_DEPLOYMENT_ID>/<RAILWAY_REPLICA_ID>@<host>:<pid>, so `status` shows it.
python -m scripts.operators.commerce_runtime_pilot_handover status

# 2. STOP AND FENCE — trusted operator work, executed against the platform:
#    remove that deployment (or roll to a new one so the platform removes it).
railway down --service nahla-saas --environment production        # or the console's Remove

# 3. VERIFY the stop and capture the platform's own answer, verbatim.
railway deployment list --service nahla-saas --environment production --json \
    > /tmp/deployments.json

# 4. build the structured stop record. It is REFUSED (exit 3, nothing written)
#    while the platform still reports the deployment as SUCCESS/DEPLOYING/SLEEPING,
#    as CRASHED (restartable under the restart policy: remove it instead), or
#    with any replica or instance reported running.
python -m scripts.operators.commerce_runtime_worker_stop_record \
    --deployment 477f560f-a47c-4750-966e-8acffc4c9596 \
    --incarnation "477f560f-a47c-4750-966e-8acffc4c9596/0" \
    --platform-json /tmp/deployments.json \
    --source "railway deployment list --service nahla-saas --environment production --json" \
    --out /tmp/stop-record.json

# 5. retire, naming how the stop was verified and handing the record over.
python -m scripts.operators.commerce_runtime_pilot_handover retire \
    --worker "477f560f-a47c-4750-966e-8acffc4c9596/0@host:41" --by "<operator>" \
    --reason "removed in the 2026-09-20 rollout" \
    --evidence '{"stop_verified_by": "railway deployment list: status=REMOVED"}' \
    --stop-record /tmp/stop-record.json
```

**Retirement needs evidence, not a sentence, and the record is read.** Four
keys are required and the command refuses without them (`deployment` and
`observed_at` are taken from the record when the operator does not repeat
them; if the operator does, they have to agree with it):

| Key | What it has to say |
| --- | --- |
| `deployment` | the exact deployment identity, as the platform names it. A worker that reported as `<deployment>/<replica>@host:pid` can only be retired against **that** deployment (`deployment_does_not_match_the_worker_s_own`). "some replica" retires nothing. |
| `stop_verified_by` | how the stop or the fencing was established — the command run, the console state read — in a form an on-call engineer can re-check. |
| `observed_at` | when the platform was asked, ISO-8601; it must be the record's own `observed_at`. |
| `stop_record` (`--stop-record <file>`) | the **structured** record `commerce_runtime_worker_stop_record` writes from the platform's captured answer: `deployment`, `incarnation`, `state`, `active_replicas`, `active_replicas_basis`, `observed_at`, `source`, plus the captured answer under `raw`. Retained verbatim on the worker row with its SHA-256 and size (≤ 64 KiB). |

The record is checked for what it says, not only for what it is: `state` has
to be a word the platform uses for something it will **not run again** —
`removed`, `terminated`, `dead`, `failed`, `skipped`, `deleted`. Inactive is
not enough: `stopped`, `exited`, `crashed` and `inactive` name a process that
can be started again under the same identity (a crashed container is, by the
service's restart policy), and they are refused as
`stop_record_state_is_not_a_fence:…`; `running`, `SUCCESS`, `sleeping` and
every other word are refused as `stop_record_state_is_not_inactive:…`.
`active_replicas` has to be `0`, the deployment has to be this one, the moment
has to be the one claimed; free text is refused outright
(`stop_record_not_structured`). **The retained capture is re-read.** The
summary is what a tool wrote; `raw` is what the platform said, and it is read
again against the same rules: a capture that names another deployment
(`stop_record_raw_names_another_deployment`), a running or restartable status
(`stop_record_raw_state_is_active:…`, `stop_record_raw_state_is_not_a_fence:…`)
or a replica or instance still reported active
(`stop_record_raw_reports_active_replicas:…`) refuses the retirement whatever
the summary says. A digest proves which bytes were retained; the reading is
what makes them evidence.

**What zero replicas rests on is written down.** The builder reads the
capture for a *measured* running count (`active_replicas`, `activeReplicas`,
`runningReplicas`, instances listed with a running status …) and refuses a
capture that reports one (`capture_reports_active_replicas:<n>:<key>`) — it
never summarises a contradiction as zero. When the capture measures nothing,
zero follows from the fence status alone and the record says so:
`active_replicas_basis` is `measured:<key>` or
`inferred_from_status:REMOVED`. A configured count (`numReplicas`) is what the
deployment asked for, not what runs; it is carried as `configured_replicas`
and contradicts nothing. Railway's `REMOVED`, `FAILED` and `SKIPPED` are the
fence statuses; `CRASHED` is refused (`deployment_status_not_a_fence:CRASHED`)
because the platform restarts a crashed container — remove the deployment,
capture again.

**The trusted operator boundary, stated plainly.** Steps 2–3 are the
operator's: this platform does not stop anything and does not query Railway.
It verifies that the record is about this deployment and incarnation, is
internally consistent with the capture it retains, says *fenced* and *zero
replicas* on a stated basis, and was observed when claimed; that the capture
is the platform's genuine answer about that deployment at that moment is the
operator's responsibility, and the retained `raw` capture is what an auditor
re-checks it against. Elapsed silence is deliberately not evidence. If the
worker has reported **after** `observed_at`
the retirement is refused with `worker reported at …, after the stop was
observed at … — it is running`: it is demonstrably alive, whatever was
believed when the command was typed. The evidence is stored on the worker row
and the name, reason and evidence are copied into the settlement evidence. A
retired worker that reports again is back in the expected set.

**Reconcile against what the deployment is supposed to contain.** Convergence
can only see processes that wrote a row, so a replica that never reported is
invisible to it — and is exactly the one that would still be admitting. State
the inventory and `status` names the gap:

```bash
# the ids exactly as the workers name themselves — on Railway,
# <deployment>/<replica>@<host>:<pid>, as `status` prints them
export COMMERCE_RUNTIME_PILOT_EXPECTED_WORKERS="477f560f-a47c-4750-966e-8acffc4c9596/0@host-a:7"
```

`status` then prints `fleet_expected=… reporting=… missing=… unexpected=…
reconciled=…`, and a worker in the inventory that has never reported becomes the
blocker `workers_expected_but_never_reported:<id>`. **The inventory is
required, not advisory:** `settle` and `release` read the fleet inside their own
transaction and reconcile it against this variable themselves, so with it unset
they refuse with `expected_worker_inventory_unstated` whatever `status` said.
Silence about the fleet is not evidence about the fleet.

What this proves is bounded, and the wording matters: the shared admission lock
orders this job against the workers that take it. It does not prove a fleet was
rolled out or shut down. Only a worker's own report, or an operator's recorded
retirement with the evidence above, says that.

**Step 3 — status.**

```bash
python -m scripts.operators.commerce_runtime_pilot_handover status
```

Reports, per tenant: barrier state and generation, worker convergence,
outstanding work including `unknown` outcomes, buffered inbounds awaiting
disposition, and the list of blockers. Exits 0 with `RESULT=REPORTED`; it is a
report, not a verdict.

**Step 4 — dispose of the deferred work, by name.**

`status` lists every pending entry with its id, the recipient, the provider's
own message id and why it was deferred. Dispose of the ones you have actually
dealt with:

```bash
python -m scripts.operators.commerce_runtime_pilot_handover dispose \
    --entry 41 --entry 42 --disposition replayed \
    --evidence '{"replayed_as_provider_message_id": "wamid.HBgN…"}' \
    --by "<operator>" --as-of "<the timestamp on the status you read>"
```

`--disposition` is one of a closed set, and each one has to **show** what it
asserts. The evidence is checked against the records before the row is closed,
not merely stored next to it:

| Disposition | Required evidence | Checked against |
| --- | --- | --- |
| `replayed` | `replayed_as_provider_message_id` (this entry's own identity) | a runtime turn for **this tenant, this channel connection and this customer** whose terminal records a **completed turn with a reply the provider accepted**, recorded after the inbound arrived |
| `answered` | `answered_by_provider_message_id` | the same, for the turn that answered it |
| `superseded` | `superseded_by_provider_message_id` | a later inbound for the same recipient on the same connection that this platform actually holds |
| `not_required` | `authorized_by` **and** `why` | the runtime's own records for this entry, **read**: they hold no turn for it, or its terminal records a reply that was not accepted (`not_attempted`, `rejected_definitive`). Refused when the turn did answer (`the_runtime_answered_this_inbound`), when the send outcome is `unknown` (`delivery_outcome_unknown`), when the provider accepted a reply of a turn that did not complete (`the_provider_accepted_a_reply:…`), and when the records **could not be read** (`evidence_could_not_be_verified`) — unavailable evidence is never permission. What was read is stored with the row (`runtime_terminal`) |
| `unanswered` | `authorized_by` **and** `why` | the same reading, under the name that says the customer was **not** answered; the terminal it found (failed, not attempted, rejected) is stored with the row. It never says anything else |

**A terminal is not an answer.** Every path that closes an obligation on the
strength of a runtime turn — normal resolution when a turn finishes, `recover`'s
finished-turn branch, and the `answered`/`replayed` dispositions — runs the same
validator: the turn must exist, have a terminal, be bound to this entry's
tenant, channel connection and customer, be recorded after the inbound arrived
when it is another message's turn (the entry's own turn is the handling of this
very inbound, whenever the bookkeeping row was written), and its terminal must
say `processing_outcome=completed` **and**
`transport_outcome=accepted`. A turn whose reply failed, was never attempted,
was definitively rejected or was abandoned leaves the entry **pending** —
`status` keeps counting it, `settle` keeps blocking on it — until an operator
disposes of it under the honest name. Provider acceptance is recorded as
acceptance; a confirmed delivery (`customer_reach=reached`) is stored
separately as `customer_delivery_confirmed` and is never inferred.

A plausible-looking id that resolves to no turn is refused
(`no_runtime_turn_for_that_identity`); one that resolves to an admitted turn
with no terminal is refused (`that_turn_has_no_terminal`); one whose terminal is
not an accepted reply is refused with the outcomes it did record
(`terminal_is_not_an_accepted_reply:processing=…,transport=…,customer_reach=…`).
An entry whose turn is admitted and still running is refused whatever the
disposition (`turn_admitted_and_unfinished:<turn id>`): the runtime owns it, and
its terminal — not an operator's note — will say what happened. A free-text
note is not proof of anything, and there is no command that would accept one.

`--as-of` is the moment you read `status`. An entry created after it is refused
by name (`arrived_after_the_inspection`) even if its id was passed, so a
selection can never grow to include something nobody looked at. The entries are
named as well, so a message that arrived while you were looking is **not**
disposed of: it is still pending and still blocks. An id that is not pending is
refused by name (`already_disposed`, `no_such_entry`).

Nothing is acknowledged and dropped. Disposed and resolved entries stay in
`commerce_runtime_deferred_inbound` as history and stop counting against the
pending limit, so the audit trail does not have to be deleted to make room.

One case the counts cannot show: if the record itself could not be written when
an inbound was refused, that message was withheld from every owner but never
recorded. It is logged, once, at error level:

```text
[COMMERCE_RUNTIME_PILOT] could not defer inbound tenant=… provider_message_id=…
  — withheld but UNRECORDED; account for it by hand before settling
```

Grep for `UNRECORDED` over the drain window before settling. A hit is a message
an operator has to account for; `settle` cannot see it and will not block on it.

**Step 5 — settle.**

```bash
python -m scripts.operators.commerce_runtime_pilot_handover settle
```

`settle` does not act on the report it prints. It prints what it found, and then
**re-decides inside the transaction that writes**: the barrier is re-read under
the tenant's lock, the generation the report showed is re-checked, the
convergence and every count are recomputed on that same session, and only then
is the transition written. Work that commits between the report and the write is
therefore seen, and the evidence stored describes the state that was actually
settled rather than the state as it looked a moment earlier.

| Result | Exit | Meaning |
| --- | --- | --- |
| `RESULT=SETTLED` | 0 | the barrier is draining, every expected worker is on the current generation, every count is zero, and nothing deferred is pending — all re-checked at the moment of the write. The evidence snapshot has been written in the same transaction |
| `RESULT=BLOCKED` | 1 | at least one concrete reason, named per tenant (below). A `settle_refused` line means the state moved while settling: run `status` and settle again |
| `RESULT=FAILED_PRECONDITION` | 2 | no tenant allowlist, or more tenants configured than it will inspect |
| `RESULT=FAILED` | 3 | the database could not be read — never read as "settled" |

Blocker names, each actionable:

| Blocker | What to do |
| --- | --- |
| `barrier_is_open_not_draining` | run `drain` first |
| `no_worker_has_reported_this_generation` | wait for step 2; nothing has confirmed the drain reached the fleet |
| `workers_behind:<ids>` | those replicas are still running the previous generation |
| `workers_stale_retire_or_wait:<ids>` | not heard from since the drain, or not at all recently — wait for the report, or retire the worker on the record |
| `open_turns=N` | admitted turns with no terminal |
| `reserved_undispatched=N` | replies reserved that nothing dispatched |
| `unresolved_attempts=N` | sends with no receipt at all |
| `unknown_outcomes=N` | sends whose recorded outcome is `unknown`; establish what the provider actually did and record it on that attempt |
| `deferred_pending=N` | accepted inbounds nobody has finished or accounted for; run `recover --apply` or `dispose` |
| `expected_worker_inventory_unstated` | set `COMMERCE_RUNTIME_PILOT_EXPECTED_WORKERS` to the deployment's replicas |
| `workers_expected_but_never_reported:<ids>` | a replica in the inventory never wrote a row — it may still be admitting; find it, or retire it on the record |
| `generation_moved:expected=X,found=Y` | the barrier changed between the report and the write; run `status` again |
| `work_counts_unavailable` | the runtime relations could not be counted |

`settle` writes the evidence snapshot — generation, convergence, counts, buffered
dispositions — **before** anything reopens, so what the decision rested on
survives the handover.

**Step 5b — recover, when there is pending work you want *handled* rather than
accounted for.**

`dispose` closes an obligation with evidence. `recover` meets it: it hands the
inbound back to the dispatcher so the runtime answers it.

```bash
python -m scripts.operators.commerce_runtime_pilot_handover recover            # plan
python -m scripts.operators.commerce_runtime_pilot_handover recover --apply    # do it
```

Without `--apply` it decides everything except the replay and prints the plan.
Each entry gets one outcome, and each one is a fact about that entry:

| Outcome | Meaning |
| --- | --- |
| `already_finished` | a turn for this identity reached a terminal **and that terminal is an accepted reply** (the validator above). Completed work is never repeated — the record is closed against that terminal instead |
| `finished_unanswered` | a turn for this identity reached a terminal that is **not** an answer — the reply failed, was never attempted or the turn was abandoned. Both dedup boundaries would refuse a replay, so the entry stays pending, named with the terminal's outcomes, for an `unanswered` (or otherwise honest) disposition |
| `replayed` | the provider's own body was rebuilt from the stored payload and handed to the dispatcher, which admitted it under the same identity |
| `barrier_closed` | the barrier is **settled or released**; the detail says `run 'drain', then 'recover --apply'`. A draining barrier is *not* closed to recovery (below) |
| `unknown_delivery` | the turn holds a send whose outcome nobody established. `unknown` is not "did not arrive", so replaying risks a second delivery; account for it by hand |
| `in_flight` | another runner holds this entry (a per-entry advisory lock); it will be picked up next run |
| `not_replayable` | the stored payload cannot be rebuilt into a message. Nothing is invented |
| `failed` | the replay raised; the entry is still pending |

The replay re-enters through the ordinary dispatcher and passes both
deduplication boundaries the way a provider retry does — because the runtime
answers for it by identity, for an allowlisted tenant and recipient only. It is
a replay, not a resend: the delivery ledger still refuses to dispatch an attempt
whose outcome is pending, accepted or unknown, so an uncertain send stays
uncertain.

**Recovery works while the barrier is draining.** A drain stops *new* work; an
inbound the provider was told we had is not new work — the settlement already
counts it — so refusing it while draining would leave the customer with nobody
able to answer. For each entry the runner states a **recovery grant** (this
tenant, this channel connection, this provider message id, this entry) for the
duration of that one replay. The claim and the seam honour it only when the
identity matches **and** a pending durable acceptance exists for it, and the
admission itself is checked again on the admitting transaction's own
connection, under the shared lock: the barrier must be open or draining, **and
the grant's entry row is locked there and must still be pending and still name
this tenant, connection and identity**. A grant captured before an operator
disposed of the entry authorises nothing: whichever of the disposition and the
admission commits second sees the other — the disposition refuses an entry
whose turn was admitted (`turn_admitted_and_unfinished`), and the admission
refuses an entry that was disposed. A turn this runtime already admitted and
never finished is replayed the same way, drain or no drain. Nothing else is
admitted through a closed barrier.

**An arrival after settlement is not a trap.** It is recorded as
`settled_window`, it blocks `release`, and the way out is the same three steps:
`drain` (the barrier goes back to draining on a new generation), `recover
--apply` (the entry is met), `settle` (the counts are re-taken). Then `release`.

`recover` requires the same tenant allowlist as every other command, and it
reads and writes only this pilot's own tables plus the dispatcher it hands work
to. It sends nothing itself.

**Step 6 — release, then stop.** Not "step 5 exited 0 an hour ago":

```bash
python -m scripts.operators.commerce_runtime_pilot_handover release
```

A settlement is evidence about the instant it was taken, and the switch is
flipped some time later. In between, an inbound can be accepted — recorded,
then abandoned by the configuration change. So `release` is not a verdict the
operator reads and acts on; it is a **transition**, written under the tenant's
exclusive lock, and acceptance takes the shared lock and reads it: from the
instant it commits, a new pilot-scoped inbound for the tenant is answered
`503 pilot_released` and **nothing is recorded** for it. Once the switch is off
the same request is the legacy path's, exactly as today. Nothing accepted can
fall between the two steps, because from `release` onward nothing is accepted.

Written only when, on that same transaction: the barrier is settled on the
generation `status` showed; nothing is pending; nothing arrived after
`settled_at`; the fleet is converged and reconciled against
`COMMERCE_RUNTIME_PILOT_EXPECTED_WORKERS`; and every work count is zero.

| Result | Exit | Meaning |
| --- | --- | --- |
| `RESULT=RELEASED` | 0 | the transition is written; new pilot-scoped inbounds are refused (retryable) until the switch is off. Re-running is idempotent |
| `RESULT=HELD` | 1 | named blockers: `barrier_is_…_not_settled`, `deferred_pending=N`, `arrived_after_settlement=N`, the fleet blockers above, or a work count. Drain again if the barrier is settled, `recover`/`dispose`, `settle`, then re-run |

Only after `release` exits 0:

```bash
COMMERCE_RUNTIME_PILOT_ENABLED=false
```

Between `release` and the switch, expect `pilot_released` refusals in the log
for any pilot-scoped inbound; Meta retries them and the legacy path answers
once the switch is off. If the handover is abandoned instead, `reopen` takes
the barrier from `released` back to `open` and acceptance resumes.

The rollback is complete when a message from a previously allowlisted handset
produces a `route=legacy` line and the legacy brain's own trace source.

**Later — reopen.** When the pilot is being turned back on:

```bash
python -m scripts.operators.commerce_runtime_pilot_handover reopen
```

Admits new work again on a fresh generation, keeping the audit trail. It refuses
unless the barrier is settled or released, because reopening before settlement
would discard the evidence — and it re-checks, under the same lock that applies the change,
that the barrier is still the settled one it was asked about **and that nothing
is still pending**. A drain that started between the check and the write is
therefore never reopened over (`RESULT=BLOCKED` with
`state_moved_since_the_precheck`), and neither is an entry that arrived in the
settled window: reopening would bury it under the traffic it lets back in. Run
`recover` or `dispose` for it first.

**Between settlement and reopening** the tenant admits no new work. An inbound
arriving in that window is recorded as `settled_window` and is answered by
nobody until the barrier reopens or an operator disposes of it. It is not lost:
it carries the same identity and payload as any other deferred entry.

### 5.2 What this proves, and what it does not

Convergence is evidence from the workers themselves, not a statement that they
were restarted. Counts are the database's account of recorded work. Neither can
see a request already on the wire to the provider, which is why an `unknown`
outcome **blocks** settlement rather than ageing out of it: elapsed time and a
cancelled wait are not proof of non-delivery, and nothing here treats them as
such.

There is no attestation step and no attestation variable. A handover depends on
the procedure above actually having been executed — a drain that is recorded,
a convergence that workers reported, counts that are zero and buffered work that
has a disposition — not on anyone asserting that the fleet is quiesced.

The job is read-only apart from the barrier itself: it sends no message, answers
no customer and changes no runtime configuration.

**What a 200 from the Meta route promises for pilot-scoped work.** Three
things, each answered `503` (retryable, nothing recorded, nothing spawned, the
replay nonce given back) when they do not hold: the request's
`X-Hub-Signature-256` **verified** (`pilot_scope_unauthenticated` otherwise —
the legacy path's audit mode does not extend to the pilot); the tenant's barrier
is not `released` (`pilot_released`); and every pilot-scoped message in the
batch was written down (`inbound_not_persisted`). Two more rules close the gap
a nonce cannot. **A nonce is a claim, not an acceptance:** the replay nonce is
written as `claimed` when a request takes it and marked `completed` only once
that request has finished deciding (obligations durable, processing
scheduled). A copy of the body that arrives while the claim is still open
inside the in-flight lease (60 s) is answered `503 replay_in_flight` — nothing
is acknowledged on another request's behalf; a copy that finds a claim older
than the lease belongs to a process that died, takes the claim over and is a
first attempt, whatever the pilot flag or the barrier say by then; a nonce
written by the **earlier** code (`1`) — or any value that is neither an open
claim nor a completed marker — says a request took the nonce and cannot say
how far it got, so it is an ambiguous acquisition taken over the same way,
never a replay (the durable records and both dedup boundaries decide, per
message, what was already done, so a retry of work that *was* recorded writes
nothing twice); only a `completed` nonce is a replay, and a refused request
gives back only the claim it holds itself (compare-and-delete), never another
request's. **And a completed nonce is still not a record:** when replay protection says a body was seen
before, the route checks that every pilot-scoped message in it is **on
record**; one that is not is treated as a first attempt.

**Emergency stop.** `COMMERCE_RUNTIME_PILOT_ENABLED=false` stops **this process**
from taking new turns and from recovering. It is the right move when the pilot
is actively misbehaving, and it is worth being exact about what it is not: it is
not instantaneous across a fleet — each replica stops when it picks the change
up — and it does not stop an HTTP send already in flight. Afterwards, run the
handover procedure to see exactly what it left behind, and finish those turns
deliberately (below) before considering the rollback complete.

**`COMMERCE_RUNTIME_PILOT_DRAINING=true`** takes one process out of rotation. It
is not the handover: it is per process, it is invisible to the job, and the
barrier is what a handover is performed against. Where it is set, it behaves the
same way the barrier does — it takes no new turn and it releases nothing to
another owner; an affected inbound is buffered with `reason=process_draining`.

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
  It **reads** the merchant's currently valid, shareable coupons and offers through
  `list_shareable_promotions` (the platform's promotion-truth resolver: campaign-only,
  expired, disabled and exhausted codes are never returned, a personal code issued to
  one customer is returned only in that customer's conversation, no code is ever
  invented, and whether the customer qualifies is reported as not determined), under
  the merchant's dashboard AI coupon policy (`ai_policy.enabled`, `allowed_levels`
  and `min_remaining_hours` — a code with less life left than the merchant's minimum
  is not handed out; a disabled policy is a `denied` read). Before a reply is
  reserved, verification refuses a draft that carries a coupon code without citing
  the coupon it came from, or any code-shaped token this turn's tools did not
  return. It creates, assigns or redeems none.
* Each promotion carries **one** reading of its discount (`discount`: `5%`, `20 SAR`,
  or empty when the record supports none) beside the raw `discount_type` and
  `discount_value`. September 2026: a coupon issued as 5% and reconciled from Salla
  stored its value as that provider's money object, so the record said "percentage"
  and "5 SAR" at once and the model quoted the amount. The sync now stores the
  number and the resolver states the reading; nothing is inferred when neither is
  readable.
* Each product carries the **option values of the variants in stock**
  (`variant_options`, with `variants_in_stock` and `variants_total`), so a colour or
  size in a reply comes from the merchant's variant rows. September 2026: the view
  carried no variant at all and a white/fuchsia dress was called black.
* Each turn is handed the products **this conversation's recent replies were
  grounded on**, read back from the persisted `evidence_refs`. They become
  identities the turn may look up and are named in the trusted-fact preamble as
  `products_shown_earlier`. 2026-09-22 07:12Z: asked about a dress shown
  earlier, the model called `get_product_details` — the right tool — and the
  isolation guard refused it, because identity was otherwise acquired only
  inside the turn that searched; the fallback search on the referring phrase
  («الفستان الأول») matched nothing, because the catalogue search requires every
  token and no product text contains an ordinal. The guard is unchanged: an id
  the model names on its own is still refused, and every fact still has to be
  read by a tool in this turn and cited as this turn's evidence.

  No **order** is claimed. The stored references are the order the reply
  *cited*, which is not provably the order the customer *saw*; the model has its
  own earlier message in the transcript and resolves "the first" from that. A
  provable presentation order needs the reply to carry structured choices.

  The set is bounded to this conversation, this tenant and at most
  `MAX_PRODUCTS` products, each re-read in the merchant's catalogue now — one
  the merchant has since removed is not carried.

  **The clock belongs to the product, not to the conversation.** A product is
  carried while the reply that last showed *it* is younger than
  `BROWSING_CONTEXT_LAPSE_SECONDS` (provisional: 72 h). A conversation that has
  carried on daily about other subjects therefore carries nothing from three
  weeks ago, and a product still being discussed stays current on its own,
  because the reply discussing it cites it. Relevance is carried by evidence;
  nothing here guesses at the subject of a message, and nothing asks the
  customer to confirm one. `MAX_REPLIES_READ` bounds the read, not the policy.

  Lapsing changes only what the platform volunteers for one turn: the
  conversation, the customer's profile and their real orders are untouched, and
  coming back costs one ordinary search — proven by
  `test_a_customer_can_go_back_to_a_product_whose_context_has_lapsed`, which
  runs the turn rather than asserting that the rows survived. The reply that
  turn sends cites the product, so the turn after it carries the product again.

  Each turn logs `[COMMERCE_RUNTIME] browsing context turn=… reason=…
  products=… seconds_since_last_product_shown=…`, so the duration can be set
  from evidence rather than opinion. The lapse is deliberately **not**
  WhatsApp's 24 h service window: that governs sending, not memory.
* A multi-product **selector** is an affordance over the answer, never the
  answer itself. Row labels are composed by the platform from the merchant's
  own values (`core/commerce_runtime/choice_rows.py`) because WhatsApp refuses
  an interactive payload whose visible titles repeat and Tenant 1's five
  dresses are all titled «فستان». Nothing is invented: when no fact tells two
  products apart, both are left out and the set reports itself **incomplete**,
  and a caller offering a selector then sends the model's text alone. A real
  option therefore never disappears from the customer's answer because of a
  display limit — only the tapping is withheld.
* Recorded, not changed: the catalogue search's clarification guard
  (`_ambiguous_reference_has_multiple_candidates`) reads product ids from the
  `artifact` / `response_bundle` shapes the legacy compose path writes. This
  runtime writes `evidence_refs` instead, so on a pilot turn the guard always
  sees none and never fires. With the products of earlier replies now carried,
  forcing a clarification question would be the wrong repair anyway; the finding
  is kept here so the gap is not rediscovered as a bug.
* `variant_options` carries only values a customer could say back. A provider may
  keep its own bookkeeping in the same mapping — Tenant 1 product 37 carries
  `option_value_ids: ['1064266980', '1837256091']` beside `المقاس` — and a
  non-scalar value is never an option anyone chooses. The rule is the shape, not
  a name list.
* A product's availability is the **synced** one: the catalog row prefers
  `metadata.in_stock` / `metadata.stock_qty` and uses the `products` columns only
  when the metadata is silent. Tenant 1 has rows whose column says available while
  their synced metadata says otherwise (last written by an ingest that leaves
  `sync_status='blocked'` and no `product_url`); the customer-facing fact is the
  synced one, and `tests/test_catalog_availability_precedence.py` locks that. The
  column drift itself is a catalog-sync item, tracked outside this runtime.
* It records no delivery or read receipt, so `customer_reach` stays `unknown`
  even for an accepted send.
* It has no reconciliation worker: an uncertain send is left uncertain rather
  than resolved automatically.
* It claims nothing about a saved or adopted address; that work is separate and
  separately approved.
* While it owns a conversation it owns **every** inbound turn in it, including
  ones the dispatcher's payment-receipt, payment-evidence, map-image,
  payment-claim and **cash-on-delivery** branches would otherwise take. The COD
  routes are owners, not formatters: they transition an order and send the
  customer a follow-up. For an allowlisted recipient a "نعم" is the runtime's
  turn, so `handle_cod_reply` does not run and no follow-up is sent.

  The claim is honoured before the **classification**, not only before the
  action. A template-button tap whose payload nobody recognises is resolved
  against this tenant's recent COD sends using the `context.id` the customer's
  client echoed back — `resolve_owned_cod_button_payload_from_context` — and
  that correlation is itself a competing owner deciding what the turn is. For
  runtime-owned traffic it does not run at all: no classification, no
  correlation, no order mutation, no follow-up send. The pilot has no
  commerce-write tool, so it cannot confirm an order itself — it answers from
  what it can observe. For every other recipient COD behaves exactly as it does
  today.
* A recovered send whose transmitted text could not be read back is kept in the
  store for the operator and is **not** shown to the model as a prior assistant
  turn: its body is the reserved intent, which the send path may have rewritten.
* A pilot-scoped inbound is written to `commerce_runtime_deferred_inbound`
  **before** the webhook answers 200, after Meta's signature has been verified
  and never before it. The record is resolved only against the **authoritative
  terminal** for that exact identity — a non-null turn id is not completion
  evidence, and `ownership_unavailable`, `admission_conflict` and an internal
  error all carry one. Until then it counts as work outstanding, exactly like an
  admitted turn with no terminal.

  Whose the message is has three answers, not two: pilot-scoped, verified out of
  scope, or **undecidable**. A scope lookup that fails, or a phone number id
  claimed by two allowlisted tenants, is undecidable — never "unrelated
  traffic" — and the request is answered `503` rather than acknowledged.

  If the record cannot be written the webhook answers `503` instead of 200 and
  processes nothing from that request, so the provider redelivers the whole
  batch; the existing deduplication is what stops the unaffected messages in it
  being processed twice. The nonce that request claimed for replay protection is
  **released** with the refusal, so the identical retry is a real retry and not
  a 200 for a message nothing ever processed. A request that *was* accepted
  keeps its nonce, so a lost acknowledgement stays idempotent.

  Nothing in this path runs while the pilot is off.
