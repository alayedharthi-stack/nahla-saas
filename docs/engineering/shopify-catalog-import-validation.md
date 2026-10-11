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
| Root unit suite (CI "Run unit tests") | `python -m pytest -q --maxfail=1` | RC 0 — 9529 passed, 790 skipped, 7 xfailed, 0 failed |
| Strict proofs, PG16 first run (trust auth) | `scripts/required_postgres_proofs.py --manifest scripts/required_postgres_proofs.json …` | RC 1 — **NOT PROVEN 754/755** (proofs 752/753, runner regressions 2/2, 0 skipped); see below |
| Strict proofs, PG16 re-run (SCRAM auth) | same | RC 0 — **PROVEN 755/755** (753/753 + 2/2), 28/28 suites, 0 skipped |
| Strict proofs, PG18 (SCRAM auth) | same, PostgreSQL 18.6 | RC 0 — **PROVEN 755/755** (753/753 + 2/2), 28/28 suites, 0 skipped |
| `lint-and-test` remaining 16 steps | each step's `run:` block from `ci.yml`, verbatim | all RC 0 (silent-except lint OK; WhatsApp OAuth boundary 16; sibling identity 94; manual gift 36; orders newest-first 15; Salla browse 41, card 75, compose 85, PR 888 69; coupon L0 156; coupon shadow 10; coupon verify 29; real-channel probe 59; staging migration operators; tenant clone 112 passed 1 skipped (pre-existing); DR parity 7) |
| GOV-002 (scanner from merge-base `53bd7a4`, `--trusted-base-scanner`) | `python <scanner> --repo . --base 53bd7a4 --head 7b4328a --trusted-base-scanner` | RC 0 — `FINDINGS=none`; MODEL / PROMPT / PERSONA / PHRASE_MAP / KEYWORD_ROUTER / CUSTOMER_REGEX changed = NO |
| Constitution compliance | `python -m pytest backend/tests/test_constitution_compliance.py backend/tests/test_intelligence_non_interference_guard.py -q` | RC 0 — 98 passed |
| Dashboard typecheck / build | `npx tsc --noEmit -p .`; `npm run build` | RC 0 / RC 0 (tracked `dist/index.html` restored afterwards) |
| Dashboard platform policy | `npm run check:platform-policy` | RC 0 — 598 OK, 0 FAIL |
| Import UI source/model check | `npm run check:shopify-catalog-import-ui` | RC 0 — 39 passed |
| Connection UI check (unchanged guard) | `npm run check:shopify-connection-ui` | RC 0 — 295 passed |
| Real-browser harness (local) | `node scripts/verify-shopify-catalog-import-ui.mjs` | RC 0 — 20/20; the old busy gate fails scenario 8 (mutation-checked) |

Catalog suites inside every strict run: `shopify_catalog_import` 55/55 and
`shopify_connection_foundation` 73/73, 0 skipped.

### The first PG16 run: an authentication artifact, then a re-run

`catalog_review_environment_guard::test_wrong_password_is_unreadable_not_accepted[server0]`
(pre-existing, #1195; not touched here) asserts that the server refuses a
wrong password. The disposable PG16 cluster had been initialised with
`--auth=trust`, so any password was accepted; CI's `postgres:16` service
authenticates TCP clients with SCRAM. Both clusters were switched to
`host … scram-sha-256` (verified: wrong password refused, CI password
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
  on the real alembic chain `0093 → … → 0121 → 0122 → 0121`); it has not
  been applied to any shared or live database.
* `read_inventory` is not requested or granted by the foundation; the
  per-location inventory path is exercised only with a mocked wider grant.
* A real back/forward-cache restore did not occur in headless Chromium; the
  harness delivered the `pageshow` event the card listens to.
