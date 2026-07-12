# AI PR Constitution Checklist

Mandatory for every AI behavior PR. A PR cannot receive PASS or merge approval until every item is checked.

Authoritative doctrine: `AGENTS.md` (Mandatory Natural Language Rule).  
Enforcement: `constitution-compliance` CI check.

- [ ] Source of final customer text identified.
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
