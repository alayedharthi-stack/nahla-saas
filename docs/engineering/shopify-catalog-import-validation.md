# Shopify catalog import — validation evidence (Draft PR #1204)

Evidence-only record. **Tested source: `7b4328ae046072b076b20f39c40e8d2887ba42fa`.**
The commit that adds this file changes nothing else; its SHA is not the
tested source. Nothing here was deployed, activated or run against a live
database or a live Shopify store. All flags remain off.

Stack (merge order): #1202 foundation → #1203 merchant connection UI →
#1204 catalog import (base `claude/shopify-merchant-connection-ui-20261010`
at `53bd7a484a3b19ceb08f8d351e21067b87bcb78d`, unchanged).

## Tested scopes

`1cc9613c7b78b6525f0e972f533a937fb7c72691` and `7b4328a` differ in three
dashboard files only (`git diff --name-only 1cc9613..7b4328a -- . ':(exclude)dashboard'`
is empty; no Python test reads those files). Backend, migrations, PostgreSQL
proofs, manifest and runner are byte-identical in both.

| Scope | Tested at | Tree |
|---|---|---|
| Root unit suite | `1cc9613` | clean |
| Strict PostgreSQL proofs, PG16 first run | `1cc9613` | clean |
| Strict PostgreSQL proofs, PG16 re-run and PG18 | `7b4328a` | clean |
| Remaining `lint-and-test` steps, GOV-002, constitution compliance | `7b4328a` | clean |
| Dashboard typecheck, build, platform policy, UI checks, browser harness | `7b4328a` | clean |

Environment: Python 3.12.3 venv with `requirements.txt`; disposable
PostgreSQL 16.15 (Ubuntu package) on 5433 and PostgreSQL 18.6 (Maven Central
`io.zonky.test.postgres:embedded-postgres-binaries-linux-amd64:18.6.0`,
SHA-1 `0a36625fa44dc0d65cb1cee6041e468d96bdbb8d` verified) on 5434, role
`nahla` / `nahla_password` as in CI; Node 22 with `npm ci`; Chromium from
`/opt/pw-browsers`.

## Results

