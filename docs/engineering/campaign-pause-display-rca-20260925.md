# Campaign pause display and live queue

## Evidence / first divergence

Owner screenshot (25 September, 02:12 Asia/Riyadh) shows paused Campaign 35,
Meta-accepted 1764, and a funnel queue of 7134, with a truncated red warning
that Meta restricts the number. No tenant-specific behavior is introduced.

Read-only API deployment logs from `15266c06-67c1-416d-9eb4-6f2ab587976a`:
24 September 21:08:13 UTC: `marketing_blocked x25`, then `status=paused`,
sent=1764, failed=7, queued=7039, manual exclusions=57.
The 21:00–22:56 UTC fetched window contains 95 accepted send log entries.
These establish provider acceptance, not customer delivery or absence of
historical duplicates. Filtered logs from deployment `f483c59e` returned no
campaign=35 entries; absence is not proof of current database state.

The screenshot's warning matches the API label added in #1149. The row
renders all last_error values red and truncated, regardless of a structured
pause reason. That conflates a protective provider pause with execution
failure, and wording suggests a number-wide restriction not established by
these receipts. The funnel separately prefers its launch snapshot queue
count over the live send-log aggregate.

## Repair

Use the structured paused state to choose an AR/EN notice. Marketing delivery
refusal is distinguished from capacity/rate-limit waits and operational
failures. Known provider/merchant pauses are amber; uncertainty and read
failures remain red. Show the full notice with wrapping, preserve raw copy
for support. Window expiry describes retry eligibility, not guaranteed Meta
delivery. Explicitly label the diagnostic sent total as provider acceptance.
Queue count comes from live rows, including zero, while historical audience
snapshot fields remain intact.

## Boundaries / validation

No dispatcher, scheduler, guards, retries, campaign status, AI behavior,
prompts, model, migration or production configuration is changed.

- Campaign debug suite: 72 passed, including stale nonzero snapshot with a
  partially drained queue and an empty queue.
- AR/EN notice checks: marketing pause amber, operational/uncertain failures
  red, active campaign cannot inherit an old pause notice.
- Dashboard TypeScript and both i18n checks passed.
- Negative control: both queue tests fail against the production router (4 vs 0 and 4 vs 2).
- Throttle and constitution suites: 78 passed; 17 PostgreSQL variants skipped locally (no PostgreSQL DSN). CI must run PostgreSQL proofs.
- Independent review and exact-head CI remain merge gates.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
