# Shopify production Docker / full ASGI evidence

The unmodified production Dockerfile builds successfully with Python **3.11.17**.
The full imported API passes **24/24** HTTP/import checks across frozen candidate
`193d1bdc`, accepted baseline `7031e3ef` and deployed-source baseline `462ede1`.
**One conditional Shopify integration blocker remains:** a configured
`API_SECRET_KEY` rejects the browser callback with **401** before Shopify's handler.
Candidate and accepted baseline both reproduce it. Runtime source remains frozen.

This test-only [Draft #1217](https://github.com/alayedharthi-stack/nahla-saas/pull/1217)
is stacked on independently accepted narrow [Draft #1216](https://github.com/alayedharthi-stack/nahla-saas/pull/1216).
The completed database/essential guard proofs are carried, not rerun. This report
supersedes the import/image uncertainty in the earlier startup reports; it grants
no merge, live rollout, app configuration or activation approval.

## Terminal evidence and exact build

[Run 38178126550](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38178126550),
job `114584750516`, ran **2026-10-11 21:52:12–21:54:27 UTC**. Overall conclusion is
**failure**, deliberately preserved for the configured-service-key callback probe.
The production-build step, full three-source ASGI comparison and final clean-source
identity check all succeeded; only the separate callback configuration step failed.

| Source | Exact SHA / tree | Built image ID |
|---|---|---|
| Frozen candidate | `193d1bdca04a84f093d5ae1bfbbde45d9e2c88fe` / `4c7cb22523fee701588513b0dc9d18f9021479b5` | `sha256:bb9209f19b835af9bb79ab01530312001f4b2ea729898f1878ed6716b7aa400a` |
| Accepted baseline | `7031e3efec639625dd537026d5a80376f3d679c9` / `600ffd638cadee2bb45e8eab7fbc1ea8134cec22` | `sha256:601036074809a5eecaaa66a344596f26d51f6b7d4e6bd2d2bed8c75054eb44dd` |
| Deployed-source baseline | `462ede1aff602cad84e9569be859cca0087b190d` / `f2866da7abec02e55592360b0360279205b6c08c` | `sha256:ccb3d6483ae4206029eea28b629b621734d2f1098e574792d49f62e3f8a7dbb7` |

The separate test driver is `d3920af2381f4ab8ccd7f09949abd139a199c39d`, tree
`e625ea6dbe83a6bb4d0a746091478ff7ae1a39b1`. All four source identities were checked
before/after execution, with clean tracked/untracked source status.

The real `python:3.11-slim` base resolved to
`python@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`,
image `sha256:541096ae9b05d3b850341b78790e2ed31e94d5aa0ea086933c6ff0c09ffa2e63`.
Dockerfile, requirements, dockerignore, Nixpacks config and start.sh bytes were
identical across all sources. Each source was baked with its own unchanged
Dockerfile, reusing and asserting identical dependency layers. Candidate `pip check`
and the real production preflight passed. No reduced dependency fixture was used.
Resolved key versions: FastAPI 0.111.0, Starlette 0.37.2, HTTPX 0.27.0, SQLAlchemy
2.0.30, psycopg2-binary 2.9.9, Pydantic 2.12.5, cryptography 50.0.2,
python-jose 3.3.0, Sentry SDK 2.71.0 and openai-agents 0.22.2.

## Full ASGI checks and source comparison

Each source passed eight checks: actual `backend.main` import and router graph;
real `/alive`, `/healthz`, `/auth/ping` and `/version`; exact callback/JWT handling
and protected unauthenticated neighbors; two real signed merchant JWTs with
spoofed opposite tenant headers against all dormant routes; installed HTTPX,
uvicorn and raw-ASGI log filters; real Sentry before-send scrubbing; resolved
encryption-library compatibility; and strict zero side-effect attempts.

Candidate and accepted baseline mount 12 Shopify routes. All three share exactly
**643 non-Shopify route definitions**, including the unchanged Salla graph, digest
`17fed89fc8f585bfb49d1f24df15b4b961e4ae7678f2725421540e6702f4b10b`.
Candidate/accepted redact Shopify HMACs and sensitive Shopify Sentry frames;
legacy's absent Shopify-specific scrubbing is recorded as the expected baseline
contrast, not represented as a Shopify feature pass. Legacy lacks Shopify routes;
its existing catch-all OPTIONS route yields 405 for authenticated unmatched paths.

All six final container reports record **0 network, 0 DB-connect and 0 subprocess
attempts**. The full reports record no additional threads. Docker uses
`--network none`; a Python audit guard refuses network/process operations and a
SQLAlchemy pre-connect guard refuses the synthetic PostgreSQL DSN. No auth,
router or DB dependency was replaced. All keys/passwords/JWTs are synthetic;
provider and Shopify credentials are absent. Source SHA metadata is supplied as
Railway supplies it, and `/version` verifies the correct SHA.

## First material divergence: configured service-key callback

With a synthetic non-empty `API_SECRET_KEY`, actual unsigned browser
`GET /merchant/integrations/shopify/callback` returns **401** on both `193d1bdc`
and `7031e3ef`, although the exact callback is public in the JWT middleware and
should reach its dormant handler's 404. The deployed-source control returns its
expected 401, since it has no Shopify callback. All three imports succeed and
all three probes record zero side-effect attempts.

`backend/core/middleware.py:252` applies the optional service-key gate. Its public
path exceptions omit the exact Shopify callback; `backend/core/middleware.py:219`
exempts it only from JWT enforcement. This is an existing shared gate interacting
with the new public callback, **not a regression introduced by #1216's SQL guard**.
Because that gate runs before the route, a normal Shopify browser redirect cannot
reach HMAC/state verification when the service key is configured. This is a
conditional activation blocker; no live configuration value was inspected.

Recommendation for a separately reviewed bounded fix: exempt only the exact
Shopify callback from the internal service-key gate, preserve protected neighbors,
and retain the handler's fail-closed HMAC/state/config checks. Do not remove or
weaken the service key globally. No fix or production workaround is applied here.

## Evidence boundaries and earlier fixture failures

[Structured results](shopify-production-asgi-results.json) retain image/source
identities, dependency layers, all six terminal reports, job steps and decoded-log
hashes. Full downloaded logs are retained locally by job ID. GOV-002 and merge-freeze
passed at the tested driver. Five CI runs are recorded transparently: initial
synthetic ADMIN_EMAIL omission; strict detection of the existing health Git
fallback plus shell errexit; all-baseline diagnosis and the legacy 405 response;
metadata correction with the final legacy expectation still pending; and this
completed 24-check comparison. These were test-only fixture corrections. No
unknown subprocess or callback failure was waived, and runtime never changed.

This proves the real Dockerfile's Python 3.11 build and full HTTP import graph.
It does **not** identify the currently running Railway builder/image, execute
lifespan, repeat real DB migrations, or prove readiness before background DB work
finishes. Real PostgreSQL startup evidence remains the accepted
[38176146625](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38176146625);
the essential 174-check guard proof remains
[38171916514](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38171916514).
No scheduler redesign, deploy, migration, source pin, live credentials/app settings,
merge, distribution choice, provider call or purchase occurred.

## Consolidated remaining rollout requirements

Only unfulfilled requirements follow. The narrow SQL repair, real PG startup and
essential guard suites, full Docker import proof, dashboard completion UI and
privacy intake implementation are already present/accepted evidence; do not list
them as missing work or repeat their completed gate cycles.

1. **Resolve the conditional callback integration.** Independently review the
   exact service-key exception and focused regression evidence before Shopify
   activation where the service key is configured. Keep #1216 runtime frozen
   until a separate source change is assigned/reviewed.
2. **Approve a concrete dormant live source/build bundle.** Re-observe actual
   Railway builder/runtime, deployment/source, target DB/version rows and retained
   footprint, API flags and compiled `VITE_SHOPIFY_CONNECTION_UI` /
   `VITE_SHOPIFY_CATALOG_IMPORT_UI` flags. Select exact reviewed source `R`,
   matching rendered predeploy command, pinned external approval payload and
   redeployable rollback deployment. Resolve the existing early Meta/Moyasar
   startup side-effect boundary before a supervised boot; no scheduler redesign
   is implied. Obtain separate explicit S0 deploy/dashboard approval.
3. **Authorize and execute the controlled S0-to-S3 window.** Obtain a fresh
   completed/restorable backup (within 72 h for S3) and verify its disposable
   restore. Enforce exclusion of every manual/automatic deployment actor and
   drain queued/in-flight predeploys. Approve exact `0121` → audit S1 → `0122` →
   audit S2 → `0123` → audit S3 and the branch-qualified DB-first rollback to S0.
   No ordinary deploy at S1/S2; the advisory lock protects cooperating holders
   only. Bind the fresh S3 payload to source/target/inventory/backup/restore/
   rollback; verify actual startup DB completion and health before releasing
   exclusion. No live step is authorized by this CI evidence.
4. **Approve dev-store app configuration and activation separately.** Confirm
   app `434031788033` / organization `240110284`, exact home/callback/uninstall/
   compliance URIs, `read_products`-only scope, dedicated encryption/client
   credentials provisioned out of band, API-version alignment, and Shopify
   grant/install semantics for the reviewed standalone authorization-code design.
   The shell's embedding-off/managed-install settings do not establish that
   proof. Review an activation gate policy: the current release gate refuses
   enabled Shopify flags. Authenticate a fresh regular Nahlah merchant tenant
   for `nahlah-dev.myshopify.com`; prove actual install/complete, refresh,
   ownership refusal, signed uninstall/reinstall and provider-owned product/
   variant mirror import. Per-location inventory needs separately approved
   `read_inventory` reauthorization; existing mirrors do not feed the AI catalog.
5. **Complete public-merchant readiness before distribution.** Decide and
   approve retention/data mapping/fulfillment and erasure policy, operate the
   durable privacy worker and mandatory webhook handling, and validate delivery
   bounds/monitoring. Intake and mirror-only erasure do not assert all legal
   fulfillment. Confirm unrelated-merchant distribution eligibility and a
   supervised merchant journey before any irreversible distribution selection.
   Meta/WhatsApp integration remains a later, separately authorized slice.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
