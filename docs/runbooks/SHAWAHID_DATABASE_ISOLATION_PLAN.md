# Shawahid Database Isolation Plan

> **Purpose**: document where Shawahid data physically lives, prove that
> Nahla and Shawahid runtimes are already code-isolated, and propose a
> safe plan to clean up the legacy Shawahid tables that ended up inside
> the Nahla-adjacent `desirable-growth → Postgres` instance.
>
> **Status**: planning only. **No execution** until explicit approval
> from the operator.

---

## 1. Where Shawahid lives — current physical layout

### 1.1 Production Shawahid runtime (the live one)

| Property | Value |
|---|---|
| Railway project | **`shawahid-service`** (own dedicated project) |
| Backend service | `shawahid-service` (FastAPI, port 8010) |
| Database service | `postgres` (in the same project) |
| Backend `DATABASE_URL` | `postgresql://…@postgres.railway.internal:5432/shawahid_db` |
| Public proxy | n/a — internal only |
| Resolution: `postgres.railway.internal` (inside `shawahid-service` project) | the project's own `postgres` service |

This is the **canonical production location**. Live Shawahid users
(teachers paying via WhatsApp) write here. Nahla never touches it.

### 1.2 Legacy / orphan Shawahid tables

| Property | Value |
|---|---|
| Railway project | **`desirable-growth`** (the SAME project that hosts `nahla-saas` and `nahla-postgres-prod`) |
| Database service | `Postgres` (the **original** service in that project, not `nahla-postgres-prod`) |
| Internal DNS (within `desirable-growth`) | `postgres.railway.internal:5432/railway` |
| Public proxy | `caboose.proxy.rlwy.net:48001` |
| Tables present | `teachers`, `evidences`, `payment_attempts`, `teacher_subscriptions`, `portfolio_exports` |
| Approx rows | 747 across all 5 tables (snapshot taken during cutover diff) |
| Volume on disk | ~305 MB allocated, ~196 MB used |
| Used by | **No production service in `desirable-growth`** since the cutover. Pre-cutover briefly considered as the migration target before we discovered the collision. |

### 1.3 Local development / repo state

| Path | Status |
|---|---|
| `shawahid-service/` (in this repo, alongside `backend/`, `dashboard/`, etc.) | **Untracked** in git — present as a working copy from a parallel codebase that the original developer kept side-by-side. |
| `shawahid-service/.env.example` | Template only — **no real secrets**. |
| `shawahid-service/Dockerfile`, `railway.toml` | Independent build config; never invoked from Nahla's pipeline. |
| Imports from Nahla (`backend/`) into `shawahid-service/` | **None**. |
| Imports from `shawahid-service/` into Nahla (`backend/`, `database/`, `dashboard/`) | **None** (verified by grep — see §3). |

---

## 2. Why two locations?

The most plausible history (from artefact dates and code comments):

1. Shawahid was originally drafted **inside the Nahla project**
   (`desirable-growth`) sharing the same Postgres instance for speed of
   experimentation.
2. The original developer realised Shawahid is a separate product and
   moved it to **its own Railway project** (`shawahid-service`) — this
   matches the explicit instruction in
   `shawahid-service/README.md`:
   > **لا تلمس هذه الخدمة كود نحلة الحالي (backend, database, dashboard) بأي شكل.**
   > **أنشئ مشروعًا جديدًا مستقلًا على Railway (لا تضيف لمشروع نحلة).**
3. The five Shawahid tables in `desirable-growth → Postgres` were never
   cleaned up — they're stranded leftovers from step 1.
4. During the cutover (2026-05-20) we *almost* used that stranded
   instance as the migration target, then aborted as soon as the
   collision was detected and provisioned `nahla-postgres-prod` as the
   true clean target. **No Nahla data was ever written into the legacy
   instance during the cutover.**

---

## 3. Code-level isolation audit

`grep -i "shawahid|شواهد"` across the Nahla codebase
(`backend/`, `database/`, `dashboard/`, `services/`, `integrations/`) —
results limited to:

| File | Line | Finding |
|---|---|---|
| `database/models.py` | 46–49 | **Comment only**: notes that `ai_blocked_numbers` may be populated at runtime via env config to include "Nahla / Shawahid / staff" numbers. No imports, no DB joins. |
| `backend/services/billing_formatter.py` | 6–9 | **Comment only**: explicit statement: *"This module deliberately does NOT import or reference anything from shawahid-service so the two codebases remain fully isolated."* |
| `scripts/trace_shawahid.py` | (whole file) | Diagnostic script we wrote during the cutover. Not imported by the app. |
| `scripts/verify_pg_backup.py`, `scripts/preflight_empty_check.py` | several | Defensive checks that **forbid** Shawahid tables (`teachers`, `evidences`, `payment_attempts`) from appearing in a Nahla restore target. |
| `docs/runbooks/POST_CUTOVER_HARDENING.md` | various | Documentation references. |

