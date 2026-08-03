# Post-Cutover Hardening Checklist

> **Control notice (2026-08-03):** Database identity, binding, and password
> operations are now governed by
> `docs/runbooks/CANONICAL_PRODUCTION_DATABASE_BINDING.md` and
> `scripts/operators/verify_canonical_production_database.py`. Those controls
> override any historical command below that captures a resolved URL or
> credential.
>
> **Status**: Migration cutover from `efficient-insight → Postgres`
> (switchyard.proxy.rlwy.net:14159) to `desirable-growth → nahla-postgres-prod`
> (kodama.proxy.rlwy.net:35880, internal `postgres-ancu.railway.internal`)
> completed and verified on **2026-05-20 ~21:00 UTC**.
> This document is the operational checklist for the next 7 days.

> **Operating principle**: every change in this runbook is **reversible**.
> Anything that destroys data or removes rollback capability is gated
> behind explicit human approval.

---

## 0. Current State (as of 2026-05-20 22:20 UTC)

| Component | State | Notes |
|---|---|---|
| `nahla-saas` (`desirable-growth`/production) | **LIVE** | Reading + writing to NEW DB only |
| `nahla-postgres-prod` (NEW) | **PRIMARY** | alembic 0065, 81 tables, 5–6 app sessions |
| `efficient-insight → Postgres` (OLD) | **FROZEN** | 0 app sessions, last write 20:28:23 UTC |
| `efficient-insight → Postgres` TCP Public Proxy | **STILL ACTIVE** | Intentional — required for rollback path |
| `DATABASE_URL` on `nahla-saas` | **Railway reference** | `${{nahla-postgres-prod.DATABASE_URL}}` (internal DNS) |
| Postgres password (OLD) | **NOT ROTATED** | Same value as during migration |
| Postgres password (NEW) | **NOT ROTATED** | Same value as Railway-provisioned |
| Original cutover dump | **PRESERVED** | `nahla-backups/nahla_cutover_20260520_234227.dump` |
| Pre-migration `0065` dump | **PRESERVED** | `nahla-backups/nahla_production_0065_backup.dump` |

---

## 1. Stabilisation Window (T+0 → T+24h)

**Goal**: confirm zero data loss and zero unintended writes to OLD over a
full diurnal cycle (peak + low traffic).

### 1.1 Active monitors

- `scripts/overnight_monitor.py` — running in background, 30-min cycles, 12h.
  Writes `_overnight.log`, `_overnight_progress.json`, and `_alerts.log`
  (only on anomalies).
- Manual log peek: `Get-Content _overnight.log -Tail 5`
- Real-time alert file: `_alerts.log` — empty file means clean.

### 1.2 What to check on Railway dashboard

| Surface | What to look for |
|---|---|
| `nahla-saas` logs | No `psycopg2.OperationalError`, no `connection reset`, no `Pool exhausted` |
| `nahla-saas` deploy status | Status `SUCCESS`, no auto-restarts |
| `Redis` service | `redis_up=True` in app health logs, no eviction spikes |
| `nahla-postgres-prod` logs | Healthy `accept connection`, no `out of connections` |
| `Postgres` (OLD) logs | **Should be quiet** — only system/wal traffic, no app inserts |
| Cloudflare/api.nahlah.ai | 5xx rate ~0%, p95 latency stable |

### 1.3 Functional smoke checklist (run every 4–8 hours during stabilisation)

- [ ] WhatsApp inbound messages → `message_events` table grows on NEW only
- [ ] WhatsApp outbound (replies / template sends) → `whatsapp_usage` increments on NEW
- [ ] Campaign sends → `campaign_send_logs.sent_at` advances on NEW
- [ ] Salla sync → `store_sync_jobs` and `sync_logs` advance on NEW
- [ ] Template sync (every 5 min) → `whatsapp_templates.updated_at` advances
- [ ] Auth: `/auth/login` 2xx, `/auth/2fa/status` 2xx
- [ ] Background jobs: `EmittersScheduler`, `Guardian`, `Salla Orders Poller`
      log periodic ticks
- [ ] DB connections: ≤ NEW pool max (default 20–30 depending on
      `database/session.py`); no `pool_timeout` errors
- [ ] Slow queries: any query > 1s in logs is flagged for review

### 1.4 Drift gate

> **The single most important rule for the next 7 days**:
> any inserts/updates on OLD DB after 2026-05-20 20:38:48 UTC = ALERT.

Re-run on demand:

