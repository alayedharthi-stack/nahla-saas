# Tenant Identity Integrity — Architecture Reference

**Status:** Enforced as of 2026-04-16 (migrations 0021 + 0022)  
**Owner:** Core Platform Team  
**Source of truth:** `backend/core/tenant_integrity.py`

---

## The Golden Rule

> **Every merchant in Nahla must have exactly one canonical tenant identity.
> All merchant-connected systems — store integration, WhatsApp connection,
> phone_number_id, waba_id, access_token, AI settings, customer records,
> conversation state, and orders — must be anchored to that same canonical
> tenant. No merchant resource may exist independently across tenants.**

---

## Canonical Tenant Ownership

**Definition:**
> The tenant that owns the enabled store integration (`integrations` table,
> `enabled = true`) for a given `(provider, external_store_id)` pair is the
> canonical merchant tenant. All other merchant resources — including the
> WhatsApp connection — must be attached to that same tenant.

Corollary: when a store and a WhatsApp connection diverge across tenants, the
tenant owning the store integration is treated as authoritative. The WhatsApp
side must be moved to match, not the other way around, unless an operator
explicitly decides otherwise through the reconciliation workflow.

---

## Hard Invariants

These invariants are enforced at the service/domain layer and cannot be
bypassed through any API endpoint:

1. **One `phone_number_id` → one tenant.**  
   A `phone_number_id` can belong to at most one `WhatsAppConnection` with
   `status = connected` across the entire platform. Any write attempt that
   would violate this is rejected with HTTP 409 and logged as `write_blocked`.

2. **One `waba_id` → one tenant.**  
   A `whatsapp_business_account_id` (WABA ID) can belong to at most one
   `WhatsAppConnection` with `status = connected`. Enforced by both the
   application guard and the database-level partial unique index
   (`uq_wa_conn_waba_id`).

3. **One `(provider, store_id)` → one tenant.**  
   A Salla/Zid store identified by `(provider, external_store_id)` can be
   bound to at most one enabled `Integration`. Enforced by the application
   guard `assert_store_not_claimed()`.

4. **Incoming webhook must resolve exactly one `WhatsAppConnection`.**  
   On every `POST /webhook/whatsapp`, the `phone_number_id` from Meta's
   payload is matched against `WhatsAppConnection.phone_number_id`.
   - If 0 rows match → message is DROPPED and logged as `tenant_resolved /
     dropped_no_match`.
   - If >1 rows match → message is DROPPED and logged as CRITICAL
     `duplicate_identity / dropped_ambiguous`. This should never happen if
     invariant #1 is intact.
   - If exactly 1 row matches → tenant is resolved and the AI pipeline runs.

5. **Conflicting writes fail loudly with HTTP 409.**  
   No write path silently overwrites an identity owned by a different active
   tenant. The 409 response includes the conflicting tenant ID.

6. **Stale disconnected rows are evicted on reconnect.**  
   When a merchant reconnects a `phone_number_id` or `waba_id`, any
   disconnected rows holding that identifier on other tenants are nulled out
   and set to `status = disconnected` before the new write completes. The
   eviction is logged as `duplicate_identity / fixed`.

7. **All write-time violations are append-only logged.**  
   Every blocked write, eviction, and conflict is recorded in the
   `integrity_events` table with `tenant_id`, `phone_number_id`, `waba_id`,
   `store_id`, `action`, `result`, `actor`, and `timestamp`. This log is
   never truncated or overwritten.

---

## Integrity Guards — Where They Run

| Write path | Guard(s) applied |
|---|---|
| `POST /whatsapp/connection/callback` | `assert_phone_id_not_claimed`, `assert_waba_id_not_claimed`, evict |
| `POST /whatsapp/connection/manual-connect` | Same |
| `POST /whatsapp/embedded/select-phone` | `assert_phone_id_not_claimed`, evict |
| `POST /admin/whatsapp/force-connect` | `assert_phone_id_not_claimed`, `assert_waba_id_not_claimed`, evict |
| Salla OAuth callback / `upsert_tenant_and_integration` | `assert_store_not_claimed` (via `tenant_resolver.py`) |
| `POST /webhook/whatsapp` | Count-exact match; CRITICAL log on ambiguity |

---

## Webhook Routing Guarantee

```
POST /webhook/whatsapp
  → extract phone_number_id from metadata
  → SELECT * FROM whatsapp_connections WHERE phone_number_id = ?
  → count = 0  → DROP + log(tenant_resolved, dropped_no_match)
  → count > 1  → DROP + log(duplicate_identity, dropped_ambiguous) + CRITICAL
  → count = 1  → resolve tenant_id → AI pipeline
```

The system never routes ambiguously. It always drops rather than guesses.

---

