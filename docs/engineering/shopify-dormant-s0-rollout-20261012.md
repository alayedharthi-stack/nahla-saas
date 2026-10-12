# Verified dormant Shopify S0 rollout, 2026-10-12

The existing Nahlah production API and dashboard successfully deployed source
`a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec` from the frozen branch
`release/shopify-dormant-s0-a8fb88e3`. The owner directly authorized this exact
source and both services in the executing local task, with Shopify disabled
and no database migration. Independent review had accepted PR #1221's exact
three dependency inventory entries and unchanged guard/runtime/migrations.

The expired execution record was replaced before deployment. Only the API
predeploy command and sealed approval payload needed refreshing within the
existing staged patch. The reviewed native scope stayed at 14 fields across
the two existing services. The fresh record was bound at 19:44:00 UTC, passes
strict rendering, and expires at 23:44:00 UTC. It was committed through the
native Railway deployment tool; no alternate route was used.

| Target | Deployment | Terminal result |
| --- | --- | --- |
| API | `67f2a118-5733-41c8-90fc-a25bee392022` | SUCCESS at 20:02:23.867 UTC |
| Dashboard | `49499c61-c777-4c62-a863-5104c84d7462` | SUCCESS at 20:00:37.507 UTC |

Production bootstrap and all **19** S0 gate checks passed at 20:01:44 UTC.
They verified actual source, the 146-file inventory, graph, target, approval,
flags, database topology and exact retained schema. The inventory digest is
`31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174`.
Application startup completed, and public API health and dashboard both
returned HTTP 200 after the rollout.

Postdeployment read-only verification established:

- Actual API and dashboard runtime source equals the exact frozen SHA.
- All five backend Shopify flags and both dashboard flags are OFF.
- The public served dashboard asset matches the deployed container byte for
  byte. Its connection and catalog predicates both call the shared truthy
  parser with literal `"0"`, so both evaluate false. This is static artifact
  verification; a full authenticated browser run was not performed.
- Exact DB heads remain `{0111,0113,0120}`, PostgreSQL 18.6 remains primary,
  six retained table definitions and the nonce definition match, and all ten
  retained relation OIDs are unchanged. No Shopify tables, sequences or stray
  objects exist. No migration, stamp, restore or activation was performed.
- Existing SQL-only catalog identity and active-binding audits pass. The seven
  enabled Salla integrations, one active Meta binding, protected existing flags
  and startup selectors remain unchanged. No customer samples were exported.
- All service configuration outside the reviewed source, predeploy and new
  variables is preserved. No staged changes remain. Both deployments are
  reported redeployable and rollback-capable.

The [machine-readable evidence](shopify-dormant-s0-rollout-20261012.json)
contains deployment/snapshot IDs, exact record and command hashes, gate checks,
health and asset hashes, runtime flags, DB identity evidence and aggregate
catalog results. The external filled record remains outside the repository;
its bytes are pinned by SHA-256. It uses the accurately carried backup browser
observation from 12:01:14 UTC, for the previously verified matching completed
647 MB card dated 2026-10-11T16:54:10.145Z with Restore available. A new browser
observation or restore rehearsal is not claimed.

The earlier proof's `provider_calls:0` was a literal, not measured provider
instrumentation. These postchecks use metadata, read-only SQL and public
static/health HTTP; they make no instrumentation claim. Uvicorn's startup INFO
line is carried on stderr and tagged error by the log transport; the actual
message and terminal health/success were verified.

Documentation follows the tested source without changing it. Keep the frozen
release branch at the exact tested SHA. Any future deployment requires a new
valid external execution binding; the current record's expiry does not affect
the already running services. S3, migrations and activation remain separate
later work. Historical review-only templates stay in the prior bundle.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
