# Backup Automation (off-Railway)

> **Goal**: a daily, encrypted, off-platform backup of
> `nahla-postgres-prod` with predictable RPO and a tested restore drill.
>
> **Status**: planning only. **No execution** without operator GO.

---

## 1. Current state (gap analysis)

| Property | Status |
|---|---|
| Manual `pg_dump` capability | ✅ proven via `scripts/run_pg_backup.py` (used during cutover) |
| Manual `pg_restore` capability | ✅ proven via `scripts/run_pg_restore.py` |
| Latest forensic dump from OLD | ✅ `nahla-backups/nahla_production_0065_backup.dump` (~196 MB) |
| Latest dump from NEW | ❌ **none yet** since cutover |
| Automated daily dumps | ❌ none |
| Off-Railway storage | ❌ all dumps live on the operator laptop only |
| Encryption at rest | ❌ |
| Restore drill | ❌ never executed against NEW |
| Recovery Point Objective | 🟡 currently undefined; effective RPO = manual cadence |
| Recovery Time Objective | 🟡 verified ~10 min during the cutover; not formalised |

## 2. Targets

| Metric | Target |
|---|---|
| RPO | **24 h** (one daily backup) |
| RTO | **30 min** (from "DB is gone" to "app on a fresh DB") |
| Retention | 7 daily, 4 weekly, 12 monthly |
| Encryption | AES-256, key not stored alongside backups |
| Off-Railway location | object storage outside `Nahlah AI` Railway account |

## 3. Three implementable patterns

### Pattern A — GitHub Actions runner (recommended)

```
schedule (cron 03:00 UTC daily)
   │
   ▼
GitHub Actions runner
   │  pulls Postgres 18 client (apt)
   │  reads RAILWAY_TOKEN from secrets
   │  uses RAILWAY_TOKEN + project-id + service-id to fetch DATABASE_URL
   │  pg_dump --format=custom --no-owner --no-acl
   │  gpg --symmetric --cipher-algo AES256
   │  aws s3 cp dump.gpg s3://nahla-backups/$(date +%F).dump.gpg
   ▼
S3 / Cloudflare R2 / Backblaze B2 (object lock + lifecycle policy)
```

- Secrets: `RAILWAY_TOKEN`, `S3_KEY`, `S3_SECRET`, `GPG_PASSPHRASE`,
  `RAILWAY_PROJECT_ID`, `RAILWAY_SERVICE_ID_PG`.
- Cost: ~$0/month (GitHub Actions free tier covers a 5-min daily job).
- Object storage cost: ~$0.10/month for 200 MB × 30 daily copies.
- Lifecycle: auto-delete daily dumps older than 7 days; weekly dumps
  older than 30 days; monthly dumps older than 365 days.

### Pattern B — Railway cron service inside the project

A separate Railway service running a Python container that does the
dump + upload nightly.

- Pros: same trust boundary as the DB; no GitHub secrets needed for the
  DSN itself (uses internal DNS).
- Cons: operationally weird — the backup process lives inside the
  thing it's backing up. If the project gets nuked, so does the
  backup process.

### Pattern C — External monitoring service (third-party)

E.g. SimpleBackups, snaplet, Aiven Klaw if applicable.

- Pros: zero code to maintain.
- Cons: vendor lock-in, third-party trust boundary, monthly cost.

**Recommendation**: **Pattern A** with **Cloudflare R2** as the object
store (zero egress cost, S3-compatible API).

## 4. Encryption strategy

- Symmetric AES-256 via `gpg --symmetric --cipher-algo AES256`.
- Passphrase stored in **two** places:
  1. Operator's password manager (1Password / Bitwarden).
  2. A printed copy in a sealed envelope in a fireproof safe.
- The passphrase is **never** stored in:
  - GitHub secrets (the Action gets a passphrase only at runtime via
    OIDC-issued short-lived secret, OR the passphrase is split: one
    half in GitHub, one half typed by operator monthly into a key-vault
    refresh job).
  - The Railway env.
  - The repo.

For initial simplicity, **store the passphrase in GitHub secrets** but
plan to migrate to a split-key arrangement within 90 days.

## 5. Schedule

| Cadence | Action |
|---|---|
| Daily 03:00 UTC | Full `pg_dump` (custom format) → `daily/YYYY-MM-DD.dump.gpg` |
| Weekly Sunday 03:30 UTC | Same dump, copied to `weekly/YYYY-WW.dump.gpg` |
| Monthly 1st of month 04:00 UTC | Same dump, copied to `monthly/YYYY-MM.dump.gpg` |
| Quarterly | DR drill (restore one of the dumps to a throwaway Postgres → run schema diff) |

## 6. Initial proof-of-life run

Before fully automating, do **one** manual end-to-end run:

```pwsh
# 1. Take a dump from NEW (with current PgClient 18.4)
$env:BACKUP_DSN = "<nahla-postgres-prod public DSN>"
$env:BACKUP_OUT = "C:\Users\STARS\Downloads\nahla-backups\nahla_postcut_$(Get-Date -Format yyyyMMdd).dump"
python scripts/run_pg_backup.py

# 2. Encrypt
gpg --symmetric --cipher-algo AES256 --output "$env:BACKUP_OUT.gpg" $env:BACKUP_OUT

# 3. Upload to R2 (or whichever bucket)
rclone copy "$env:BACKUP_OUT.gpg" r2:nahla-backups/manual/

# 4. Verify decryption + structure on a different machine OR a Railway
#    throwaway service.
gpg --decrypt nahla_postcut_YYYYMMDD.dump.gpg > restored.dump
python scripts/verify_pg_backup.py    # set VERIFY_DUMP=restored.dump
```

This proves the chain works before we wire it to a cron.

## 7. Monitoring

- GitHub Action emails on failure (default).
- Optional: post a Sentry breadcrumb / Datadog event on success / failure.
- Operator dashboard (manual): once a week, list bucket contents and
  confirm the daily dumps are present.

## 8. Out-of-scope

- WAL-archiving / PITR. Out of reach on Railway hobby/starter without
  managed plug-ins. Revisit when we move to a managed Postgres
  (Aiven / Neon / RDS).
- Cross-cloud replication. Not needed at current scale.
