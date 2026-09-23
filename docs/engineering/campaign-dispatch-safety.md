# Campaign dispatch safety — incident, fix and remediation runbook

Status: prepared 2026-09-23. Code fix in this PR; **no production data was
changed and nothing was re-sent** while preparing it.

## 1. Incident (platform-wide defect, first seen on one large campaign)

A manual marketing campaign (audience 8,857) was started with
`POST /campaigns/{id}/dispatch-now` at 10:20:29 UTC and again at 10:55:40 UTC.
Evidence below comes from the runtime logs of deployment `8350f841`
(commit `e0a65c9`) and the webhook logs of `9e79cdd4` (commit `bbc2e4d`).

| Time (UTC) | Evidence |
| --- | --- |
| 10:21:05 | first accepted message |
| 10:34:05 | first post-accept failure `Spam Rate limit hit` (Meta 131048, the number-level spam limit) |
| ≈10:40 → end | almost every accepted message is later reported failed (131048 or 131049 "healthy ecosystem engagement") |
| 10:56:17 | first recipient accepted twice (two wamids ~0.1–0.2 s apart) |
| 11:15:28.840 | last accepted message (both copies) |
| 11:14:55 → 11:16:07 | deploy of `bbc2e4d`; old container removed — this is what stopped the workers |
| after 11:16 | new container: status webhooks only, no sends |

## 2. Root causes (confirmed) vs. hypotheses

**Confirmed — duplicate sends.** `dispatch-now` always spawned a new
background thread. The dispatcher loaded a batch of `queued` rows, then per row
set `status='sending'` with an *uncommitted* flush. `provider_send_message`
commits the caller's session (token-state persistence) before the HTTP call,
releasing the row lock; a second worker holding a stale ORM copy that still said
`queued` then overwrote the row and sent too. Both wamids were recorded on the
same row in turn, so one became an "orphan" for webhook attribution
(`campaign_send_log=orphan`). Reproduced on PostgreSQL 16 against `origin/main`:
two concurrent dispatchers, 7 recipients → **12 requests**. Same scenario with
this fix → 7.

**Confirmed — sending continued long after Meta refused.** The only breaker
looked at *synchronous* errors. Meta accepted each request (HTTP 200 + wamid)
and failed it afterwards via webhook, which no breaker read. The dispatcher also
had no notion of the business-portfolio messaging limit (250 at the time).

**Confirmed — misleading counters.** "Sent" counted Meta acceptances;
"failed" counted only pre-accept rejections (6), not the post-accept failures;
the error panel read a capped 10-entry list unrelated to the counters.

**Confirmed — unsafe recovery paths.** A `sending` row older than 5 min became
`failed/watchdog_timeout` (retryable) or `queued`, so a later dispatch-now could
re-send to someone whose first request had been accepted. A timeout, a 2xx
without wamid, or any exception around the Meta call was a retryable failure.

**Hypothesis, not proven:** why Meta's overview showed 253 of 250. The logs are
consistent with ~253–257 recipients having a copy that Meta never reported as
failed, but delivery receipts are not in the logs (see §5).

## 3. The fix

* **Campaign lease** (`campaign_dispatch_leases`): one conditional UPDATE/INSERT
  admits one live worker per campaign across threads, processes and replicas;
  renewed on every recipient, lapses when a worker dies. `dispatch-now` returns
  `reason=already_running` while it is live; the scheduler/wave paths get
  `already_running` and leave their rows queued.
* **Atomic recipient claim**: `UPDATE … SET status='sending' WHERE id=:id AND
  status='queued'`, committed before any request. A lapsed-lease worker is fenced
  at its next claim.
* **Attempt ledger** (`campaign_send_attempts`): one row per request with its own
  wamid and phases `claimed → request_started → accepted | rejected | not_sent |
  uncertain | abandoned`. A crash before `request_started` is provably unsent
  (re-queued); after it, the recipient is `uncertain` and never re-sent
  automatically. Transport errors before a connection exist are `not_sent`;
  timeouts after the request left, 5xx/non-JSON gateway answers and 2xx without
  a wamid are `uncertain`.
* **Webhook inbox** (`campaign_status_event_inbox`): unique per
  `(wamid, status)` — redeliveries are no-ops; events that beat the attempt's
  commit are parked and applied right after it (both sides re-check after their
  own commit). Delivered/read/failed are timestamps, so order does not matter;
  read implies delivered.
* **Shared messaging budget**: keyed by business portfolio (else WABA, else
  number). Counts distinct recipients in the last 24 h across every campaign and
  tenant in the scope (ledger + pre-ledger rows). The limit comes from the stored
  Meta tier only when fresh (≤24 h); unknown/stale → Meta's starting limit (250),
  never "unlimited". Campaigns may use 90 % (`NAHLA_CAMPAIGN_LIMIT_BUDGET_PERCENT`).
  When spent the campaign is **paused** (`messaging_limit_reached`), queue intact.
* **Breakers**: post-accept `spam_rate_limit` ×5, `rate_limit` ×10,
  `marketing_blocked` ×25 in 15 min in the scope; synchronous `spam_rate_limit`
  ×5; 3 consecutive uncertain outcomes → pause with a reason.
* **Safe stop**: `POST /campaigns/{id}/stop` (and the existing pause action) sets
  a stop flag the worker checks before every claim.
* **Retry policy**: a recipient is re-queued only if every attempt is proven
  unsent and the code is retryable. Ambiguous legacy codes (`watchdog_timeout`,
  `exception`, `no_message_id`) are never auto-retried.