**Conclusion**: Nahla's running code has **zero functional dependency**
on Shawahid. The two products are **already code-isolated**. The only
remaining cleanup is operational (the orphan tables in step 1.2 above).

---

## 4. Shared secrets / shared URLs audit

| Surface | Result |
|---|---|
| `nahla-saas` Railway env vars (production) | Inspected via `railway variables --service nahla-saas --json` on 2026-05-20. **No** Shawahid DSN, **no** Shawahid OAuth credentials, **no** Shawahid OpenAI key, **no** Shawahid admin password. |
| `shawahid-service` Railway env vars | Inspected via `railway variables --service shawahid-service --json`. **No** Nahla DSN, **no** Nahla JWT secret, **no** Nahla webhook tokens. |
| Shared Postgres user / password | Different password material; different `postgres` users in different projects. |
| Shared Cloudflare zones | `api.nahlah.ai` (Nahla) and Shawahid's domain are managed independently. |
| Shared OpenAI / Anthropic keys | Each project has its own API key in its own variables; rotating one does not affect the other. |
| Shared Sentry project / DSN | Each has its own DSN. |
| Shared Redis | Nahla uses `desirable-growth → Redis`; Shawahid does not use Redis. |

**Conclusion**: **No shared secrets.** Rotating Nahla's credentials does
not break Shawahid, and vice-versa.

---

## 5. Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Cleanup script accidentally drops Shawahid production data (in `shawahid-service/postgres`) | very low | catastrophic for Shawahid teachers | gate all cleanup commands by **DSN allow-list** that only matches `desirable-growth → Postgres`; refuse to run if the DB has more than the 5 known orphan tables. |
| Cleanup leaves dangling FK to a Nahla table (none expected) | very low | low | dry-run via `pg_dump --schema-only` first; abort on any unexpected reference. |
| A still-running consumer points to the orphan DB (e.g., legacy cron, abandoned worker) | low (we already audited) | medium | enable `log_connections=on` for 24 h on `desirable-growth → Postgres` before any cleanup; confirm zero non-admin connections. |
| Re-introducing Shawahid code into Nahla via copy-paste | low | medium | the comment in `billing_formatter.py` is the canonical guard — keep it. Add a similar comment in `database/models.py`. |
| Local `shawahid-service/` directory accidentally pushed to Nahla origin | low (currently untracked) | low | already covered by `.gitignore` patterns and team discipline. Optionally move it out of the Nahla working tree to a sibling directory. |

---

## 6. Decision: keep or remove the orphan DB?

The orphan tables sit on `desirable-growth → Postgres` (the **original**
Postgres service in the project, _not_ the new `nahla-postgres-prod`).

| Path | Pros | Cons | Recommendation |
|---|---|---|---|
| **Keep as-is** | zero risk; cheap; preserves a forensic copy | confusing for future operators; pays Railway storage; ambiguous answers to "which DB is real" | not recommended past 30 days |
| **Move the data into the canonical Shawahid project** then drop tables here | single source of truth; no data loss | requires Shawahid-side schema check + downtime; Shawahid owner must approve | **only if Shawahid owner asks for it** |
| **Take a final dump → cold archive → delete the service** | cleanest end state | irrevocable (mitigated by the dump); requires Shawahid owner sign-off | **recommended** after 30 days of no objections |

The third path is the goal of this plan.

---

## 7. Step-by-step isolation plan (proposal — _not_ executed)

**Pre-conditions**:

- [ ] Nahla cutover has been clean for at least **14 days**
      (no inserts on `efficient-insight → Postgres`, no rollback events).
- [ ] Shawahid product owner has confirmed the data in
      `desirable-growth → Postgres` is **legacy** and not the system of
      record.
- [ ] An explicit operator (you) authorises each step in writing.

**Step 7.1 — Take a forensic dump of the orphan DB**

```pwsh
railway link --project desirable-growth --environment production --service Postgres
railway variables --service Postgres --json > _orphan_pg.json
$env:BACKUP_DSN = (python -c "import json; print(json.load(open('_orphan_pg.json','r',encoding='utf-8-sig'))['DATABASE_PUBLIC_URL'])")
$env:BACKUP_OUT = "C:\Users\STARS\Downloads\nahla-backups\desirable_growth_orphan_$(Get-Date -Format yyyyMMdd).dump"
python scripts/run_pg_backup.py
Remove-Item _orphan_pg.json
```

This gives us an irrevocable safety net.

**Step 7.2 — Verify the dump is exactly the orphan Shawahid data**

```pwsh
python scripts/verify_pg_backup.py    # uses VERIFY_DUMP env var pointing at the new dump
python scripts/diff_extra_tables.py   # must report ONLY: teachers, evidences,
                                       # payment_attempts, teacher_subscriptions,
                                       # portfolio_exports
```

