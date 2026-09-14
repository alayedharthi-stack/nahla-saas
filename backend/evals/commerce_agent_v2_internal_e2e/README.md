# Commerce Agent V2 INTERNAL_E2E acceptance channel

`INTERNAL_E2E` runs the Phase 2.6 corpus through the canonical Commerce Agent
V2 runtime without WhatsApp transport. It is disabled by default and initially
limited to Tenant 1.

Required operator configuration:

```text
NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED=true
NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS=1
DATABASE_URL=<approved test database>
```

The operator supports fixture provisioning, one-turn submission, per-alias
reset, seeded 180-turn scheduling, sequential execution, A/B/C concurrency
waves, and scoring:

```text
python scripts/operators/commerce_v2_internal_e2e.py provision
python scripts/operators/commerce_v2_internal_e2e.py schedule --output /tmp/internal-e2e-plan.jsonl
python scripts/operators/commerce_v2_internal_e2e.py run --schedule /tmp/internal-e2e-plan.jsonl --output /tmp/internal-e2e-evidence.jsonl --concurrency-waves
python scripts/operators/commerce_v2_internal_e2e.py score --evidence /tmp/internal-e2e-evidence.jsonl --output /tmp/internal-e2e-report.json
```

The service invokes `CommerceAgentContext.from_trusted_scope`,
`run_commerce_agent(execution_mode="outbound")`, the canonical Conversation
Session, all seven read-only tools, guardrails, SDK trace hooks, and
`build_commerce_delivery_plan`. It persists explicit internal message IDs and
never creates or reports provider WAMIDs.

Each synthetic identity is bound to a normal Conversation row through a
non-phone `internal_e2e:t1:customer:<alias>` external identity. It deliberately
does not create a normal Customer row, so the fixture cannot enter customer
segments, scoring, campaigns, marketing, or customer-count analytics. Internal
message directions likewise stay out of normal inbound/outbound unread and
message metrics.

The hard gate is `external_egress_total == 0`. Any attempted provider boundary
inside the confinement context is denied, recorded on the turn artifact, and
marks the turn `test_contract_failed`.
