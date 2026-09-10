# Semantic catalog implementation — review candidate

Parent: PR #975 at `05614c42f7dc84d292f7411799e78b676dbfdbd4`.
Work branch: `fix/pr975-ai-product-understanding`.
This is an isolated follow-up; the original PR branch, templates work and
production have not been modified. No merge, deploy, WhatsApp send or production
database write is authorized by this report.

## Proven gaps addressed

- Browse reply validation used word remainders and proximity of negation. A
  coordinated extra product could escape validation; a different product's
  negation could be attributed to the eligible product.
- `availability_guard_correction` reached execution data but was dropped by
  `catalog_product_answer` facts and its provider serializer.
- Read-only catalog follow-ups had no structured model-owned capability contract.

## Implementation

The existing slot-model selection and canonical provider supply structured
interpretations. No new model selection or fixed customer-facing text is added.

Read-only discovery accepts model-selected browse/search/details/image/link,
variant questions, conversation, clarification or defer. IDs are restricted to
fresh current-tenant catalog facts; current references require a fresh focus.
The engine consumes this contract before CE2. Execution, tenant permissions and
send gates remain authoritative. Active checkout, human priority, non-commerce
blocks and URL-only enrichment are excluded. This is not a replacement of every
existing inbound classifier: out-of-scope turns and an unconfigured provider
retain the existing path. Failed configured interpretation requests clarification.

For browse replies, the model extracts all claims with subject IDs and exact
quotes. Code validates IDs, boolean inventory evidence and Decimal prices against
the same catalog snapshot. Candidate and facts hashes prevent reuse across a
different reply or catalog. Unknown, incomplete or malformed interpretation does
not authorize text. No language-specific remainder or negation-distance matching
is used by this browse guard.

Correction reason and current catalog IDs now reach the actual persona provider
user payload; PDPs are sanitized and projected with their product IDs. Corrections
are reconstructed from authoritative bundle products, not arbitrary instructions
or product rows in the incoming correction dictionary. The model still authors
the reply. There is one existing correction compose, followed by a fresh semantic
check. Failure blocks the unverified text and retains an already grounded card.
Off bypasses verification; shadow never rewrites; enforce applies the contract.

Structured capability/status and verification/recompose diagnostics are included
in the closed reply-metadata export. Candidate text, model instructions and
correction blobs are not added to that export.

## Verification and limits

- Targeted contracts, Salla presentation, existing product understanding, direct
  PDP and brain/webhook-to-sender replays pass locally. Exact count is recorded
  with the final commit report; model responses in these tests are fixtures.
- Constitution: 57 passed. No-silent-except lint passed.
- Nine live-model tests are skipped without credentials. An opt-in evaluation
  file covers pronouns, photo/link requests, social turns, variants, coordinated
  products, false negatives and per-product prices. No claim of live model
  understanding or live customer acceptance is made.
- Two existing delivery-exemption failures in
  `test_availability_browse_variant_conflict.py` reproduce on parent `05614c42`
  and on this branch. These tests/code were not weakened or changed.
- Local replay setup initially lacked anthropic/phonenumbers dependencies. After
  installing the declared dependencies, the targeted webhook replay passed.
- A selected discovery turn adds one interpretation call; an eligible browse
  candidate adds one verification call. A rejected candidate may add the existing
  one correction compose plus one re-verification. Each new call has an 8-second
  caller deadline and no retries. A timed-out synchronous provider call may still
  finish in its worker; timeout is not a cancellation/billing guarantee. Live
  quality, latency and incremental cost remain unevaluated.

## Governance — blocked, not approved for release

The scanner used for the local gate is identical to the trusted main BASE
scanner (blob `bda0769d9fbaff69526ec26f19337bd02e7bad5b`). Scan against
`f67b7681541bbab400781abc8428319a71c6a6af` exits 1. It flags protected persona,
routing/regex and canned-reply surfaces. Some findings point at pre-existing
literals shifted by insertions; they have not been suppressed or reclassified.
The relevant surface changes and internal instructions are disclosed here.

Actual implementation: model choice unchanged; internal model instructions and
provider user-payload construction changed; catalog decision ownership changed;
no customer phrase map or regex was added; no fixed customer reply was added.
Scanner flags must not be reported as all NO or CI green.

`AGENTS.md` and `intelligence-non-interference-policy.md` require any necessary
owner exception to exist in BASE through an authorization-only PR before runtime
consumption. No same-PR waiver, protection change or scanner change was made.
Resolve that gate and run the opt-in model evaluation before release approval.
