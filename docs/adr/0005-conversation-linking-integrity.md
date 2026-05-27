# ADR 0005 — Conversation-Linking Integrity for Order-Flow Short-Circuits

| Status      | Accepted (May 2026)                                  |
|-------------|------------------------------------------------------|
| Wave        | W2.0.3                                                |
| Predecessor | W2.0.1 (inbound lifecycle telemetry), W2.1 (read-only inbox audit) |
| Successors  | TBD (W2.0.4 idempotency reorder, W2.0.5 orphan recovery — both deferred until production telemetry validates W2.0.3) |
| Owners      | Backend / Conversational Engine                      |

## Context

The Wave 2.1 read-only investigation produced a definitive root-cause
chain for the visibility drift between WhatsApp and Nahla's inbox:

1. There is **no** cached `Conversation.last_message_at` column. The
   `/conversations` endpoint orders rows live, via:

       MAX(MessageEvent.created_at) GROUP BY conversation_id
       ORDER BY ... NULLS LAST
       LIMIT 200 OFFSET 0

2. Any `MessageEvent` written without a valid `conversation_id`
   becomes an **orphan**: it never enters the `GROUP BY` aggregate,
   so it cannot move its conversation up the order — the row freezes
   at its previous live timestamp or never enters the SQL recency
   horizon (the `LIMIT 200` cap) at all.

3. The four order-flow short-circuits inside `_dispatch_message`
   (`backend/routers/whatsapp_webhook.py`) historically wrote both
   inbound and outbound MessageEvents WITHOUT a `conversation_id`:

   - `payment_claim_short_circuit`
   - `payment_receipt_short_circuit`
   - `map_image_short_circuit`
   - `payment_evidence_short_circuit`

   Each branch is a high-frequency happy path during checkout, so the
   orphan rate scales with order volume — exactly the symptom
   merchants reported ("recent media never surfaces in the inbox",
   "the conversation does not bubble up").

W2.0.1 telemetry confirmed `EVENT_MESSAGE_SAVED_ORPHAN` fires inside
these branches in production. W2.1 confirmed the inbox query is
ordering-live, with `fetch_cap=200` and `fetch_cap_hit=true` over an
8 684-conversation tenant. The closing step is to stop **producing**
new orphans on these paths.

## Decision

Introduce a small, fail-open **conversation-linking** block at the
top of every order-flow short-circuit, immediately after the existing
`record_lifecycle(EVENT_*_SHORT_CIRCUIT)` call and BEFORE the first
`StateManager.save_message`. The block:

1. Resolves a Conversation row via `routers.conversations._get_or_create_conversation(db, tenant_id, sender)`.
2. Captures the resulting id into a per-branch local variable
   (`_w203_conv_id_pc`, `_w203_conv_id_rc`, `_w203_conv_id_mp`,
   `_w203_conv_id_ev`).
3. Threads `conversation_id=...` into **every** subsequent
   `save_message` call in that branch (inbound + outbound).
4. Records lifecycle telemetry:
   - `EVENT_AUTO_LINK_OK` on success — also stamps the resolved id
     onto the trace so the summary line shows `convo_id=…` even if
     a later persist fails.
   - `EVENT_AUTO_LINK_FAILED` on failure — logs a warning and falls
     **open** to the legacy orphan write (the user's media is still
     persisted; the row is still routed; the inbox just does not
     bubble that conversation, which is exactly the pre-W2.0.3
     behaviour).

The closed event vocabulary in `core.inbound_lifecycle.ALL_EVENTS`
is extended with the two tokens. The trace's `_apply` method now
honours `conversation_id` from `EVENT_AUTO_LINK_OK` payloads.

## Out of Scope (explicit non-goals)

To preserve the narrow blast radius requested in the task:

- **No** behaviour change in the brain, payment understanding,
  receipt verdict logic, OCR pipeline, or relational layer.
- **No** new `Conversation.last_message_at` cache column. This stays
  a pure SQL ordering decision, owned by `/conversations`.
- **No** pagination, ordering, or filter changes in the inbox query.
- **No** orphan recovery (back-fill) for existing orphan rows.
- **No** migrations, daemons, or background jobs.
- **No** retry / reorder of `IdempotencyGuard.mark_processed` (that
  is W2.0.4 if telemetry justifies it).
- **No** changes to `_post_wa`, dedup, status stamping, or admin
  metrics paths — those continue to read the same persisted rows.

## Architectural invariants

The patch pins the following structural rules; tests in
`tests/test_conversation_linking_integrity.py` enforce them:

1. Every order-flow short-circuit declares exactly one
   `_w203_conv_id_<suffix>` local variable BEFORE its first
   `save_message`.
2. Every short-circuit imports the resolver under a per-branch alias
   `_w203_resolve_<suffix>`.
3. Every `save_message` call inside a short-circuit threads
   `conversation_id=_w203_conv_id_<suffix>`.
4. The fail-open exception handler emits `EVENT_AUTO_LINK_FAILED`
   and proceeds. It never re-raises.

## Consequences

### Positive

- New inbound rows on the four short-circuits carry a valid
  `conversation_id`, so they enter the inbox `MAX` aggregate and
  move their conversation to the top exactly like a brain-path save
  would.
- Operators can now grep for `auto_link_ok` / `auto_link_failed` to
  watch the W2.0.3 success rate in real time without enabling any
  enforcement flag — telemetry-first stays the rule.
- The summary line's `convo_id=` field is now populated on the
  short-circuit paths, which previously left it empty.
- Pre-existing orphans are still queryable (no migration), so a
  later `W2.0.5` orphan-recovery wave can decide what to do with
  them based on production data.

### Negative / risks

- A spurious `_get_or_create_conversation` failure now logs a
  WARNING per inbound. We accept this as a debuggability win: the
  failure was previously invisible (write succeeded as orphan).
- The resolver runs synchronously inside the request path; under
  pathological DB latency the short-circuit becomes slightly slower.
  Observed p95 in W2.0.1 traces is well under the existing
  per-request budget; we re-evaluate if telemetry shows otherwise.

## Rollout

No feature flag. The block is fail-open by design — if anything in
the resolver path misbehaves, the branch reverts to the pre-W2.0.3
orphan write and the operator gets a structured warning. Roll
forward immediately to all tenants.

## Verification

Tests added (`tests/test_conversation_linking_integrity.py`):

- Closed vocabulary pin (both new events).
- Happy-path: short-circuit yields `orphan_messages=0` and
  `convo_id=…` on the summary.
- Resolver failure: falls open to legacy orphan, emits
  `auto_link_failed`, never raises.
- Branch-token pin: the four `branch=…` details are honoured.
- Source-level invariants: every patched branch declares the
  per-branch variable, imports the per-branch alias, and threads
  `conversation_id=` into every `save_message`.

Existing W1 / Wave 0 / W3 / W2.0.1 / W2.0.1.5 tests pass unchanged
(371 passed in the cross-wave sweep).
