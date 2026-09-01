# AI PR Constitution Checklist

Mandatory for every AI behavior PR. A PR cannot receive PASS or merge approval until every item is checked.

Authoritative doctrine: `AGENTS.md` (Mandatory Natural Language Rule + Final customer text provenance rule).  
Root-cause gate: `docs/engineering/root-cause-first-policy.md` (no Brain/Prompt/Compose/State/Memory patch from live tests until channel→facts layers are proven; Principle of Evidence applies).  
Intelligence gate: `docs/engineering/intelligence-non-interference-policy.md` (GOV-001 — keep the model free; fix the system around it).  
Enforcement: `constitution-compliance` CI check.

**Merge-blocking status:** The check runs in CI but is **not** merge-blocking until GitHub branch protection marks `constitution-compliance` as Required. Owner actions: `docs/engineering/merge-and-ci-policy.md`.

**Waiver policy:** Tracked violations are `FAILING POLICY WITH TEMPORARY WAIVER` — never approved exceptions. New waivers require governance PR + `governance_baseline_version` bump.

## Final customer text provenance (required)

- [ ] **True source of final customer text identified** — LLM, approved template, sanitizer, dedup, fallback, or guard.
- [ ] Reviewer states whether any valid LLM candidate was replaced before wire-out.
- [ ] If template: maps to an approved exception in `constitutional_policy.py`.
- [ ] If fallback: compose was attempted first and fallback metadata is present.

## Compose and metadata

- [ ] Decision → facts → compose → guards → sanitizer → dedup → wire traced.
- [ ] `compose_source`, `response_mode`, and `chosen_path` asserted.
- [ ] LLM candidate compared with final outbound.
- [ ] Any deterministic text maps to an approved exception in `constitutional_policy.py`.
- [ ] Any fallback runs only after compose failure.
- [ ] `fallback_reason` and `fallback_action_type` recorded.
- [ ] No fixed normal-path customer prose.
- [ ] No deterministic postprocessor replacement.
- [ ] Tests assert behavior, facts, and metadata—not exact normal prose.
- [ ] Adjacent pre-existing paths reached by the change were checked.
- [ ] Constitution compliance check is green.

## GOV-001 Intelligence non-interference (required)

- [ ] `INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE` stated on the assignment/PR.
- [ ] Report `MODEL_CHANGED`, `PROMPT_CHANGED`, `PERSONA_CHANGED`, `PHRASE_MAP_CHANGED`, `KEYWORD_ROUTER_CHANGED`, `CUSTOMER_REGEX_CHANGED` — default **NO**.
- [ ] Fix order followed: state → truth → context → routing → capability → execution → persistence → postprocess → only then raw model evaluation.
- [ ] No phrase maps, keyword routers, or customer-regex intent repair.
- [ ] Model/prompt/persona unchanged unless MODEL-BLAME GATE passed **and** `OWNER_APPROVAL_REQUIRED=YES`.
