# PR #1086 — terminal CI results on head `e12ecba44a782c3d0f31ca4b67eb72858f306959`

Audit record captured 2026-09-18 from the GitHub Actions API (job and step
conclusions). This head carried the harness **and** the two `ci.yml` gate
steps; the `ci.yml` change was removed from the PR afterwards by owner
decision (governance split), so these are the only CI executions of the
gate steps to date.

Base: `origin/main` @ `4a270df82bcf11cdd88b1ef6d9946dc2dc46ca3e` (PR #1084 merge).

## Workflow runs for this head

| Workflow | Run | Event | Conclusion |
| --- | --- | --- | --- |
| CI (`.github/workflows/ci.yml`) | [35358526808](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/35358526808) | pull_request | mixed — see jobs |
| gitleaks | [35358526738](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/35358526738) | pull_request | success |
| Merge freeze gate | [35358527006](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/35358527006) | pull_request_target | success |
| GOV-002 trusted intelligence guard | [35358526985](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/35358526985) | pull_request_target | **failure** |

## CI run 35358526808 — jobs

| Job | Job id | Conclusion | Notes |
| --- | --- | --- | --- |
| lint-and-test | 105643737864 | **success** | step "Run unit tests" success (14:49:15–14:53:26); step **"Commerce reliability gate (unit tier)" success** (14:53:26–14:53:48); all 15 later steps success |
| a1-postgres-integration | 105643737914 | **success** | step **"Commerce reliability gate (PostgreSQL tier)" success** (14:52:40–14:52:45) after "Commerce lifecycle CAS concurrency - PostgreSQL" success |
| constitution-compliance | 105643737859 | **failure** | step "GOV-002 — Intelligence non-interference diff guard" failure (14:49:40); step "Constitution compliance" skipped |
| trusted-context-layer1 | 105643737503 | success | |
| dashboard-platform-policy | 105643737831 | success | |
| staging-dr-executor-artifact | 105643737879 | success | |
| whatsapp-catalog-sync-postgres | 105643737951 | success | |

## GOV-002 finding (both failing checks)

```
FILE=.github/workflows/ci.yml
LINE=1
CHANGE_CLASS=GOVERNANCE_CORE_CHANGE
REASON=governance core changed together with unrelated files
CHANGE_DIGEST=ea73362e0d7a87a14e58f649bd69d1efee7994eb4da436c198260f5ec58fe35d
AUTHORIZED_EXCEPTION_ID=
MODEL_CHANGED=NO PROMPT_CHANGED=NO PERSONA_CHANGED=NO PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO CUSTOMER_REGEX_CHANGED=NO
```

Owner decision (2026-09-18): governance split — no exception, no bypass,
no branch-protection change, no optional-workflow workaround. The `ci.yml`
hunk is preserved verbatim in
`docs/engineering/commerce-reliability-gate-ci-steps.patch`.

## What the two gate steps measured in CI

The CI-printed gate reports were not captured from the job logs (the log
tail is dominated by service-container output); the step conclusions above
are the CI evidence. The same commands on the same head, run locally before
the push, produced:

| Tier | Result |
| --- | --- |
| unit | PASS (pre-correction semantics): 83 collected, 77 passed, 6 xfailed (RB-01, RB-02, RB-03, RB-04, RB-05, UC-01), 0 skipped, 0 failed; PR #1084 modules 17/17 and 24/24 executed, hashes unchanged |
| postgres | PASS (pre-correction semantics): 5 collected, 3 passed, 2 xfailed (RB-03-PG, UC-02), 0 skipped, 0 failed; disposable database created and dropped |

"PASS" here is the pre-correction gate semantics that treated unassigned
owners and unset expiries as warnings. After the owner's fail-closed
correction the same measurement is reported as **NOT ACCEPTED** (unapproved
debt) in acceptance mode; see `docs/engineering/commerce-reliability-gate.md`.