## Reconciliation Principles

The reconciliation workflow (`POST /admin/tenant-integrity/reconcile`) merges
one tenant (source) into another (target). It follows these rules:

**Always run `dry_run = true` first.** The response shows every action that
would be taken before any data is modified.

**What is moved (live mode):**
- The source tenant's `WhatsAppConnection` row — reassigned to the target
  tenant's ID (only if the target does not already have a `connected` WA).
- All `Integration` rows from source → target.
- All FK-referenced rows in: `orders`, `products`, `customers`,
  `coupon_codes`, `tenant_settings`, `merchant_addons`, `merchant_widgets`,
  `store_sync_jobs`, `store_knowledge_snapshots`, `billing_subscriptions`,
  `billing_invoices`, `billing_payments`, `conversation_logs`,
  `conversation_traces`, `ai_action_logs`, `system_events`, `users`,
  `whatsapp_usage`, `webhook_guardian_log`, `integrity_events`.
- The source `Tenant` row is deleted after all FK references are moved.

**What is NEVER auto-merged:**
- If the target tenant already has `status = connected` on its own
  WhatsApp connection, the source WA connection is **discarded** (not merged).
  The operator sees a `warning` in the dry-run output and must confirm.
- The reconciliation never modifies phone numbers, access tokens, or WABA IDs
  that belong to the target tenant. It only moves what belongs to the source.

**Rollback:** The live merge is wrapped in a database transaction. On any
failure, it rolls back entirely and returns `status = failed`. Nothing is
partially committed.

**Audit:** Both dry-run and live runs are logged as `reconciliation_started`
and (on completion) `reconciliation_done` in `integrity_events`.

---

## Health Classification

A tenant is classified as:

| Status | Criteria |
|---|---|
| **Healthy** | At most one store integration; WhatsApp connected or intentionally absent; `webhook_verified = true`; no duplicate identifiers |
| **Warning** | `store_no_wa` (store connected but no WA) or `wa_connected_no_store` (WA without store) or `sending_disabled` |
| **Critical** | `multiple_salla_integrations` or `webhook_not_verified` (WA connected but webhook subscription unverified) |

---

## Recurring Scans

| Scan | Trigger | Behavior |
|---|---|---|
| Post-deploy integrity check | Application startup + 90 s delay | Full `run_integrity_audit()`; logs every conflict to `integrity_events` and application log; **does not auto-fix** |
| Webhook Guardian | Every 5 minutes (scheduler) | Checks all connected WA tenants for stalled activity (> 15 min) and missing subscriptions; auto-resubscribes |
| On-demand admin audit | `GET /admin/tenant-integrity` | Full per-tenant report; returns duplicate, orphan, and health lists |

---

## integrity_events Table — Event Vocabulary

| event | meaning |
|---|---|
| `tenant_resolved` | Normal routing: `phone_number_id` matched exactly one tenant |
| `duplicate_identity` | Same phone/waba/store_id found on >1 tenant |
| `cross_tenant_conflict` | WA and store on different tenants |
| `write_blocked` | Write rejected by an integrity guard |
| `reconciliation_started` | Merge workflow initiated (dry_run or live) |
| `reconciliation_done` | Merge workflow completed successfully |
| `orphaned_wa_connection` | WA connection has no associated store integration |
| `orphaned_store` | Store integration has no associated WA connection |

---

## What Cannot Break This

- **Frontend assumptions cannot override it.** Guards run at the
  service/domain layer, not in request handlers or React components.
- **Admin force-connect respects the same guards.** Even admin routes cannot
  bypass `assert_phone_id_not_claimed` or `assert_waba_id_not_claimed`.
- **The database index is a second line of defence.** Even if the application
  guard is bypassed somehow, the `uq_wa_conn_waba_id` partial unique index
  will reject the write at the database level.
- **The webhook router is count-exact.** It never resolves ambiguity silently.

---

## Known Limitations (see Runbook for handling)

1. `assert_store_not_claimed` only matches on `external_store_id`. Legacy rows
   where `external_store_id IS NULL` are matched via `config->>'store_id'`
   only by the `upsert_tenant_and_integration` fallback, not by the guard.
2. Merchants with stale or expired access tokens may pass the write guard
   (tokens are not validated at write time) but will fail at the Meta API
   layer when webhooks are attempted.
3. The reconciliation workflow requires an operator to pick the canonical
   tenant manually. The system reports candidates but never auto-selects.
4. `phone_number_id` uniqueness is enforced only for rows with
   `status = connected`. Rows with `status = disconnected` may still hold the
   same `phone_number_id` as a historical record until evicted on reconnect.
5. `integrity_events` is append-only and grows indefinitely. A retention/
   archival policy should be added before the table exceeds millions of rows.
