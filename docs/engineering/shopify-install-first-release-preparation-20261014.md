# Draft Shopify install-first release preparation

This source is Draft-only. It does not authorize deployment, migration, live
flag changes, credentials, Shopify app configuration or a live store test.
Production deployment `ade73b62-6127-4aff-bc3b-c0fd97d06c58` at
`a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec` remains a separate dormant S3 release.

## Closed post-0124 states

`S4_DORMANT` and `S4_PILOT` both require the exact version multiset
`0111 / 0113 / 0124` and seventeen Shopify tables. Their additional tables are
`shopify_owner_identities` and `shopify_install_flows`. S0 and S3 keep their
existing contracts. Google 0125, combined sibling states, falsely stamped S3,
intermediate phases and definition drift are refused. Normal bootstrap stays
at 0093; no `head` or `heads` upgrade is permitted.

The source-bound canonical inventory covers the gate, migration graph,
retained definitions, install configuration/admission, acquisition routes,
install implementation and the separate S4 expectations. Candidate definitions
must come from disposable PostgreSQL 16 and 18 and agree before adoption.
The fifteen existing Shopify definitions and retained relation definitions
must equal the committed S3 pins. An incomplete S4 expectations file refuses
the release before connecting to a database.

## Pilot and legacy acquisition

S4_PILOT requires only the connection and install-first server flags ON. Every
catalog, privacy and dashboard build flag checked by the API gate stays OFF.
Admission contains only `nahlah-dev.myshopify.com`, a shop-bound private-code
hash and a whole-second UTC deadline within seven days. Hashes/codes are not
printed. Owner identity still comes from Shopify's verified online grant.
Same shop GID and owner reuse the tenant; changed owner/domain fail closed.
Existing TOTP stays mandatory, new passwordless MFA enrollment stays disabled,
and the owner-proof session lasts no more than five minutes. Existing session
switching still requires explicit consent.

Whenever the install-first flag is ON, legacy JWT `/shopify/start`,
`/shopify/callback` and `/shopify/complete` refuse server-side before their
configuration, authentication and storage work. Missing admission/configuration
does not reopen them. Whitespace flag parsing is consistent across the release
gate, install-first runtime and legacy guard. Status, disconnect and uninstall
retain their existing connection-gated behavior.

## Schema-retaining rollback

`scripts/operators/shopify_install_rollback.draft.json` specifies S4_DORMANT,
all Shopify flags OFF and preservation of 0124. A future rollback must use an
exact independently reviewed 0124-compatible source/inventory and newly
approved, phase-bound bootstrap command. This Draft source supports that
dormant state. The old a8fb/S3 release is incompatible after 0124. Downgrading
0124 would erase owner/flow evidence and is not the rollback strategy.

The disposable rehearsal upgrades the named 0124 revision, checks dormant and
restricted-active contracts, turns flags OFF while retaining schema and owner/
flow rows, compares relation definitions/OIDs, rejects owner-definition drift
and refuses Google-only/combined version rows. It never uses live credentials
or the production database.

## Evidence and future authorization

Final readiness requires successful exact-head protocol/browser, release-gate,
retained-schema and sibling regression evidence, zero unexpected skips, the
canonical S4 capture and independent source/artifact review. A failed or partial
run is not release approval. Salla, Meta/WhatsApp and AI behavior are outside
the implementation changes.

Only after this Draft preparation passes, the smallest future authorization
bundle would bind the exact reviewed source/inventory, migration 0124 only,
the reviewed schema-compatible dormant rollback, existing-target deployment,
and a bounded owner-only development-shop pilot. Callback/scope configuration
and credential handling require separate explicit authorization. No old
approval record is refreshed or reused by this work.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