```pwsh
$env:NEW_DSN = (python -c "import json; print(json.load(open('_newpg.json','r',encoding='utf-8-sig'))['DATABASE_PUBLIC_URL'])")
$env:OLD_DSN = (python -c "import json; print(json.load(open('_srcpg.json','r',encoding='utf-8-sig'))['DATABASE_PUBLIC_URL'])")
python scripts/last_write_proof.py
```

OLD `max(created_at)` should never advance past **2026-05-20 20:28:23 UTC**.
If it does, **STOP** and investigate before any further hardening.

---

## 2. Secrets Rotation (T+24h → T+72h)

> **Order matters**: rotate OLD first because it can no longer break
> production. Rotate NEW only after a clean drift-gate review.

### 2.1 OLD DB (`efficient-insight → Postgres`) password rotation

**Risk**: very low. Nothing depends on it operationally.

1. Open Railway dashboard → `efficient-insight` → `Postgres` → Variables.
2. Regenerate `POSTGRES_PASSWORD` (Railway has a "rotate" affordance, or set
   a new strong random value).
3. Wait for the service to redeploy.
4. **Do not** update any external service to use the new password — nothing
   should be using OLD anymore.
5. **Verify** there are no failed-auth bursts in `Postgres` logs (would
   indicate a stale consumer we missed):

   ```pwsh
   railway link --project efficient-insight --environment production --service Postgres
   railway logs --service Postgres | Select-String "authentication failed" | Select-Object -Last 20
   ```

   Empty output = all clean.

### 2.2 NEW DB (`nahla-postgres-prod`) password rotation

**Risk**: medium. Brief downtime if the `${{...}}` reference resolution
lags. Schedule for low-traffic window.

1. Verify `nahla-saas` `DATABASE_URL` is **still** the Railway reference,
   not a literal value:

   ```pwsh
   railway link --project desirable-growth --environment production --service nahla-saas
   railway variables --service nahla-saas --kv | Select-String "^DATABASE_URL"
   ```

   Expected: `DATABASE_URL=${{nahla-postgres-prod.DATABASE_URL}}` (literal
   reference). If it shows a resolved value, **STOP** and re-issue the
   reference before rotating; otherwise rotation will leave `nahla-saas`
   stranded on the old password.

2. In Railway dashboard → `nahla-postgres-prod` → Variables, regenerate
   `POSTGRES_PASSWORD`. Railway will:
   - Restart the Postgres service with the new password.
   - Re-render `${{nahla-postgres-prod.DATABASE_URL}}` consumers.
   - Trigger a redeploy of `nahla-saas` automatically.

3. While the redeploy is in flight, expect a 30–90s blip. Webhook providers
   (Meta, 360dialog) will retry — no data loss.

4. Verify post-rotation:

   ```pwsh
   curl.exe -sS https://api.nahlah.ai/alive
   curl.exe -sS https://api.nahlah.ai/auth/ping
   $env:NEW_DSN = (railway variables --service nahla-postgres-prod --kv | Select-String "^DATABASE_PUBLIC_URL=").ToString().Split("=",2)[1]
   python scripts/check_alembic.py   # alembic_version=0065, public_tables=81
   ```

### 2.3 Local `.dump` files

The dump files (`nahla-backups/*.dump`) embed user/password metadata in
their internal TOC, but **not** the live password — restoring them creates
local copies, it doesn't grant access to remote DBs. Still:

- Treat them as **classified evidence** until DR retention expires.
- Do **not** commit them (already excluded by `.gitignore`).
- Do **not** upload to shared drives without encryption.

### 2.4 Local working files with embedded DSNs

These are intentionally untracked (covered by `.gitignore` patterns
`_*.json`, `_*.txt`, `_*.log`, `_*.err`):

- `_newpg.json`, `_srcpg.json`, `_newpg_check.json`, `_oldpg_check.json`
- `_saas_vars_before.json`, `_saas_vars_after.json`, `_saas_vars_now.json`

After password rotation in §2.1 / §2.2, these become outdated. Either:

- Delete them, or
- Regenerate via `railway variables --json` against the relevant service.

---

## 3. TCP Public Proxy Shutdown — OLD DB (T+72h+)

**Pre-conditions** (all must be true):

- [ ] §1 stabilisation passed: 7 days of clean drift-gate.
- [ ] §2.1 OLD password rotation completed (or scheduled
      simultaneously — rotating + closing the proxy in the same
      maintenance window is acceptable).
- [ ] No external scripts on engineer laptops still reference
      `switchyard.proxy.rlwy.net:14159` for read access.
