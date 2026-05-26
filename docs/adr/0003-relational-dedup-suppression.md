# ADR 0003 — Relational Dedup Suppression

**Status:** Accepted (2026-05-26) — locked before W3 implementation.
**Scope:** Wave 3, phase 1 — *narrow seasonal hotfix*. W3.1 + W3.2 + W3.3 only.
**Out of scope (deferred):** W3.4 (AI Pause Guard recovery suppression), W3.5
(media-kind seasonal-greeting detection).

---

## Context

Production audit on Tenant 33 during Eid season surfaced a category error in
the post-Brain dedup pipeline.

The customer sent a religious greeting / supplication ("الله يحفظك",
"بارك الله فيك", "كل عام وأنت بخير"). The Brain replied warmly and naturally.
On the next, lexically-similar greeting from the customer, the Brain again
composed a warm reply — and the *Webhook Outbound Dedup Guard*
(`backend/routers/whatsapp_webhook.py`) detected ≥85% Jaccard token overlap
with the previous outbound and **substituted** the Brain's reply with one of
three canned "I already answered, point to the gap" lines:

```python
_DEDUP_FALLBACK_REPLIES = [
    "ذكرت لك للتو نفس النقطة 🌷 وش الجزء اللي تبيني أوضحه أكثر؟",
    "هذي نفس الإجابة قبل قليل — قلي على وجه التحديد إيش الناقص.",
    "هذي قريبة من سؤال قبل قليل 🌷 أي نقطة تحب أوضحها لك أكثر؟",
]
```

The merchant's report:

> العميل أرسل دعاء ورسالة دينية طويلة، والرد الأول من الذكاء كان ممتازًا
> وطبيعيًا جدًا. لكن عندما أرسل دعاء آخر أو رسالة مشابهة، ظهرت عبارة:
> "هذي نفس الإجابة قبل قليل — قلي على وجه التحديد إيش الناقص"

## Diagnosis

The dedup guard was designed for the transactional-loop scenario:

> Customer asks "كم السعر؟" twice → Brain replies with the same price → guard
> assumes the Brain is stuck and asks the customer to clarify what's missing.

It treats every reply with the same numeric threshold (85% lexical overlap),
regardless of conversational mode. But:

> **In social, religious, and seasonal exchanges, lexical repetition is the
> relationship itself.**
>
> "الله يحفظك" ↔ "ويحفظك"
> "بارك الله فيك" ↔ "وفيك بارك الله"
> "كل عام وأنت بخير" ↔ "وأنت من أهله، تقبل الله منا ومنكم"
>
> These are ritual exchanges. High lexical overlap is *what makes them
> correct*, not evidence of a loop.

The current relational layer (Wave 1, Commits 1–3):

* Has `SOCIAL_CHECK_IN` and `GRATITUDE_GENERIC` moments, but *no specific*
  religious-ritual or seasonal-greeting moment.
* The Wave 1 Commit 3 safety-net suppression (`SUPPRESSIBLE_NETS = {"store_link",
  "location"}`) does not cover the dedup substitution layer — by design, since
  Commit 3 was scoped to artifact-injecting safety nets, not outbound rewriters.
* The AI Pause Guard's loop scorer is independent and only knows
  sales-intent keywords for decay.

→ The Brain produces the right reply; the post-Brain dedup layer corrupts
it because it cannot read the conversational mode.

## Decision

Introduce a **narrow, additive** suppression gate that protects the Brain's
outbound text from the dedup-substitution layer **only** when the conversational
mode is religious, seasonal, or social-warm.

### Architectural rules (locked)

1. The gate is a **pure function**. No DB, no I/O, no state mutation.
2. The gate runs **only at one call site**: just before
   `_DEDUP_FALLBACK_REPLIES` substitution in `routers/whatsapp_webhook.py`.
3. The gate is **kill-switched** behind `RELATIONAL_DEDUP_SUPPRESSION_ENABLED`
   (default OFF). When the flag is off the gate is inert and the legacy
   substitution path runs unchanged.
4. The gate **never** modifies the Brain's reply, the conversation state,
   the Brain's prompt, the AI Pause Guard score, or any persisted artifact.
5. The gate **only** decides "should the dedup substitution be skipped?".
   It returns a typed decision the call site reads. The Brain's reply is
   then sent as-is iff the gate said `suppress=True`.
6. The dedup guard **stays alive** for transactional turns. We do not
   widen the allow-bypass list; we only add a moment-conditional carve-out.
7. The AI Pause Guard / Loop Detector is **untouched** in W3.3.
   W3.4 will revisit it later behind a separate flag if production data
   shows the recovery line is also misfiring on relational moments.
