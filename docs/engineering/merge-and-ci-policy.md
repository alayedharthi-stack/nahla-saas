# Merge and CI Protection Policy

Nahla engineering policy for merging into `main`. Applies platform-wide.

## Merge and CI Protection Policy

Nahla engineering policy:

1. No PR may be merged into `main` unless required CI checks are green.
2. Required checks:
   - `lint-and-test`
   - `Scan repository for leaked secrets` / gitleaks
3. Local test claims are not enough if GitHub CI is red.
4. If GitHub shows red on a merged PR, treat it as an engineering incident until explained.
5. Admin/owner bypass is prohibited except with explicit written justification:
   - reason
   - risk
   - why it cannot wait
   - follow-up fix or revert plan
6. A PR that changes AI behavior must include relevant regression tests.
7. A PR that changes constitution/policy must include docs/tests only unless explicitly scoped otherwise.
8. If CI fails after merge, fix forward immediately or revert depending on production risk.
9. Before Phase 2 / FactBoundPersonaComposer runtime, `main` must be protected.

## GitHub branch protection settings for main

Repository admin must enable:

- Require a pull request before merging
- Require status checks to pass before merging
- Required checks:
  - `lint-and-test`
  - `Scan repository for leaked secrets`
- Require branches to be up to date before merging, if practical
- Restrict or disallow admin bypass
- Require conversation resolution before merging, if available
- Do not allow force pushes to `main`
- Do not allow deletions of `main`

## Incident rule

If any red CI is observed on merged PRs:

- stop new runtime work
- identify the failing check
- determine if required or optional
- fix CI or document why optional
- restore green `main` before continuing

## Related

- `AGENTS.md` — operational vs personality doctrine
- `docs/architecture/nahla-ai-merchant-assistant-policy.md` — merchant assistant behavior policy
