# Salla coupon case identity repair

## Observed first divergence

On the verified demo tenant1/store22825873, Nahla created `NHLT2610182025`
(provider1847319598). Salla's UI edit from1% to2% returned a lowercase code.
The tenant/code exact lookup inserted a lowercase import instead of updating
the bound manual row. The original status worker then rejected the detail
code's case difference and correctly reported failed synchronization. The
provider coupon was disabled from Salla; the original local row remains stale.
No production records are changed by this PR.

## Final change

The coupon importer resolves provider ID within a verified tenant/integration/
store binding, accepts ASCII-case-only code variations, preserves the local
code and origin/allocations/merchant metadata, and records the provider spelling
separately. Existing coupon term updates keep their prior ownership behavior.
The active-state worker retains exact provider ID and binding checks, allowing
the same limited spelling equivalence before and after provider confirmation.

Unbound historical records require exactly one enabled Salla merchant-store
scope for their tenant before adoption. Same normalized code with another ID,
another store's binding, conflicting ID aliases, real renames, and multiple
local records are blocked. Imports report a partial identity conflict rather
than claiming full success. A tenant row lock serializes concurrent missing-row
imports; this adds transaction serialization within a tenant. No index or
schema migration is introduced. Queries by provider metadata/case alias may
scan the tenant coupon catalog; full-catalog performance remains a rollout
consideration. Recent polling is unchanged.

ASCII normalization deliberately does not collapse Unicode case expansions,
lookalikes, whitespace, punctuation, or accents. A provider-ID match does not
authorize transferring a row between merchants or stores. Ambiguous existing
duplicates remain blocked rather than being automatically merged.

The branch is based on deployed composite `efb8f7a7d7c2fabe876f71cfff17bdaa8a87ccd6`.
Only coupon sections of store_sync.py change; Shopify code, product deletion
and row-lock protections, migrations0121-0124, release gates and secrets remain
unchanged. This PR is review-only; no merge or deployment is authorized here.

## Evidence and test boundaries

The create→case-edit regression fails on the prior runtime with two rows.
Tests cover create/import, upper/lower/mixed replay, term updates, origin and
allocation preservation, disable/re-enable, wrong tenant/store/integration,
conflicting provider IDs, ambiguous legacy records and existing duplicates.
Actual local PostgreSQL tests exercise two concurrent import transactions and
the existing two-worker status dispatch proof; providers are fakes and external
network is blocked. This does not claim every application interleaving is proven.

Actual queue persistence and dispatcher replay tests cover both salla channels.
`coupon.updated` currently has no coupon-state handler in `_dispatch_salla`:
it is acknowledged as unhandled without changing coupons. Thus queue replay
can be harmless, but webhook-driven coupon synchronization is NOT implemented
or proven by this patch. Poll payload replay is a separate tested path. No new
webhook subscriptions or routing are added.

## Safe reconciliation proposal — not an executable production repair

1. Read only the authorized tenant/store/provider ID. Inventory both local row
   IDs, complete metadata, allocation/rule/usage references and pending intents;
   read current provider detail. Produce a reviewable snapshot and digest.
2. Stop if either binding, provider ID, code equivalence or ownership differs;
   never choose a canonical record merely by lowest/highest ID. Preserve the
   original authored row only after authorship/references are proven.
3. Keep the provider inactive during a separately approved repair. Lock the
   tenant and both local rows in one transaction; re-check snapshot and provider
   identity before applying any reviewed local changes.
4. Preserve all original values and metadata in an audit snapshot. Plan to
   retain the duplicate as an inactive historical alias, not delete it. Readers,
   allocation, retry and identity resolution must explicitly recognize that
   retired alias before any operational binding is moved. That alias handling
   and executable repair are deliberately NOT silently introduced by this PR.
5. Only after separate review may canonical terms and inactive provider truth
   be reconciled, compatible pending intents settled, and the duplicate's live
   binding retired while retaining its historical binding and all references.
   Conflicting intents/allocations require owner resolution, not auto-merge.
6. Re-read both systems; prove one operational identity, retained history,
   no delivery/message side effects, and reversible local audit changes. Then
   rerun create/edit/disable/enable/replay/isolation on the exact approved release.

## UI limitations, separate from this identity defect

Salla's new-coupon screen exposed disabled templates for this demo session.
That blocks this browser's Salla-origin creation test; it does not establish
that Salla lacks coupon creation. A known supported UI/account path must be
restored before that direction can be accepted; no force-click or hidden API
was used. Salla's existing-coupon value editor did work.

Nahla supports value entry at creation and state toggles on issued coupons,
but its issued-coupon UI exposes no value/date editor. Therefore post-creation
value editing from Nahla is a missing product surface for the requested full
bidirectional edit capability, not a failure of the tested state toggle. This
PR does not invent that editor, advertise full value synchronization, or alter
automation rules as a substitute.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
