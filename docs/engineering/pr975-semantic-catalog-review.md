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
retain the existing path. A timed-out, malformed, over-limit, forged or stale
interpretation also defers to the existing routing path; model/protocol failure
does not create a customer clarification decision.

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
  PDP, brain/webhook-to-sender replays and Constitution pass locally: 207 passed
  and 9 skipped on the current review tree. Model responses in deterministic
  tests are fixtures.
- No-silent-except lint and the trusted-base GOV-002 scan pass.
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

## Governance and release status

The owner-authorized exception registry was merged separately in PR #981 before
this implementation was refreshed onto its trusted base. The current PR HEAD
keeps the original implementation commit in its ancestry and includes current
`main`. The trusted-base scanner, Constitution and all ten GitHub checks pass
without a same-PR waiver, protection change or scanner change.

Actual implementation: model choice unchanged; internal model instructions and
provider user-payload construction changed; catalog decision ownership changed;
no customer phrase map or regex was added; no fixed customer reply was added.

The PR remains a draft and is not approved for merge or deployment. Run the
opt-in live-model evaluation before requesting release approval; deterministic
fixtures and skipped Layer 3 tests are not proof of live model quality, latency
or cost.
