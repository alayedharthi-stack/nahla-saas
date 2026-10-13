# Private logical restore rehearsal — 2026-10-13

Latest result, 14:08 UTC: **PASS — private clone migration and rollback rehearsal**.
The owner explicitly approved the 146-file transfer at 13:55 UTC. The previously
denied upload was retried once and accepted. The original archive was restored
on the same private target without reconnecting the source database. The exact
reviewed harness and unchanged `a8fb88e3` observers, comparators and migrations
completed all six operations below.

| Operation actually executed | Verified heads | Shopify tables | Result |
| --- | --- | ---: | --- |
| `upgrade 0121` | `0111, 0113, 0121` | 5 | PASS |
| `upgrade 0122` | `0111, 0113, 0122` | 12 | PASS |
| `upgrade 0123` | `0111, 0113, 0123` | 15 | PASS |
| `downgrade 0123@-1` | `0111, 0113, 0122` | 12 | PASS |
| `downgrade 0122@-1` | `0111, 0113, 0121` | 5 | PASS |
| `downgrade 0121@-1` | `0111, 0113, 0120` | 0 | PASS |

Every step preserved the contents and row counts of all 127 original data
tables, all 117 original sequence values, and the definitions and relation OIDs
of all 128 original tables. `alembic_version` was checked separately against the
exact expected heads. Data equality used SHA256 of length-prefixed, sorted JSON
row records on the same clone; customer rows were not exported. All snapshots
also matched the same clone cluster, database, deployment and archive identity.

The pre-DDL baseline allowed only the eight independently reviewed array-cast
representations. Its SHA256 is
`e30b3ec8c8ac21d527b757f2c224755f3681a9c93ecb70ef783ecd5fd9a41268`.
The original production comparator still reports the expected
`FAIL / retained_definition_drift` against stock retained expectations on this
restored clone. **This is not a production-gate PASS.** No production guard,
expectations, inventory, schema, app deployment or Shopify flag was changed.

See the [execution record](clone-rehearsal-execution.json),
[byte-identical remote result](clone-rehearsal-result.json), and
[exact harness](clone_rehearsal.prepared-not-run.py). The harness keeps its
historical filename; these exact bytes were executed in this continuation.
The separate [Alembic environment](clone-alembic-env.py) runs the unchanged
revision directory without importing the production application environment.
The 146-file source inventory matched before and after execution.

PostgreSQL shut down at `14:08:10.813 UTC`; Railway reported zero running replicas
at `14:08:31 UTC`, with no pending work. The immutable archive, baseline and
private proof files remain on the existing volume. Runtime setup and rehearsal
used about seven capped compute minutes (CPU/RAM envelope `$0.004858`, not an
invoice). Retained volume storage remains billable within the owner's existing
`$1` total budget; permanent deletion is not authorized.

**Remaining production scope:** independently review the completed evidence,
then obtain separate, fresh, time-bound S3 authorization and an exclusion window
with source/inventory, production target, backup/recovery and rollback bindings.
Production migration and Shopify activation remain unauthorized.

The [09:27 pause record](clone-rehearsal-pause.json) is retained as history.
It also corrects the initial access-revocation claim: empty-value updates had
not removed source references from a fresh runtime. All five were subsequently
replaced by a fixed invalid sentinel, verified exactly in the current runtime,
and unset in every operator process before any database operation. No source
connection or credential display occurred during this continuation.

## Earlier logical archive capture and restore proof

The new logical archive was restored successfully on a private PostgreSQL 18.6
target. Source-snapshot row counts matched for 128 tables, Alembic heads remained
`0111, 0113, 0120`, and 117 restored sequences matched the values captured in the
archive. Locale/encoding also matched. The target was stopped at
`2026-10-13T08:38:17.224Z`; Railway reported zero running replicas at 08:38:35 UTC.
The retained archive volume was not deleted.

The paragraphs below describe the earlier 08:35 logical restore proof. At that
point migration rehearsal had not run; the successful continuation above now
supersedes that readiness state. Neither result grants production S3 approval.

## Retained artifact

| Field | Value |
| --- | --- |
| Kind | PostgreSQL custom-format logical archive |
| Snapshot | `2026-10-13T08:18:50Z` |
| Completed | `2026-10-13T08:18:55Z` |
| Verified | `2026-10-13T08:35:27Z` |
| Actual bytes | `26077103` |
| SHA256 | `5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c` |
| Project | `f0090862-0a40-4293-bd5d-e94df58762b5` |
| Environment | `ede962ce-3042-4dae-94de-623837e83ed9` |
| Source service | `b77b3d27-47b0-4a3a-83fd-44def66a3a84` |
| Source volume | `009bd0d5-85ed-4de4-99fc-94ea963c9d65` |
| Temporary target/storage service | `6afcbd3e-15dd-46c1-9183-932fe02d075f` |
| Retained storage volume | `2f25c7cf-f0b1-467b-bb98-e203ec40e6d9` |
| Private artifact path | `/recovery/shopify-logical-20261013/nahla.dump` |
| Manifest path | `/recovery/shopify-logical-20261013/backup.manifest.json` |
| Verification path | `/recovery/shopify-logical-20261013/verification.json` |
| Final checkpoint path | `/recovery/shopify-logical-20261013/checkpoint.json` |

