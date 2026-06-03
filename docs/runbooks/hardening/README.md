# Hardening Runbooks (Phase 2 — post-cutover)

Authoritative index of the hardening track. Every doc in this folder is
a **plan**, not a script. Nothing here has been executed.

| # | Doc | Purpose | Status |
|---|---|---|---|
| 01 | [`01_OLD_PASSWORD_ROTATION.md`](./01_OLD_PASSWORD_ROTATION.md) | Rotate the OLD `efficient-insight → Postgres` password to invalidate any leaked DSN copies | drafted |
| 02 | [`02_NEW_PASSWORD_ROTATION.md`](./02_NEW_PASSWORD_ROTATION.md) | Rotate `nahla-postgres-prod` password (live DB; reference-driven) | drafted |
| 03 | [`03_TCP_PROXY_SHUTDOWN.md`](./03_TCP_PROXY_SHUTDOWN.md) | Take all 3 public TCP proxies offline, in order | drafted |
| 04 | [`04_INTERNAL_NETWORK_ONLY_ARCHITECTURE.md`](./04_INTERNAL_NETWORK_ONLY_ARCHITECTURE.md) | Target end-state diagram + acceptance criteria | drafted |
| 05 | [`05_PGBOUNCER_READINESS.md`](./05_PGBOUNCER_READINESS.md) | Decide YES / DEFER for pgBouncer; deploy plan if YES | drafted |
| 06 | [`06_BACKUP_AUTOMATION.md`](./06_BACKUP_AUTOMATION.md) | Daily encrypted off-Railway backups | drafted |
| 07 | [`07_DR_DRILL.md`](./07_DR_DRILL.md) | Quarterly DR drill plan | drafted |
| 08 | [`08_MERCHANT_PROVISIONING_FILLED_GAP.md`](./08_MERCHANT_PROVISIONING_FILLED_GAP.md) | **`filled_gap` global email collision** — defer until Salla Embedded stable; prevents `users_email_key` on new stores | **deferred** |
| — | [`ANTI_PATTERNS.md`](./ANTI_PATTERNS.md) | Concrete things we've stepped on (build/deploy) — read before any change to git tracking, Dockerfile, or env vars | living doc |

## Companion docs (existing, not in this folder)

- [`../POST_CUTOVER_HARDENING.md`](../POST_CUTOVER_HARDENING.md) — top-level
  hardening checklist (what was created during cutover).
- [`../SHAWAHID_DATABASE_ISOLATION_PLAN.md`](../SHAWAHID_DATABASE_ISOLATION_PLAN.md) —
  isolation + cleanup plan for the legacy Shawahid orphan tables.

## Recommended timeline

```
T+0          cutover (DONE)
T+0 → T+24h  overnight monitor (RUNNING)
T+24h        verification report (D, this conversation)
T+24h → 72h  doc 01 — rotate OLD password
T+72h → 7d   doc 02 — rotate NEW password
T+7d         doc 06 — backup automation live; first manual drill
T+7d → 14d   doc 03 §4 — close OLD TCP proxy
T+14d        doc 07 — DR drill #1
T+30d        SHAWAHID_DATABASE_ISOLATION_PLAN — execute §7
T+30d        doc 03 §5, §6 — close legacy + NEW TCP proxies
T+45d        POST_CUTOVER_HARDENING.md §4 — pause OLD service
```

No step in this folder is executed without explicit operator GO. The
operator writes the GO message in chat, this assistant runs the
runbook, posts the resulting report into
`docs/runbooks/exec-logs/YYYY-MM-DD-<step>.md`.