- [ ] No CI / scheduled job points to OLD.

**Steps**:

1. In Railway dashboard → `efficient-insight` → `Postgres` → Settings →
   Networking → **TCP Proxy** → toggle off.
2. Internal address remains accessible (`postgres.railway.internal:5432`)
   for any same-project tooling.
3. Verify from a non-Railway machine that `switchyard.proxy.rlwy.net:14159`
   is unreachable: `Test-NetConnection switchyard.proxy.rlwy.net -Port 14159`
   should fail.

**Rollback** (if needed for emergency read access): re-enable the toggle,
proxy comes back in <1 min.

---

## 4. OLD DB → Read-Only Standby / Cold Archive (T+14d+)

**Decision tree**:

| Path | Use when | Cost | Recovery time |
|---|---|---|---|
| **Read-only standby** | You expect to query historical OLD data on demand for forensics, audits, or partial restores. | Same as keeping it live — Railway charges for the running Postgres service. | Instant (just `SELECT`). |
| **Cold archive (recommended)** | You only need OLD as a disaster-recovery insurance policy and don't need ad-hoc queries. | ~zero (only S3/B2 storage cost for the dump file). | Hours (provision new Postgres + `pg_restore`). |

### 4.1 Read-only standby path

1. Add to `Postgres` variables on `efficient-insight`:
   - `default_transaction_read_only=on` (Postgres GUC) — refuses any
     `INSERT/UPDATE/DELETE` even from privileged sessions.
2. Optionally revoke write privileges:

   ```sql
   REVOKE INSERT, UPDATE, DELETE, TRUNCATE
     ON ALL TABLES IN SCHEMA public FROM PUBLIC;
   ```

3. Document the read-only DSN somewhere ops-only (e.g.,
   `docs/security/PHASE_1A_RUNBOOK.md`).
4. The TCP proxy stays **off** (per §3); use `railway connect` for ad-hoc
   shell access.

### 4.2 Cold archive path

1. Take a **final** dump (full, custom format) from OLD into a new file
   `nahla_OLD_archive_<YYYYMMDD>.dump`.
2. Verify with `scripts/verify_pg_backup.py`.
3. Copy to durable off-machine storage:
   - Encrypted ZIP onto OneDrive / iCloud, or
   - S3 / B2 / R2 bucket with object lock + lifecycle to Glacier.
4. Once verified, the OLD service can be **paused** in Railway (keeps the
   service definition + volume but stops the container — no compute cost
   in plans that support it). Do **not** delete.
5. Keep the archive for the retention window in §5.

---

## 5. Backup Retention Policy

> Decide once, document once, automate later.

| Tier | Source | Retention | Storage | Encryption |
|---|---|---|---|---|
| **Hot** | NEW DB live volume | always-on | Railway-managed | platform default |
| **Daily** | `pg_dump` (cron) of NEW DB | 14 days, rolling | Local Railway volume | platform default |
| **Weekly** | `pg_dump` (cron) of NEW DB | 8 weeks | Off-Railway (S3/B2/R2) | KMS or age-encrypted |
| **Monthly** | `pg_dump` (cron) of NEW DB | 12 months | Off-Railway, immutable | KMS, object lock |
| **Pre-cutover archive** | `nahla_cutover_20260520_234227.dump` + `nahla_production_0065_backup.dump` | 12 months minimum | Off-Railway, immutable | mandatory |

**Implementation note**: today there is no scheduled backup job. The
`scripts/run_pg_backup.py` we wrote during the migration is one-shot. As
part of hardening, schedule a daily run via either:

- A Railway "cron job" service (a small Python container that calls
  `pg_dump` and uploads to S3), or
- GitHub Actions on a schedule with secrets-based DSN, or
- An external scheduler (e.g., Cloudflare Cron Triggers).

Tracked under §6 as a follow-up task.

---

## 6. Disaster Recovery Plan

### 6.1 Recovery Time Objective (RTO) targets

| Scenario | RTO | RPO |
|---|---|---|
| App container crash / single deploy failure | < 5 min (auto-restart by Railway) | 0 (DB unaffected) |
| NEW DB corruption / accidental drop | < 60 min | < 24 h (last daily dump) |
| Region/Railway-wide outage | < 4 h | < 24 h |
| Catastrophic data loss + backup loss | best-effort | up to weekly |

### 6.2 Restore drill (run quarterly)

1. Spin up a throwaway Postgres service on Railway (template
   `postgres-ssl:18`).