The original archive remains byte-identical and mode `0400`. Its archive and
metadata live on the persistent `/recovery` volume; the restored database lived
in ephemeral `/tmp/nahla-shopify-restore-pgdata`. Stopping compute therefore did
not remove the independently restorable archive. This is a new artifact and
does **not** establish restoration of the earlier 647 MB Railway volume card.

Manifest SHA256:
`4a818d9cee54d6cf8511d0b4bb0099ef4a144a55161988129f545816d2ea7e5e`.
Verification SHA256:
`2b9611337d0916dca5d60a0606327f4081fccc9c9bd54a959097da2ffc0c41be`.

## What was actually checked

The source connection used TLS and a repeatable-read, read-only transaction.
Its exported snapshot was shared with `pg_dump`; table counts and schema were
captured from that same snapshot. The full custom archive was restored with
`pg_restore --exit-on-error --single-transaction --clean --if-exists` into the
new, empty, loopback-only target. Owner/ACL statements were not suppressed in
the actual archive restore. No extra source roles were present.

The first textual schema comparison stopped with a mismatch. All 33 changed
expressions were PostgreSQL's reparsing of varchar literal arrays cast to
`text[]` into arrays whose individual elements are cast to `text`. A narrow
comparison-only rewrite of exactly those 33 expressions made the entire schema
text byte-equal. All 33 original/transformed constant-array pairs also evaluated
equal on PostgreSQL. Constraint names, literal values and order, validity,
columns, and other schema text were preserved. The source and target schema
files, initial failed comparison, and later verification remain on the private
volume. The archive itself was never normalized or modified.

Sequence verification extracted `SEQUENCE SET` entries from the captured
archive and compared their `last_value` and `is_called` values against the
restored sequences. It did not compare sequences to later live source reads.
No cross-database OID equality was asserted.

## Release-gate compatibility requires review

The frozen release is `a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec`.
Its `evaluate_retained` compares constraint definitions as exact strings;
`normalize` only converts JSON shapes and does not canonicalize SQL.
The pinned retained expectations contain eight legacy array-coercion
expressions in these four tables:

- `commerce_runtime_deferred_inbound`: 4
- `commerce_runtime_handover_barrier`: 2
- `commerce_runtime_handover_workers`: 1
- `commerce_runtime_navigation_snapshots`: 1

A clearly labeled **static simulation**, seeded from the pinned expectations
and changing only those expression representations, returns
`FAIL / retained_definition_drift` for those four tables. This is a prediction
from the real comparator, not a live run of the release gate against the clone.
The logical restore is verified, but application/release-gate compatibility
after restoring it has not been established. Do not silently treat the
comparison normalization as a change to the release guard or as permission to
set an S3 execution record. The guard, expectations, production binding, and
frozen release source remain unchanged.

## Access, shutdown, cost, and retention

The target had no public domain or TCP proxy. PG18 and its dump/restore tools
were verified before adding temporary source references. After capture and
comparison, the five `SRC_PG*` references were replaced with empty values using
`skipDeploys`; stopping the container subsequently discarded the old runtime
values. There were zero `psql`, `pg_dump`, or `pg_restore` client processes at
the final checkpoint. A fast PostgreSQL shutdown was confirmed in provider
logs. SSH exited 137 as its container stopped; that transport exit is not a
failed restore. Railway's high-level `Online/SUCCESS` label remained stale,
while the replica count was zero.

The service retains `NEVER` restart, 1 vCPU/1 GB caps, and a 3300-second compute
timeout for any explicitly approved future restart. Approximately 47 minutes
of capped PostgreSQL runtime has a resource-envelope estimate of USD0.032618.
At full 1 GB capacity, retained storage is about USD0.005/day or USD0.15 for
30 days at the verified [Railway rates](https://docs.railway.com/pricing/plans#resource-usage-pricing).
These are estimates, not a billing receipt; initialization and transport usage
were not separately metered here. Compute is stopped. Track retained storage
against the owner's USD1 total cap.

Preserve this original archive through migration/S3 verification and the
recovery window. There is no automatic one-hour archive deletion. Permanent
cleanup requires separate explicit owner approval.

## Review material

`report.json` contains the safe machine-readable result and the static gate
simulation. The three accompanying shell files are the actual executed
operator code, with hashes in that report. They contain no credentials or
dump contents. Raw archives, raw schema/data files, and private command errors
remain solely on the private Railway volume.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
