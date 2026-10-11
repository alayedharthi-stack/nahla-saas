# Multi-head Shopify migration plan — disposable-PostgreSQL evidence

Final frozen source `2097e1eac186` (branch `claude/shopify-multihead-migration-plan-20261011`, stacked on
`claude/shopify-deployed-compat-20261011` at `1e9f1fdaf866`, Draft PR #1211). The first frozen source
`84f3100ac735` and its retained results are reported as such; the review corrections that followed are
listed in §3. Every run below used disposable PostgreSQL servers on this machine only (synthetic role,
SCRAM over loopback); no live, staging, Railway or credentialed target was touched, and nothing was
provisioned for this slice. Planned or running tests are not reported as passes; only terminal results.

## 1. Pre-freeze failure and its confirmed cause (preserved)

First run of the three strengthened chain cases on the then-uncommitted working tree
(2026-10-11T15:13:27Z): `1 failed, 2 passed in 263.67s (0:04:23)`.

* PASSED `test_shopify_catalog_import_pg.py::test_alembic_chain_upgrades_0121_to_0122_and_back`
  (S0 → S1 → S2 → S1 → S0 with exact rows, schema and sentinels).
* PASSED `test_shopify_privacy_webhooks_pg.py::test_alembic_chain_upgrades_0122_to_0123_and_back`
  (S0 → S1 → S2 → S3 → S2 → S1 → S0).
* FAILED `test_shopify_connection_pg.py::test_alembic_chain_upgrades_0120_to_0121_and_back` at the
  first read-only preflight call: `AssertionError: assert 2 == 0` — the guard exited 2 with
  `connection or repository error: ModuleNotFoundError\n`.
  **Confirmed cause:** the guard loaded the repository migration graph from the repository root
  without `database/` on `sys.path`, so revision modules that import helpers relative to
  `database/` (as `env.py` arranges when Alembic runs from there) could not be imported; a second
  defect found in the same pass was that the guard passed the SQLAlchemy driver spelling
  `postgresql+psycopg2://` straight to psycopg2. Both were guard-side fixes (path insertion and
  driver-prefix normalisation); no migration, test expectation or topology changed.
* Pre-freeze diagnostic re-run of the connection case after the fix
  (2026-10-11T15:19:09Z): `1 passed in 75.19s (0:01:15)` (diagnostic only).

## 2. First frozen source `84f3100ac735` — retained terminal results

Frozen source `84f3100ac735`; HEAD and tree recorded before and after:

```text
[2026-10-11T15:22:38Z] BEFORE frozen runs: HEAD=84f3100ac735b80ee4449cd4139debeb9163a565 expected=84f3100ac735b80ee4449cd4139debeb9163a565 match=yes porcelain=0 branch=claude/shopify-multihead-migration-plan-20261011
pg16 postmaster=632 pg18 postmaster=17638
AFTER 84f runs: HEAD=84f3100ac735b80ee4449cd4139debeb9163a565 porcelain=0 2026-10-11T15:31:50Z
```

| Run | Server | Result |
|---|---|---|
| three Shopify PostgreSQL proof suites (manifest ids 73 + 55 + 60 = 188), JUnit | PostgreSQL 16.15, port 5433 | `188 passed, 2 warnings in 503.80s (0:08:23)`; JUnit {"cases": 188, "passed": 188, "failure": 0, "error": 0, "skipped": 0}; started 2026-10-11T15:22:38Z, [2026-10-11T15:31:07Z] rc=0 HEAD_after=84f3100ac735b80ee4449cd4139debeb9163a565 porcelain_after=0 |
| three strengthened chain cases | PostgreSQL 18.6, port 5434 (existing disposable server) | `3 passed in 325.11s (0:05:25)`; JUnit {"cases": 3, "passed": 3, "failure": 0, "error": 0, "skipped": 0}; [2026-10-11T15:28:04Z] rc=0 HEAD_after=84f3100ac735b80ee4449cd4139debeb9163a565 porcelain_after=0 |
| DB-free: guard contract, bootstrap target, proofs-runner regression | none | `30 passed in 75.26s (0:01:15)` |
| no-silent-except lint | none | `no-silent-except: OK (624 pre-existing, all baselined)` |
| constitution compliance + non-interference guard | none | `98 passed in 165.35s (0:02:45)` |
| GOV-002 scanner from `1e9f1fdaf866` (trusted-base mode) | none | `MODEL_CHANGED=NO PROMPT_CHANGED=NO FINDINGS=none` |

## 3. Review corrections → final frozen source `2097e1eac186`

Independent GitHub review of `84f3100ac735` asked for three corrections, applied as one source commit
(tests, the read-only guard and docs only):

1. **Schema snapshot by canonical definition.** `backend/tests/_alembic_topology_probe.py` now
   reads every constraint through `pg_get_constraintdef` (primary key, unique, check, foreign key
   with referenced table, ON DELETE / ON UPDATE, MATCH and deferrability) and every index through
   `pg_get_indexdef` (method, columns, expressions, ordering and operator classes, INCLUDE, partial
   predicate), plus columns with `format_type`, nullability, default, identity and generated
   markers. A **negative control** runs last inside the connection chain case (same node id, proof
   inventory unchanged): a same-name CHECK predicate change, a same-name foreign key re-created with
   `ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED`, and a same-name index re-created with a
   partial predicate must each be detected (the diff names exactly that table) and must vanish
   again once restored.
2. **Plan §4 and gates.** Every command binds `DATABASE_URL="$CLONE_DSN"` explicitly, each step is
   its own operator-approved block under `set -euo pipefail`, nothing sequences the blocks, the
   clone-only environment is entered out of band (no DSN in the document), independent review
   (gate A) and per-environment owner approval (gate B) precede any clone/staging action, and
   `head` (ambiguous, refused by Alembic with several heads) and `heads` (every branch to its own
   head, would add `0092`, `0114`, `0115`) are explained separately.
3. **Secret-safe guard output.** `scripts/audit_alembic_topology_readonly.py` prints only revision
   ids the repository knows; any other `version_num` value is reported as `<unknown>` and counted
   (`alembic_version.unknown_revision_count:N`), never echoed, in text and JSON output; the
   expectation must be well-formed ids. Pinned by two negative output tests in
   `tests/test_alembic_topology_audit_script.py` (a DSN-shaped row and a control-sequence row never
   appear in the report, the text output or the JSON output).
4. **Whole-row sentinel digests (parent finding).** The projection compared selected columns only,
   so seeded `product_ids` / `query_digest` / `expires_at`, `granted_scopes` and
   `source_observed_at` were not covered. The probe now takes a sha256 of the canonical text form
   of each of the 11 planted rows (every column, token columns included) in the inserting
   transaction and compares it after every step; values are never returned or printed. A second
   negative control mutates a formerly unchecked seeded column (`query_digest`, `granted_scopes`,
   `source_observed_at`), shows the check rejects it naming only the table, and restores the exact
   original value so the check passes again. Test coverage only; no runtime defect was observed.
   The first two diagnostics of this cycle failed inside the negative controls themselves
   (`pg_get_constraintdef(oid, true)` pretty-prints differently from the canonical form, and the
   mutated CHECK `>= 1` was violated by the seeded row with generation 0); both were corrected in
   the probe (canonical `pg_get_constraintdef(oid)`, a satisfiable but different predicate).

Pre-freeze diagnostic of the corrected connection case (2026-10-11T15:35:38Z):
`1 passed in 25.00s` (diagnostic only).

## 4. Final frozen source `2097e1eac186` — terminal results

Frozen source `2097e1eac186`; HEAD and tree recorded before and after:

```text
[2026-10-11T15:36:54Z] BEFORE frozen runs: HEAD=2097e1eac186a6a5d6ca7a90439b17621587d7b6 expected=2097e1eac186a6a5d6ca7a90439b17621587d7b6 match=yes porcelain=0 branch=claude/shopify-multihead-migration-plan-20261011
pg16 postmaster=632 pg18 postmaster=17638
[2026-10-11T15:46:52Z] AFTER frozen runs: HEAD=2097e1eac186a6a5d6ca7a90439b17621587d7b6 porcelain=0
```

| Run | Server | Result |
|---|---|---|
| three Shopify PostgreSQL proof suites (manifest ids 73 + 55 + 60 = 188), JUnit | PostgreSQL 16.15, port 5433 | `188 passed, 2 warnings in 266.34s (0:04:26)`; JUnit {"cases": 188, "passed": 188, "failure": 0, "error": 0, "skipped": 0}; started 2026-10-11T15:41:02Z, [2026-10-11T15:45:32Z] rc=0 HEAD_after=2097e1eac186a6a5d6ca7a90439b17621587d7b6 porcelain_after=0 |
| three strengthened chain cases | PostgreSQL 18.6, port 5434 (existing disposable server) | `3 passed in 78.74s (0:01:18)`; JUnit {"cases": 3, "passed": 3, "failure": 0, "error": 0, "skipped": 0}; [2026-10-11T15:46:52Z] rc=0 HEAD_after=2097e1eac186a6a5d6ca7a90439b17621587d7b6 porcelain_after=0 |
| DB-free: guard contract, bootstrap target, proofs-runner regression | none | `32 passed in 71.05s (0:01:11)` |
| no-silent-except lint | none | `no-silent-except: OK (624 pre-existing, all baselined)` |
| constitution compliance + non-interference guard | none | `98 passed in 159.71s (0:02:39)` |
| GOV-002 scanner from `1e9f1fdaf866` (trusted-base mode) | none | `MODEL_CHANGED=NO PROMPT_CHANGED=NO FINDINGS=none` |

Proofs manifest `scripts/required_postgres_proofs.json` is unchanged in both commits: the three
chain cases keep their node ids, so the required-id counts (611 deployed + 188 Shopify = 799) are
untouched.

## 5. What the strengthened chain cases prove (every step, both servers)

* `alembic_version` rows exactly `{0111, 0113, 0120}` after the genuine upgrades `0093`, `0111`,
  `0113`, `0120` (no stamp, merge or row edit), then exactly S1 `{0111, 0113, 0121}`,
  S2 `{0111, 0113, 0122}`, S3 `{0111, 0113, 0123}` forward and S2, S1, S0 back through
  `downgrade 0123@-1`, `0122@-1`, `0121@-1`; rows compared as a sorted list (duplicates would
  show), `0111` and `0113` present at every step.
* Non-Shopify schema compared by canonical PostgreSQL definition before and after every step for
  every table outside `shopify_*` / `alembic_version` (§3 item 1), with the negative control.
* Synthetic sentinel rows planted at S0 and verified after every step in `tenants`, `products`,
  `customers`, `customer_addresses`, `customer_address_provenance` (0110, navigation branch),
  `commerce_runtime_conversations` and `commerce_runtime_navigation_snapshots` (0111/0113
  branches), `commerce_runtime_handover_workers` (0111), `meta_catalog_memberships`,
  `catalog_channel_retirements` (0118) and `meta_catalog_authorizations` (0120); the token column
  is compared by digest only and no row, token or DSN is ever printed. Row counts of these
  tables are compared after every step.
* The read-only preflight is exercised on real PostgreSQL inside the connection case: exit 0 for
  exactly S0 / S1, exit 1 for the next state or a subset.

## 6. Disposable servers (identity, no secrets)

PostgreSQL 16.15 (port 5433; restarted after the 14:59Z reboot with the recorded options):

```text
connected over TCP with the synthetic CI password; server_version/password_encryption/addr/port/system_identifier: ('16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)', 'scram-sha-256', '127.0.0.1/32', 5433, 7695297279087089957)
role password stored as SCRAM-SHA-256: True
non-template databases present: 2
wrong password refused: True
```

PostgreSQL 18.6 (port 5434; existing binaries and data directory restarted; nothing installed):

```text
connected over TCP with the synthetic CI password; server_version/password_encryption/addr/port/system_identifier: ('18.6', 'scram-sha-256', '127.0.0.1/32', 5434, 7695293626485950146)
role password stored as SCRAM-SHA-256: True
non-template databases present: 1
```

## 7. Evidence commit binding

Before the evidence commit the working tree equalled the tested source 2097e1eac186a6a5d6ca7a90439b17621587d7b6 exactly (git status --porcelain empty, HEAD 2097e1eac186a6a5d6ca7a90439b17621587d7b6). The evidence commit adds only docs/engineering/shopify-multihead-migration-plan-evidence.md; git diff --name-only 2097e1eac186a6a5d6ca7a90439b17621587d7b6 <evidence commit> lists that single path, so every other blob (tests, probe, guard, plan, runbook, migrations, runtime) is byte-identical to the tested source. Tested source tree id: 512b21c97266d7819df267715b4db231f52a657e.

## 8. Commands (as run, DSNs omitted)

```text
NAHLA_RELIABILITY_REQUIRE_PG=1 NAHLA_RELIABILITY_PG_ADMIN_DSN=<disposable admin DSN> CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1 LEGACY_MIG_PG_TEST_DATABASE_URL=<disposable DSN> \
  python -m pytest -q -p no:cacheprovider -rA --junitxml=<scratch>/frozen_proofs188_pg16_junit.xml \
    backend/tests/test_shopify_connection_pg.py backend/tests/test_shopify_catalog_import_pg.py backend/tests/test_shopify_privacy_webhooks_pg.py
(same variables pointing at port 5434) python -m pytest -q -p no:cacheprovider -rA --junitxml=<scratch>/frozen_chain3_pg18_junit.xml <the three chain node ids>
python -m pytest -q -p no:cacheprovider tests/test_alembic_topology_audit_script.py tests/test_normal_bootstrap_migration_target.py tests/commerce_reliability/test_required_postgres_proofs_runner.py
python scripts/lint_no_silent_except.py
python -m pytest -q -p no:cacheprovider backend/tests/test_constitution_compliance.py backend/tests/test_intelligence_non_interference_guard.py
python <scanner blob of 1e9f1fdaf866>/scripts/lint_intelligence_non_interference.py --repo . --base 1e9f1fdaf866cb27be0c33ea6b52aa2796983522 --head <frozen sha> --trusted-base-scanner
```

## 9. Flags

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```

No migration revision, `env.py`, runtime, catalog, import, AI, Salla, bootstrap, governance or
CI file changed; all Shopify flags remain OFF; no live, staging or credentialed action was taken.
