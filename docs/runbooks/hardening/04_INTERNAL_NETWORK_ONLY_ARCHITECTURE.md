# Internal-Network-Only Architecture (target end state)

> Where we want production to be after all the hardening runbooks have
> been executed. This is a **target picture**, not a runbook. The
> individual transitions live in their own docs.

---

## 1. Target topology (post all hardening)

```
                           ┌──────────────────────────────────────────────────────────┐
                           │ desirable-growth (Railway project, environment=production) │
                           │                                                          │
                           │   ┌──────────────────────┐    ┌──────────────────────┐  │
                           │   │ creative-intuition    │    │ nahla-saas            │  │
   Cloudflare ─── HTTPS ──►│   │ (Vite preview)        │    │ FastAPI               │  │
   app.nahlah.ai           │   │ port 8000             │    │ port 8000             │  │
                           │   │                       │    │                       │  │
                           │   │  internal DNS:        │    │  ${{nahla-postgres-prod.DATABASE_URL}}
                           │   │  creative-intuition   │    │  ${{Redis.REDIS_URL}} │  │
                           │   │  .railway.internal    │    │                       │  │
                           │   └───────────────────────┘    └─────────┬─────────────┘  │
                           │                                          │                │
   Cloudflare ─── HTTPS ──►├──────────────────────────────────────────┤                │
   api.nahlah.ai           │                                          ▼                │
                           │                       ┌──────────────────────────────┐   │
                           │                       │ nahla-postgres-prod          │   │
                           │                       │ Postgres 18                  │   │
                           │                       │ internal-only DNS:           │   │
                           │                       │ postgres-ancu                │   │
                           │                       │   .railway.internal:5432     │   │
                           │                       │ TCP public proxy: OFF        │   │
                           │                       └──────────────────────────────┘   │
                           │                                          ▲                │
                           │                                          │                │
                           │                       ┌──────────────────────────────┐   │
                           │                       │ Redis (8.x)                  │   │
                           │                       │ internal-only DNS            │   │
                           │                       │ TCP public proxy: OFF        │   │
                           │                       └──────────────────────────────┘   │
                           │                                                          │
                           │   ┌────────────────────────┐                             │
                           │   │ Postgres (legacy)       │  ◄── DELETED or PAUSED      │
                           │   │ (was Shawahid orphan)   │     after T+30d            │
                           │   └────────────────────────┘                             │
                           └──────────────────────────────────────────────────────────┘

                           ┌──────────────────────────────────────────────────────────┐
                           │ efficient-insight (Railway project)                       │
                           │                                                          │
                           │   ┌────────────────────────┐                             │
                           │   │ Postgres (OLD Nahla)    │  ◄── PAUSED (cold archive)  │
                           │   │ TCP public proxy: OFF   │                             │
                           │   └────────────────────────┘                             │
                           └──────────────────────────────────────────────────────────┘

                           ┌──────────────────────────────────────────────────────────┐
                           │ shawahid-service (Railway project — separate product)    │
                           │ Out of Nahla scope. Owned by Shawahid product owner.     │
                           └──────────────────────────────────────────────────────────┘
```

## 2. Acceptance criteria

A change is "done" only when ALL of these are true.

- [ ] No production-facing public TCP proxy exists for any Postgres
      service in any Nahla project.
- [ ] No production-facing public TCP proxy exists for Redis.
- [ ] All `DATABASE_URL` and `REDIS_URL` values across `nahla-saas`
      production env are **Railway variable references** (the
      `${{...}}` syntax), never literal DSNs.
- [ ] `nahla-saas` runtime resolves DB / Redis exclusively via
      `*.railway.internal` hostnames (verified in production logs).
- [ ] `creative-intuition` (frontend) reaches the API only through the
      Cloudflare public surface (`api.nahlah.ai`), never through
      Railway-internal hostnames (those wouldn't work for the browser
      anyway, but worth asserting).
- [ ] Every developer / operator who needs ad-hoc DB access uses
      `railway connect`, not a hard-coded public DSN.
- [ ] Local `.env` / scratch files no longer contain any production DSN
      with password (covered by `.gitignore`'s `_*` patterns).

## 3. What this buys us

| Before (current) | After |
|---|---|
| 3 public DB endpoints reachable from the open internet (OLD, NEW, legacy) | 0 public DB endpoints |
| Password leak = direct production breach risk | Password leak = limited to the brief window before rotation, AND requires Railway CLI auth to do anything operational |
| Each engineer's `pg_dump` keeps a public DSN in their PowerShell history | All engineer access goes through Railway IAM |
| External monitoring needs a public DSN | External monitoring uses HTTP health endpoints only |

## 4. What this gives up

- Slightly slower ad-hoc psql sessions (`railway connect` adds a few
  hundred ms vs direct TCP). Acceptable.
- BI / dashboard tools that need a JDBC URL can't talk to DB directly
  any more. Mitigation: spin up a dedicated read-only replica in the
  same project and only that one carries the public proxy, scoped to a
  read-only role. (Future, not now.)

## 5. Order of operations to reach this state

| # | Runbook | Window |
|---|---|---|
| 1 | Cutover (DONE) | T+0 |
| 2 | Stabilisation monitor (DONE / running) | T+0 → T+24h |
| 3 | OLD password rotation (`01_OLD_PASSWORD_ROTATION.md`) | T+24h → T+72h |
| 4 | NEW password rotation (`02_NEW_PASSWORD_ROTATION.md`) | T+72h → T+7d |
| 5 | Backup automation off-Railway (`06_BACKUP_AUTOMATION.md`) | T+7d |
| 6 | TCP proxy shutdown — OLD (`03_TCP_PROXY_SHUTDOWN.md` §4) | T+7d |
| 7 | DR drill #1 (`07_DR_DRILL.md`) | T+14d |
| 8 | Shawahid orphan cleanup (`SHAWAHID_DATABASE_ISOLATION_PLAN.md`) | T+30d |
| 9 | TCP proxy shutdown — Shawahid orphan (§5 of 03) | T+30d |
| 10 | TCP proxy shutdown — NEW (`03_TCP_PROXY_SHUTDOWN.md` §6) | T+30d |
| 11 | OLD service paused / cold-archived (`POST_CUTOVER_HARDENING.md` §4) | T+45d |

## 6. Out-of-scope of this document

- Replication / standby topology. Tracked in `08_REPLICATION_PLAN.md`
  if/when we add a read replica.
- Multi-region. Not on the roadmap.
