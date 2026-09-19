# Commerce runtime owner pilot — activation, verification and rollback

Operating document for the owner-only trial of the commerce runtime on
WhatsApp. It is **off** by default and reaches nothing until every variable
below is set. Contract: `docs/architecture/commerce-runtime-pilot-activation.md`.

---

## 1. Preconditions

| # | Precondition | How to check |
| --- | --- | --- |
| 1 | The runtime's tables exist in the target database | `SELECT to_regclass('public.commerce_runtime_turns'), to_regclass('public.commerce_runtime_delivery_sequences');` — both non-null |
| 2 | The Anthropic key and model are configured for that service | `ANTHROPIC_API_KEY` present; `CLAUDE_MODEL` / `ANTHROPIC_MODEL` as already configured for the legacy path |
| 3 | The test store's tenant id is known **from configuration**, not from a name | read the tenant id from the store's own connection row (step 3.1) |
| 4 | The test conversations' phone numbers are known | the owner's own test handsets, nothing else |
| 5 | The deployed revision contains the pilot | the merge commit of the pilot PR is the deployed commit |

Revisions `0108` and `0109` are merged and validated, but the normal bootstrap
target is pinned at `0093`, so a database that has never had them applied will
report `runtime_schema_unavailable` and the pilot will answer nothing. Applying
them is a deliberate operator step (`alembic upgrade 0109`), taken with the
owner's knowledge, and it creates only new tables — it alters no existing table
and backfills no data.

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
| `COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST` | comma-separated tenant ids | the one test store's id |
| `COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST` | comma-separated phone numbers | the owner's test handsets only |
| `COMMERCE_RUNTIME_PILOT_MAX_STEPS` | optional, ≤ 6 | leave unset (4) |
| `COMMERCE_RUNTIME_PILOT_MAX_TOOL_CALLS` | optional, ≤ 8 | leave unset (6) |
| `COMMERCE_RUNTIME_PILOT_TOOL_TIMEOUT_SECONDS` | optional, ≤ 20 | leave unset (10) |
| `COMMERCE_RUNTIME_PILOT_PROVIDER_TIMEOUT_SECONDS` | optional, ≤ 60 | leave unset (35) |
| `COMMERCE_RUNTIME_PILOT_DEADLINE_SECONDS` | optional, ≤ 120 | leave unset (75) |

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
2. Set `COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST` and
   `COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST`, still with the switch off, and
   redeploy. Still nothing should route.
3. Set `COMMERCE_RUNTIME_PILOT_ENABLED=true` and redeploy.
4. Send one message from an allowlisted handset and confirm exactly one
   `route=commerce_runtime` line with `replied=True`.
5. Send one message from a **non**-allowlisted handset in the same store and
   confirm a `route=legacy` line with `recipient_not_allowlisted`, and that the
   legacy brain answered it as before.

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
| `reason=runtime_schema_unavailable` | revisions 0108/0109 are not applied on this database |
| `reason=ownership_unavailable` | another invocation holds the turn, or an earlier turn in that conversation is still open |

The customer's text never appears in the log line; only `reply_chars`.

Durable evidence for one turn:

```sql
SELECT * FROM commerce_runtime_turn_terminals WHERE turn_id = :turn_id;
SELECT * FROM commerce_runtime_delivery_sequences WHERE turn_id = :turn_id;
SELECT r.* FROM commerce_runtime_delivery_receipts r
  JOIN commerce_runtime_delivery_attempts a ON a.id = r.attempt_id
  WHERE a.sequence_id = :sequence_id ORDER BY r.receipt_no;
```

---

## 5. Rollback

**Immediate, no deploy:** set `COMMERCE_RUNTIME_PILOT_ENABLED=false`. The next
turn takes the legacy path. Setting it removes the route, not the history: turns
already recorded keep their terminals and receipts.

**Narrower:** remove one number from
`COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST`, or empty it entirely. An empty
list permits nothing, so this is equivalent to switching the pilot off for that
store while leaving the tenant configured.

**Full:** redeploy the previous revision. Nothing in the pilot changes a legacy
code path, so the legacy behaviour on the previous revision is unchanged.

The rollback is complete when a message from a previously allowlisted handset
produces a `route=legacy` line and the legacy brain's own trace source.

### If a turn is stuck

A turn whose delivery was reserved but never dispatched stays the eligible turn,
and the conversation reports `ownership_unavailable` for later messages. This is
deliberate: the reply is reserved and must not be composed twice. The next
inbound message for the **same** provider message id resumes and dispatches it.
To clear it manually, dispatch or complete that turn through the ledger; do not
delete the sequence, and never send its text by hand as well as through the
ledger.

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