If anything else appears, **STOP** and investigate.

**Step 7.3 — Confirm zero live connections**

Enable Postgres `log_connections=on` and `log_disconnections=on` for
24 h via Railway env vars, then:

```pwsh
$env:AUDIT_DSN = "<the orphan DSN>"
python scripts/security_audit_db.py
```

Expected: only operator/probe sessions; no app sessions, no replication
slots, no cron jobs.

**Step 7.4 — Move the dump off-Railway**

Encrypt and upload the dump to durable cold storage (S3 / B2 / R2 with
object lock, or encrypted ZIP onto the operator's backup OneDrive).
Record the SHA-256.

**Step 7.5 — Decide: shutdown vs. drop**

| Sub-path | Action | Reversibility |
|---|---|---|
| **A. Pause the service** | Railway dashboard → `desirable-growth → Postgres` → "stop"; volume retained. | Trivial — re-start to recover. |
| **B. Drop the service** | Railway dashboard → service → delete. Volume can be deleted last. | Recoverable only via the dump in 7.4. |

Sub-path A is cheaper in operator stress; Sub-path B is cheaper in
running cost. Both are safe given 7.1.

**Step 7.6 — Tear down the legacy TCP proxy**

If `desirable-growth → Postgres` had a TCP proxy (`caboose.proxy.rlwy.net:48001`),
toggle it off in the Railway dashboard once 7.5 is complete.

**Step 7.7 — Repo hygiene**

- Move `shawahid-service/` out of the Nahla working tree. Recommended
  layout:

  ```
  C:\Users\STARS\Downloads\
  ├── nahla-saas\         (this repo)
  └── shawahid-service\   (separate working tree, separate git remote)
  ```

  This eliminates the chance of accidentally including Shawahid files
  in a Nahla `railway up` build context.

- Add a one-line comment to `database/models.py` near the
  `ai_blocked_numbers` field, mirroring the style of
  `billing_formatter.py`, to make the isolation explicit at the
  schema level.

---

## 8. Rollback plan

> Each step has an exact undo. None of them are destructive once 7.1
> succeeds.

| Step | Failure mode | Undo |
|---|---|---|
| 7.1 (dump) | `pg_dump` fails | Re-run; orphan DB is read-only-ish so retry is safe. |
| 7.2 (verify) | Reports non-Shawahid tables | **Halt.** Do not proceed. Re-snapshot and consult product owner. |
| 7.3 (connection audit) | Finds an app session | **Halt.** Identify the consumer (script, cron, abandoned worker). Do not pause / drop until the consumer is fixed. |
| 7.4 (offsite copy) | Upload fails | Retry; meanwhile keep the local dump file intact. |
| 7.5A (pause) | Service won't pause | Investigate Railway dashboard; harmless. |
| 7.5B (drop) | Service deletion fails | Re-attempt via Railway CLI / dashboard. |
| 7.6 (TCP proxy off) | Reveals an external consumer | Re-enable the proxy in <1 min. |
| 7.7 (repo move) | Some path-based tooling breaks | Move directory back; tooling needs to be path-agnostic anyway. |

If at **any** point post-7.5B someone says "we need that orphan data
back": restore the dump from 7.1 / 7.4 onto a fresh Postgres service
(any project) using `scripts/run_pg_restore.py`. RTO ≤ 30 min.

---

## 9. Out-of-scope (intentionally _not_ in this plan)

- Anything inside the **production Shawahid service**
  (`shawahid-service` Railway project). That is its own product with
  its own owner and its own runbook; we do not touch its DB, secrets,
  TCP proxies, or migrations.
- `efficient-insight → Postgres` (the OLD Nahla DB). Covered by
  `POST_CUTOVER_HARDENING.md`, not here.
- The Nahla-side `desirable-growth → nahla-postgres-prod` (the NEW
  production Nahla DB). Covered by `POST_CUTOVER_HARDENING.md`.
- Any code change inside Shawahid's repo (`shawahid-service/app/`).
  Outside our scope.

---

## 10. Open questions for the operator

1. Has the Shawahid product owner explicitly confirmed that the data in
   `desirable-growth → Postgres` is **legacy** and not used by any
   live Shawahid feature?
2. Cold-archive destination for the forensic dump (matches the §5 row
   in `POST_CUTOVER_HARDENING.md` — same answer should apply).
3. Pause-vs-drop preference for `desirable-growth → Postgres` after
   §7.4 succeeds?
4. Acceptable target date for the cleanup window (recommended: **T+30
   days from cutover**, i.e., on or after 2026-06-19).

---

*Last updated: 2026-05-21 ~01:35 KSA, immediately after the cutover.
This plan is a proposal; nothing in it has been executed.*
