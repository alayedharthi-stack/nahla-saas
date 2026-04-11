# Nahla SaaS Ownership Rules

This document defines the official ownership model for the Nahla SaaS modular monolith migration.

Its purpose is to make architectural boundaries explicit, reduce duplicate implementations, and establish one official source of truth for each major system.

## Architectural Layers

Current repository note:

The long-term target remains the modular monolith layout below, but the
current runtime still mounts most live HTTP routes from `backend/routers`
and still carries real business logic in `backend/core` for several domains.

This document therefore distinguishes between:

- target ownership (`backend/modules`, `backend/platform`)
- transitional runtime paths that still exist today

Any future cleanup should move runtime truth toward this target instead of
creating new owners elsewhere.

| Layer | Official Directory | Responsibility |
| --- | --- | --- |
| HTTP Layer | `backend/api/routes` (target) / `backend/routers` (current transitional runtime) | Public API surface, request/response mapping, route registration, auth guards, input/output schemas, and transport concerns only. |
| Domain Layer | `backend/modules` | Business logic, domain services, workflows, rules, provider-specific orchestration, repositories, and internal system ownership. |
| Infrastructure Layer | `backend/platform` | Shared platform concerns such as configuration, database wiring, middleware, observability, scheduler, and audit support. |

## Source Of Truth Rules

1. Every major system must have exactly one official source-of-truth directory.
2. New business logic must be added under the owning directory in `backend/modules`.
3. HTTP route files must not become the long-term home of business logic.
4. Shared runtime/platform concerns must live under `backend/platform`.
5. Any duplicate implementation outside the official owner should be treated as transitional until merged or removed.

## Official System Ownership Map

| System | Official Source Of Truth | Ownership Scope |
| --- | --- | --- |
| Auth | `backend/modules/auth` | Authentication flows, JWT/session rules, authorization policy, identity lifecycle, and auth-related service logic. |
| Tenant | `backend/modules/tenant` | Tenant resolution, tenant settings, tenant identity boundaries, tenant-scoped guards, and multi-tenant ownership rules. |
| WhatsApp | `backend/modules/whatsapp` | WhatsApp connection lifecycle, embedded signup, webhook handling, sending, verification, usage, and tenant-specific WhatsApp state. |
| AI Orchestration | `backend/modules/ai` | AI reply orchestration, merchant context, prompts, memory, safety guards, and AI decision flows. |
| Billing | `backend/modules/billing` | Plans, subscriptions, payment gateways, billing workflows, payment state, and billing-related integrations. |
| Campaigns | `backend/modules/campaigns` | Campaign lifecycle, automations, outreach workflows, intelligence-driven actions, and campaign-related business rules. |
| Store Integrations | `backend/modules/integrations` | Salla/Zid/Shopify integrations, OAuth flows, sync orchestration, provider adapters, and integration status logic. |
| Widgets | `backend/modules/widgets` | Merchant widget configuration, storefront widget delivery, public widget bundles, and widget behavior rules. |
| Conversations | `backend/modules/conversations` | Conversation state, conversation workflows, handoff state, message history rules, and conversation domain logic. |
| Orders | `backend/modules/orders` | Order retrieval, order workflows, checkout/order state, and order-related domain operations. |
| Catalog | `backend/modules/catalog` | Product catalog ownership, catalog sync outcomes, product lookups, and catalog-related business logic. |
| Coupons | `backend/modules/coupons` | Coupon generation, coupon policies, coupon validation, and coupon lifecycle logic. |
| Analytics | `backend/modules/analytics` | Reporting, KPIs, aggregates, event analytics, and analytics-specific domain services. |
| Location | `backend/modules/location` | Address parsing, geolocation support, delivery/location normalization, and location-related business logic. |
| Marketplace | `backend/modules/marketplace` | Marketplace-facing flows, marketplace integrations/features, and marketplace-specific business ownership. |

## Boundary Guidance

### `backend/api/routes` (target) / `backend/routers` (current)

Target state:

- expose endpoints
- validate inputs
- serialize outputs
- call module-owned services
- avoid becoming the permanent home of core business workflows

Current transitional note:

Most live routes still mount from `backend/routers`. During the migration,
`backend/routers` is acceptable as a transport layer, but it should be
progressively thinned until business logic is owned by `backend/modules`.

This HTTP layer should not be treated as the source of truth for any major domain.

### `backend/modules`

This is the main business layer of the modular monolith target architecture.

Current state:

- `backend/modules/ai` is already an active canonical owner
- many other module directories currently exist as placeholders only
- their runtime truth still lives elsewhere until migration is completed

Each top-level directory under `backend/modules` is expected to own one system boundary. Internal structure may evolve, but ownership should stay stable.

Typical contents of a module may include:

- service logic
- repositories
- provider adapters
- internal schemas
- orchestration flows
- policies and validation rules

### `backend/platform`

This layer is shared infrastructure, not a business domain.

Current state:

Several real platform/runtime concerns still live under `backend/core` and
other top-level backend packages. `backend/platform` is the target owner,
not yet the full runtime truth today.

It should contain only platform-wide concerns such as:

- app configuration
- database/session plumbing
- middleware
- observability
- scheduler/runtime jobs
- audit/logging support

It should not become a fallback location for business logic that belongs in a module.

## Decision Rule For Future Work

When adding or refactoring code:

- if it defines business behavior for a single system, it belongs in that system's module under `backend/modules`
- if it exposes HTTP only, it belongs under `backend/api/routes`
- if it is cross-cutting runtime/platform support, it belongs under `backend/platform`

## Migration Intent

This ownership map does not move existing files by itself.

It defines the target architecture so future refactors can:

- merge duplicated implementations into one owner
- move business logic out of route files
- retire parallel service-style directories
- converge the repository toward one modular monolith backend
