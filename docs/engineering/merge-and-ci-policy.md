# Merge and CI Protection Policy

Nahla engineering policy for merging into `main`. Applies platform-wide.

## Merge and CI Protection Policy

Nahla engineering policy:

1. No PR may be merged into `main` unless required CI checks are green.
2. CI jobs that exist today:
   - `lint-and-test`
   - `constitution-compliance` (constitutional governance gate)
   - `Scan repository for leaked secrets` / gitleaks
3. **Important:** GitHub branch protection for `main` currently requires `constitution-compliance`, `lint-and-test`, and `Scan repository for leaked secrets` / gitleaks as merge-blocking status checks. Repository files cannot themselves enforce GitHub branch protection; that configuration is external. A required check is not the same as a non-bypassable gate — admin bypass and CODEOWNERS enforcement are documented separately below and are **not** claimed proven here.
4. Local test claims are not enough if GitHub CI is red.
5. If GitHub shows red on a merged PR, treat it as an engineering incident until explained.
6. Admin/owner bypass is prohibited except with explicit written justification:
   - reason
   - risk
   - why it cannot wait
   - follow-up fix or revert plan
7. A PR that changes AI behavior must include relevant regression tests.
8. A PR that changes constitution/policy must include docs/tests only unless explicitly scoped otherwise.
9. If CI fails after merge, fix forward immediately or revert depending on production risk.
10. Before Phase 2 / FactBoundPersonaComposer runtime, `main` must be protected.

## GitHub branch protection settings for main

Proven current required status checks on `main` (GitHub branch protection, external to this repository):

1. **`constitution-compliance`** — required / merge-blocking.
2. **`lint-and-test`** — required / merge-blocking.
3. **`Scan repository for leaked secrets`** / gitleaks — required / merge-blocking.

Repository files cannot themselves create or enforce those GitHub settings.

Still **unverified** unless separately proven (do not treat as current fact):

- CODEOWNERS review enforcement for governance files
- Admin bypass restricted or disabled

Recommended remaining admin configuration (not claimed complete):

1. Require **Code Owner review** for:
   - `AGENTS.md`
   - `backend/modules/ai/compose/constitutional_policy.py`
   - `backend/modules/ai/compose/tracked_violations_baseline.json`
   - `backend/tests/test_constitution_compliance.py`
   - `.github/workflows/ci.yml`
   - `.github/CODEOWNERS`
   - deterministic exception / tracked-violation registries
2. Prevent bypass where repository settings permit (disable admin bypass if available).
3. Require explicit review for any change to:
   - `tracked_violations_baseline.json`
   - `allowed_violation_ids`
   - `governance_baseline_version`
   - `DETERMINISTIC_EXCEPTIONS`

Do **not** describe the constitution gate as completely non-bypassable. Required-check status is proven; bypass restrictions and CODEOWNERS enforcement are not.

### Owner verification checklist

- [x] `constitution-compliance` visible on PR checks
- [x] `constitution-compliance` marked Required on `main`
- [ ] CODEOWNERS enforced for governance files
- [ ] Admin bypass restricted/disabled where possible

## Tracked constitutional waivers

Tracked violations (`tracked_violations_baseline.json`) are **FAILING POLICY WITH TEMPORARY WAIVER** — not approved behavior.

- New violation IDs require `governance_baseline_version` bump in a **dedicated governance PR**.
- An ordinary AI feature PR **cannot** add both a new deterministic normal-path violation and its own waiver.
- Expired waivers fail CI automatically.
- Removal PRs must delete the waiver entry when the runtime violation is fixed.

### Constitution scanner blind spots (known, documented)

Repository CI cannot catch every deterministic prose path. Known limits as of PR #566:

- `return T.<template>()` without `result.data["chosen_path"]` in the same block (many legacy responder paths).
- `templates.py` function bodies: scanned, but findings require a `chosen_path` assignment in the same block.
- Sanitizer/dedup postprocessors: metadata contract tested; not all replacement paths are AST-gated.
- PR-diff context unavailable in CI: baseline JSON lock is static; co-updating `violations` + `allowed_violation_ids` in one PR can pass CI unless GitHub CODEOWNERS review blocks it.
- Closed builder registry (`payment_barcode_intro_text`, `minimal_emergency_fallback` only): unknown builders are not flagged.

Synthetic pattern proofs live in `TestScannerPatternProofs` inside `test_constitution_compliance.py`.

## Incident rule

If any red CI is observed on merged PRs:

- stop new runtime work
- identify the failing check
- determine if required or optional
- fix CI or document why optional
- restore green `main` before continuing

## Related

- `AGENTS.md` — operational vs personality doctrine
- `docs/engineering/ai-pr-constitution-checklist.md` — AI PR checklist
- `docs/architecture/nahla-ai-merchant-assistant-policy.md` — merchant assistant behavior policy
