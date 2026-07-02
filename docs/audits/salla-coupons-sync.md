# Salla Coupons Sync — Audit & Phase Plan

**Date:** 2026-07-02  
**Scope:** Coupon sync visibility and taxonomy (Phase 1). No push, usage webhook, or deletion reconciliation.

---

## Current data model

| Table / column | Role |
|----------------|------|
| `coupons` | Tenant-scoped coupon rows; unique on `(tenant_id, code)` |
| `coupons.source_type` | `manual` \| `system` \| `imported` (migration 0038) |
| `coupons.coupon_level` | bronze / silver / gold / vip |
| `coupons.allocation_channel` | ai / campaign / autopilot / shared |
| `coupons.metadata` (JSONB `extra_metadata`) | Flexible bag: origin, pool flags, usage, sync hints |

**Missing first-class columns (deferred):**

- No `salla_coupon_id` column — mapping lives in `extra_metadata` for Phase 1
- No `sync_status` column — stored in metadata
- No `used_count` column — pool coupons use `metadata.used`; others may use `usage_count`

---

## Dashboard behavior (`POST /coupons`, `/coupons` UI)

- Merchants can create coupons from the dashboard.
- Creation writes a **DB-only** row (`source_type=manual`, `metadata.source=dashboard`).
- **No push to Salla** on dashboard create (Phase 2).
- List UI previously showed origin/level/channel but **no Salla sync state**.
- Usage bar read `usage_count` / `usage_limit` only — misleading for pool coupons that track `metadata.used`.

---

## Nahla → Salla paths

| Path | Flow | Mapping saved? |
|------|------|----------------|
| `coupon_generator` (pool / on-demand) | Creates coupon in Salla first, then DB | Sets `metadata.salla_synced=true` but **does not persist Salla coupon id** |
| Dashboard manual create | DB only | No Salla metadata |
| Automations / promotions | Issue `Coupon` rows via generator or promotion engine | Depends on generator path |

---

## Salla → Nahla paths

| Path | Flow | Taxonomy before Phase 1 |
|------|------|-------------------------|
| `StoreSyncService.sync_coupons()` | `adapter.get_coupons()` → upsert by `code` | Updated discount fields only; **did not set `source_type=imported`** or sync metadata |
| Webhooks | None for coupons | — |
| Full / incremental store sync | Calls `sync_coupons()` as part of catalog sync | Same gaps |

---

## Usage sync gaps

- Salla order webhooks do **not** update Nahla `usage_count` or `metadata.used`.
- Pool coupons: redemption sets `metadata.used=true` locally; UI showed `0/∞` when `usage_count` absent.
- Imported Salla coupons may expose `maximum_uses` in normalised payload but Nahla does not reconcile live usage from Salla.

---

## Missing mapping fields (pre–Phase 1)

- `extra_metadata.salla_coupon_id` / `external_id` — not set on import
- `extra_metadata.sync_status`, `sync_direction`, `last_synced_at` — absent
- `source_type=imported` — not applied on Salla import
- Failed push state — no `sync_error` taxonomy yet (Phase 2 push)

---

## Phase 1 (this PR) — visibility & taxonomy

**Implemented:**

1. `sync_coupons()` sets import metadata: `source= salla`, `salla_synced`, `sync_status`, `sync_direction`, `last_synced_at`, `salla_coupon_id`.
2. New imports get `source_type=imported`; existing Nahla system pool rows are preserved on re-sync.
3. `GET /coupons` exposes sync visibility fields derived from metadata evidence.
4. Pool usage display: `metadata.used` → `0/1` or `1/1` when no numeric `usage_count`.
5. Dashboard `/coupons` shows Salla sync badge and clearer source labels.

**Explicitly not in Phase 1:**

- Dashboard push to Salla
- Retry queue / failed push handling
- DB migration for `salla_coupon_id`
- Coupon webhooks or order-driven usage sync

---

## Phase 2 — Dashboard push (planned)

- Push dashboard-created coupons to Salla on create (or explicit action).
- Persist Salla id on success; set `sync_direction=nahla_to_salla`, `sync_status`, `sync_error` on failure.
- Optional “Push to Salla” / retry UX.

---

## Phase 3 — Import hardening (planned)

- Stable external-id upsert (not code-only).
- Deletion / deactivation reconciliation when coupon removed in Salla.
- Scheduled incremental coupon sync.

---

## Phase 4 — Usage sync (planned)

- Order webhook handler updates `usage_count` or `metadata.used` from Salla `used_count`.
- Align pool and catalog coupons to a single usage representation.

---

## Validation checklist (platform-wide)

1. Operational claims require metadata evidence — no “synced” badge without `salla_synced` or `sync_status=synced`.
2. Fixes apply to all Salla merchants, not one store.
3. Cross-tenant isolation: coupons scoped by `tenant_id`; sync never leaks codes across tenants.
