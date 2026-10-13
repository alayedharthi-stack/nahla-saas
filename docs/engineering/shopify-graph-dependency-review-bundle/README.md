# Shopify graph dependency pin correction: S0 review bundle

Draft PR: https://github.com/alayedharthi-stack/nahla-saas/pull/1221

Tested source R: `a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec`. Frozen Git ref: `release/shopify-dormant-s0-a8fb88e3` at exactly R.
Any later documentation head is not the deployment source. Preserve this ref
and verify its HEAD again before any separately reviewed execution.

The full production Dockerfile reproduces the stock import refusal before any
database check. Alembic reads all revision modules for graph discovery at S0,
including 0115's top-level `PaymentBase` import. Its exact repository closure
is the empty `backend/__init__.py`, docstring-only `backend/payments/__init__.py`
and existing `backend/payments/models.py`. Only those three fixed inventory
entries are added. There is no backend wildcard, new payment behavior or import
guard change. The whole contract AST is identical after removing these entries.
Gate, renderer, migrations, model files, schema expectations and app runtime
remain byte-identical to the accepted baseline source `94da9e2bcb41248180808e791d98f384c46f5779`.

Inventory: **146 files**, SHA-256 `31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174`.
The canonical inventory uses Git blob bytes, avoiding local CRLF differences.
File hashes and dependency review are in `dependency-closure.json`.

The [final real-image proof](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38243591231)
passed all **19** S0 gate checks against disposable PostgreSQL 18.6. Exact
schema, version rows and retained relation OIDs were preserved; financial and
Shopify table counts stayed zero. All three file mutations were refused before
planted code/database access; an arbitrary unreviewed backend module was still
refused. Four focused inventory tests passed. Application startup/ASGI/Salla
suites were not rerun; prior accepted proofs are carried. Full evidence and
honest superseded test-driver failures are in `proof-results.json`.

CI rebuilt the complete frozen source with the unchanged production Dockerfile;
baseline and corrected images share identical dependency layers. It did not
retrieve or rerun the failed Railway registry artifact. See its observed
deployment/image digest and the distinct CI image IDs in `proof-results.json`.

This is a **review-only** S0 bundle. Independent source review is pending.
No live configuration, deployment, migration, restore or activation was done.
`approval.S0.REVIEW_PENDING.json` is an unfilled external-record template;
stock strict rendering refuses it with `approval_missing`, exit 2. This does
not negate the parent's earlier explicit owner authorization. After review,
reconcile that authorization for this exact source and targets, reobserve the
eligible backup/rollback/heads/flags, fill a new external record outside the
repo, and rerender/review its payload and command together. Never reuse the
expired prior 94da execution payload or these fail-closed preview commands.

Review pins for the current unfilled bytes:

- Payload SHA-256: `0c1ea28056cebd6cd0181f041b8ac79555df491e9955ce8d2da3c9700193470b`
- Command SHA-256 (UTF-8, no terminal LF): `f90cd0831848d36cb4c02a59ece73d4dbbaad700b97ccf0c599a3b925851ecdc`
- Combined patch file SHA-256: `02af706425d04171929bdb6c92735065e5c850111691fcf4dc4fab9c03a018f9`

The patch previews target the existing API and dashboard only, select the
dedicated frozen release branch and set all five backend/two Vite Shopify
flags to `0`. Builders, starts, domains, volumes, regions, replica counts,
existing settings and provider credentials are inherited unchanged. The API
gate cannot attest another service's compiled Vite flags; future dashboard
builds require their own verification. S0 needs no clone, migration or S3
prerequisite. S3 and activation remain separate later tasks.

`live-state.last-observation.json` carries recovery evidence from 10:41-10:48
UTC: both services healthy at 462ede1, legacy guard/settings restored, flags
OFF, exact S0 heads, no Shopify objects and no staged changes. Its backup card
was last observed by the parent at 07:19:57 UTC, so this is historical evidence,
not a fresh action-time approval. No live retry before independent review.

All bundle files except the manifest are SHA-256 pinned in `manifest.json`.
Raw connector logs remain private; recorded log byte lengths/digests specify
their precise decoding and canonicalization.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
