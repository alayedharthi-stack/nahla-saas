# Nahla SaaS AI Migration Plan

This document maps all AI-related files currently spread across the repository and defines their proposed target location under the modular monolith structure at `backend/modules/ai`.

This document started as an analysis-only planning document.

Current status note:

Several migration steps described here are now partially or fully implemented
inside the repository, especially under `backend/modules/ai/orchestrator/`.

This file therefore remains the AI migration reference, but it should now be
read as:

- a target ownership map
- a risk map
- a sequencing guide for remaining migration work

not as a statement that nothing has yet been implemented.

## Scope

This plan covers files related to:

- AI reply generation
- LLM orchestration
- conversation AI logic
- prompt building
- AI tools and AI pipelines

The analysis focuses especially on:

- `services/`
- `ai-engine/`
- `backend/`
- `whatsapp-service/`
- `conversation-service/`

## Target AI Module Structure

The long-term target for AI ownership is:

```text
backend/modules/ai/
├── orchestrator/
├── engine/
├── conversation/
├── knowledge/
├── prompts/
├── memory/
├── guards/
├── execution/
├── commerce/
├── sales/
├── adapters/
├── observability/
└── templates/
```

Current implemented subset:

- `backend/modules/ai/orchestrator/`
- `backend/modules/ai/prompts/`
- `backend/modules/ai/commerce/`
- `backend/modules/ai/templates/`

Current transitional runtime owners still in play:

- `services/ai-orchestrator/`
- `ai-engine/`
- `backend/routers/whatsapp_webhook.py`
- `backend/routers/ai_sales.py`
- `backend/core/conversation_engine.py`
- `whatsapp-service/`

## AI File Inventory And Proposed Migration Map

| Current File | Current Role | Proposed Location Under `backend/modules/ai` | Risk | Migration Order |
| --- | --- | --- | --- | --- |
| `services/ai-orchestrator/main.py` | Orchestrator service entrypoint | `backend/modules/ai/orchestrator/app.py` | Medium | 5 |
| `services/ai-orchestrator/api/routes.py` | Main orchestration pipeline and API surface | `backend/modules/ai/orchestrator/api/routes.py` | High | 4 |
| `services/ai-orchestrator/engine/claude_client.py` | Claude/Anthropic client and model calls | `backend/modules/ai/orchestrator/engine/claude_client.py` | Medium | 2 |
| `services/ai-orchestrator/prompt/builder.py` | Prompt builder for orchestration flow | `backend/modules/ai/prompts/builder.py` | Low | 1 |
| `services/ai-orchestrator/memory/loader.py` | AI memory loading from database | `backend/modules/ai/memory/loader.py` | Medium | 3 |
| `services/ai-orchestrator/memory/updater.py` | AI memory update after turns | `backend/modules/ai/memory/updater.py` | Medium | 3 |
| `services/ai-orchestrator/fact_guard/checker.py` | Fact checking / grounded response guard | `backend/modules/ai/guards/fact_checker.py` | Medium | 3 |
| `services/ai-orchestrator/fact_guard/data_fetcher.py` | Fetches grounding data for FactGuard | `backend/modules/ai/guards/fact_data_fetcher.py` | Medium | 3 |
| `services/ai-orchestrator/policy/guard.py` | Policy/safety guard for AI actions | `backend/modules/ai/guards/policy_guard.py` | Medium | 3 |
| `services/ai-orchestrator/commerce/permissions.py` | AI commerce permissions catalog | `backend/modules/ai/commerce/permissions.py` | Low | 1 |
| `services/ai-orchestrator/commerce/permission_guard.py` | Runtime permission enforcement for AI actions | `backend/modules/ai/commerce/permission_guard.py` | Medium | 3 |
| `services/ai-orchestrator/execution/action_execution_guard.py` | Final action execution safety layer | `backend/modules/ai/execution/action_execution_guard.py` | Medium | 3 |
| `services/ai-orchestrator/README.md` | Orchestrator documentation | `backend/modules/ai/orchestrator/README.md` | Low | 6 |
| `ai-engine/main.py` | Stateless fallback AI engine | `backend/modules/ai/engine/fallback_engine.py` | Medium | 5 |
| `backend/core/conversation_engine.py` | Intent engine, stage logic, AI reply path, FactGuard integration | `backend/modules/ai/conversation/engine.py` | High | 4 |
| `backend/core/nahla_knowledge.py` | Platform-level AI system prompt builder | `backend/modules/ai/knowledge/nahla_system_prompt.py` | Medium | 2 |
| `backend/core/store_knowledge.py` | Merchant/store knowledge context building | `backend/modules/ai/knowledge/store_context.py` | Medium | 2 |
| `backend/routers/whatsapp_webhook.py` | WhatsApp AI reply path using conversation engine and Claude | `backend/modules/ai/adapters/whatsapp_webhook_ai.py` | High | 4 |
| `backend/routers/ai_sales.py` | AI sales processing and orchestrator integration | `backend/modules/ai/sales/service.py` | High | 4 |
| `backend/template_ai/generator.py` | Template generation helper for AI-assisted templates | `backend/modules/ai/templates/generator.py` | Low | 1 |
| `backend/template_ai/policy_validator.py` | Template policy validation | `backend/modules/ai/templates/policy_validator.py` | Low | 1 |
| `backend/template_ai/health_evaluator.py` | Template quality/health evaluation | `backend/modules/ai/templates/health_evaluator.py` | Low | 1 |
| `backend/observability/event_logger.py` | Logs AI-related execution metadata such as orchestrator usage | `backend/modules/ai/observability/event_logger.py` or shared observability wrapper | Medium | 5 |
| `backend/observability/health.py` | Orchestrator health probes and AI-related readiness checks | `backend/modules/ai/observability/health.py` or shared observability wrapper | Low | 5 |
| `whatsapp-service/ai_client.py` | Calls orchestrator, then fallback engine | `backend/modules/ai/adapters/whatsapp_service_client.py` | Low-Medium | 5 |
| `whatsapp-service/webhook.py` | Uses AI client for inbound WhatsApp flow | Keep under WhatsApp module later, but AI-facing logic should be absorbed into `backend/modules/ai/adapters/` | High | 6 |