8. **Payment / order / receipt / OCR / media / handoff / takeover** paths
   are untouched. The gate's input set is `(inbound_text, relational_moment,
   overlap)` — none of those routes feed it transactional state, and the
   gate explicitly *refuses* to suppress when the moment is
   `TRANSACTIONAL_ACTIVE` (so a stray "الحمد لله" mid-funnel does not bypass
   dedup).

### W3.1 — Additive moments

Two new values added to `ConversationMoment` (the enum stays closed; tests
fail the build on drift):

* `RELIGIOUS_RITUAL_EXCHANGE` — short turn whose dominant content is a
  religious supplication / blessing / ritual formula. Independent of any
  product / order / payment context.
* `SEASONAL_GREETING` — turn carrying explicit seasonal greeting phrases
  (Eid, Ramadan, "كل عام", congratulations).

Classifier extension is **conservative**: the new moments fire only when
no transactional / complaint / praise-post-delivery moment matched. They
take precedence over the lighter `SOCIAL_CHECK_IN` / `GRATITUDE_GENERIC`
moments.

### W3.2 — `RelationalDedupSuppressionGate`

A new pure module exposing:

* Closed `RELIGIOUS_RITUAL_MARKERS` Arabic phrase set (≤30 entries —
  deliberate, not a regex catch-all).
* Closed `SEASONAL_GREETING_MARKERS` Arabic phrase set.
* `should_suppress_dedup_substitution(inbound_text, relational_moment, overlap)`
  → `DedupSuppressionDecision(suppress, reason, moment, …)`.
* `is_relational_dedup_suppression_enabled()` — kill switch reader.
* `log_dedup_suppression(...)` — emits the canonical `[CX] dedup_suppression`
  log line (gated by the flag).

Suppression fires when **any** of the following is true:

| Trigger | Source |
| --- | --- |
| `moment ∈ {RELIGIOUS_RITUAL_EXCHANGE, SEASONAL_GREETING, SOCIAL_CHECK_IN, GRATITUDE_GENERIC, PRAISE_POST_DELIVERY}` | Wave 1 relational classifier |
| `inbound_text` matches a `RELIGIOUS_RITUAL_MARKERS` phrase | text backstop (works even if relational layer is OFF) |
| `inbound_text` matches a `SEASONAL_GREETING_MARKERS` phrase | text backstop |

Suppression **never** fires when:

| Block | Reason |
| --- | --- |
| `moment == TRANSACTIONAL_ACTIVE` | mid-funnel → keep dedup honest |
| `moment ∈ COMPLAINT_*` | complaint loops are real loops, must surface to recovery / handoff |
| `moment == ESCALATION_REQUEST` | handoff path bypasses dedup anyway |
| Reply already bypassed dedup via `_reply_carries_new_signal` | call site never reaches the gate |

### W3.3 — Wiring (single call site)

In `routers/whatsapp_webhook.py`, immediately before
`_short_followup_instead_of_repeat(history)` is called, the gate is consulted.
If suppression fires, the Brain's reply is left untouched, a structured
`[CX] dedup_suppression` line is logged, and the substitution is skipped.

The wiring path is wrapped in `try/except` with a defensive `log_only`
fallback — if the gate explodes, the legacy substitution runs (zero
regression).

### Telemetry

```text
[CX] dedup_suppression
  decision=<suppress|legacy>
  reason=<social_check_in|gratitude_generic|praise_post_delivery
         |religious_ritual_exchange|seasonal_greeting
         |religious_marker_text|seasonal_marker_text
         |moment_blocks_suppression|no_marker>
  moment=<token-or-empty>
  overlap=<float, 2dp>
  would_have_replaced=<bool>
  tenant_id=<int>
  conversation_id=<int|None>
```

## Consequences

### Positive

* The merchant's reported failure mode disappears without altering Brain
  behaviour or any transactional layer.
* The Wave 1 relational layer gets one more concrete consumer
  (`dedup_suppression`), strengthening the architectural pattern that
  *post-Brain layers must respect the conversational mode*.
* Telemetry surfaces how often this gate would have triggered — a
  pre-condition for any future hardening.

### Negative / risks

* The phrase markers list is small and deliberate; rare regional dialects
  may not match. This is acceptable for a hotfix — production telemetry
  will tell us whether the marker list needs extension.
* Two consecutive religious greetings *with no commerce intent in between*
  are now both replied to verbatim. This is the correct human behaviour;
  no longer a regression.
* Adding moments is a closed-enum change. Every Wave 1 consumer that
  enumerates `ConversationMoment` must be updated to handle the new
  values (or fall through to NONE behaviour, which is the contract).

## Rollout plan

1. Land W3.1 + W3.2 + W3.3 with `RELATIONAL_DEDUP_SUPPRESSION_ENABLED=false`
   in code (default OFF).
2. Deploy. Verify zero behavioural delta with the flag off.
3. Ops flips the flag globally on production for both currently-active
   tenants.
4. Monitor `[CX] dedup_suppression decision=suppress` distribution for 24h.
   Confirm:
   * the offending substitution is gone for religious / seasonal turns,
   * `decision=legacy` still fires on transactional repeated questions,
   * no payment / order / handoff regression in `[ORDER_FLOW_STATE]` /
     `[CHAT_DEDUP]` / `[AI_GUARD]` lines.
5. After Eid season, revisit data → decide if W3.4 (AI Pause Guard) is
   warranted, and if the marker list needs widening.

## What we are *not* doing in this ADR

* Not redesigning loop detection.
* Not removing or weakening the dedup guard.
* Not touching the AI Pause Guard.
* Not adding canned religious replies. The Brain owns wording.
* Not adding tenant-specific configuration. Hotfix is global once the
  flag is on.
* Not introducing media-kind detection for greeting cards (W3.5 territory).
* Not changing any payment, order, receipt, OCR, extraction, handoff, or
  takeover behaviour.
