# P0 Silent Except Baseline Census

> Generated: `2026-06-06 02:56 UTC`
> Command: `python scripts/lint_no_silent_except_census.py`
> Baseline file: `scripts\lint_no_silent_except_baseline.txt` (read-only)

Observation-only. Does **not** modify baseline or production code.

## Executive summary

| Metric | Count |
|--------|------:|
| Live violations (instances) | 685 |
| Unique keys (`path::message`) | 203 |
| Baseline entries (committed) | 133 |
| Covered by baseline | 133 |
| **Unbaselined (CI reports as "NEW")** | **552** |
| Baseline gap | 685 - 133 = **552** |

## By violation type

| Type | Instances |
|------|----------:|
| silent pass | 519 |
| silent return | 38 |
| logger.debug-only | 128 |

## Top 20 files by violation count

| Rank | File | Instances |
|-----:|------|----------:|
| 1 | `backend/routers/whatsapp_webhook.py` | 161 |
| 2 | `backend/modules/ai/brain/pipeline.py` | 19 |
| 3 | `backend/modules/ai/brain/decision/engine.py` | 18 |
| 4 | `backend/modules/ai/brain/product_discovery_gate.py` | 17 |
| 5 | `backend/modules/ai/postprocess/safety_nets.py` | 16 |
| 6 | `backend/core/order_flow.py` | 15 |
| 7 | `backend/core/automation_engine.py` | 12 |
| 8 | `backend/modules/ai/media/normalizer.py` | 11 |
| 9 | `backend/core/scheduler.py` | 10 |
| 10 | `backend/routers/conversations.py` | 10 |
| 11 | `backend/routers/webhooks.py` | 10 |
| 12 | `backend/modules/ai/brain/commerce/conversational_priority.py` | 10 |
| 13 | `backend/core/inbound_lifecycle.py` | 9 |
| 14 | `backend/core/outbound_send_status.py` | 9 |
| 15 | `backend/routers/customers.py` | 9 |
| 16 | `backend/routers/templates.py` | 9 |
| 17 | `backend/services/store_sync.py` | 9 |
| 18 | `backend/modules/ai/brain/commerce/fallback_guard.py` | 9 |
| 19 | `backend/services/offer_decision_service.py` | 8 |
| 20 | `backend/store_adapters/salla_adapter.py` | 8 |

## Top 20 unbaselined keys (excess over baseline)

These drive CI failure today - legacy debt not captured in the frozen baseline.

| Rank | Excess | Found | Baseline | Key |
|-----:|-------:|------:|---------:|-----|
| 1 | +104 | 120 | 16 | `backend/routers/whatsapp_webhook.py::silent pass on broad except` |
| 2 | +38 | 41 | 3 | `backend/routers/whatsapp_webhook.py::logger.debug-only on broad except (use logger.exception)` |
| 3 | +16 | 16 | 0 | `backend/modules/ai/brain/product_discovery_gate.py::silent pass on broad except` |
| 4 | +10 | 10 | 0 | `backend/core/scheduler.py::silent pass on broad except` |
| 5 | +10 | 10 | 0 | `backend/modules/ai/brain/commerce/conversational_priority.py::silent pass on broad except` |
| 6 | +10 | 10 | 0 | `backend/modules/ai/brain/decision/engine.py::silent pass on broad except` |
| 7 | +10 | 13 | 3 | `backend/modules/ai/brain/pipeline.py::silent pass on broad except` |
| 8 | +9 | 9 | 0 | `backend/core/inbound_lifecycle.py::silent pass on broad except` |
| 9 | +9 | 9 | 0 | `backend/core/outbound_send_status.py::silent pass on broad except` |
| 10 | +9 | 9 | 0 | `backend/routers/customers.py::silent pass on broad except` |
| 11 | +8 | 8 | 0 | `backend/core/order_flow.py::logger.debug-only on broad except (use logger.exception)` |
| 12 | +8 | 8 | 0 | `backend/modules/ai/postprocess/safety_nets.py::logger.debug-only on broad except (use logger.exception)` |
| 13 | +8 | 8 | 0 | `backend/modules/ai/postprocess/safety_nets.py::silent pass on broad except` |
| 14 | +7 | 7 | 0 | `backend/modules/ai/brain/commerce/fallback_guard.py::silent pass on broad except` |
| 15 | +7 | 7 | 0 | `backend/routers/catalog.py::silent pass on broad except` |
| 16 | +7 | 7 | 0 | `backend/services/salla_orders_poller.py::silent pass on broad except` |
| 17 | +6 | 6 | 0 | `backend/core/send_governor.py::silent pass on broad except` |
| 18 | +6 | 6 | 0 | `backend/modules/ai/brain/decision/engine.py::logger.debug-only on broad except (use logger.exception)` |
| 19 | +6 | 6 | 0 | `backend/modules/ai/brain/pipeline.py::logger.debug-only on broad except (use logger.exception)` |
| 20 | +6 | 7 | 1 | `backend/modules/ai/media/normalizer.py::silent pass on broad except` |

## Interpretation

- The lint gate is **designed** to allow pre-existing violations via baseline.
- CI fails when `unbaselined > 0`, not when a PR introduces delta vs `main`.
- A resync from `133` to `685` entries documents debt;
  it does not fix violations in code.

## Next steps (platform, not PR-scoped)

1. Review this census.
2. `P0 Silent Except Baseline Resync` with this report attached.
3. `P0 Silent Except Gate PR Delta Mode` so PRs are not hostage to baseline drift.
