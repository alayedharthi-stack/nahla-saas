# 0004 — Inbound Lifecycle Telemetry (W2.0.1)

**Status**: Accepted (May 2026)
**Wave**: 2.0 — Inbound Materialization Stabilization, Phase 1 (telemetry only)
**Supersedes**: nothing
**Followed by (planned)**: W2.0.3 (conversation linking), W2.0.4 (idempotency reorder).
The choice between them will be made *from data* produced by this ADR.

---

## Context

During the May 2026 Eid season audit, the on-call team noticed a class of
**"vanished inbound"** failures: WhatsApp messages that demonstrably arrived
at the webhook (Meta / 360dialog provider logs confirmed delivery to our
endpoint with HTTP 200) but **never appeared in Nahla's conversation list**.
Two recurrent shapes:

1. Customer sends an **image-only** receipt (no caption). The conversation
   stays empty in the merchant's dashboard.
2. Customer sends a **long Eid greeting / duaa / religious message**. Same
   silent loss.

The architectural investigation (three subagents tracing
`whatsapp_webhook.py`, `media/normalizer.py`, and the persistence layer)
identified at least **27 distinct silent-drop points** across the inbound
ingestion path. The three with highest suspected impact:

- **Idempotency mark-before-persist trap** —
  `IdempotencyGuard.mark_processed` commits the dedup mark BEFORE any
  `Conversation` / `MessageEvent` row is written. If anything downstream
  partially fails and Meta retries the webhook, the duplicate guard fires
  and the original message is permanently lost.
- **Orphan `MessageEvents`** — Order-flow short-circuits
  (`payment_claim_short_circuit`, `payment_receipt_short_circuit`,
  `map_image_short_circuit`, `payment_evidence_short_circuit`) call
  `StateManager.save_message(..., conversation_id=None, ...)` without first
  ensuring a `Conversation` row exists. The `MessageEvent` is written but
  invisible to the dashboard, which filters by `conversations`.
- **Swallow-exception rollback chains** — `_persist_inbound_only`,
  `_handle_media_fallback`, and `StateManager.save_message` catch
  exceptions broadly and `logger.warning` without re-raising. A flushed-
  but-uncommitted `Conversation` is silently rolled back when the
  subsequent `save_message` errors.

We do not yet know which of these fires most in production.

## Decision

Implement **structured inbound-lifecycle telemetry** before any behavioural
change. Specifically:

1. New module: `backend/core/inbound_lifecycle.py`. A pure observation
   layer with:
   - A closed event vocabulary (`EVENT_*` constants, exported via
     `ALL_EVENTS`).
   - A `ContextVar`-scoped `InboundLifecycleTrace` dataclass, opened by
     the `inbound_lifecycle_trace(...)` context manager wrapping each
     per-message dispatch.
   - `record_lifecycle(event_name, ...)` — append to the active trace,
     no-op outside one. **Never raises.**
   - `emit_lifecycle_summary(trace)` — emits ONE canonical
     `[INBOUND_LIFECYCLE]` log line at end of dispatch, on success OR
     uncaught exception.
   - `emit_standalone_event(...)` — for HTTP-layer rejects that fire
     before a per-message context exists.
   - Kill switch: `INBOUND_LIFECYCLE_TELEMETRY_ENABLED` (default ON,
     since this is observation-only and adds no behavioural risk).

2. Wiring at known drop points (additive, never modifying control flow):
   - `_handle_whatsapp_body`, `_handle_360dialog_body` — wrap each
     `_dispatch_message` call with the context manager.
   - `_dispatch_message` — events at `missing_phone_id`, in-memory dedup
     drop, DB session fail, unknown / ambiguous tenant, DB dedup drop,
     dedup mark, tenant resolved, unsub short-circuit, normalizer ok / fail,
     unsupported type, empty-text fallback / no-fallback, payment / receipt
     / map / payment-evidence short-circuits, brain invocation.
   - `_persist_inbound_only` — `persist_inbound_only_ok` / `_fail`.
   - `_handle_media_fallback` — `media_fallback_ok` / `_fail`.
   - `_handle_merchant_message` — `end_dropped` on the empty-text guard.
   - `routers/conversations._get_or_create_conversation` —
     `conversation_created` (on flush of new row), `conversation_lookup_hit`.
   - `core/conversation_engine.StateManager.save_message` —
     `message_saved` (with `conversation_id`), `message_saved_orphan`
     (when `conversation_id` is None — this is the orphan smoke alarm),
     `message_save_rollback` on the exception path.
   - `core/runtime_perf.spawn_background` — standalone `bg_rejected`
     event at task explosion, since the upstream provider already saw
     200 OK.
   - HTTP entry — standalone `http_signature_reject` /
     `http_replay_reject`.

