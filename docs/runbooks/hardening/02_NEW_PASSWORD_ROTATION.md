# NEW DB Password Rotation Plan

> **Target**: `desirable-growth → nahla-postgres-prod`
> (host `kodama.proxy.rlwy.net:35880`, internal
> `postgres-ancu.railway.internal:5432/railway`).
>
> **Status**: planning only. **No execution** without explicit operator GO.

---

## 1. Why this is more delicate than OLD

NEW **is** the live production DB. A bad rotation = `nahla-saas` cannot
authenticate = users see 502 / 503 until fixed.

The single safest rotation pattern depends on `nahla-saas` referencing
`${{nahla-postgres-prod.DATABASE_URL}}` (a Railway variable reference,
not a literal DSN). When that's true, Railway re-renders the reference
on every read of `DATABASE_URL`, so rotating `POSTGRES_PASSWORD` on
`nahla-postgres-prod` also updates `nahla-saas`'s `DATABASE_URL` on the
next deployment.

## 2. Pre-conditions

- [ ] OLD password rotation (`01_OLD_PASSWORD_ROTATION.md`) completed
      and clean for 24 h.
- [ ] **Hard verify** the reference is still a literal `${{...}}` in
      `nahla-saas`:

      ```pwsh
      railway link --project desirable-growth --environment production --service nahla-saas
      railway variables --service nahla-saas --kv | Select-String "^DATABASE_URL"
      ```

      Expected output (literal, not a resolved URL):

      ```
      DATABASE_URL=${{nahla-postgres-prod.DATABASE_URL}}
      ```

      **If it shows a resolved DSN, STOP**. Re-issue the reference first:

      ```pwsh
      railway variables --service nahla-saas \
          --set 'DATABASE_URL=${{nahla-postgres-prod.DATABASE_URL}}' \
          --skip-deploys
      railway redeploy --service nahla-saas --yes
      ```

      Then re-verify.

- [ ] Low-traffic window scheduled (recommended: 03:00–04:00 KSA / 00:00
      UTC). Expect 30–90 s of partial unavailability while
      `nahla-saas` redeploys.
- [ ] Operator on standby with rollback DSN handy.

## 3. Steps

1. **Capture the current Railway-resolved DSN** (for emergency rollback
   if Railway's reference resolution lags):

   ```pwsh
   railway variables --service nahla-postgres-prod --json > _new_pg_before_rotation.json
   ```

2. **Capture `nahla-saas` env snapshot** (pristine, includes the
   reference):

   ```pwsh
   railway variables --service nahla-saas --json > _saas_vars_before_pwd_rot.json
   ```

3. **Rotate** in Railway dashboard:
   - Project: `desirable-growth` → `nahla-postgres-prod` → Variables
   - Edit `POSTGRES_PASSWORD` → "Generate new" or paste a 32-char random
   - Save

4. Railway:
   - Restarts the Postgres service (~30 s of DB unavailability)
   - Detects the variable change → triggers a redeploy of every service
     that references `${{nahla-postgres-prod.DATABASE_URL}}` — that's
     `nahla-saas` (~30–60 s of HTTP 502 while the new container starts)

5. **Verify after redeploy**:

   ```pwsh
   curl.exe -sS https://api.nahlah.ai/alive       # expect 200
   curl.exe -sS https://api.nahlah.ai/auth/ping   # expect 200

   railway variables --service nahla-postgres-prod --json > _new_pg_after_rotation.json
   python -c "import json; a=json.load(open('_new_pg_before_rotation.json',encoding='utf-8-sig'))['POSTGRES_PASSWORD']; b=json.load(open('_new_pg_after_rotation.json',encoding='utf-8-sig'))['POSTGRES_PASSWORD']; print('rotated' if a!=b else 'NOT ROTATED')"
   ```

6. **Validate the app actually reconnected**:

   ```pwsh
   railway link --project desirable-growth --environment production --service nahla-saas
   railway logs --service nahla-saas | Select-String -Pattern "Connected|psycopg2.OperationalError" -Context 0,2 | Select-Object -Last 10
   ```

   Expect: app log lines indicating successful DB reconnection. Any
   `psycopg2.OperationalError: password authentication failed` =
   reference didn't re-resolve → see Rollback.

7. **Run drift sanity check** to prove writes still flow:

   ```pwsh
   $env:NEW_DSN = "<from _new_pg_after_rotation.json>"
   $env:OLD_DSN = "<from _saas_vars_before.json (pre-cutover)>"
   python scripts/last_write_proof.py
   ```

   Expect: NEW `max(created_at)` advancing in real time; OLD frozen.

8. **Cleanup**:

   After 1 h of clean operation:

   ```pwsh
   Remove-Item _new_pg_before_rotation.json,
              _new_pg_after_rotation.json,
              _saas_vars_before_pwd_rot.json
   ```

## 4. Rollback

Two scenarios:

### 4.1 Railway successfully rotated but `nahla-saas` reference didn't update

(Symptoms: `nahla-saas` logs show `password authentication failed`.)

```pwsh
# Force the reference to re-resolve by re-issuing it:
railway variables --service nahla-saas \
    --set 'DATABASE_URL=${{nahla-postgres-prod.DATABASE_URL}}'
# (no --skip-deploys this time — let Railway redeploy)
```

### 4.2 The new password itself broke something

(Very unlikely if it's a random alphanumeric. More likely if a manual
typo with special characters Railway's URL encoder mishandles.)

Restore the previous password value:

```pwsh
$old_pw = (python -c "import json; print(json.load(open('_new_pg_before_rotation.json',encoding='utf-8-sig'))['POSTGRES_PASSWORD'])")
# In Railway dashboard, paste $old_pw back into POSTGRES_PASSWORD on nahla-postgres-prod
```

Railway redeploys both services; service restored in ~60 s.

## 5. Failure-mode catalogue

| Symptom | Likely cause | Fix |
|---|---|---|
| `nahla-saas` redeploy never starts after rotation | Railway didn't detect `DATABASE_URL` reference change | Manually trigger `railway redeploy --service nahla-saas --yes` |
| `nahla-saas` redeploys but logs show old password | `DATABASE_URL` was previously stored as a literal value | Re-issue the reference (see §2 hard verify) and redeploy |
| Postgres service won't accept new password | Rare Railway template glitch — usually a transient | Wait 60 s and retry; or paste the same password to force a re-template |
| `nahla-saas` boots but writes fail | Stale connection pool holding old credentials | Force a redeploy: `railway redeploy --service nahla-saas --yes` |

## 6. Out-of-scope

- Disabling the public TCP proxy (`03_TCP_PROXY_SHUTDOWN.md`).
- Adding pgBouncer (`05_PGBOUNCER_READINESS.md`).
