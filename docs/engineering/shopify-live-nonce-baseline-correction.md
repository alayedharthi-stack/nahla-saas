# Existing adopted nonce baseline correction

Draft PR: https://github.com/alayedharthi-stack/nahla-saas/pull/1220

The production read-only canonical comparison found exactly one mismatch:
`whatsapp_oauth_nonces.created_at` has no server default, while the prior
synthetic snapshot expected `CURRENT_TIMESTAMP`. The six other retained full
definitions, nonce types/nullability/constraints/indexes, sequence ownership,
tenant/user key types and relation identities match. PostgreSQL 18 NOT NULL
catalog rows are already excluded; this difference is an actual absent default.

First divergence: the retained expectation was captured from a freshly
migrated fixture rather than the existing adopted nonce definition.
Source of truth: exact production PostgreSQL catalog metadata, read in a bounded
READ ONLY transaction through the existing supported Railway relay.
Provenance: deployed source `462ede1aff602cad84e9569be859cca0087b190d`, existing
production project/environment/API/private DB as bound in the regenerated
bundle; no customer rows, provider secrets or unrelated browser tabs were read.
Evidence: old comparator output `retained_definition_drift` only for the nonce
relation; sole canonical difference `retained.nonce.columns[7][3]`, expected
`CURRENT_TIMESTAMP`, observed `null`. Canonical live retained digest was
`d4a6915c412b91a17f1d743c8fd53c8d6e09a46402124099505420fc39a2d867`.

The correction changes that one JSON expectation to `null`. The gate,
`normalize`, `evaluate_retained`, observer SQL, ORM model, nonce writer and all
migrations remain unchanged. This is an exact existing-schema pin, not an
allowlist of defaults. Added `CURRENT_TIMESTAMP`, `now()` or any other default
fails the same comparator. Other constraint/index/type/nullability/identity
mutations remain covered.

Migration 0103 gives newly created tables a server default, but does not require
it when adopting a compatible existing table. The unchanged ORM uses a Python
timestamp default and the unchanged raw SQL writer explicitly inserts
`created_at`. Shopify state is stored separately in `shopify_oauth_states`.
The actual historical creation path of the production nonce table is not
asserted. No production ALTER is needed or performed by this correction.

The synthetic gate builder now creates a compatible no-default nonce table
before 0103 adopts it. Creation is conditional on absence; calling the builder
again never drops or rewrites an existing fixture's default. The focused
sequential case creates one disposable database, preserves the nonce OID
through adoption, then carries its retained definitions, identities and nonce
rows through deployed S0 startup, candidate S0 startup, genuine 0121/0122/0123
upgrades and two candidate S3 startups. No fixture reset occurs between steps.
Real inherited nonce insertion/consumption functions run with a synthetic HMAC
key and disposable sessions; tenant, mode, invalid mode, redirect, expiry and
replay refusals remain enforced.

Candidate startup is strictly error-free. The immutable legacy 462 control has
the two already-known `whatsapp_usage.conversations_used` missing-column
warnings, repaired in accepted PR1216. This focused control requires those exact
two statement prefixes and UndefinedColumn classification and fails on any
additional error. It neither hides them nor changes legacy/candidate source.
All startup probes retain provider network refusal, zero provider client calls
and zero scheduler registrations.

Only five selected tests run on pinned PostgreSQL 18.6 and Python 3.11.17:
two database-free expectation/drift cases, retained PostgreSQL drift mutation
and recovery, complete adopted S0/S3 snapshot equivalence, and the sequential
adoption/startup/nonce proof. The full prior application/ASGI suites are carried.
Local database-free results were 2 passed; local Docker was unavailable.
Detailed final CI results and superseded failures are recorded in the companion
`shopify-live-nonce-baseline-results.json`.

The regenerated dormant bundle binds the corrected release, canonical Git-byte
inventory, unfilled approval-record bytes and command/patch previews together.
The unchanged strict renderer refuses those unfilled records. This does not
discard the parent's earlier owner merge/deploy authorization; the parent
reconciles it against the exact reviewed corrected release before completing
the out-of-repository execution record. Independent review precedes any live
execution. S0 requires no migration, restored clone or S3 prerequisite.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