3. The canonical summary line answers, in one grep:
   ```
   [INBOUND_LIFECYCLE] trace_id=… provider=… phone_id=… msg_id=… msg_type=…
   tenant_id=… sender=*1234 body_len=… has_caption=… elapsed_ms=…
   convo_created=true|false convo_lookup_hit=true|false convo_id=…
   message_saved=true|false orphan_messages=N rollbacks=N
   persist_only=fails/attempts media_fallback=fails/attempts
   final=… path=ev1->ev2->…
   ```

## Architectural rules (locked)

1. **Telemetry only.** No state writes. No behavioural change. Every
   `record_lifecycle(...)` call is wrapped in try/except internally;
   the public API never raises. A regression test pins this contract.
2. **No coupling.** `core/inbound_lifecycle` imports nothing from the
   routers, webhook, or persistence layers. Wiring is one-way.
3. **Closed event vocabulary.** Adding a new event token is a deliberate
   change; the test suite enumerates the public set and fails on drift.
4. **Never logs PII.** Phone numbers are masked to last-4 digits. Body
   text is **not** recorded — only its length.
5. **Bounded log size.** Path token in summary is capped at the trailing
   16 events; an omitted-prefix marker keeps the line below typical
   single-line log budgets.
6. **Re-entrant safe.** Nested `inbound_lifecycle_trace(...)` shares the
   parent trace; only the outer scope emits the summary.
7. **Default ON.** Operators flip OFF in seconds via the env var if
   volume becomes an issue; no code change required.

## Out of scope

- Behavioural fixes (W2.0.3 conversation linking, W2.0.4 idempotency
  reorder, etc.).
- Payment verification (Wave 1).
- Relational layer / Wave 0.
- W3 dedup suppression.
- OCR / media-understanding improvements (former Wave 2).

## Consequences

### Positive

- Operators get a single greppable line per inbound that answers
  *"did this message materialize, and if not, where did it die?"*.
- Forensic debugging of "vanished inbound" complaints no longer requires
  joining provider logs against `message_events` and `conversations`.
- Wave 2.0.3 vs. 2.0.4 sequencing decision will be **data-driven** rather
  than from architectural intuition.

### Risks (and why they are acceptable)

- **Log volume.** ~1 INFO line per inbound + standalone events at HTTP
  rejects. Comparable to existing `[WEBHOOK_IN]` / `[INBOUND_MEDIA_RAW]`
  cadence. Kill switch flips OFF without a deploy.
- **Memory.** `InboundLifecycleTrace` instances are short-lived (one per
  inbound, garbage-collected at context exit). Bounded `events` list
  via path-token cap.
- **Test gating.** Every wiring site has a regression test ensuring the
  legacy contract (return values, exception swallowing) is preserved
  byte-equivalent.

## Rollout

1. Land this commit (telemetry-first, default ON).
2. Observe production for ≥48h on Tenant 33; tabulate the distribution
   of `final=…` and `path=…` tokens.
3. Decide between W2.0.3 (conversation linking — close orphan-message
   leak) and W2.0.4 (idempotency reorder — close mark-before-persist
   leak) **based on the observed frequency** of each failure mode.
4. Kill switch:
   ```bash
   INBOUND_LIFECYCLE_TELEMETRY_ENABLED=0
   ```

## Verification

- `tests/test_inbound_lifecycle_telemetry.py` (30 tests):
  - Architectural invariants (closed vocabulary, kill switch, masking,
    contextvar isolation, never-raises contract).
  - Lifecycle scenarios (happy text, dedup memory drop, dedup DB drop,
    orphan via payment short-circuit, full brain path, unsub
    short-circuit, missing/unknown phone-id drops).
  - Telemetry-only contract: `StateManager.save_message` and the
    `_get_or_create_conversation` wirings preserve return values,
    exception-swallowing semantics, and run safely outside any active
    trace.
- Regression sweep (relational, payment, receipt, handoff, dedup,
  brain audit log, decision router, safety-net suppression) — all
  green; the two pre-existing failures on `main`
  (`test_dash_customer_name_falls_back_to_polite_address`,
  `test_rejects_invalid_kind`) verified unchanged with stash bisect.
