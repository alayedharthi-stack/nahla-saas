# OLD DB Password Rotation Plan

> **Target**: `efficient-insight → Postgres`
> (host `switchyard.proxy.rlwy.net:14159`, internal
> `postgres.railway.internal:5432/railway`).
>
> **Status**: planning only. **No execution** without explicit operator GO.

---

## 1. Why rotate this first

OLD is no longer in the runtime path. Rotating its password:

- **Cannot break production** (no service uses these credentials).
- **Invalidates all the leaked DSN copies** that were temporarily handed
  around during the migration (operator laptop, scratch JSON files,
  PowerShell history).
- Provides a clean audit boundary: any future "successful auth as
  postgres on `switchyard.proxy.rlwy.net:14159`" after rotation = a
  consumer we missed.

## 2. Pre-conditions

- [ ] Cutover stable for at least 24 h (overnight monitor clean).
- [ ] `last_write_proof.py` confirms OLD `max(created_at)` is still
      `2026-05-20 20:28:23 UTC` (no post-cutover writes).
- [ ] No CI / scheduled job points to OLD (audit by grepping repo + any
      bookmarked DSNs you keep).
- [ ] Operator is online and able to monitor for 30 min after the change.

## 3. Steps

1. **Capture current credential** for rollback:

   ```pwsh
   railway link --project efficient-insight --environment production --service Postgres
   railway variables --service Postgres --json > _old_pg_before_rotation.json
   ```

   Keep this file local + offline. After rotation succeeds and 24 h
   pass, delete it.

2. **Rotate** in Railway dashboard:
   - Project: `efficient-insight` → `Postgres` → Variables
   - Edit `POSTGRES_PASSWORD`
   - Click "Generate new" (Railway provides a strong random) **or** paste
     a 32-char random of your own
   - Save

3. Railway will **redeploy the Postgres service** automatically. Expect
   ~30 s of unavailability on the public proxy (which nothing depends
   on).

4. **Verify**:

   ```pwsh
   railway variables --service Postgres --json > _old_pg_after_rotation.json
   # confirm new password value differs:
   python -c "import json; a=json.load(open('_old_pg_before_rotation.json',encoding='utf-8-sig'))['POSTGRES_PASSWORD']; b=json.load(open('_old_pg_after_rotation.json',encoding='utf-8-sig'))['POSTGRES_PASSWORD']; print('rotated' if a!=b else 'NOT ROTATED — RETRY')"
   ```

5. **Watch logs for stale consumers** for 24 h:

   ```pwsh
   railway logs --service Postgres | Select-String -Pattern "authentication failed|FATAL"
   ```

   - Empty → clean. Done.
   - Any hits → identify the source IP / app_name in the log line and
     fix the consumer.

6. **Cleanup**:

   ```pwsh
   Remove-Item _old_pg_before_rotation.json, _old_pg_after_rotation.json
   ```

## 4. Rollback

Within Railway dashboard, paste the previous password back into
`POSTGRES_PASSWORD` (held in `_old_pg_before_rotation.json`) and save.
Railway redeploys; old credentials are restored in ~30 s.

After 24 h post-rotation, this rollback is no longer practical (the
prior password file should be deleted). At that point a forgotten
consumer would need its own DSN updated to the **new** password — by
operator action.

## 5. Out-of-scope

- Closing the TCP public proxy (separate runbook
  `03_TCP_PROXY_SHUTDOWN.md`).
- Touching the NEW DB password (separate runbook
  `02_NEW_PASSWORD_ROTATION.md`).
- Deleting the OLD service (covered by §4 of `POST_CUTOVER_HARDENING.md`).