## Files That Are AI-Adjacent But Not Primary AI Owners

These files are related to AI behavior or AI data, but they are not the core ownership layer for the `backend/modules/ai` migration.

| File | Why It Matters | Proposed Handling |
| --- | --- | --- |
| `backend/core/config.py` | Contains `ORCHESTRATOR_URL`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` and other AI-related config | Keep as shared config for now; later split AI-specific config into an AI config wrapper |
| `backend/main.py` | Registers AI-related routers | Keep in place; update imports only during migration |
| `backend/routers/system.py` | Includes orchestrator/system health information | Keep in place; route should call AI-owned observability service later |
| `backend/routers/templates.py` | Uses `template_ai` components | Keep as HTTP layer; internal logic should later depend on `backend/modules/ai/templates` |
| `database/models.py` | Holds AI-relevant schema and metadata fields | Remains owned by `database/`; do not move |
| `database/migrations/versions/0009_template_ai_columns.py` | Schema history for template-AI fields | Remains in migrations history |
| `database/migrations/versions/0008_ai_sales_tables.py` | Schema history for AI sales | Remains in migrations history |
| `test_anthropic.py` | AI smoke test | Move later to `tests/` or `scripts/` if desired |
| `test_claude.py` | AI smoke test | Move later to `tests/` or `scripts/` if desired |
| `dashboard/src/api/aiSalesAgent.ts` | Frontend API adapter for AI sales | Frontend remains in `dashboard/`; no move into backend AI module |
| `dashboard/src/pages/AiSalesLogs.tsx` | Frontend UI for AI logs | Frontend only; no backend move |

## Files Reviewed With No Primary AI Ownership

These areas were reviewed but do not currently appear to be primary AI ownership locations:

| Area | Finding |
| --- | --- |
| `conversation-service/` | Conversation and handoff domain exists, but no direct LLM orchestration or prompt pipeline was identified |
| `services/message-router/` | Appears placeholder-oriented and not currently an active AI pipeline owner |

## Risk Levels

### Low Risk

Files that are mostly isolated helpers, documentation, or deterministic logic:

- `services/ai-orchestrator/prompt/builder.py`
- `services/ai-orchestrator/commerce/permissions.py`
- `backend/template_ai/generator.py`
- `backend/template_ai/policy_validator.py`
- `backend/template_ai/health_evaluator.py`
- AI docs and smoke tests

### Medium Risk

Files that depend on shared configuration, database models, or runtime contracts:

- `services/ai-orchestrator/engine/claude_client.py`
- `services/ai-orchestrator/memory/*`
- `services/ai-orchestrator/fact_guard/*`
- `services/ai-orchestrator/policy/guard.py`
- `services/ai-orchestrator/commerce/permission_guard.py`
- `services/ai-orchestrator/execution/action_execution_guard.py`
- `backend/core/nahla_knowledge.py`
- `backend/core/store_knowledge.py`
- `ai-engine/main.py`
- AI observability files

### High Risk

Files that currently sit on live request paths or bind together many moving parts:

- `services/ai-orchestrator/api/routes.py`
- `backend/core/conversation_engine.py`
- `backend/routers/whatsapp_webhook.py`
- `backend/routers/ai_sales.py`
- `whatsapp-service/webhook.py`

## Recommended Migration Order

The order below minimizes breakage by moving leaf utilities before live entrypoints.

### Phase 1: Low-risk AI utilities

Move or mirror first:

1. `services/ai-orchestrator/prompt/builder.py`
2. `services/ai-orchestrator/commerce/permissions.py`
3. `backend/template_ai/generator.py`
4. `backend/template_ai/policy_validator.py`
5. `backend/template_ai/health_evaluator.py`

### Phase 2: Shared AI knowledge and model helpers

Then move:

1. `services/ai-orchestrator/engine/claude_client.py`
2. `backend/core/nahla_knowledge.py`
3. `backend/core/store_knowledge.py`

### Phase 3: Memory, guards, and execution controls

Then move:

1. `services/ai-orchestrator/memory/loader.py`
2. `services/ai-orchestrator/memory/updater.py`
3. `services/ai-orchestrator/fact_guard/checker.py`
4. `services/ai-orchestrator/fact_guard/data_fetcher.py`
5. `services/ai-orchestrator/policy/guard.py`
6. `services/ai-orchestrator/commerce/permission_guard.py`
7. `services/ai-orchestrator/execution/action_execution_guard.py`

### Phase 4: Live AI business flows

Then migrate the high-coupling runtime components:

1. `backend/core/conversation_engine.py`
2. `backend/routers/whatsapp_webhook.py`
3. `backend/routers/ai_sales.py`
4. `services/ai-orchestrator/api/routes.py`

### Phase 5: Runtime wrappers and compatibility layer

Then migrate or collapse runtime entrypoints:

1. `ai-engine/main.py`
2. `services/ai-orchestrator/main.py`
3. `whatsapp-service/ai_client.py`
4. AI observability wrappers

### Phase 6: Final cleanup

After AI logic is stable inside `backend/modules/ai`:

1. review `whatsapp-service/webhook.py`
2. decide whether `services/ai-orchestrator/` remains a temporary compatibility shell or is fully retired
3. retire duplicate AI ownership paths
4. update docs and tests

## Current Status Snapshot

The repository has already completed these migration themes:

- canonical AI prompt builder under `backend/modules/ai/prompts/`
- canonical commerce permissions under `backend/modules/ai/commerce/`
- canonical template AI implementation under `backend/modules/ai/templates/`
- canonical adapter / pipeline / engine stack under `backend/modules/ai/orchestrator/`
- provider abstraction, provider registry, provider chain execution,
  resilience, observability, cost metadata, and prompt metadata under
  `backend/modules/ai/orchestrator/`
- compatibility shims for `backend/template_ai/*`
- compatibility shims for low-risk `services/ai-orchestrator` prompt/commerce helpers

What is still transitional:

- `services/ai-orchestrator/api/routes.py` still owns the live `/orchestrate`
  runtime path
- `ai-engine/main.py` still owns a legacy fallback path
- `backend/routers/whatsapp_webhook.py` and `backend/routers/ai_sales.py`
  still sit on live request paths
- memory / guards / execution / conversation logic are not yet fully owned by
  `backend/modules/ai`

## Ownership Decision

After the modular monolith migration, the official owner of AI logic should be:

`backend/modules/ai`

Specifically:

- reply generation lives under `backend/modules/ai/conversation/`
- orchestration lives under `backend/modules/ai/orchestrator/`
- prompts live under `backend/modules/ai/prompts/`
- merchant/platform context lives under `backend/modules/ai/knowledge/`
- safety and policy checks live under `backend/modules/ai/guards/`
- execution gates live under `backend/modules/ai/execution/`
- AI sales logic lives under `backend/modules/ai/sales/`
- transport-specific adapters live under `backend/modules/ai/adapters/`

## Summary

The repository currently has AI logic distributed across:

- `services/ai-orchestrator/`
- `ai-engine/`
- `backend/core/`
- `backend/routers/`
- `backend/template_ai/`
- `whatsapp-service/`

The migration should converge all core AI behavior into:

`backend/modules/ai`

with the highest caution around:

- `backend/routers/whatsapp_webhook.py`
- `backend/core/conversation_engine.py`
- `backend/routers/ai_sales.py`
- `services/ai-orchestrator/api/routes.py`

These are the most sensitive runtime paths and should be migrated last among the AI files.
