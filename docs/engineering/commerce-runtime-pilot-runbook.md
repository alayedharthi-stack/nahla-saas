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
(`alembic downgrade 0109` drops exactly what it created), and it is refused
rather than reconciled when a relation of that name already exists with a
different shape. It has been applied and reversed in isolated databases only;
applying it to the shared pilot database is a separate authorisation.

### 1.2 Applying the schema (owner-approved, one-off)

`scripts/operators/commerce_runtime_pilot_migration.py` is the job, run the way
this repository already applies a production migration: a dedicated Railway
one-off service built from a pinned `ops/…` branch, `restartPolicyType: NEVER`,
whose only variables are the pilot database's `DATABASE_URL`, the confirmation
token, and the database the operator is authorising this run to touch.

```bash
NAHLA_COMMERCE_RUNTIME_MIGRATION_CONFIRM=RUN_COMMERCE_RUNTIME_0110 \
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
| revision `0108` with exactly its three foundation relations, or `0109` with those plus the ledger six | accepted: that is what each revision creates, and the job upgrades to `0111` |
| all twelve exist and the revision is already `0111` | `RESULT=ALREADY_APPLIED`, exit 0, Alembic is not run |
| `alembic upgrade 0111` ran and all twelve relations and the revision are verified | `RESULT=SUCCESS`, exit 0 |
| anything else after the upgrade | `RESULT=FAILED`, exit 4, with what was observed |

Every line is prefixed `[commerce-runtime-0111]`. Rolling the schema back is
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
| `COMMERCE_RUNTIME_PILOT_DRAINING` | take **this process** out of rotation: no new turns, finish our own. Not the handover mechanism (§5) | unset; a handover uses the shared barrier |
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
  'stop_reason': …, 'steps_used': …, 'tool_calls_used': …, 'tools_called': …,
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
`open → draining → settled → open`.

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
python -m scripts.operators.commerce_runtime_pilot_handover retire \
    --worker "<worker id from status>" --by "<operator>" \
    --reason "terminated in deploy 1234" \
    --evidence '{"deployment": "railway:nahla-backend@deploy-1234",
                 "stop_verified_by": "railway deployment status=REMOVED, replicas=0",
                 "observed_at": "2026-09-20T09:05:00Z"}'
```

**Retirement needs evidence, not a sentence.** Three keys are required and the
command refuses without them:

| Key | What it has to say |
| --- | --- |
| `deployment` | the exact deployment or process identity, as the platform that runs it names it. "some replica" retires nothing. |
| `stop_verified_by` | how the stop or the fencing was actually established — the command run, the console state read — in a form an on-call engineer can re-check. |
| `observed_at` | when that was seen, ISO-8601. |

Elapsed silence is deliberately not one of them. If the worker has reported
**after** `observed_at` the retirement is refused with
`worker reported at …, after the stop was observed at … — it is running`: it is
demonstrably alive, whatever was believed when the command was typed. The
evidence is stored on the worker row and the name and reason are copied into the
settlement evidence. A retired worker that reports again is back in the expected
set.

**Reconcile against what the deployment is supposed to contain.** Convergence
can only see processes that wrote a row, so a replica that never reported is
invisible to it — and is exactly the one that would still be admitting. State
the inventory and `status` names the gap:

```bash
export COMMERCE_RUNTIME_PILOT_EXPECTED_WORKERS="host-a:7,host-b:7"
```

`status` then prints `fleet_expected=… reporting=… missing=… unexpected=…
reconciled=…`, and a worker in the inventory that has never reported becomes the
blocker `workers_expected_but_never_reported:<id>`. With the variable unset it
prints `fleet_expected=UNSTATED` rather than implying a reconciliation happened.

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
| `replayed` | `replayed_as_provider_message_id` | a runtime turn for **this tenant and this channel connection** that reached a terminal |
| `answered` | `answered_by_provider_message_id` | the same |
| `superseded` | `superseded_by_provider_message_id` | a later inbound for the same recipient that this platform actually holds |
| `not_required` | `authorized_by` **and** `why` | nothing — it is the one disposition that claims no delivery, and it is an operator's recorded judgement |

A plausible-looking id that resolves to no turn is refused
(`no_runtime_turn_for_that_identity`), and one that resolves to an admitted turn
with no terminal is refused too (`that_turn_has_no_terminal`): an admitted turn
is not a handled one. A free-text note is not proof of a replay, and there is no
longer a command that would accept one.

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
| `deferred_pending=N` | accepted inbounds nobody has finished or accounted for; run `dispose` |
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
| `already_finished` | a turn for this identity reached a terminal. **Completed work is never repeated** — the record is closed against that terminal instead |
| `replayed` | the provider's own body was rebuilt from the stored payload and handed to the dispatcher, which admitted it under the same identity |
| `barrier_closed` | this tenant is not admitting work (draining, or settled and not reopened). Nothing is replayed into a closed barrier |
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

`recover` requires the same tenant allowlist as every other command, and it
reads and writes only this pilot's own tables plus the dispatcher it hands work
to. It sends nothing itself.

**Step 6 — release, then stop.** Not "step 5 exited 0 an hour ago":

```bash
python -m scripts.operators.commerce_runtime_pilot_handover release
```

A settlement is evidence about the instant it was taken. Switching the pilot off
on the strength of one taken ten minutes ago abandons whatever arrived since —
and something does arrive: an inbound in the settled window is accepted and
recorded rather than lost. `release` therefore derives the verdict **now**, from
the barrier and the entries this tenant holds, and never from a stored one:

| Result | Exit | Meaning |
| --- | --- | --- |
| `RESULT=RELEASED` | 0 | settled, nothing pending, nothing arrived after `settled_at`. Switching off abandons nothing |
| `RESULT=HELD` | 1 | named blockers: `barrier_is_…_not_settled`, `deferred_pending=N`, `arrived_after_settlement=N` |

Only after `release` exits 0:

```bash
COMMERCE_RUNTIME_PILOT_ENABLED=false
```

The rollback is complete when a message from a previously allowlisted handset
produces a `route=legacy` line and the legacy brain's own trace source.

**Later — reopen.** When the pilot is being turned back on:

```bash
python -m scripts.operators.commerce_runtime_pilot_handover reopen
```

Admits new work again on a fresh generation, keeping the audit trail. It refuses
unless the barrier is settled, because reopening before settlement would discard
the evidence — and it re-checks, under the same lock that applies the change,
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
