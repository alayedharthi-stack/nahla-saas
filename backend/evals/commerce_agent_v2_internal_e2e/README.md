# Commerce Agent V2 INTERNAL_E2E acceptance channel

`INTERNAL_E2E` runs the Phase 2.6 corpus through the canonical Commerce Agent
V2 runtime without WhatsApp transport. It is disabled by default and initially
limited to Tenant 1.

Required operator configuration:

```text
NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED=true
NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS=1
```

Work uses the existing authenticated admin API and does not need database
credentials or shell access. Every route is protected by `require_admin`; the
tenant is the server-side constant `1`, aliases are typed `A|B|C`, request
bodies reject unknown fields, and batch execution always loads the checked-in
180-turn corpus:

```text
GET  /admin/internal-e2e/status
POST /admin/internal-e2e/fixtures/provision       {"reset_aliases": []}
POST /admin/internal-e2e/fixtures/A/reset
POST /admin/internal-e2e/turns                    {"alias":"A","text":"...","service_tier":"auto"}
POST /admin/internal-e2e/batches                  {"seed":260914,"concurrency_waves":true}
GET  /admin/internal-e2e/batches/{batch_id}
POST /admin/internal-e2e/batches/{batch_id}/score
GET  /admin/internal-e2e/results?trace_id={trace_id}
```

The local CLI still requires `DATABASE_URL=<approved test database>` and is
available for engineering-only operation:

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

Isolation values are not declarations. Each turn carries `safety_proofs` built
from the actual Conversation scope, Session history row provenance, registered
tool evidence ownership, and discovered order ownership. The scorer rejects a
missing/unproven proof, a proof/value mismatch, or measured leakage. Persisted
orders with `source=internal_e2e` are independently excluded at production
poller, automation, COD, payment, shipment, analytics, and customer-scoring
boundaries after the synchronous confinement context has ended.