2. Restore the latest off-Railway weekly archive into it via
   `scripts/run_pg_restore.py` with `--clean --if-exists --no-owner --no-acl`.
3. Run `scripts/full_drift_map.py` against this DB and your dump's
   expected counts (recorded in the dump's metadata or alongside it).
4. Tear down. Document the wall-clock time.

If the drill exceeds the RTO target, raise an action item: dump size has
grown, restore tool needs to be parallelised, or the storage tier needs
to be promoted.

### 6.3 Rollback to OLD (only valid until §3 / §4 close)

While OLD is preserved (alive in `efficient-insight`), rollback is **one
command** away:

```pwsh
# 1. Set DATABASE_URL back to the OLD DSN (read it from _saas_vars_before.json)
$old = (python -c "import json; print(json.load(open('_saas_vars_before.json','r',encoding='utf-8-sig'))['DATABASE_URL'])")
railway link --project desirable-growth --environment production --service nahla-saas
railway variables --service nahla-saas --set "DATABASE_URL=$old" --skip-deploys

# 2. Trigger a redeploy
railway redeploy --service nahla-saas --yes
```

After §3 (TCP proxy off), this command requires re-enabling the proxy
first — still possible but slower. After §4.2 (cold archive), rollback
becomes **restore-from-dump** and is significantly slower.

---

## 7. Monitoring & Alerts (post-stabilisation)

### 7.1 What to alert on

| Signal | Threshold | Severity |
|---|---|---|
| `nahla-postgres-prod` connection count | > 80% of `max_connections` | warning |
| `nahla-postgres-prod` disk usage | > 80% of volume size | warning |
| `nahla-postgres-prod` disk usage | > 90% of volume size | critical |
| Replication lag (if standby added later) | > 5 s | warning |
| `nahla-saas` `/alive` 5xx rate | > 1% over 5 min | critical |
| `nahla-saas` p99 latency | > 5 s over 5 min | warning |
| `pg_stat_database.deadlocks` rate | any non-zero rate sustained 5 min | warning |
| WAL files behind | > 10 segments | warning |
| Sentry / log error rate | > 10× baseline | critical |
| Any insert on OLD DB after 2026-05-20 20:38:48 UTC | 1 row | **critical** |

### 7.2 Implementation paths (pick one)

- **Easiest**: Railway built-in metrics + email alerts on resource thresholds.
- **Better**: ship logs/metrics to Datadog/Grafana Cloud and define dashboards.
- **Self-hosted**: a tiny `pg_exporter` + Prometheus + Grafana stack inside
  the same Railway project (works, but adds operational load).

### 7.3 What NOT to monitor with public probes

The public DSN (`kodama.proxy.rlwy.net:35880`) should be considered
**operator-only**. Production traffic uses the internal DNS and never the
proxy. External monitoring should hit `https://api.nahlah.ai/healthz` and
let the app probe its own DB internally — this validates routing and
networking together.

---

## 8. Connection Limits & pgBouncer (future)

### 8.1 Current shape

- `nahla-saas` runs **1 replica** with SQLAlchemy pool (likely
  `pool_size=5..10`, `max_overflow=10..20`). See `database/session.py`.
- Postgres `max_connections` default on Railway template is **100**.
- Headroom is large today (5–6 active sessions).

### 8.2 When pgBouncer becomes worth it

| Trigger | Action |
|---|---|
| `nahla-saas` scales to ≥ 3 replicas | introduce **transaction** pooling |
| Connection count regularly > 30 | introduce **transaction** pooling |
| Background workers move into separate services | introduce pgBouncer or Postgres' own **Built-in CONNECTION_POOLING** |

### 8.3 Recommended deployment (when triggered)

1. Add pgBouncer as a Railway service (image
   `edoburu/pgbouncer` or `bitnami/pgbouncer`).
2. Configure `POOL_MODE=transaction`, `MAX_CLIENT_CONN=200`,
   `DEFAULT_POOL_SIZE=20`.
3. Update `nahla-saas` `DATABASE_URL` to point to pgBouncer instead of
   Postgres directly. Keep it as a Railway reference:
   `${{pgbouncer.PGBOUNCER_URL}}`.
4. Verify all SQLAlchemy features used by the app are compatible with
   transaction pooling (no session-scoped temp tables, no `SET LOCAL`
   that must persist, no advisory locks that span statements without
   explicit connection retention).

---

## 9. Internal-Network-Only Architecture (target end state)

After §3 and §4 close, the production runtime should look like:

```
                     ┌──────────────────────────────────────────────────┐
                     │ desirable-growth (Railway project, production)   │
                     │                                                  │
   Cloudflare ─────► │  nahla-saas ───► nahla-postgres-prod (internal) │
   api.nahlah.ai     │       │              ▲                          │
                     │       └──► Redis ────┘                          │
                     │                                                  │
                     │                                                  │
                     │  (no service references switchyard)              │
                     └──────────────────────────────────────────────────┘

                     ┌──────────────────────────────────────────────────┐
                     │ efficient-insight (kept as cold archive)         │
                     │                                                  │
                     │  Postgres (OLD) — paused / read-only / archived  │
                     │  TCP proxy: OFF                                  │
                     └──────────────────────────────────────────────────┘
```

### 9.1 Acceptance criteria

- [ ] No service in any project references `switchyard.proxy.rlwy.net:14159`.
- [ ] No service in any project references `kodama.proxy.rlwy.net:35880`
      from runtime code (only operators on demand).
- [ ] `DATABASE_URL` in **every** production service is a Railway
      reference, never a literal DSN.
- [ ] Local `.env*` files developers carry use a dedicated dev/staging DB,
      never the production DSN.
- [ ] `desirable-growth → Postgres` (the empty/shawahid one) is either
      removed or clearly documented as belonging to a different project's
      data — not Nahla.

---

## 10. Files NOT to delete

These remain in the workspace until at least **2026-08-20** (90 days
post-cutover) and ideally on encrypted off-machine storage:

| File | Why |
|---|---|
| `_final_snapshot.json` | Authoritative post-cutover state |
| `_monitor_history.json` | First-30-min monitoring evidence |
| `_routing_proof.json` | Proves writes routed to NEW |
| `_saas_vars_before.json` | Rollback DSN reference |
| `_overnight_progress.json` (live) | Stabilisation evidence |
| `_alerts.log` (live) | Anomaly trail |
| `nahla-backups/nahla_cutover_20260520_234227.dump` | Cutover ground truth |
| `nahla-backups/nahla_production_0065_backup.dump` | Pre-cutover safety net |
| `_cutover_drift.txt` | drift=0 verification at restore time |
| `scripts/full_drift_map.py` | Reproducible drift check |
| `scripts/last_write_proof.py` | Reproducible OLD-write detection |
| `scripts/run_pg_backup.py`, `run_pg_restore.py` | Tooling for DR drills |

---

## 11. Outstanding Action Items

> Tracked here, not in Linear yet.

- [ ] **T+24h**: review `_overnight.log` for clean run; confirm zero rows
      added to OLD; decide on §2 timing.
- [ ] **T+24h–72h**: rotate OLD password (§2.1).
- [ ] **T+72h–7d**: rotate NEW password (§2.2).
- [ ] **T+7d**: schedule daily `pg_dump` cron job (§5).
- [ ] **T+7d**: turn off TCP proxy on OLD (§3).
- [ ] **T+14d**: decide read-only standby vs cold archive (§4) and execute.
- [ ] **T+30d**: first DR restore drill (§6.2).
- [ ] **T+30d**: implement `last_write_proof` as a recurring alert
      (§7.1 last row).
- [ ] **whenever ≥3 replicas**: introduce pgBouncer (§8).
- [ ] **anytime**: audit every Railway service in every project for any
      reference to `switchyard.proxy.rlwy.net` (§9.1 first bullet).
- [x] **DONE 2026-05-21 ~01:53 KSA**: untrack `dashboard/node_modules`
      (commit `42d054ff`) — fixes Railway build sandbox
      `no space left on device`. Permanently captured as anti-pattern
      A1 in [`hardening/ANTI_PATTERNS.md`](./hardening/ANTI_PATTERNS.md#a1-never-commit-dashboardnode_modules).

---

## 12. Open Questions for the Operator

- Off-Railway backup destination preference: S3, B2, R2, OneDrive,
  iCloud? Determines §5 implementation.
- Monitoring tool of choice: Railway-native, Datadog, Grafana Cloud, or
  self-hosted? Determines §7 implementation.
- Long-term policy on `desirable-growth → Postgres` (the unrelated
  shawahid-data instance still in the same project): keep, move to its
  own project, or delete? Affects §9.1.
- Retention window for cold archive: 12 months minimum is the placeholder
  in §5; legal/compliance may require longer.

---

*Last updated: 2026-05-21 ~01:25 KSA, immediately after cutover and
overnight monitor start. Update this file as actions complete; treat it
as the runbook of record for the migration's tail.*
