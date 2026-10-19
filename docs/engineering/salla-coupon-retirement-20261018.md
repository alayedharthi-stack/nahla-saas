# Explicit coupon quarantine and rollback contract

## Current compatibility completion

Salla uses unapplied revision0126 with parent0124. Revision0125 is reserved for
the external SHA-pinned Google sibling and is never admitted to a production
profile. SQL/trigger bodies of the original quarantine migration are preserved.
Bootstrap remains exactly0093. Source topology is exactly
0092/0111/0114/0115/0126; historical topologies remain explicit, unknown heads
and the Google-combined graph remain rejected by the production gate.

The named S4_COUPON_PRE_DORMANT/PILOT profiles require database0111/0113/0124
and the closed pre-quarantine observation. S4_COUPON_POST_DORMANT/PILOT require
0111/0113/0126 and the closed post observation. Shopify's own branch revision
stays0124 and its17 definitions/retained data remain unchanged. No profile
automatically applies a migration. Existing flag, identity, backup/restore,
age, target, source, inventory and independent approval checks still apply.

Closure includes every quarantine table column, key, constraint and index;
all owned relations/types; full function bodies, arguments, language, owner
relationship, security/volatility/parallel/strict/leak/config/ACL properties;
all user triggers on coupons and their referenced functions even without a
retirement prefix; exact binding/enabled state; coupons/audit RLS and policies.
Missing, disabled, changed, extra or unknown objects fail. Read-only checks
observe again after the transaction boundary and retain table OID checks.

Post0126 functional rollback is ONLY the tested alias-aware same-release SHA,
with its exact runtime inventory, actual verified deployment UUID, fresh
post0126 schema observation digest, all flags OFF and an independently reviewed
zero-failure/zero-skip JUnit digest proving that an ordinary+canonical batch
commits while retired history/audit stay unchanged. The actual final bridge
SHA is bound externally by exact-head CI and the release approval; no synthetic
fixture UUID is a production rollback. A4 and efb8 are rejected for POST.
The old efb8 containment behavior documented below is a negative proof only.

The historical S4 pre0126 prior-source contract retains its exact reviewed
source/inventory pin and original source-schema file digest. Its compatibility
report additionally binds the fresh current schema-file digest and the equal
retained+17-definition digest. These explicit separate fields avoid claiming
an old source carried the new migration-file digest. They are never accepted
for POST0126. Drift in either proof is refused.

The canonical source inventory is regenerated (currently167 entries), including
the alias-aware coupon runtime closure and new read-only schema observer.
The earlier157-byte-identical claim is superseded for the necessary contract,
expectation and inventory edits. Shopify application/UI source and existing
migrations0121-0124 remain unchanged; actual disposable pre/post observations
verify its17 definitions, rather than reusing an old database digest.

Release order, each operation separately authorized: reviewed bridge source and
closed PRE profile; deploy/verify that actual bridge; manual0126 with empty
audit; verify closed POST profile and compatible rollback evidence; only then
fresh fenced CAS reconciliation. No deploy, migration, reconciliation, provider
write, approval token or production rollback is performed by these tests.

Clearing provider IDs alone leaves a code collision and permits the old importer
to rebind history. Migration0126 adds an immutable private audit and persistent
database write fence. No Shopify table, previous migration0121-0124, router,
release gate, flag or secret changes. No production repair is executed.

## Reviewed transaction

`reconcile_duplicate` is a library primitive with no API, CLI, scheduler or auto
invocation. Its caller owns the maintenance fence, provider GET and transaction.

1. Prove canonical authorship from the creation evidence, not ID ordering.
   Inventory coupon rules, offer decisions, campaigns/revisions, customer/event
   and allocation metadata and free-text references. Preserve references;
   require zero unresolved conflicting assignments/intents. This inventory
   remains an operator review prerequisite, not a claim of automatic scanning.
2. Establish a bounded maintenance fence for mutations, polling and retries.
   GET the exact approved provider ID in the verified store. Verify case-only
   code equivalence, inactive state and percentage terms. No PUT or message.
