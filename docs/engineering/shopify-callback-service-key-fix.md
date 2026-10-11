# Exact Shopify GET callback service-key exception

With `API_SECRET_KEY` configured, the old middleware returns 401 to Shopify's
browser callback before it can verify Shopify's signed query. This narrow fix
lets only **GET `/merchant/integrations/shopify/callback`** reach the existing
handler. Its HMAC/state/tenant/session security remains unchanged.

[Draft #1218](https://github.com/alayedharthi-stack/nahla-saas/pull/1218) is stacked
on evidence-only [Draft #1217](https://github.com/alayedharthi-stack/nahla-saas/pull/1217).
The earlier 401 counterexample is preserved there. #1216 remains frozen at
`193d1bdc`; #1217 remains frozen at `23013bc1`. This fix is for independent source
and evidence review before activation. No merge, live rollout or configuration
approval is asserted.

## Verified authentication chain before the exception

The code audit confirmed the following unchanged chain:

* `routers/shopify_connection.shopify_callback` first checks the feature flag
  and complete client/redirect/key configuration. Unavailable configuration
  refuses before state acceptance.
* `services/shopify_connection/oauth.py` strictly parses required parameters,
  rejects duplicate/ambiguous fields, verifies the hex query HMAC-SHA256 with
  constant-time comparison (missing secret refuses), enforces timestamp bounds
  and canonical shop identity, all before state lookup.
* `lifecycle.begin_authorization` persists a hash of a random nonce, bound to
  the authenticated tenant, user and hash of the session JWT `jti`, shop,
  redirect fingerprint, fixed return path and installation generation, with
  an expiry. It does not reserve or rebind another tenant's shop.
* `lifecycle.accept_callback` locks the state row with `FOR UPDATE`, requires
  pending/unexpired state, same shop/redirect, a still-eligible initiating actor
  and unchanged ownership/generation. It consumes the pending state once,
  encrypts the code with tenant/shop/state associated data and stores a hash
  of a fresh completion handle. The browser callback binds no shop.
* Authenticated `/complete` DB-revalidates its merchant actor and then locks
  the completion state, requiring the same tenant, user and session-reference
  hash before decrypting or exchanging any code. Ownership/generation and
  actor checks are repeated before a claim. The inherited Redis revocation
  lookup remains best-effort; this patch does not alter or strengthen it.

No authentication, lifecycle, crypto, router, dashboard, Salla, Meta or AI code
in that chain changed. A public browser callback needs Shopify authentication
and the durable nonce, while its later tenant-binding operation still needs
the explicit authenticated Nahlah session.

## Exact runtime scope

`backend/core/middleware.py` gains **five lines** in the existing public
exceptions inside `api_key_middleware`:

```python
# Shopify authenticates this browser redirect with HMAC and single-use state.
or (
    request.method == "GET"
    and path == "/merchant/integrations/shopify/callback"
)
```

No prefix, trailing-slash normalization or extra method is accepted by this
new predicate. The service key remains configured and enforced elsewhere.
The existing global OPTIONS/CORS exception remains unchanged. Local and CI
scope checks remove only this predicate from the candidate AST and require
equality of the **entire middleware module** with baseline. The workflow also
requires this to be the only changed runtime file.

## Completed real-production-image proof

[Run 38179310679](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38179310679),
job `114588288125`, completed **success** on **2026-10-11 22:09:53–22:11:54 UTC**.
Every step passed, including disposable resource removal and final clean-source
identity. GOV-002 and merge-freeze passed at the tested source.

| Source | SHA / tree | Built image ID |
|---|---|---|
| Tested candidate | `47d2dcb29c6798aa9f12c6ad1537c0f1937174ff` / `35d472205027089ec04532acde891bbb6e633c29` | `sha256:bade0909c2e44faa79e0e8dbaa2167a7f4776a0d32af98b7319cb674d482c2f3` |
| Exact baseline | `23013bc16e0543c1106e8662b4dda4ba772022cd` / `be60e1794c1981a33e02aa89051851e86e4bc8f8` | `sha256:f278762ed1822204073872d7aa51c476f9ba76606da5aee53ca946b077c2f030` |

Both use their unchanged real Dockerfile, with identical asserted dependency
layers. `python:3.11-slim` resolved to
`python@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`,
base image `sha256:541096ae9b05d3b850341b78790e2ed31e94d5aa0ea086933c6ff0c09ffa2e63`.
Actual Python is **3.11.17**; `pip check`, installed dependency freeze and the
real production preflight passed. Build/start/config files and the carried
full-ASGI proof script were byte-compared with baseline. Both source identities
and clean tracked/untracked status were checked before/after execution.

* **8 full-ASGI checks passed with the service key configured and flags off**:
  actual full `backend.main` import/export, real liveness/version, exact public
  callback/protected neighbors, two signed merchant sessions, dormant routes,
  installed credential log filters, Sentry redaction and production encryption
  compatibility. This run used `--network none` and recorded zero network,
  DB-connect and subprocess attempts. One ordinary AnyIO request worker thread
  was observed; no Shopify worker was started.
* **4 baseline control groups / 30 HTTP requests passed**: exact GET reproduces
  the original 401; HEAD/POST/PUT/PATCH/DELETE remain 401 without the internal
  key and 405 with it; trailing, suffix, case, double-slash and encoded-slash
  path variants remain 401; other Shopify/Salla/Meta protection remains the
  same. Existing OPTIONS remains 204. No baseline DB connection occurred.
* **12 candidate groups / 72 HTTP requests passed** against real
  **PostgreSQL 16.15**, server version `160015`. The disposable fixture creates
  actual Tenant/User tables and invokes the actual migration 0121 upgrade in
  an Alembic Operations context. It is not a production migration/version graph.
  Real `/start` with a signed merchant JWT persists the correct actor/session
  nonce despite a spoofed opposite tenant header. A correctly signed anonymous
  GET callback commits encrypted code and a hashed completion handle; replay
  refuses without replacing that code. Incorrect/missing/duplicate HMAC, stale
  timestamp and malformed shop reject before nonce consumption. Signed unknown
  state, expired state, wrong shop/redirect and revoked actor reject. Missing
  client secret fails closed without consuming state. Cross-tenant, different
  user and new-session `/complete` each return 403 `session_mismatch` before any
  provider call. Flags are restored off; both tenants' dormant routes refuse,
  and no shop connection or token grant exists.

All three reports share **643 unchanged non-Shopify route definitions**,
digest `17fed89fc8f585bfb49d1f24df15b4b961e4ae7678f2725421540e6702f4b10b`.
Both callback reports record zero network/process/unexpected-database attempts;
candidate uses exactly two connections to the named synthetic PostgreSQL fixture.
The callbacks use the real app/middleware/routers/session dependency, with no
dependency overrides, replacement routes or provider mocks.

## Evidence and review boundary

[Structured results](shopify-callback-service-key-results.json) contain the
three actual reports, complete job steps, source/image identities, shared
dependency layers and security-source hashes. Full decoded job log is retained
locally as `callback-final-114588288125.log`. The original saved file is preserved:
its UTF-8 bytes retain the leading BOM and include one additional terminal LF
introduced by the apply_patch capture. Its 323,650 bytes hash to
`5b630b50f5231d1f2e633b1fe96ae7bbd5fcd305efce5bd0d615683b0c5eed37`.
An exact fresh decoded download is 323,649 bytes, has the same BOM and one terminal
LF, and hashes to `2292949304e988b75c567b634976ca599f9c61a77d0476516648c14c42614e17`.
The saved file was verified byte-for-byte equal to that download plus one `0x0A`.

The canonical published digest now removes one leading UTF-8 BOM, normalizes
CRLF to LF, and preserves the download's remaining bytes and original terminal
newline without trimming or appending anything. These 323,646 bytes hash to
`eec4624c6057ad29860429446f14a9bcc951a151d493ccc4c39d4372a3d64293`.
The earlier label omitted the capture's extra LF/BOM; this is an evidence-only
representation correction, with no difference in log content, reports or source.
Local Python/YAML syntax, entire-module AST scope, diff check and trusted-base
GOV-002 passed. This workflow passed on its first run; no assertion was waived.

All credentials and generic merchant data are synthetic. Full HTTP proof has
no network; PostgreSQL proof uses an internal-only disposable Docker network
and rejects provider/process attempts, even if app code catches them. Lifespan
is not run, and accepted startup/essential guard suites are carried without
reruns. No provider token exchange, shop binding, live migration, deployment,
source pin, app permission/credential/settings change, merge or purchase occurred.

Independent review found no functional or security blocker and verified the
actual reports/source hashes; the log representation discrepancy is clarified
above. The earlier
[remaining rollout checklist](shopify-production-asgi-evidence.md) continues
to apply: after acceptance of this fix the demonstrated service-key callback
blocker is resolved in source; live source/build/backup/restore/deployment-window
approval, Shopify grant/configuration/activation and the actual fresh-merchant
dev-store journey remain unexecuted. No activation is authorized by this proof.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
