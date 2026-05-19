# Webhook Security — Operator Runbook (Phase 1B)

This document is the single source of truth for verifying, observing, and
enforcing webhook signatures across every external provider Nahla
ingests. It complements `PHASE_1A_RUNBOOK.md` (which covers auth, secrets,
Sentry, Cloudflare).

Phase 1B ships every provider in **audit-only mode**. Code paths exist to
reject bad signatures, but they are gated by environment flags that
default to `false`. Promoting those flags is an operator action that
follows the rollout schedule below.

---

## 1. What landed in code

- `backend/core/webhook_security.py` — verifier library for Meta, Salla
  (Communication + Sync OAuth), Zid, Moyasar, HyperPay, plus `check_replay`
  and the combined `evaluate_replay` helper.
- `backend/core/webhook_audit.py` — Redis counters + bounded
  recent-failures ring; `record_result` and `record_replay`.
- `backend/core/webhook_enforcement.py` — per-tenant override stored in
  `TenantSettings.extra_metadata.webhook_enforcement.<provider>.enforce`.
- `backend/routers/whatsapp_webhook.py` — Meta `POST /webhook/whatsapp`
  now verifies `X-Hub-Signature-256` against `META_APP_SECRET`.
- `backend/routers/zid_oauth.py` — Zid `POST /webhook/zid` now uses the
  library + audit + enforce flag.
- `backend/routers/webhooks.py` — Salla Communication and Sync OAuth
  endpoints route through the library and respect per-tenant overrides.
- `backend/routers/admin_webhook_security.py` — operator dashboard:
  - `GET /admin/webhooks/audit-summary` — counts per provider/tenant/status
  - `GET /admin/webhooks/audit-summary/failures` — recent invalid samples
  - `POST /admin/webhooks/enforcement` — flip a per-tenant flag
- `backend/scripts/backfill_d360_coexistence_secret.py` — one-shot
  back-fill of 360dialog connections missing `coexistence_internal_secret`.
- `scripts/preflight_check.py` — refuses to boot in production when
  `ZID_WEBHOOK_REQUIRED_AT_BOOT=true` and `ZID_WEBHOOK_SECRET` is empty.

---

## 2. Environment flags introduced

All default to safe values. Promote in the order described in §4.

| Variable                                | Default | Promote when                                                            |
|-----------------------------------------|---------|-------------------------------------------------------------------------|
| `META_WEBHOOK_ENFORCE_SIGNATURE`        | `false` | `valid` ≥ 99% across all merchants for ≥7 days                          |
| `META_WEBHOOK_ALLOW_MISSING_SIGNATURE`  | `true`  | After enforce flips and audit shows zero `missing` for ≥3 days          |
| `ZID_WEBHOOK_ENFORCE_SIGNATURE`         | `false` | After 7-day clean audit window                                          |
| `ZID_WEBHOOK_REQUIRED_AT_BOOT`          | `false` | Set together with `ZID_WEBHOOK_ENFORCE_SIGNATURE` so a misconfig boots fail-fast |
| `MOYASAR_WEBHOOK_REQUIRE_VERIFIED`      | `false` | After audit; enforces strict verification on `/payments/webhook/moyasar` and `/billing/webhook/moyasar/subscription` once promoted (separate task) |
| `HYPERPAY_WEBHOOK_REQUIRE_VERIFIED`     | `false` | After audit                                                             |
| `WEBHOOK_REPLAY_PROTECTION_ENABLED`     | `false` | After signature enforcement is solid; this only enables observation     |
| `WEBHOOK_REPLAY_REJECT_ENABLED`         | `false` | After observing replay rates per provider, only when legitimate-retry rate is < 0.1% |

Salla still uses the legacy flags from Phase 1A:

- `SALLA_WEBHOOK_ENFORCE_SIGNATURE` (default `false`)
- `SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE` (default `true`)

These are the **global fallback** for Salla. The per-tenant override
takes precedence when set — see §5.

---

## 3. Daily monitoring

```sh
# Aggregate counts for the last 7 days, all providers
curl -H "Authorization: Bearer $ADMIN_JWT" \
     https://api.nahlah.ai/admin/webhooks/audit-summary?days=7

# Recent invalid / missing samples for triage
curl -H "Authorization: Bearer $ADMIN_JWT" \
     "https://api.nahlah.ai/admin/webhooks/audit-summary/failures?provider=salla&limit=50"
```

Expected shape (truncated):

