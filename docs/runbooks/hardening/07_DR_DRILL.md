# Disaster Recovery Drill

> **Goal**: prove, on a recurring schedule, that we can take a backup
> and bring up a fully working replacement Postgres in ≤ 30 min, with
> ≤ 24 h of data loss, without help from anyone outside the operator.
>
> **Status**: planning only. **No execution** without operator GO.
> Recommended cadence: **quarterly**, plus once before declaring the
> hardening track "done".

---

## 1. Drill scenarios (pick one per quarter)

| # | Scenario | What we prove |
|---|---|---|
| **D1** | "Latest daily backup, restore on a fresh DB, point app at it (in staging)." | Backup chain + restore tooling + app-to-DB rewiring. |
| **D2** | "Loss of `nahla-postgres-prod` — assume it's unrecoverable. Stand up a new Postgres in `desirable-growth` from yesterday's dump, switch `nahla-saas` to it." | Full RTO. Hardest variant. |
| **D3** | "Restore *only one tenant's data* into a side database for forensic / customer-support reasons." | Selective restore tooling. |
| **D4** | "Restore from a 30-day-old monthly archive — verify it still decrypts and loads." | Long-term archive integrity. |

Run **D1 first** (lowest stakes). Run **D2** after backup automation
is one quarter old.

## 2. D1 — Detailed plan (the staging variant — _not_ executed)

### 2.1 Preconditions

- [ ] Backup automation (`06_BACKUP_AUTOMATION.md`) running for ≥ 7 days.
- [ ] At least one daily dump exists in the off-Railway bucket.
- [ ] An empty Postgres service exists in a non-production environment
      (e.g., a `staging` environment in `desirable-growth`, or a
      throwaway project).
- [ ] Operator has the GPG passphrase to hand.
- [ ] Drill window scheduled on a low-traffic day; **production is not
      touched**.

### 2.2 Steps

1. **Pick the dump**:

   ```pwsh
   rclone ls r2:nahla-backups/daily/ | Sort-Object | Select-Object -Last 1
   $latest = "<that filename>"
   rclone copy r2:nahla-backups/daily/$latest .
   ```

2. **Decrypt**:

   ```pwsh
   gpg --decrypt $latest > drill_restore.dump
   ```

3. **Provision a target**:

   - Railway dashboard → `desirable-growth` → "+" → Postgres → name
     `nahla-postgres-drill`
   - Pull its `DATABASE_PUBLIC_URL` (temporarily enable a TCP proxy
     just for this drill).

4. **Restore**:

   ```pwsh
   $env:RESTORE_DSN = "<drill DSN>"
   $env:RESTORE_IN = "drill_restore.dump"
   python scripts/run_pg_restore.py
   ```

5. **Verify counts**:

   ```pwsh
   $env:VERIFY_DSN = "<drill DSN>"
   python scripts/verify_restore.py
   ```

   Expect counts close to production (within 24 h of writes).

6. **Point a `nahla-saas-staging` deployment at it** (if we have one):

   ```pwsh
   railway link --project desirable-growth --environment staging --service nahla-saas
   railway variables --service nahla-saas --set 'DATABASE_URL=${{nahla-postgres-drill.DATABASE_URL}}'
   railway redeploy --service nahla-saas --yes
   ```

7. **Smoke-test** via the staging Cloudflare hostname:

   ```pwsh
   curl.exe -sS https://api-staging.nahlah.ai/alive
   curl.exe -sS https://api-staging.nahlah.ai/auth/ping
   ```

8. **Record metrics**:

   | Metric | Measured |
   |---|---|
   | RTO (start of drill → app green on staging) | ___ min |
   | RPO (gap between dump time and "now") | ___ h |
   | Steps that needed manual intervention | ___ |
   | Issues found | ___ |

9. **Tear down**:

   ```pwsh
   railway variables --service nahla-saas --set 'DATABASE_URL=${{nahla-postgres-prod-staging.DATABASE_URL}}'
   railway redeploy --service nahla-saas --yes
   # Delete nahla-postgres-drill in dashboard
   Remove-Item drill_restore.dump, $latest
   ```

### 2.3 Pass criteria

- RTO ≤ 30 min.
- All endpoint smoke tests pass.
- No need to call anyone else.
- Drill report (filled-in §8 above) committed to
  `docs/runbooks/dr-drills/YYYY-MM-DD.md`.

If RTO > 30 min: identify the bottleneck step, ticket it, re-drill in
≤ 30 days.

## 3. D2 — Production-loss drill (the harder variant — _later_)

This drill assumes `nahla-postgres-prod` is gone. The cutover script
`scripts/run_pg_restore.py` plus the `02_NEW_PASSWORD_ROTATION.md`
runbook are sufficient to do this for real, but should be **practised**
quarterly without actually destroying anything.

The safe practise pattern: do D1, then ALSO flip the *production*
`nahla-saas`'s `DATABASE_URL` to point at the drill DB for **2
minutes** with traffic, then flip back. (Requires sign-off and a
4-AM window.)

Document for D2 will be written after D1 has succeeded twice.

## 4. Post-drill template

Each drill produces a 1-page report at
`docs/runbooks/dr-drills/YYYY-MM-DD.md`:

```markdown
# DR Drill — YYYY-MM-DD

- Scenario: D1 / D2 / D3 / D4
- Operator: <name>
- Start: HH:MM UTC
- End: HH:MM UTC
- RTO measured: <min>
- RPO at start of drill: <h>
- Source dump: <path>
- Issues found: <list>
- Tickets opened: <links>
- Pass / Fail
```

## 5. Out-of-scope

- Compliance certifications (SOC 2, etc.). Future.
- Cross-region failover. Not on roadmap.
