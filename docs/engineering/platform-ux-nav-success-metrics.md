# Platform UX Navigation — Success Metrics

**Gate:** Platform UX P2 (telemetry baseline + feature flags)  
**Status:** Draft — reviewable thresholds, not production-final  
**IA note:** The eight primary destination names and page-to-destination mapping are **not yet approved**. This document defines how we will measure success once the simplified navigation experiment ships behind `VITE_PLATFORM_NAV_SIMPLIFIED_8`.

## Goal

Reduce the **visible** sidebar from ~27 top-level items to **8 primary destinations** while keeping every existing route and deep page reachable (no route deletion).

## Event sources (closed registry)

| Metric area | Primary events | Notes |
|-------------|----------------|-------|
| Use of primary destinations | `platform_nav_click`, `platform_page_view` | Filter `nav_group` / `path` once IA is locked |
| Reach deep/internal pages | `platform_page_view` | Paths deeper than top-level nav (e.g. `/settings/security`) |
| Clicks to reach a page | *Derived* | Sequence analysis: `platform_nav_click` → `platform_page_view` (same session, ordered timestamps) |
| Overview engagement | `overview_loaded`, `overview_period_changed`, `overview_cta_clicked` | KPI load, period changes, CTAs to `/wa-usage` and `/billing` |
| Navigation errors / 404 | **Gap** | No event in `PLATFORM_TELEMETRY_EVENTS` yet — requires governance approval before adding |
| Return to nav / abandonment | **Gap** (proxy) | Proxy via `platform_page_view` churn (rapid path changes without task completion) until a dedicated event is approved |

## Proposed success thresholds (post-flag launch)

Review after 2–4 weeks of flagged traffic. Numbers are directional, not merge gates.

| Metric | Baseline (pre-simplification) | Target (simplified nav) |
|--------|------------------------------|-------------------------|
| Median nav clicks to reach a task page | Establish from derived sequences | ≤ 2 clicks for top 80% of sessions |
| Share of sessions using only primary nav paths | Establish from `platform_nav_click` | ≥ 70% of navigations via primary destinations |
| Deep-page reach rate | `platform_page_view` on non-primary paths | No regression > 5% vs baseline |
| Overview CTA click-through | `overview_cta_clicked` / `overview_loaded` | No regression vs baseline; optional +10% to `/wa-usage` |
| Support tickets citing “can’t find page” | Ops / support tags | No increase vs 4-week pre-launch average |

## Measurement workflow

1. **Pre-flag (now):** P2 wires baseline events; collect 2+ weeks of navigation and overview data with current sidebar.
2. **Flag on (future PR):** Set `VITE_PLATFORM_NAV_SIMPLIFIED_8=true` in staging → production canary.
3. **Compare:** Same dashboards, split by flag exposure (env/deployment cohort).
4. **Gate decision:** Product + engineering review thresholds and gaps before locking the eight destinations.

## Gaps requiring registry approval

Before treating navigation health as fully instrumented:

- **`platform_nav_not_found`** (or equivalent) — user landed on unknown route / catch-all
- **`platform_nav_back`** (or equivalent) — explicit “back to menu” / nav reset

Do not add these names without updating `PLATFORM_TELEMETRY_EVENTS` and constitution/compliance review.

## Related

- Telemetry module: `dashboard/src/lib/platformTelemetry.ts`
- Feature flags: `dashboard/src/lib/platformFeatureFlags.ts`
- Rollback: `docs/engineering/platform-ux-nav-rollback.md`