```json
{
  "providers": {
    "salla": {
      "totals": {"valid": 1234, "invalid": 0, "missing": 12, "secret_not_configured": 0, "replay": 0},
      "by_day": {"2026-05-12": {"valid": 200, ...}, ...},
      "tenants": {"5": {"valid": 1000, "invalid": 0, ...}, ...}
    },
    "meta": {...},
    "zid":  {...}
  },
  "env_flags": {...},
  "since": "2026-05-12",
  "until": "2026-05-18"
}
```

The healthy steady state is `valid` dominating per provider per tenant
with `invalid + missing` summing to less than 1% of total. Spikes in
`missing` typically mean the merchant rotated their Partner Portal secret
and forgot to update Railway, or installed Nahla on a Salla store whose
Partner Portal entry has no webhook secret yet.

---

## 4. Rollout sequence

The order matters. Each step assumes the previous step's audit window
is clean.

### 4a. Meta global flip

1. Confirm `META_APP_SECRET` value in Railway matches the App Dashboard.
2. Watch `audit-summary?provider=meta&days=7`.
3. When `valid` ≥ 99% for 7 consecutive days:
   - Set `META_WEBHOOK_ENFORCE_SIGNATURE=true` in Railway.
   - Redeploy. `start.sh` will run preflight and uvicorn will pick up
     the new flag.
4. Watch the dashboard for any uptick in `missing` over the next 48h.
   Investigate with the recent-failures ring before flipping
   `META_WEBHOOK_ALLOW_MISSING_SIGNATURE=false`.

### 4b. Zid global flip

1. Audit `audit-summary?provider=zid&days=7`. Even with the stub handler,
   `valid` should already be 100% from any active Zid merchant.
2. If `ZID_WEBHOOK_SECRET` is unset in Railway, set it now and configure
   the same value in Zid Partner Portal for every active app.
3. Set both flags together to avoid a half-open state:
   - `ZID_WEBHOOK_ENFORCE_SIGNATURE=true`
   - `ZID_WEBHOOK_REQUIRED_AT_BOOT=true`
4. Redeploy. Preflight refuses to start the worker if the secret is
   missing.

### 4c. Salla per-tenant rollout

Salla is the highest-volume merchant integration and Partner Portal
config varies per tenant; the rollout is per-merchant rather than global.

For each live Salla merchant (track in
`docs/security/SALLA_WEBHOOK_MIGRATION.md` if you want a paper trail):

1. Confirm Partner Portal entry for both apps (Communication +
   Sync OAuth) has the matching webhook secret.
2. Trigger a test webhook from Partner Portal.
3. Confirm `audit-summary?provider=salla` shows `valid` for that
   tenant_id with no `missing/invalid` for ≥3 days.
4. Flip the per-tenant flag:

   ```sh
   curl -X POST -H "Authorization: Bearer $ADMIN_JWT" \
        -H "Content-Type: application/json" \
        -d '{"tenant_id": 5, "provider": "salla", "enforce": true}' \
        https://api.nahlah.ai/admin/webhooks/enforcement

   curl -X POST -H "Authorization: Bearer $ADMIN_JWT" \
        -H "Content-Type: application/json" \
        -d '{"tenant_id": 5, "provider": "salla_oauth", "enforce": true}' \
        https://api.nahlah.ai/admin/webhooks/enforcement
   ```

5. After all live merchants are flipped, promote the global env flags:
   - `SALLA_WEBHOOK_ENFORCE_SIGNATURE=true`
   - `SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE=false`

6. Tear down per-tenant overrides (optional cleanup): they remain
   correct (true) but become redundant once the global default is
   `true`.

### 4d. 360dialog secret back-fill

Run during a chosen quiet window:

```sh
# Dry-run first — prints the plan, makes no changes
python backend/scripts/backfill_d360_coexistence_secret.py

# Targeted single tenant
python backend/scripts/backfill_d360_coexistence_secret.py --tenant 12

# Real run, all candidates
python backend/scripts/backfill_d360_coexistence_secret.py --apply

# Real run, capped (staged rollout)
python backend/scripts/backfill_d360_coexistence_secret.py --apply --limit 5
```

The script is idempotent: rows that already have a
`coexistence_internal_secret` are skipped. Each successful row gets a
fresh `secrets.token_urlsafe(24)` secret pushed to 360dialog and
persisted on the connection's `extra_metadata`.

### 4e. Moyasar / HyperPay enforcement

After the audit window:

