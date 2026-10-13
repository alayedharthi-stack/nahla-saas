# Production S3 execution evidence — 2026-10-13

**Database S3 is complete and verified. The new API validation deployment failed
closed because its approval had expired. Production startup on the new deployment
is not proven.** The prior API deployment remains online; Shopify stays disabled.
This directory is historical evidence, not permission to execute its expired record.

## Result and chronology

The owner approved a window from **15:37:12 to 16:07:12 UTC**, with new forward
steps stopping by 15:57:12. Source remained
`a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec`, inventory 146 files,
SHA256 `31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174`.
The reviewed execution package was commit
`d4e7a5bb2db3b2e519e748b6cf97e99387fcc5a9`; it is preserved unchanged.

| Event | UTC | Evidence |
| --- | --- | --- |
| Fresh S0 baseline | 15:39:54.359744 | `execution-s0-baseline.json` |
| 0121 completed, exit 0 | 15:42:05.277825 | `upgrade-0121.json` |
| S1 phase audit PASS | 15:42:19.829146 | S1 timestamped audit |
| 0122 completed, exit 0 | 15:42:42.389154 | `upgrade-0122.json` |
| S2 phase audit PASS | 15:43:02 | S2 timestamped audit |
| 0123 completed, exit 0 | 15:44:19.678288 | `upgrade-0123.json` |
| S3 phase audit PASS | 15:47:57.627539 | S3 timestamped audit |
| Four read-only integrity/privacy audits PASS | 15:48:17.429819 | `s3-audits.json` |
| Exact signed one-off S3 gate PASS, 19 checks | 15:48:56.414833 | `s3-gate.json`, `oneoff-s3-gate.log` |
| Exact two-field API patch staged | 15:49:22.507 | Native patch `088c8e81-4c21-4e8e-b7ae-7036b0a6ab0f` |
| Native accept-deploy request submitted | approximately 15:50 | Platform confirmation remained pending; exact request timestamp was not captured |
| Owner window expired | 16:07:12 | Expired approval record |
| Provider created deployment | 16:07:41.999 | `terminal-deployments.json` |
| Read-only S3 audit still PASS | 16:08:45.805159 | Post-window S3 timestamped audit |
| Deployment entered BUILDING | 16:12:06.715 | Native deployment observation |
| Pre-deploy gate refused expired approval | 16:15:16.741214190 | `terminal-gate-logs.json` |
| Deployment terminal FAILED | 16:15:28.182 | `terminal-deployments.json` |
| API and dashboard HTTP 200 | 16:17:12.816788 | Post-terminal public health observation |

Deployment `7f45fdf1-368b-453d-a9c5-1ca6ad08b7c0` was therefore created
**after** the owner window expired, following a tool request made before expiry.
The evidence does not establish when the provider received the request relative
to the native confirmation. It must not be described as a deployment executed
within the window. The final gate reported `approval_expired`, skipped database
checks, and returned `RESULT=FAIL`; there is no successful new application startup.

Native approval card reference reported by the parent:
`manager:1cdbb1bd5b984c1ba2c0b1bff7bfcd77:1`.
No further production mutation was initiated after that delayed call returned.

## Actual production state

- DB heads: `0111`, `0113`, `0123`; 15 Shopify tables, all empty.
- Stock retained definitions and all 16 protected table definitions/OIDs match
  the immutable S0 baseline. Live business traffic continued; protected row-value
  equality across that traffic is not claimed.
- All five backend Shopify flags remained OFF. Both dashboard Shopify flags
  were verified OFF before the window; the dashboard was not redeployed or changed.
- Prior API deployment `67f2a118-5733-41c8-90fc-a25bee392022` remains SUCCESS,
  with one running replica and zero crashed replicas after the failed attempt.
- Dashboard deployment `49499c61-c777-4c62-a863-5104c84d7462` remains SUCCESS.
  Both services use the same `a8fb88e3` release.
- The S3 pre-deploy command and sealed approval variable were committed to the
  API configuration. No changes remain staged. That approval is now expired;
  it cannot authorize a future deployment.
- No database rollback, archive restore, Shopify activation, dashboard deploy,
  source switch, or unrelated configuration change occurred.

The attempted cancellation was limited to capability discovery. The connector
offered no cancellation method. Railway CLI 4.36.1 `down` selects the latest
**SUCCESS** deployment, which would have targeted the healthy predecessor, so it
was not executed. The parent subsequently observed BUILDING and kept the earlier
queued-only cancellation scope unchanged. No cancellation mutation occurred.
See [official CLI implementation](https://raw.githubusercontent.com/railwayapp/cli/v4.36.1/src/commands/down.rs).

## What was validated

Executed in production: exact per-step topology checks and three named migrations,
S0/S1/S2/S3 read-only phase audits, Shopify catalog collision audit, Shopify privacy
audit, product identity audit, active Meta item-binding audit, exact signed one-off
S3 gate, public health/asset checks, and native deployment/log inspection.

The failed deployment itself validated bootstrap, frozen source/inventory,
libraries, migration graph, service/database identity, and rejection of expired
approval. It did not complete database checks or application startup. Authenticated
browser end-to-end merchant installation and live Shopify sync were not run.

The historical approval's `fresh_preflight.observed_at_utc` retained the candidate
timestamp 14:30:35; its baseline digest is the actual 15:39:54 execution baseline.
The immutable timestamped audit is the source of truth for the fresh check.
The expired approval bytes are retained exactly and have not been rewritten.

## Minimal remaining approval — validation only

Request a fresh **30-minute** owner window while the owner is available for the
native confirmation. Scope: repeat read-only S3/health/source/flags checks; create
a fresh S3 validation-only approval record and matching command; replace only
the API `NAHLA_SHOPIFY_RELEASE_APPROVAL_B64` and `deploy.preDeployCommand`; commit
one same-source API validation deployment; inspect terminal success, actual
startup/gate logs, health, S3 state and flags.

The new record must explicitly exclude DDL, branch downgrade, archive restore,
dashboard deployment, Shopify activation and unrelated changes. Do not reuse the
old DDL/rollback finalizer as authority for this smaller scope. Keep the source
pinned and Shopify OFF; do not rerun 0121, 0122 or 0123. Confirm no overlapping
deploy or schema changes for the new window. A late native approval does not extend
the window. Failure or expiry means stop and report, not automatic redeployment,
database rollback, a guard bypass or restoration.

## Evidence integrity

`manifest.json` binds the published files, expired S3 record, command hashes,
reviewed source and runbook. Raw command output and diagnostic samples remain
private; only allowlisted operational metadata and closed gate log lines are
published. `execution-outcome-20261013T161015.json` is an earlier preserved snapshot
when the deployment was QUEUED; this report and terminal files supersede that
snapshot's pending-deployment status.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
