# Salla coupon active-state synchronization: test evidence

Date: 2026-10-17. Draft change only; not deployed.

## First divergence and source of truth

The live test isolated two persistence/execution failures, before any AI layer:

* Salla list normalization produced an active value, but import/reconciliation did not persist it.
* Nahla PATCH changed metadata.active without any provider update; historical creation evidence still displayed as synchronized.

Salla coupon details in the bound merchant connection are the authoritative remote state. A dashboard request is an intent, not confirmation of a remote write. The provider update contract is documented at https://docs.salla.dev/coupon/update (PUT /coupons/{coupon_id}, status active/inactive). Acceptance of a status-only payload must still be verified on the demo merchant after an approved deployment.

## Narrow change

Persist a tenant/integration/store-bound pending status intent before dispatch. Hold the coupon row lock during dispatch; read details and verify provider ID/code before writing; PUT only status; read details again before declaring success. Keep the last confirmed local active state on errors and expose pending/failed. Retry due intents through the existing coupon poller with bounded backoff, GET-before-PUT recovery, fair due ordering, and a 20-operation batch cap. A failed status update cannot fall into the coupon-create push path.

Import active/inactive for both imported and Nahla-origin coupons without changing their origin. Retain pending intents during polling. Ignore older source versions and observations started before the last local confirmation. For conflicting equal-resolution source timestamps, fetch current provider details; ambiguous/failed reconciliation remains failed. Unversioned snapshots are protected by poll start time and pending intent; this is not a claim that arbitrary unversioned external events can be totally ordered.

No AI, prompts, persona, Shopify, authorization scopes, secrets, permanent-delete behavior, value editor, or date/scheduling behavior changed.

## Live observations before the fix

Only the verified demo store 22825873, tenant 1, integration 3 was used. Neither the separate demo tenant 35 nor the real tenant 33 was modified. Both test coupons were verified inactive in both systems at 2026-10-17T17:32:21.099Z and were left that way. No later browser writes were made for this fix.

| Test | Result before fix | Evidence / timing |
| --- | --- | --- |
| Nahla coupon creation to Salla | PASS | NHLT2610171722, provider 95935710; API POST succeeded 2026-10-17T17:21:42.314Z |
| Salla coupon creation to Nahla | PASS | SALT2610171725, provider 95935748; poll recorded created=1 at 17:27:46.852Z after submit 17:27:43.063Z |
| Salla value edit to Nahla, no duplicate | PASS | 1% to 2%; opposite UI observed within 15.177s |
| Nahla disable to Salla | FAIL | Source remained enabled after fresh opposite read, 70.143s observation bound |
| Salla disable to Nahla | FAIL | Source disabled while Nahla active after repeated successful polls, 59.861s bound |
| Final cleanup | PASS | Both coupons inactive on both sides; one row per code per system |
| Source expiry/start-time fidelity | OPEN, separate scope | Early activation display and expiry timezone mismatch require raw-source verification |

UI observation bounds include navigation/reload/filter delays and are not synchronization SLA measurements. Detailed sanitized chronology is in the local salla-live-coupon-results-20261017.json evidence file. The pre-fix deployed commit was 7f9ea3a48b002e77bf7676d1e6b92f73e25120cd; this isolated PR is based on main b7d2d6811911a80a597992e0de0c6a833c2c9e12 to exclude the concurrent Shopify branch.

## Automated validation of the proposed fix

| Check | Result | Practical limit |
| --- | --- | --- |
| Coupon regression set: normalization, pending/failure, disable/re-enable, retry/replay, stale/duplicate/equal-timestamp observations, tenant/store/provider identity, fair retry, validation rollback, adapter status-only payload | 93 PASS, 0 skip; 13.83s | SQLite and fake Salla; no live API mutation |
| Real PostgreSQL concurrent dispatch | 1 PASS, 0 skip; 2.44s | PostgreSQL 17, two independent sessions, SKIP LOCKED and one PUT; does not prove every router/import interleaving |
| Dashboard TypeScript | PASS | tsc --noEmit; no browser acceptance of unpublished UI |
| Independent review | PASS after fixes | Three initial findings addressed: mutating picker commit, retry starvation, equal-second conflict; reviewer did not rerun tests |
| Live regression after fix | BLOCKED / not attempted | Explicit no-deploy instruction; requires approved deployment then demo-store retest |
| Orders, COD confirmation and WhatsApp | BLOCKED | Dedicated authorized test phone not established; no messages/orders sent |

Commands (from repository root, with an isolated test environment):

```powershell
python -m pytest tests/test_coupon_active_sync.py tests/test_coupon_salla_two_way_sync.py tests/test_salla_coupons_poller.py tests/test_salla_coupon_fetch.py tests/test_salla_coupon_discount_normalise.py tests/test_coupon_sync_visibility.py tests/test_coupon_remote_compensation.py -q
# Disposable PostgreSQL DB / schema only, with A1_PG_TEST_DATABASE_URL set:
python -m pytest tests/test_coupon_active_sync_pg.py -q
# Fail instead of skip when PG coverage is required:
# A1_PG_INTEGRATION_REQUIRED=1
cd dashboard
node node_modules/typescript/bin/tsc --noEmit
```

Local tests disabled dotenv and blocked external Python network/DB access; one initialization connection attempt was blocked in the coupon regression run. The PostgreSQL race run allowed only the disposable loopback DB and reported zero blocked attempts. No real Redis claim is made. The disposable cluster was stopped after the test; the existing PostgreSQL service was not changed.

## Latency evidence and live retest

Metadata records observation/apply timestamps and pre-commit apply durations. The coupon_active_sync_committed log is emitted only after DB commit and reports api_to_db_commit_ms (detail/PUT/detail through commit) and request_to_db_commit_ms (including retry delay). Source-to-apply duration is recorded only when the provider supplies a usable timezone; a timezone-free string is not treated as UTC latency evidence. Existing poll completion duration includes fetch and commit, not event delivery latency. No coupon webhook timestamp/SLA is invented.

After approval to deploy: use only the already verified demo connection; disable and re-enable each direction, then leave both coupons disabled; verify one remote/local row per code; force a controlled provider failure in a test environment and recover by replaying the same intent; observe commit logs and provider timestamps. Re-run the coupon suite and PG proof. Before any order/message test, obtain the exact dedicated authorized test phone and preserve the no-payment/no-shipment boundary. Confirm the user's requested COD status names against the store's actual statuses before asserting the transition.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
