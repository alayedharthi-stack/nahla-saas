# Start-once campaign continuation (25 September 2026)

Owner request: a merchant starts a campaign once; temporary provider/capacity
waits continue from the durable queue without repeated dashboard clicks.
This explicitly supersedes the former manual-only policy for a **typed,
marketing-only post-accept breaker**. It does not authorize duplicate sends.

## First divergence

`AUTO_RESUME_REASONS` supported only shared capacity and pre-accept rate limits.
The observed campaign 35 / tenant 33 paused as `provider_throttling` after
25 `marketing_blocked` receipts. `_note_capacity_wait` cleared its wait, so the
scheduler deliberately excluded it. The 15-minute breaker ageing out only made
a manual run possible; it did not schedule one.

## Behaviour

- Capacity: keep waiting durably; recheck every 15 minutes to discover tier
  upgrades without a dashboard visit (refresh cache maximum age 15 minutes).
  Per-recipient shared-scope capacity admission and the 90% margin are unchanged.
- Proven pre-accept rate limit: existing 5–60 minute exponential backoff remains.
- Post-accept **marketing-only** breaker: 1, 2, 4, 8, 16, then at most 24 hours
  between continuation checks, persisted across deploys and other wait reasons.
  These are application cooldowns, not Meta guarantees. Only untouched or
  proven-not-accepted recipients may pass the existing claim guard; accepted,
  delivered, read, uncertain and unresolved copies are never requeued here.
- Every due continuation re-reads the shared breaker before activating or
  requeueing. Late marketing receipts extend the cooldown. Concurrent spam or
  post-accept rate blocks take precedence and remain action-required.
- Merchant stop, cancellation, a live lease or changed wait state wins even if
  it arrives while the asynchronous tier refresh is in progress.
- Legacy pauses without the typed wait are not silently adopted by deployment.
  The owner-authorized existing campaign needs a scoped adoption or explicit
  resume after rollout. No blanket backfill is permitted.
- The page exposes `marketing_delivery_backoff` and the next **check** time,
  not a guaranteed delivery time. Accepted and delivered counts stay separate.

## Evidence and gates

Tests use a generic store, scripted provider, real dispatcher and scheduler.
The new suite is registered in the strict PostgreSQL proof manifest (SQLite and
PostgreSQL variants must pass in that job; skips cannot count as proof).
Coverage: durable cooldown, late receipts, mixed spam/marketing priority,
manual stop including during refresh, legacy record refusal, shared capacity,
accepted-copy exclusion, live-worker exclusion, bounded backoff, automatic tier
upgrade discovery, unreadable evidence and concurrent scheduler workers.

No model, prompt, persona, customer wording, Commerce Runtime, pagination,
or staged Railway patch 762f0a9a is changed. UI operational labels are deterministic.