* **Frequency cap**: uncertain recipients count as contacted; a failure after
  acceptance frees the recipient only when the ledger proves no copy arrived
  (pre-ledger rows keep counting — their single wamid column can't prove it).
* **Counters**: recipient buckets (`failed_before_accept`, `failed_after_accept`,
  `failed_total`, `pending_delivery`, `uncertain`, `in_flight`) and separate
  message-scope counts (`messages_*`); `error_breakdown` counts the same rows.
  `lifecycle` shows `stalled` when status says active but no worker holds the
  lease, and `paused` with its reason.
* Meta text classification: "Spam Rate limit hit" → `spam_rate_limit` (was the
  retryable `rate_limit`); "healthy ecosystem engagement" → `marketing_blocked`.

Meta references: messaging limits are applied per business portfolio since
2025-10-07 and `messaging_limit_tier` is deprecated in favour of
`whatsapp_business_manager_messaging_limit` (Meta for Developers,
"Messaging Limits"). `fetch_meta_phone_tier` still requests the deprecated
field; switching it is a follow-up (it writes connection rows, so it is not
changed in this PR).

Schema: three **new** tables, created by the boot-time `create_all`
(`backend/main.py`). No existing table is altered. No Alembic revision is added
in this PR because the repository's bootstrap migration contract pins the
accepted heads; a parity revision should follow in a governance PR.

## 4. Tests

`tests/test_campaign_send_ledger.py` — runs on SQLite and, with
`NAHLA_CAMPAIGN_LEDGER_PG_DSN`, on PostgreSQL (real row locks): concurrent
dispatches, lease takeover, CAS on a stale ORM copy, crash before request /
after request / after accept before save, timeout / no-wamid / gateway HTML,
connect error retried once, early / duplicate / out-of-order webhooks, failure
after accept, per-wamid attribution with two copies, multi-attempt history,
frequency cap (uncertain + ledger-proven failure + legacy rows), shared limit
across two campaigns and numbers, pre-ledger usage, stale/missing tier,
post-accept breaker, stop, dispatch-now refusal, `stalled` lifecycle, the real
webhook handler (PG only), and an 8-thread race for lease and claim.
`tests/test_campaign_send_reconciliation.py` covers the reconciliation rules.

## 5. Reconciliation of the incident campaign (read-only, logs)

`scripts/operators/campaign_send_reconciliation.py --no-database` over every
`campaign=<id>` log line of the old deployment (10:15–11:20 UTC, pages of
<500 lines, overlapping pages de-duplicated) and every `status_failed` line
for the tenant on both deployments:

| Messages (wamids) | count |
| --- | --- |
| accepted by Meta | 1,926 |
| reported failed after acceptance | 1,667 (131048 spam limit 1,033 · 131049 ecosystem 490 · undeliverable 108 · experiment 36) |
| no failure report (delivered, read or pending — logs cannot tell) | 259 |

| Recipients | count |
| --- | --- |
| accepted at least once | 1,481 (1,036 once, **445 twice**) |
| every copy failed (after acceptance) | 1,224 |
| rejected before acceptance only | 6 |
| one copy, no failure report (candidates for "delivered once") | 253 |
| two copies, at least one without failure report | 4 |
| excluded before sending (manual exclusion, snapshot 10:55:41) | 55 |
| no attempt started | ≈7,315 (8,857 − 55 − 1,481 − 6; exact figure from the DB run) |

Coverage and gaps:
* Delivery/read receipts are logged with a truncated wamid, so **delivered vs.
  pending is not derivable from logs**; the two "delivered" categories need the
  database run below. No recipient is claimed delivered here.
* 6 failure events carry wamids absent from the accept lines (other sends or
  a dropped log line). UI "sent 1,483" vs 1,481 unique accepted recipients in
  logs: a 2-row gap consistent with rows whose final state was never logged.
* Late failure webhooks may still arrive.

Authoritative run (read-only transaction; reads `campaign_send_logs`,
`message_events` — which keeps one row per accepted copy with its receipt
flags — and `campaign_send_attempts` if present):

```bash
DATABASE_URL=<read-only credentials> python scripts/operators/campaign_send_reconciliation.py \
  --tenant-id <T> --campaign-id <C> --emit-recipients --out reconciliation.json
```

## 6. Production remediation plan (separate from the code fix; needs approval)

1. **Before deploy**: nobody presses "إرسال يدوي الآن" or "استئناف" on the
   affected campaign. On the current code that re-queues watchdog rows and could
   re-send.
2. Deploy this PR. Boot creates the three tables; existing rows are untouched.
   The campaign is not resumed automatically (rescue only targets campaigns with
   zero send-log rows; wave/scheduler paths respect `paused`).
3. Run the read-only reconciliation (§5) and review it.
4. Proposed data corrections — **only after review, in one audited transaction**:
   * rows left in `sending` by the killed workers → `uncertain` (the new code
     does this itself on the next dispatch; doing it explicitly just makes the UI
     honest earlier);
   * optionally create `campaign_send_attempts` rows for the 1,926 historical
     wamids (from `message_events`) so counters and frequency cap see both
     copies. Not required for safety; changes reporting only.
   * no change to `sent`/`failed` classification of historical rows.
5. Resume decision (merchant + support): the portfolio limit was exhausted and
   Meta flagged the number (131048). Do **not** resume until the 24 h window has
   rolled and Meta shows the limit/quality recovered. The new dispatcher then
   sends at most the remaining budget, pauses itself at the limit or on renewed
   throttling, never re-sends to anyone accepted or uncertain, and the frequency
   cap keeps the 1,481 contacted recipients out of other campaigns for 14 days.
6. Follow-ups: read `whatsapp_business_manager_messaging_limit`; an Alembic
   parity revision for the three tables; wire order/automation template sends
   into the budget ledger.
