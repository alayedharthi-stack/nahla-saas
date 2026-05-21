# pgBouncer Readiness Assessment

> **Status**: planning only. **Decision pending** — this doc is the
> material the operator needs to choose YES / DEFER / NO.

---

## 1. Why even consider pgBouncer

`nahla-saas` runs on Railway as a single FastAPI worker pool.
SQLAlchemy creates a connection pool inside the worker process.
A second worker / replica would have its own pool. As the system grows
(more workers, scheduled jobs, WebSocket fanout, batch importers),
total open connections to Postgres grow with `workers × pool_size`.

Postgres on Railway's hobby tier defaults to a low `max_connections`
(typically 100). A few horizontally scaled workers can saturate it.

pgBouncer in **transaction-pool mode** turns N application connections
into a much smaller fixed number of backend connections, which:

- Lets us scale the app horizontally without raising
  `max_connections` on Postgres.
- Smooths spikes from periodic batch jobs.
- Provides a clean place to enforce per-app credentials.

## 2. Current connection-budget snapshot (taken 2026-05-21)

From the post-cutover audit:

| Source | Count |
|---|---:|
| `nahla-saas` workers on `nahla-postgres-prod` | ~12 idle, ~3 active during writes |
| Operator probes (audit / monitor scripts) | 1–2 |
| Railway control-plane | 1 |
| **Total typical** | **~15** |
| **Peak observed** | **~25** during the cutover window |
| Postgres `max_connections` on `nahla-postgres-prod` | TBD (default likely 100) |

**Verdict at current scale**: we are **not yet** under connection
pressure. pgBouncer would solve a problem we don't have today.

## 3. Triggers that would push us to deploy pgBouncer

- Sustained `pg_stat_activity` count > 60 (more than 60 % of default
  budget).
- Adding a second `nahla-saas` instance (horizontal replica).
- Enabling a heavy WebSocket fanout that requires its own DB connection
  per session.
- Adding a long-running ETL / analytics worker.

If **any** of those happen, deploy pgBouncer **before** the workload
goes live.

## 4. Two deployment options

### Option A — Container alongside the app (simpler)

Run pgBouncer as a sidecar service in `desirable-growth`:

```
nahla-saas ─► pgbouncer (port 6432, internal-only) ─► nahla-postgres-prod (5432)
```

- Pros: full control, pgBouncer config in repo, easy to version.
- Cons: one more service to monitor; sidecar adds 30–50 ms median
  query latency on cold queries.

Reference Dockerfile / Railway service:
- Image: `edoburu/pgbouncer:latest` (well-maintained official-ish)
- Env: `DATABASE_URL` = the resolved
  `${{nahla-postgres-prod.DATABASE_URL}}`
- Mode: `pool_mode=transaction`, `default_pool_size=20`,
  `max_client_conn=200`, `reserve_pool_size=5`,
  `server_reset_query=DISCARD ALL`
- App-side change: `nahla-saas`'s `DATABASE_URL` becomes
  `${{pgbouncer.PGBOUNCER_URL}}`. Migrations bypass pgBouncer (use
  `${{nahla-postgres-prod.DATABASE_URL}}` directly via Alembic) to
  avoid the prepared-statement issue.

### Option B — Use Railway's built-in PgBouncer add-on (if/when offered)

Railway has experimented with managed pgBouncer plugins. Status varies
by date.

- Pros: zero ops; managed by Railway.
- Cons: less control; couples us to Railway's pricing/availability;
  may not be available on our plan.

**Recommendation when needed**: start with **Option A**, migrate to
Option B only if it becomes a stable, supported plugin.

## 5. App-side compatibility matrix

| Behaviour | Compatible with pgBouncer transaction-pool? |
|---|---|
| Plain SQLAlchemy `select` / `insert` / `update` | ✅ |
| Per-request transactions (FastAPI's typical pattern) | ✅ |
| `LISTEN` / `NOTIFY` | ❌ — not supported in transaction-pool. Move to Redis Pub/Sub. |
| Server-side prepared statements (`statement_cache_size` > 0) | ⚠️ — must disable in SQLAlchemy or use `prepare_threshold=0` for `psycopg`. |
| Long-running transactions / advisory locks | ⚠️ — switch to session-pool for the connection that holds them. |
| Alembic migrations | ⚠️ — bypass pgBouncer; use direct DSN. |

Action items if/when we adopt pgBouncer:

- [ ] Set `engine = create_engine(..., pool_pre_ping=True, isolation_level="READ COMMITTED")` and configure `prepare_threshold=0` (psycopg2 `executemany_mode='values_plus_batch'` is OK).
- [ ] Confirm we don't use `LISTEN` / `NOTIFY` anywhere in the codebase (grep `pg_listen` / `LISTEN`).
- [ ] Configure Alembic to read a separate `DATABASE_URL_DIRECT` env var that bypasses pgBouncer.
- [ ] Add an integration test that issues 200 concurrent inserts to make sure the bouncer config holds.

## 6. Decision matrix

| Symptom | Action |
|---|---|
| Steady-state connections < 30 % of `max_connections` | **Defer.** Do not deploy pgBouncer. |
| Adding a second `nahla-saas` replica or a heavy job | **Deploy.** Use Option A. |
| Connection storms during product launches | **Deploy.** Use Option A. |
| Random `FATAL: too many connections` errors | **Deploy NOW.** Use Option A. |

## 7. Rollback

pgBouncer is a pure indirection. To roll back: change `nahla-saas`'s
`DATABASE_URL` from `${{pgbouncer.PGBOUNCER_URL}}` back to
`${{nahla-postgres-prod.DATABASE_URL}}`, redeploy. No data is touched.
RTO ≤ 60 s.

## 8. Out-of-scope

- Read replicas (`08_REPLICATION_PLAN.md` if we add one).
- Multi-tenant connection isolation per-tenant. Currently
  `nahla-saas` enforces tenant isolation at the application layer; no
  connection-level isolation is planned.