1. Set `MOYASAR_WEBHOOK_REQUIRE_VERIFIED=true` in Railway.
2. Set `HYPERPAY_WEBHOOK_REQUIRE_VERIFIED=true` in Railway.
3. Redeploy. The handlers in `backend/routers/webhooks.py` will then
   require a valid signature for every payment webhook before mutating
   billing rows.

(These flags are read by the Moyasar / HyperPay handlers' future
follow-up; today the handlers already verify signatures when the secret
is present. The flag is the lever to refuse-on-missing.)

### 4f. Replay protection

1. Set `WEBHOOK_REPLAY_PROTECTION_ENABLED=true` in Railway.
   The handlers will start dedup'ing body hashes and writing `replay`
   counters to the dashboard, but **will not reject** anything yet.
2. Watch `audit-summary` for `replay` counts per provider.
3. Confirm legitimate-retry rate is < 0.1% per merchant for ≥7 days.
4. Set `WEBHOOK_REPLAY_REJECT_ENABLED=true`.
5. Replays are now dropped with a 200 `{"status":"ignored","reason":"replay"}`.

If the legitimate-retry rate is too high for any provider, leave
rejection off for that provider and rely on application-level dedup
(Salla `external_event_id`, Meta `inbound_dedup`).

---

## 5. Per-tenant override storage

```json
{
  "tenant_settings.extra_metadata": {
    "webhook_enforcement": {
      "salla":       {"enforce": true,  "updated_at": "2026-05-18T10:30:00Z", "updated_by": "ops@nahlah.ai"},
      "salla_oauth": {"enforce": true,  "updated_at": "2026-05-18T10:31:00Z", "updated_by": "ops@nahlah.ai"},
      "meta":        {"enforce": false, "updated_at": "2026-05-19T07:00:00Z", "updated_by": "ops@nahlah.ai"},
      "zid":         {"enforce": false, "updated_at": "2026-05-19T07:01:00Z", "updated_by": "ops@nahlah.ai"}
    }
  }
}
```

Resolution order: per-tenant override wins; otherwise the global env
flag for the provider applies.

To inspect the current state for a tenant:

```sql
SELECT extra_metadata->'webhook_enforcement'
FROM tenant_settings
WHERE tenant_id = 5;
```

---

## 6. Rollback

If an enforcement flip causes a merchant outage:

1. **Per-tenant**: flip the flag back via the admin endpoint with
   `enforce: false`. Effect is immediate (next request).
2. **Global env flag**: set the env var to `false` in Railway and
   redeploy. Workers re-import config on startup.
3. **Replay rejection**: set `WEBHOOK_REPLAY_REJECT_ENABLED=false`. The
   PROTECTION flag can stay on; only the rejection path turns off.

A panic-button "audit-only-everywhere" rollback is a single redeploy
with every `*_ENFORCE_*` env var set to `false`.

---

## 7. What is intentionally NOT in scope of Phase 1B

- Outbound webhook signing (we publish webhooks for merchant-side
  integrations; that's a separate Phase 2 concern).
- Per-tenant Meta secrets — current architecture uses one shared Meta
  app for every Embedded Signup merchant. If that ever changes, the
  per-tenant override path is already wired (`webhook_enforcement.meta`)
  but the verifier still uses the platform `META_APP_SECRET`.
- Stripe — no inbound Stripe webhook is implemented today. Dead
  references in `webhooks.py` and `billing.py` docstrings have been
  removed in this phase.
- DB migration for a dedicated `webhook_signature_audit` table. Audit
  storage is Redis-only by design (see "Audit storage decision" below).

---

## 8. Audit storage decision

Phase 1B uses Redis hashes + bounded LISTs rather than a new
PostgreSQL table:

- High-volume providers (Meta inbound) would balloon a relational
  audit table to tens of thousands of rows per merchant per day.
- Salla / Moyasar / HyperPay already persist their events to
  `webhook_events` for the durable dispatcher queue, so per-event
  storage is already covered for those providers.
- Operator dashboards need aggregates per (provider, tenant, day, status).
  Redis hashes (`webhook:audit:counters:<provider>:<day>` with
  `<tenant>:<status>` fields) answer that query in O(1) per
  provider-day.
- A bounded LIST per provider (`webhook:audit:recent_failures:<provider>`,
  capped at 200 entries with 7-day TTL) gives operators forensic detail
  for the most recent failures without unbounded growth.

If audit retention beyond 30 days becomes a requirement (e.g. for SOC2
evidence collection), promoting the counter hashes to a Postgres table
is a single dispatcher-style worker that nightly drains the previous
day's counters. Not in scope for Phase 1B.
