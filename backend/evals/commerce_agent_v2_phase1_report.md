# Commerce Agent V2 — Phase 1 Initial Eval Report

Date: 2026-09-12
Mode: offline, read-only, official Agents SDK runner
Configured baseline: `gpt-5.6-sol`, reasoning effort `high`

## Result

- Replay cases: **10/10 passed** through `Runner.run` and the real Phase-1 tools.
- Grounded end-to-end scripted case: **1/1 passed**, including structured output,
  catalog evidence, safe tool arguments, trace provenance, and token accounting.
- Phase-1 safety/contract suite: **38/38 passed**.
- Live provider selection/wording eval: **not executed** because this environment
  has no `OPENAI_API_KEY`. No synthetic result is presented as a live Sol result.

The scripted model is used only to make tool selection deterministic. Tool calls
still pass through the official Agents SDK runner and execute the production
catalog/knowledge domain services against an isolated test database.

## Expected vs actual tool behavior

| Case | Expected tools | Actual tools | Result |
|---|---|---|---|
| `catalog-browse-ar` | `search_products` | `search_products` | Pass |
| `catalog-specific-ar` | `search_products` | `search_products` | Pass |
| `price-followup-ar` | `search_products`, `get_product_details` | Same, same order | Pass |
| `product-provenance-ar` | `search_products`, `search_product_knowledge` | Same, same order | Pass |
| `gift-recommendation-ar` | `search_products` | `search_products` | Pass |
| `budget-recommendation-ar` | `search_products` | `search_products` | Pass |
| `global-kb-ar` | `search_merchant_knowledge` | `search_merchant_knowledge` | Pass |
| `linked-kb-ar` | `search_products`, `search_product_knowledge` | Same, same order | Pass |
| `missing-product-ar` | `search_products` | `search_products`; `not_found` is safe | Pass |
| `missing-fact-ar` | `search_products`, `search_product_knowledge` | Same; structured safe fallback | Pass |

## Assertions covered

- Empty-query catalog browse and text search remain tenant-scoped.
- A product ID must first be discovered in the current run and is then checked
  again against the active tenant before details or linked knowledge are read.
- Global merchant knowledge excludes product-linked sections.
- Product knowledge includes only sections linked to the exact selected product.
- Price, stock, product URL, and image URL are emitted by catalog evidence.
- Missing products/knowledge produce explicit statuses instead of invented facts.
- The final result is a validated `CommerceReply`; legacy text markers trip the
  output guardrail.
- Shadow scheduling is gated off by default and owns no outbound/write symbol.

## Command

```bash
PYTHONPATH=backend:database pytest -q backend/tests/test_commerce_agent_v2_phase1.py
```

Observed result: `38 passed`.

## Required next eval before tenant enablement

Run the same fixture with the configured live `gpt-5.6-sol` provider in a
non-production environment, score actual model-selected tools and grounded
wording, and review every mismatch. This is intentionally a release gate; the
offline replay proves wiring and safety contracts, not live model quality.
