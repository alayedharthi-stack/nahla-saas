# Shopify release gate: independent local validation (Draft #1214)

Status: **Draft; modified guard not accepted for merge or rollout.**
The source under review is `7f89dcbe174a1b639509cd6b68d421a3a8e939c7`,
tree `b5ca2faedb1447d4b7cd6df63c47d0866543d422`. It preserves Claude's
`2ab3042d` fixture correction and `61ea14cf` historical report. The stacked
base remains `8308c16e39a18745239e869e6996c7aeaf680fd4` (#1211); #1209 stays
frozen. Migration sources end at 0123. No Google 0124 change was absorbed.

## Independent fixes

- Bootstrap compiles the repository bytes it verified, in the same isolated
  Python process. Repository source loaders ignore bytecode caches, including
  direct `SourceFileLoader` use. The finder refuses uninventoried source,
  sourceless bytecode, native initializers and dependency shadows before
  their loaders run. Tests use import markers and unchecked-hash bytecode;
  the direct-loader control uses a synthetic pinned migration and does not
  claim a full Alembic integration run.
- Snapshot validation rejects inherited libpq target variables and query
  target overrides before a connection, using the existing strict driver
  parser and an explicit validated URL. A forbidden-target probe is never
  attempted.
- The server check reads the effective `default_transaction_read_only`
  setting, including server-wide defaults, separately from the guard's
  intentionally read-only transaction. The fake-connection control covers
  an effective `on` value without relying on database/role setting rows.
- The wrong-owner fixture uses Claude's valid `shopify_webhook_events.id`
  target and asserts the actual `pg_depend` ownership before guard refusal
  and restoration. Its real PostgreSQL run remains blocked locally.
- The operator plan requires enforced exclusion of all automated/manual
  deployment actors and draining queued/in-flight pre-deploys throughout
  the manual migration window. The advisory key coordinates only holders
  that acquire it; the #1211 plain upgrades do not. OID equality does not
  prevent a stale S0 observation after a concurrent 0121 commit.
- Step C (`upgrade 0093`) proof is distinguished from full startup. Salla
  cleanup, `create_all` and safe alters remain an acceptance gap.

## Tests actually run locally

Existing Windows Python 3.13; pytest 9.0.3. Available libraries are
SQLAlchemy 2.0.49, Alembic 1.18.4, psycopg2-binary 2.9.11. Deployment pins
remain unchanged at 2.0.30 / 1.13.1 / 2.9.9. No software was installed and
no versions or guards were weakened to obtain a passing result.

| Run | Source/provenance | Actual result |
|---|---|---|
| Available DB-free gate controls | Frozen `7f89dcbe`, tracked source clean before/after; 2026-10-11 19:18:21-19:18:51 UTC | 132 passed, 1 skipped, 11 deselected; 26.91 s; rc 0 |
| Gate PostgreSQL module | Frozen `7f89dcbe`; no disposable admin DSN/host alias | 10 skipped; no PG16/PG18 passes claimed |
| Silent-exception lint | Frozen `7f89dcbe` | OK, 624 pre-existing entries all baselined; rc 0 |
| Trusted GOV-002 scanner | Exact scanner blob from base `8308c16e`, base to frozen `7f89dcbe` | FINDINGS=none; rc 0; all interference flags NO |
| Constitution compliance | Pre-freeze local working tree, 19:10:55-19:13:42 UTC | 57 passed; 166.13 s; rc 0 |
| Intelligence non-interference guard | Pre-freeze local working tree, 19:15:37-19:16:51 UTC | 41 passed; 73.71 s; rc 0 |

The two constitution suites are working-tree results, not advertised as a
fresh frozen-head run. Their files and the guarded application sources are
unchanged by these commits. Test-generated Windows cache directories were
outside the tracked source and removed before the final clean check.

The full gate module was attempted before freezing: **130 passed, 5 failed,
1 skipped, 8 errors**. The byte-inventory failure came from initial Windows
CRLF materialization; canonical Git bytes restored the expected digest.
The effective-default change required the existing exception fixture to
include the new valid server answer; that fixture was corrected. The final
available selection passes both controls. The remaining eleven excluded
cases require the Unix command-rendering harness (the Windows executable
contains backslashes rejected by the renderer) or the exact deployment
library pins (the CLI correctly refuses the installed versions). This is
not a full-suite pass. The legacy-checkout case is skipped because
`NAHLA_LEGACY_462_CHECKOUT` is unset. No software installation or pin change
was attempted to remove these gaps.

Exact available-selection command:

```text
py -3.13 -m pytest tests/test_shopify_release_gate.py -k 'not bootstrap_refuses_a_tampered_module and not bootstrap_refuses_a_planted_initializer and not bootstrap_refuses_wrong_release_identity and not bootstrap_hands_over and not render_round_trips and not cli_fails_closed' -q -ra --tb=short
```

Other commands: `py -3.13 -m pytest backend/tests/test_shopify_release_gate_pg.py -q -ra --tb=short`;
`py -3.13 scripts/lint_no_silent_except.py`;
the base scanner extracted with `git show 8308c16e39a18745239e869e6996c7aeaf680fd4:scripts/lint_intelligence_non_interference.py`,
then `--repo . --base 8308c16e39a18745239e869e6996c7aeaf680fd4 --head 7f89dcbe174a1b639509cd6b68d421a3a8e939c7 --trusted-base-scanner`;
`py -3.13 -m pytest backend/tests/test_constitution_compliance.py -q -ra --tb=short`;
`py -3.13 -m pytest backend/tests/test_intelligence_non_interference_guard.py -q -ra --tb=short`.

## Frozen inventory and unchanged scope

| Field | Value |
|---|---|
| gate version / inventory count | 2 / 143 |
| gate SHA-256 | `d88f4701b8080b0969462d0bedb1108dd4526e1c16f54d945b2cdd5762e81c76` |
| inventory digest | `f39d8cae33d8ede8c833de84bae89b830d2cf4a18fc74d3b87874d7f7d6da187` |
| database Python digest | `b3fcf5d151a106a9068985118d6489c381844c1ba5553fbffc9d94ffe272aa50` |
| unexpected initializers | `[]` |

`git diff --exit-code 61ea14cf..7f89dcbe -- database requirements.txt backend/modules/ai integrations/salla .github/workflows scripts/lint_intelligence_non_interference.py`
confirmed no changes to those paths. The only new source commit changes the
gate, renderer, their tests and the operator plan. Evidence is published in
a separate documentation commit; it changes no tested code or inventory.

## Remaining acceptance blockers

1. Run the full DB-free suite with the pinned libraries and supported Unix
   harness, then the modified gate on disposable PostgreSQL 16 and 18.
   Claude's retained 10/10 PG reports concern guard `2ab3042d`, whose
   runtime differs; they are not carried as passes for this modified guard.
2. Verify full startup compatibility at the relevant Shopify states,
   independently of Alembic Step C. Independently review the exact new head
   and the enforceability of deployment exclusion before any migration.
3. Obtain a separately reviewed external phase approval, fresh backup
   evidence and, for S3, verified restore rehearsal. The observed 647 MB
   Railway backup card with Restore available is completed-backup evidence,
   not restore proof or rollout approval.
4. The supported browser tools remain unavailable after the update, so
   the new brief was not delivered to Claude and no new Claude report is
   claimed for these local changes. No evidence supports recommending a
   device reboot as the remedy.

No live target was contacted by these tests. No migration, deployment,
source-pin change, credentials, Shopify settings, permission change or
feature activation occurred. Capability remains disabled. GitHub's stacked
PR checks cover GOV-002 and merge-freeze only, not the DB-free, startup or
PostgreSQL proof suites.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
