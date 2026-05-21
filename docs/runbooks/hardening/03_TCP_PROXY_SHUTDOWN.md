# TCP Public Proxy Shutdown Checklist

> **Targets** (in order of safety):
>
> 1. `efficient-insight → Postgres` public proxy
>    (`switchyard.proxy.rlwy.net:14159`) — **OLD Nahla DB**.
> 2. `desirable-growth → nahla-postgres-prod` public proxy
>    (`kodama.proxy.rlwy.net:35880`) — **NEW Nahla DB**.
> 3. `desirable-growth → Postgres` public proxy
>    (`caboose.proxy.rlwy.net:48001`) — **legacy Shawahid orphan**.
>
> **Status**: planning only. **No execution** without explicit operator GO.
> The TCP proxy is the only off-Railway way to reach a DB; once off, the
> only path is `railway connect` from an authenticated CLI.

---

## 1. Why we delay this

Public proxies are slow but they are also the only zero-cost emergency
escape hatch. We close them only after we're confident:

- Internal networking is healthy (proven daily by the app working).
- Backup automation is in place (so restore drills don't need the proxy).
- Operators have practiced `railway connect` for ad-hoc DB shells.

## 2. Pre-conditions for any proxy shutdown

- [ ] At least 7 days since cutover.
- [ ] Both password rotations complete (`01_OLD_PASSWORD_ROTATION.md`,
      `02_NEW_PASSWORD_ROTATION.md`).
- [ ] Daily backup automation running (or scheduled within 7 days).
- [ ] Operators verified `railway connect --service <postgres-svc>`
      works for ad-hoc psql shell.
- [ ] No external monitoring / dashboard / BI tool is configured against
      the public proxy.

## 3. Pre-shutdown verification (run for each target before disabling)

```pwsh
$proxyHost = "<host from the table above>"
$proxyPort = "<port>"

# Confirm reachable now (sanity check)
Test-NetConnection $proxyHost -Port $proxyPort

# List CONNECTED clients via Postgres pg_stat_activity
$env:AUDIT_DSN = "postgresql://postgres:<pwd>@${proxyHost}:${proxyPort}/railway"
python scripts/security_audit_db.py
Remove-Item Env:AUDIT_DSN
```

Expected: only operator-recognised IPs (your own + Railway internal
ranges). If anything else, **stop** and identify the consumer.

## 4. Target 1 — OLD (`efficient-insight → Postgres`) — recommended T+7d

**Why first**: nothing should depend on this any more.

1. Railway dashboard → `efficient-insight` → `Postgres` → Settings →
   Networking → "TCP Proxy" toggle → **OFF**.
2. Confirm closed:

   ```pwsh
   Test-NetConnection switchyard.proxy.rlwy.net -Port 14159
   # Expect: TcpTestSucceeded : False
   ```

3. Internal access remains via `postgres.railway.internal:5432` from
   any service in `efficient-insight` — but no service there needs it
   now.

**Rollback**: re-toggle ON in dashboard. <60 s.

## 5. Target 2 — Legacy Shawahid orphan — recommended T+30d (after §7 of `SHAWAHID_DATABASE_ISOLATION_PLAN.md`)

Same procedure as Target 1, on `desirable-growth → Postgres` /
`caboose.proxy.rlwy.net:48001`. Done as a step inside the Shawahid
isolation plan (not standalone).

## 6. Target 3 — NEW (`nahla-postgres-prod`) — recommended T+30d

**Why last**: it's the live DB. Closing the proxy means operators must
use `railway connect` for ad-hoc inspection.

1. Confirm operators are comfortable with the alternative:

   ```pwsh
   railway link --project desirable-growth --environment production --service nahla-postgres-prod
   railway connect    # opens a psql against the internal address
   ```

2. Document the new ad-hoc inspection workflow in
   `docs/security/PHASE_1A_RUNBOOK.md` (section: "operator psql access").

3. Toggle the proxy OFF on `nahla-postgres-prod` (same as Target 1).

4. Confirm:

   ```pwsh
   Test-NetConnection kodama.proxy.rlwy.net -Port 35880  # expect failure
   curl.exe -sS https://api.nahlah.ai/alive              # expect 200
   ```

5. Update the local `.gitignore`'d helper files (`_newpg.json` in any
   future use) — the public DSN is now obsolete.

**Rollback**: re-toggle ON in dashboard. App is unaffected (it uses
internal DNS); operators regain proxy access in <60 s.

## 7. Out-of-scope

- Deleting the OLD service (`POST_CUTOVER_HARDENING.md` §4).
- Touching `shawahid-service`'s own proxy (separate product, separate
  runbook in that project).
