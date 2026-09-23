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
| 10:34:05 | first post-accept failure titled `Spam Rate limit hit` |
| ≈10:40 → end | almost every accepted message is later reported failed ("Spam Rate limit hit" or "…healthy ecosystem engagement") |
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

Raw Meta error codes are `REDACTED` in these logs; failures are identified by Meta's error *title* only. The classifier maps these titles to `spam_rate_limit` / `marketing_blocked`; the numeric codes (131048 / 131049 in Meta's catalogue) are not asserted for these events.

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
* **Atomic shared budget**: the budget check and the recipient reservation
  run in one transaction under a lock on the scope's row
  (`campaign_messaging_scopes`), and reservations that have not started yet
  already count. Two campaigns (any process/replica) on one portfolio cannot
  both take the last slot — proven by a truly concurrent two-thread test on
  PostgreSQL with a widened race window, whose negative control (scope lock
  removed) overshoots.
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

Schema: four **new** tables (`campaign_dispatch_leases`,
`campaign_send_attempts`, `campaign_status_event_inbox`,
`campaign_messaging_scopes`), created by the boot-time `create_all`
(`backend/main.py`). No existing table is altered. No Alembic revision is added
in this PR because the repository's bootstrap migration contract pins the
accepted heads; a parity revision should follow in a governance PR.

Proven on a production-like PostgreSQL 16 database (`alembic upgrade 0093` —
the pinned bootstrap target — then the pre-PR `create_all`, then this PR's
`create_all`): exactly the four tables appear with their unique indexes
(`uq_campaign_send_attempt_wamid`, `uq_campaign_send_attempt_log_no`,
`uq_campaign_status_event_wamid_status`, …); every pre-existing table's
columns, indexes and foreign keys are identical before and after; a second
boot changes nothing (`test_boot_create_all_on_a_production_like_database`).

If the tables are late or missing (boot `create_all` still running or
failed): every send path checks `ledger_available()` first and refuses —
`dispatch_campaign` returns `ledger_unavailable` without touching the campaign
(it is not marked failed), `dispatch-now` answers `reason=ledger_unavailable`,
wave ticks put the wave back to pending, nothing reaches Meta. Read paths
(campaign list, stats, pause) keep working through savepoints; the status
webhook falls back to its pre-ledger path. Once the tables exist the next
tick/click proceeds (`test_missing_ledger_tables_fail_closed`).

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

`scripts/operators/campaign_send_reconciliation.py --no-database
--infer-accepted-from-failures` over every `campaign=<id>` log line of the old
deployment (10:15–11:20 UTC, pages of <500 lines, overlapping pages
de-duplicated) and every `status_failed` line for the tenant on both
deployments:

| Messages (wamids) | count |
| --- | --- |
| accepted by Meta — accept line in the logs | 1,926 |
| accepted by Meta — accept line missing, proven by its failure webhook | 6 |
| **accepted, total** | **1,932** |
| reported failed after acceptance | 1,673 — by Meta's error title: "Spam Rate limit hit" 1,037 · "…healthy ecosystem engagement" 492 · "Message undeliverable" 108 · "User's number is part of an experiment" 36 |
| no failure report (delivered, read or pending — logs cannot tell) | 259 |

| Recipients | count |
| --- | --- |
| accepted at least once | **1,483** (1,034 once, **449 twice**) |
| every copy failed (after acceptance) | 1,226 |
| rejected before acceptance only | 6 |
| one copy, no failure report | 252 |
| two copies, at least one without failure report | 5 |
| excluded before sending (manual exclusion, snapshot 10:55:41) | 55 |
| no attempt started | ≈7,313 (8,857 − 55 − 1,483 − 6; exact figure from the DB run) |

How 1,481 became 1,483 (evidence, not an estimate): the 6 failure webhooks
whose wamid had no accept line all arrived 11:12:38–11:12:47 UTC. Two of them
are for recipients with no accept line at all (…0004 and …6780); each has one
`campaign_send_log=matched` and one `orphan` event — i.e. a sent recipient row
of this campaign plus the duplicate copy. The other four are the missing
second copies of two recipients already counted. So ~6 accept lines are
absent from the Railway export around 11:12:3x (that window returned 214 lines,
below the 500 cap — not pagination). Cross-check: the export counts **925**
unique accepted recipients before the 10:55:41 snapshot, which also says
`sent: 925`.

Coverage and gaps:
* Delivery/read receipts are logged with a truncated wamid, so **delivered vs.
  pending is not derivable from logs**; the two "delivered" categories need the
  database run below. No recipient is claimed delivered here, and the 259
  messages without a failure report are *not* "delivered".
* The inferred copies rely on the webhook's own `campaign_send_log=matched`
  flag; a DB run replaces this inference with the rows themselves.
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
2. Deploy this PR. Boot creates the four tables; existing rows are untouched.
   Verify after boot: `SELECT to_regclass('campaign_send_attempts')` (and the
   other three) is not null before allowing any campaign action.
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
   Meta reported "Spam Rate limit hit" for this number. Do **not** resume until the 24 h window has
   rolled and Meta shows the limit/quality recovered. The new dispatcher then
   sends at most the remaining budget, pauses itself at the limit or on renewed
   throttling, never re-sends to anyone accepted or uncertain, and the frequency
   cap keeps the 1,481 contacted recipients out of other campaigns for 14 days.
6. Follow-ups: read `whatsapp_business_manager_messaging_limit`; an Alembic
   parity revision for the three tables; wire order/automation template sends
   into the budget ledger.
