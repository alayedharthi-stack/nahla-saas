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

S4_PILOT requires the connection and install-first server flags ON. The only
other flag it may add is the read-only catalog import, bounded to the pilot
shop (see "Pilot catalog import" below). Every privacy and dashboard build
flag checked by the API gate stays OFF.
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

## Pilot catalog import (bounded)

S4_PILOT may set `NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED` only together with
`NAHLA_SHOPIFY_CATALOG_IMPORT_SHOPS` =
`{"nahlah-dev.myshopify.com": {"expires_at": "YYYY-MM-DDTHH:MM:SSZ"}}`: exactly
that shop, one field, a whole-second UTC deadline in the future and at most
seven days ahead. Anything else (another or second shop, missing/extra
fields, a malformed or far deadline, any other Shopify flag) refuses the
release with `catalog_import_bound_invalid` or `shopify_flag_enabled` before
a database connection. S0, S3 and S4_DORMANT (the pilot's rollback) keep the
import OFF.

The runtime enforces the same bound (`services/shopify_catalog/config.py`,
`import_shop_allowed`). While the install-first switch is ON the bound is
mandatory: absent, empty or malformed, it admits no shop. A start for a shop
outside it answers 403 `shop_not_admitted` before any database statement.

Import limits under the pilot:

* shop: `nahlah-dev.myshopify.com` only, and only while its connection is
  ready (active with this generation's uninstall-subscription proof);
* time: runs start only before the bound's deadline (≤ 7 days); a run
  already started may finish within its existing 6-hour run deadline;
* data: read-only Admin GraphQL at the pinned `2026-10`, scope
  `read_products` only (inventory levels are not read without
  `read_inventory`); it writes only the provider-scoped `shopify_catalog_*`
  mirror — never the shared catalog, Salla, Meta, WhatsApp or the AI runtime;
* size: the existing caps — ≤ 25,000 products, ≤ 2,048 variants per product,
  ≤ 200,000 variants, ≤ 5,000 locations, ≤ 1,000,000 inventory levels,
  8 MiB per response, bounded retries — beyond them the run fails closed;
* concurrency: one running import per connection.

The dashboard's `VITE_SHOPIFY_CATALOG_IMPORT_UI` is not enabled by this
contract and stays an owner build decision for the separate frontend service.

## Schema-retaining rollback

`scripts/operators/shopify_install_rollback.draft.json` specifies S4_DORMANT,
all Shopify flags OFF and preservation of 0124. A future rollback must use an
exact independently reviewed 0124-compatible source/inventory and newly
approved, phase-bound bootstrap command. This Draft source supports that
dormant state. The manifest is included in the canonical source inventory and
consumed by both runtime approval validation and the offline renderer. For the
first pilot, rollback uses this exact reviewed source with flags OFF; an older
source is not accepted merely because its deployment is redeployable.

One exception, S4_DORMANT only: a schema-preserving release cannot name a
deployment of itself before its first deployment exists, so its manifest may
pin exactly one earlier source (`dormant_prior_rollback`: full commit SHA, that
source's runtime inventory digest/count, and an S4 schema-expectations hash
that must equal this source's). Evidence for that rollback must name the same
full SHA in the deployment snapshot, its pinned inventory, every Shopify flag
`"false"`, and otherwise meet every S4 requirement below. S4_PILOT still
requires a dormant rollback of the same source as the candidate, so a pilot is
possible only after the candidate's own dormant deployment. This source pins
the deployed baseline `59216725836bdb0d104e5b8f5107369999efad63` (157 files,
inventory `f6cc722d…`, identical S4 definitions `68082c65…`).

S4 external evidence must name S4_DORMANT, the exact source and inventory,
0111/0113/0124, all seventeen table names, the full canonical schema file hash,
the pinned manifest hash, the same target and deployment ID, and independent
review. Verification must match the deployment snapshot timestamp, precede
approval, remain no older than 24 hours and have an unexpired validity window
of at most 24 hours. Missing, old S3, mismatched or stale evidence fails before
a database connection. No real approval is generated by these draft tests.

The old a8fb/S3 release is incompatible after 0124. Downgrading
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