3. In one transaction, validate integration, lock tenant then explicit row IDs
   in ascending order. Re-read full before-images and compare the reviewed
   digest. Retry/poll drift aborts. Inventory must name both IDs and this digest,
   carry a review reference and maintenance-fence reference, and bind a matching
   provider payload observed within30seconds. References are attestations;
   the primitive does not itself implement a global maintenance lock.
4. Preserve canonical ID/code/source/rules/allocations/usage/custom fields.
   Reconcile reviewed percentage terms and verified inactive status. Settle
   only desired=false for the same integration/store; preserve full pending
   attempts in audit. Contrary intent or third identity aborts.
5. Retain imported duplicate ID/code/source/history/references. Remove only
   operational provider aliases/pending work; mark inactive/non-allocatable.
   `salla_retirement` records original code, provider binding and canonical link.
   Flush both changes then insert full immutable private before/after images,
   inventory and provider observation atomically. Caller commits or rolls back;
   the primitive never commits or contacts a provider. Publish digests only.
6. Re-read DB/provider and repeat acceptance. Exactly one operational identity,
   no deleted/moved references, no messages or unrelated records affected.

The primitive requires PostgreSQL0126, verified binding, distinct explicit
manual/imported rows, matching provider aliases/code and no contrary intent.
No ID10505/10506, tenant1 or provider1847319598 is hardcoded in runtime.

## Persistent quarantine

Importer/duplicate detection ignore retired rows; dispatch/retries skip them;
queue, push and native allocation reject them. DB triggers reject EVERY UPDATE
or DELETE of a retired row and reserve the tenant/code alias and provider
identity against recreation. Audit UPDATE/DELETE is also rejected. Historical
rows remain readable. Existing API edits/deletes reaching the DB fail instead
of changing history; this PR adds no history editor or router change.

## Rollback

Before commit, transaction rollback restores rows and removes staged audit/fence.
After commit, application rollback MUST retain0126 and its DB fence. Downgrade
refuses while any retirement exists. Tests execute the actual pinned efb8 import
method: lower/mixed-case payloads fail closed instead of rebinding/recreating.
This protects data; it DOES NOT restore old-version coupon availability. The
old polling transaction may fail and delay all its coupons. Keep coupon writes,
polling and retries fenced and roll forward to compatible source. Do not claim
efb8 understands the alias protocol or resume its coupon writers without review.

Retirement is never automatically undone. Later canonical data corrections
require fresh provider truth and a separate approved CAS transaction. Never
restore stale active=true, remove the persistent fence, or overwrite intervening
real use merely because before-images exist. The rollback is containment,
not functional recovery. All original attempts remain private immutable audit.

## Proof and release

Actual PG tests cover transaction rollback, immutable history, old-source
import, activation/delete/recreation, retries/push/allocation, identity isolation
and import/dispatch locks. Existing source tests remain. CI includes this
composite base in a separate full root-runtime workflow and adds exact-head PG16/18 jobs
with zero-skip JUnit checks. Local PG17 is not a substitute for those jobs.
Independent final-head review, canonical inventory verification, full runtime CI,
release gates and separate data-transaction approval remain prerequisites.
No merge or deployment is authorized. Coupon webhook handling and missing UI
edit/creation surfaces remain the separately documented acceptance gaps.

## Audit-integrity source repair (2026-10-19)

Reconciliation now requires both an INSERT RETURNING receipt and a locked
read-back matching every audit and identity field. The two coupon changes and
audit verification share a savepoint; failure rolls back this repair while the
caller retains ownership of the outer transaction. The schema observer also
includes rewrite-rule definitions and enabled states; both profiles require
their absence on the protected ownership surface.

This follow-up is reviewed with ordinary unit tests using in-memory results.
It does not repeat the prohibited database security reproduction. The new empty
rules requirement is source-defined, not a claim of a fresh database capture.
No production safety, successful live integration, or permission to reconcile
data follows from unit or CI success alone. Existing database and rollout
requirements remain subject to the explicitly allowed review scope.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
