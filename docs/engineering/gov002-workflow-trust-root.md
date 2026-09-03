# GOV-002 workflow trust root

This is the enforcement record for how intelligence non-interference CI is
triggered. A repository unit test cannot prove GitHub branch protection.

## Bootstrap (#924 only)

```text
BOOTSTRAP_HEAD_TRUST_EXCEPTION=YES_ONE_TIME
```

BASE (`main` at `a11ead57f7df6e934cdeef407433df31ab7bce73`) does not contain
the scanner or the dedicated workflow. PR #924 therefore uses the HEAD scanner
and HEAD workflow copies, which are owner-reviewed in this PR. That is not a
standing trust model.

After #924 merges:

```text
TRUSTED_BASE_SCANNER_REQUIRED=yes
```

Do not claim `HEAD_SCANNER_CAN_SELF_BYPASS=no` at the complete workflow level
while `HEAD_CAN_SKIP_GOV002_AND_STILL_SATISFY_REQUIRED_CHECK=yes`.

## GitHub API investigation (2026-09-03)

Repository: `alayedharthi-stack/nahla-saas` (id `1195156616`)

```text
owner_type=User
visibility=public
org_rulesets_endpoint=HTTP 404
repository_rulesets_list=[]
```

Classic branch protection on `main` (readback):

```text
required_status_checks=
  Scan repository for leaked secrets (app_id=15368)
  lint-and-test (app_id=15368)
  constitution-compliance (app_id=15368)
enforce_admins=true
require_code_owner_reviews=false
```

`constitution-compliance` is produced by `.github/workflows/ci.yml`, which is
loaded from PR HEAD on `pull_request`. HEAD can remove the GOV-002 step and
keep the job name.

### Strongest mechanism that is actually available

**`pull_request_target` on `.github/workflows/gov002-intelligence-non-interference.yml`.**

After this file exists on `main`, GitHub loads that workflow YAML from BASE,
not from feature-PR HEAD. Deleting or rewriting the file in a feature PR does
not stop BASE `pull_request_target` from running.

This is not sufficient by itself: `gov002-trusted-base-scanner` is **not** a
required status check today, so a PR can still satisfy merge-required
`constitution-compliance` after gutting GOV-002 from `ci.yml`.

### Preferred mechanism that this account cannot activate

GitHub ruleset rule `type=workflows` pinning

`.github/workflows/gov002-intelligence-non-interference.yml`

at `ref=refs/heads/main`.

API evidence:

1. Repository rulesets **do** work on this public user-owned repo.
   A disabled `required_status_checks` probe was created as id `22169042`
   and deleted; list readback returned `[]`.
2. `POST /repos/.../rulesets` with `type=workflows` **fails**:
   - without `repository_id`: HTTP 422
     `Invalid property /rules/0: data matches no possible input`
   - with `repository_id=1195156616` and existing `.github/workflows/ci.yml`
     on `main`: HTTP 422
     `Invalid rule 'workflows': Invalid parameter workflows: Workflow error at index 0`
3. `GET /orgs/alayedharthi-stack/rulesets` → HTTP 404 (not an organization).
4. GitHub docs: ruleset required workflows are configured at organization or
   enterprise level.

`CODEOWNERS` does not close this bypass. Scanner detection of `ci.yml` does
not close it if HEAD prevents the scanner from running.

### External configuration after this investigation

```text
WORKFLOW_TRUST_ROOT=github.workflow:pull_request_target (BASE YAML after #924 merges)
RULESET_ID=(none; type=workflows POST 422 on this user-owned repository)
RULESET_ENFORCEMENT=(not available)
REQUIRED_CHECK=constitution-compliance
DEDICATED_CHECK=gov002-trusted-base-scanner (emitted, not required)
WORKFLOW_DEFINITION_CONTROLLED_BY_PR_HEAD=yes
HEAD_CAN_SKIP_GOV002_AND_STILL_SATISFY_REQUIRED_CHECK=yes
```

Platform limitation: user-owned repository cannot pin a required workflow
outside PR HEAD. Moving the repository under a GitHub Organization would be
required before `type=workflows` can be retried as an org ruleset.

## What a unit test can prove

If the BASE scanner still runs, deleting the trusted workflow and changing
semantic runtime is `GOVERNANCE_CORE_CHANGE` plus the semantic finding.

What a unit test cannot prove: GitHub still *invokes* that scanner after HEAD
deletes the workflow or guts `constitution-compliance`. That property is the
external GitHub configuration above.