| Check | Command | Result |
|---|---|---|
| Root unit suite (CI "Run unit tests") | `PATH=/tmp/claude-0/venv312/bin:$PATH timeout 5400 python -m pytest -q --maxfail=1` (repository root) | RC 0 — 9529 passed, 790 skipped, 7 xfailed, 0 failed |
| Strict proofs, PG16 first run (trust auth) | [strict command](#strict-proofs-command), port 5433, label `final_pg16` | RC 1 — **NOT PROVEN 754/755** (proofs 752/753, runner regressions 2/2, 0 skipped); see below |
| Strict proofs, PG16 re-run (SCRAM auth) | [strict command](#strict-proofs-command), port 5433, label `final_pg16_scram` | RC 0 — **PROVEN 755/755** (753/753 + 2/2), 28/28 suites, 0 skipped |
| Strict proofs, PG18 (SCRAM auth) | [strict command](#strict-proofs-command), port 5434, label `final_pg18`, PostgreSQL 18.6 | RC 0 — **PROVEN 755/755** (753/753 + 2/2), 28/28 suites, 0 skipped |
| `lint-and-test` remaining 16 steps | each step's `run:` block from `ci.yml`, verbatim | all RC 0 (silent-except lint OK; WhatsApp OAuth boundary 16; sibling identity 94; manual gift 36; orders newest-first 15; Salla browse 41, card 75, compose 85, PR 888 69; coupon L0 156; coupon shadow 10; coupon verify 29; real-channel probe 59; staging migration operators; tenant clone 112 passed 1 skipped (pre-existing); DR parity 7) |
| GOV-002 (scanner from merge-base `53bd7a4`, `--trusted-base-scanner`) | `git show 53bd7a484a3b19ceb08f8d351e21067b87bcb78d:scripts/lint_intelligence_non_interference.py > /tmp/claude-0/nahla_intelligence_guard.py && python /tmp/claude-0/nahla_intelligence_guard.py --repo . --base 53bd7a484a3b19ceb08f8d351e21067b87bcb78d --head 7b4328ae046072b076b20f39c40e8d2887ba42fa --trusted-base-scanner` | RC 0 — `FINDINGS=none`; MODEL / PROMPT / PERSONA / PHRASE_MAP / KEYWORD_ROUTER / CUSTOMER_REGEX changed = NO |
| Constitution compliance | `/tmp/claude-0/venv312/bin/python -m pytest backend/tests/test_constitution_compliance.py backend/tests/test_intelligence_non_interference_guard.py -q` | RC 0 — 98 passed |
| Dashboard typecheck / build (in `dashboard/`) | `npx tsc --noEmit -p .`; `npm run build` | RC 0 / RC 0 (tracked `dist/index.html` restored afterwards with `git checkout -- dashboard/dist/index.html`) |
| Dashboard platform policy (in `dashboard/`) | `npm run check:platform-policy` | RC 0 — 598 OK, 0 FAIL |
| Import UI source/model check (in `dashboard/`) | executed: `npx --yes tsx@4 scripts/check-shopify-catalog-import-ui.mts` (npm-script equivalent: `npm run check:shopify-catalog-import-ui`) | RC 0 — 39 passed |
| Connection UI check, unchanged guard (in `dashboard/`) | executed: `npx --yes tsx@4 scripts/check-shopify-connection-ui.mts` (npm-script equivalent: `npm run check:shopify-connection-ui`) | RC 0 — 295 passed |
| Real-browser harness, local (in `dashboard/`) | executed: `timeout 600 node scripts/verify-shopify-catalog-import-ui.mjs` | RC 0 — 20/20; the old busy gate fails scenario 8 (mutation-checked) |

### Strict proofs command

Executed by a local wrapper (`run_strict.sh <port> <label>`) from the
repository root, verbatim apart from `${PORT}` / `${LABEL}` (5433 /
`final_pg16`, 5433 / `final_pg16_scram`, 5434 / `final_pg18`). The DSNs use
the synthetic credentials of CI's disposable PostgreSQL service
(`nahla` / `nahla_password`) on local throw-away clusters; no live
database or credential is involved.

```bash
set -u
export NAHLA_RELIABILITY_REQUIRE_PG=1
export NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://nahla:nahla_password@127.0.0.1:${PORT}/postgres
export CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1
export LEGACY_MIG_PG_TEST_DATABASE_URL=postgresql://nahla:nahla_password@127.0.0.1:${PORT}/nahla_saas
OUT=/tmp/claude-0/proofs/${LABEL}
rm -rf "$OUT"; mkdir -p "$OUT"
git rev-parse HEAD > "$OUT/head.txt"; git status --porcelain > "$OUT/status.txt"
/tmp/claude-0/venv312/bin/python -c 'import os; import sqlalchemy as sa; e=sa.create_engine(os.environ["NAHLA_RELIABILITY_PG_ADMIN_DSN"]); c=e.connect(); print("server_version_num=", c.exec_driver_sql("SHOW server_version_num").scalar()); print(c.exec_driver_sql("select version()").scalar()); c.close(); e.dispose()' > "$OUT/server.txt" 2>&1
PATH=/tmp/claude-0/venv312/bin:$PATH python scripts/required_postgres_proofs.py \
  --manifest scripts/required_postgres_proofs.json \
  --junit-dir "$OUT/junit" --report "$OUT/report.json" > "$OUT/runner.log" 2>&1
echo $? > "$OUT/rc.txt"
```

Recorded per run: `head.txt` (`1cc9613c7b78b6525f0e972f533a937fb7c72691` for
`final_pg16`, `7b4328ae046072b076b20f39c40e8d2887ba42fa` for
`final_pg16_scram` and `final_pg18`), an empty `status.txt` (clean tree),
`server.txt` (`server_version_num= 160015` / `180006`), `report.json`
(verdict and per-suite counts) and `rc.txt`. This mirrors the CI step's
environment (same four variables, same manifest, `--junit-dir` and
`--report` flags).

Catalog suites inside every strict run: `shopify_catalog_import` 55/55 and
`shopify_connection_foundation` 73/73, 0 skipped.

### The first PG16 run: an authentication artifact, then a re-run

`catalog_review_environment_guard::test_wrong_password_is_unreadable_not_accepted[server0]`
(pre-existing, #1195; not touched here) asserts that the server refuses a
wrong password. The disposable PG16 cluster had been initialised with
`--auth=trust`, so any password was accepted; CI's `postgres:16` service
authenticates TCP clients with SCRAM. Both clusters were switched to
`pg_hba.conf` lines `local all all trust`,
`host all all 127.0.0.1/32 scram-sha-256` and
`host all all ::1/128 scram-sha-256` (verified: wrong password refused, CI password
accepted) — PG18 before its run started, PG16 after its first run ended —
and the full inventory was re-run on PG16 under a new label. The first
report is kept as evidence, not replaced. No test, manifest, rule or source
was changed for this.

Earlier, stopped attempts (at `c0f1562`, and a password-less DSN artifact in
`commerce_runtime_synthetic_probe`) were preliminary and are not evidence
for any commit.

### Strict inventory

Additive only: the 27 pre-existing suites, their 700 node ids,
`required_env` and the manifest rules are byte-identical to the base;
`shopify_catalog_import` (55 proofs) is appended — 28 suites, 755 cases
(753 proofs + 2 runner regressions). The runner self-test pins the new id.

## GitHub CI on this stacked base

At PR head `7b4328a` GitHub ran exactly two checks:
`gov002-trusted-base-scanner` (success) and `publish-merge-freeze-gate`
(success). `ci.yml` triggers only for pull requests into `main` / `master`,
so `lint-and-test`, `constitution-compliance`, `dashboard-platform-policy`,
`a1-postgres-integration`, `whatsapp-catalog-sync-postgres` and the
PostgreSQL 18 compatibility job did **not** run on GitHub for this PR —
not run is not passed. Filters and branches were not changed to produce
checks. The local runs above are the evidence until the stack reaches
`main`.

## Not validated

* No live Shopify store or live Admin API 2026-10 call. Field and scope
  facts are coordinator-verified against the official 2026-10 reference
  (2026-10-11); this environment's fetch of shopify.dev was refused by the
  network policy (HTTP 403).
* Migration `0122` was applied only to disposable databases (in-process and
  on the real alembic chain: `alembic upgrade` to `0093`, `0111`, `0113`,
  `0120`, `0121`, `0122`, then `alembic downgrade 0122@-1`); it has not
  been applied to any shared or live database.
* `read_inventory` is not requested or granted by the foundation; the
  per-location inventory path is exercised only with a mocked wider grant.
* A real back/forward-cache restore did not occur in headless Chromium; the
  harness delivered the `pageshow` event the card listens to.
