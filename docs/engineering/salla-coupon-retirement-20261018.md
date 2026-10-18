# Explicit coupon quarantine and rollback contract

Clearing provider IDs alone leaves a code collision and permits the old importer
to rebind history. Migration0125 adds an immutable private audit and persistent
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

The primitive requires PostgreSQL0125, verified binding, distinct explicit
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
After commit, application rollback MUST retain0125 and its DB fence. Downgrade
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
Independent final-head review, Shopify157 byte verification, full runtime CI,
release gates and separate data-transaction approval remain prerequisites.
No merge or deployment is authorized. Coupon webhook handling and missing UI
edit/creation surfaces remain the separately documented acceptance gaps.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
