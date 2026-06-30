# AI Commerce Confidence Gate

Use this gate before re-enabling live AI for customers.

## Run

From repo root:

```bash
python backend/scripts/run_ai_commerce_confidence_suite.py
```

Or directly:

```bash
cd backend
python -m pytest \
  tests/test_store_ai_pause.py \
  tests/test_ai_commerce_scenario_runner.py \
  tests/test_ai_commerce_scenario_kb_shipping.py \
  tests/test_ai_commerce_scenario_kb_delivery_fixes.py \
  tests/test_ai_commerce_compose_smoke.py \
  tests/test_ai_playground_dry_run.py \
  tests/test_ai_playground_regression_scenarios.py \
  tests/test_post_delivery_review_request.py \
  tests/test_order_delivered_stamp.py \
  tests/test_ai_commerce_confidence_hardening.py \
  -q --tb=line
```

## What it covers

| Suite | Purpose |
|-------|---------|
| `test_store_ai_pause.py` | Global store AI off + conversation pause gates |
| `test_ai_commerce_scenario_runner.py` | Scenario runner foundation, orders, payment, catalog |
| `test_ai_commerce_scenario_kb_shipping.py` | KB/FAQ routing, shipping, tracking guards |
| `test_ai_commerce_scenario_kb_delivery_fixes.py` | Availability + delivery confirmation detection |
| `test_ai_commerce_compose_smoke.py` | Decision + FakeFacts compose smoke |
| `test_ai_playground_dry_run.py` | Playground service + KB sanitization |
| `test_ai_playground_regression_scenarios.py` | Playground HTTP regression |
| `test_post_delivery_review_request.py` | Review emitter idempotency |
| `test_order_delivered_stamp.py` | `delivered_at` stamping |
| `test_ai_commerce_confidence_hardening.py` | Real-problem confidence scenarios |

## Failure diagnostics

Confidence tests print on failure:

- message
- reply_text
- decision_topic / owner / blocked_reason
- would_send / warnings / side_effects
- order_count before/after
- customer name/phone when scenario runner is used

## Deferred to separate PRs

- Full `MerchantBrain.process` + postprocess guard coverage
- Canary / allowlist rollout tooling
- Handoff-active webhook persona coverage beyond pause gates
- Subscription-blocked playground preview parity
- Full catalog unknown-variant matrix
- OCR/image inbound scenarios
- End-to-end LLM compose fixtures

## Next step after green gate

1. Canary / allowlist on test numbers
2. Gradual store re-enable
3. Monitor outbound + order/payment truthfulness
