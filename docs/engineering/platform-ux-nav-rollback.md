# Platform UX Navigation — Rollback Runbook

**Scope:** Future simplified navigation (`VITE_PLATFORM_NAV_SIMPLIFIED_8`) and Overview command center (`VITE_OVERVIEW_COMMAND_CENTER`).  
**P2:** Wires telemetry and flag scaffold only — **no UI change** when flags are off (default).

## Principles

- **No route deletion:** All pages and React Router entries remain; rollback only switches which UI path is active.
- **Default is current UX:** Absent or false env values keep today’s sidebar and overview.

## Immediate rollback (production)

1. Set environment variables on the dashboard deployment:
   - `VITE_PLATFORM_NAV_SIMPLIFIED_8=false` (or unset)
   - `VITE_OVERVIEW_COMMAND_CENTER=false` (or unset)
2. Redeploy the dashboard (Vite bakes env at build time).
3. Verify: sidebar shows full merchant/admin item list; overview layout unchanged from pre-experiment.

No backend or database changes required.

## Code rollback (experiment PR)

If the simplified-nav or command-center **implementation PR** introduced a defect:

1. `git revert <merge-commit-sha>` for that experiment PR (or revert the feature branch merge).
2. Redeploy dashboard from `main`.
3. Confirm CI `dashboard-platform-policy` is green.

## Rollback P2 itself (telemetry + flags only)

P2 does not change visible navigation. To remove wiring while keeping P1 registry:

1. Revert the P2 PR (`git revert <p2-merge-sha>`).
2. Redeploy dashboard.
3. Effect: `trackPlatformEvent` calls in Layout/Sidebar/Overview stop; feature-flag module removed; docs removed. Billing telemetry from P1 remains.

## Verification checklist

- [ ] Sidebar item count unchanged from pre-experiment (~27 merchant items)
- [ ] Direct URLs still resolve (e.g. `/catalog`, `/settings/security`, `/admin/tenants`)
- [ ] No spike in client errors (Sentry / browser console)
- [ ] PostHog/GA4: baseline events stop if P2 reverted; experiment events absent if flags off

## Feature flag reference

| Env key | Function | Default |
|---------|----------|---------|
| `VITE_PLATFORM_NAV_SIMPLIFIED_8` | `isNavSimplified8Enabled()` | `false` |
| `VITE_OVERVIEW_COMMAND_CENTER` | `isOverviewCommandCenterEnabled()` | `false` |

Truthy values: `true` or `1` (case-insensitive). Any other value = off.

## Related

- Success metrics: `docs/engineering/platform-ux-nav-success-metrics.md`
- Flags: `dashboard/src/lib/platformFeatureFlags.ts`
