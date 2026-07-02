# Salla Full API Link — Coupon Phase 2 Blocker Audit

**Date:** 2026-07-02  
**Branch:** `audit/salla-full-api-link-for-coupons`  
**Scope:** Why embedded Salla can show “connected” while Full API link is incomplete, and why `/coupons` coupon-only sync cannot import from Salla.  
**Out of scope:** Dashboard → Salla coupon push, usage sync, expired-coupon archiving, OrderFlow, WhatsApp, billing, SallaTenantGuard refactors.

---

## Executive summary

Nahla uses a **Dual Integration Architecture** for Salla:

| Layer | How it connects | Typical credentials | Durable Admin API? |
|-------|-----------------|---------------------|--------------------|
| **Embedded / Communication App** | `/salla/token-login` from Salla iframe | Short-lived `api_key` from introspect or embedded token; usually **no** `refresh_token` | No — session can expire; no `api_sync_enabled` |
| **Full API link (Sync OAuth)** | `/api/salla/oauth/start` → `/api/salla/oauth/callback` | `access_token` + **`refresh_token`** + `api_sync_enabled=True` | Yes — canonical Admin API path |
| **Easy Mode (legacy)** | `app.store.authorize` webhook | Webhook-delivered tokens | Yes for legacy installs (`easy_mode`) |

**Coupon Phase 2 is blocked** because production shows **Full API link incomplete** and `POST /coupons/sync-salla` returns the generic “no connected store” error. Root cause class: **missing Sync OAuth completion** (`api_sync_enabled` + `refresh_token`), not missing coupon-specific adapter methods.

Manual coupon **create** on `/coupons` works without Salla (local `coupons` table, `source_type=manual`, badge `لم يُرسل إلى سلة`). **Import/sync from Salla** requires a live `SallaAdapter` from `get_adapter()`, which in practice needs valid, non-revoked integration credentials — the designed durable path is Full API link.

---

## 1. “Full API link incomplete” — source of truth

### UI component

| Item | Location |
|------|----------|
| Screen | `dashboard/src/pages/SallaEntryScreen.tsx` (`/app/entry`) |
| Card label | `t.status.apiFull` → **ربط API الكامل** (`dashboard/src/i18n/embedded.ts`) |
| Incomplete text | `t.status.incomplete` → **غير مكتمل** |
| CTA when incomplete | Orange banner + **ربط المتجر** → `openOauthSync()` → `GET /api/salla/oauth/start?token=<JWT>` |

### API endpoint

`GET /api/salla/integration-status` — `backend/routers/salla_oauth.py` (`salla_integration_status`).

### Exact “complete” condition

Rendered in `SallaEntryScreen.tsx`:

```ts
const apiSyncOk     = integration?.api_sync_enabled ?? false
const apiSyncEasy   = integration?.easy_mode ?? false
const apiSyncCounts = apiSyncOk || apiSyncEasy   // card shows “complete” if true
```

Backend sets `api_sync_enabled`:

```python
api_sync_enabled = (
    bool(cfg.get("api_sync_enabled"))
    and bool(cfg.get("refresh_token"))
    and bool(integration.enabled)
)
```

**Card shows “غير مكتمل” when:**

- `api_sync_enabled` is false **or** `refresh_token` missing **or** integration `enabled` is false, **and**
- `easy_mode` is false (not Easy Mode webhook install).

**Embedded “connected”** is much weaker:

```python
if integration:
    embedded_connected = True
```

Any `integrations` row for `provider='salla'` marks embedded as connected — no token, refresh, or `api_sync` check.

### Requirements checklist (Full API link)

| Requirement | Set by | Required for “complete”? |
|-------------|--------|--------------------------|
| `integrations` row (`provider=salla`) | token-login / OAuth / webhook | Yes (embedded connected) |
| `config.store_id` | token-login / OAuth | Yes (identity) |
| `config.api_key` (access token) | token-login introspect or Sync OAuth | Yes for API calls |
| `config.refresh_token` | **Sync OAuth callback only** (or rare introspect) | **Yes for api_sync_enabled** |
| `config.api_sync_enabled=True` | **Sync OAuth callback** | **Yes** |
| `integration.enabled=True` | provisioning / OAuth | Yes |
| `config.api_canonical=True` | Sync OAuth callback | Informational |
| OAuth `scope=offline_access` | `/api/salla/oauth/start` | Required to receive refresh_token |
| `needs_reauth=False` | default; set on 401 without refresh | Must be false for adapter |
| Tenant ↔ store mapping | `resolve_salla_store_identity`, guards | Required for correct tenant |

**Not checked** on the status card: coupon-specific scopes, `merchant_id` alone, or OAuth on the Communication App.

---

## 2. Embedded connected vs Full API connected

### Embedded connected (Communication App)

- Merchant opens Nahla inside Salla → `POST /salla/token-login`.
- Introspects embedded token via `https://api.salla.dev/exchange-authority/v1/introspect`.
- Persists `api_key` (+ optional short-lived access from introspect).
- **Typically does not** receive `refresh_token` (logged explicitly in token-login).
- **Does not** set `api_sync_enabled=True`.
- Issues Nahla JWT with `tenant_id` + `store_id`.
- Returns `needs_api_sync: true` when `derive_salla_login_integration_flags()` says Sync OAuth is still required (`backend/store_integration/salla_login_flags.py`).

### Full API connected (Sync OAuth app)

- Merchant clicks **ربط المتجر** → `/api/salla/oauth/start` (scope `offline_access`).
- Callback `/api/salla/oauth/callback` exchanges code, merges config:
  - `api_key`, `refresh_token`, `api_sync_enabled=True`, `api_canonical=True`, `app_type=custom_oauth_sync`.
- `pick_active_salla_integration()` prefers this row (`_is_api_sync` wins over embedded/easy).

### Can embedded be connected without OAuth token?

- **Embedded UI “connected”:** yes — row exists even if tokens were cleared later.
- **Working Admin API:** needs non-empty `api_key`; durable path needs `refresh_token` + `api_sync_enabled`.
- **Partial OAuth:** Sync OAuth can fail mid-flow (no `refresh_token` in response → logged as scope issue).
- **Store identity without API credentials:** possible briefly after failed provision; token-login normally sets `api_key`.
- **Tenant mapping:** token-login and OAuth callback both use `claim_store_for_tenant` / identity helpers; mismatch blocks OAuth with `store_owned_by_other_tenant`.

---

## 3. `POST /coupons/sync-salla` guard

**File:** `backend/routers/coupons.py` → `sync_salla_coupons`.

```python
svc = StoreSyncService(db, tenant_id)
adapter = svc._get_adapter()
if not adapter or not hasattr(adapter, "get_coupons"):
    raise HTTPException(400, detail="لا يوجد متجر سلة متصل أو لا يدعم مزامنة الكوبونات")
synced = await svc.sync_coupons()
```

### What `_get_adapter()` does

`backend/services/store_sync.py` → `store_integration.registry.get_adapter(tenant_id)`:

1. `pick_active_salla_integration(db, tenant_id)` — canonical Salla row.
2. Returns **`None`** if:
   - no integration row;
   - `integration.enabled` is false;
   - `config.needs_reauth` is true;
   - no adapter class;
   - unhandled exception.
3. Otherwise builds `SallaAdapter(api_key, store_id, refresh_token, ...)`.

`SallaAdapter` **always** implements `get_coupons`. The 400 error means **`get_adapter()` returned `None`**, not “unsupported adapter”.

### `sync_coupons()` vs endpoint

Inside `StoreSyncService.sync_coupons()`, a missing adapter returns `0` silently. The router adds the stricter 400 guard.

### Production symptom mapping

| Observation | Likely cause |
|-------------|--------------|
| Embedded connected + API incomplete | token-login only; Sync OAuth not completed |
| sync-salla 400 generic message | `get_adapter()` → `None` |
| Manual coupons work | `POST /coupons` does not use Salla adapter |
| WhatsApp connected | unrelated to Salla adapter |

**Exact blocker for affected tenant (inferred):** integration row exists (embedded OK) but adapter cannot be constructed — most commonly **`needs_reauth`**, **`enabled=false`**, **missing/empty `api_key`**, or **no row for JWT `tenant_id`** when using full dashboard outside embedded session. Secondary: Sync OAuth never run → even with `api_key`, token may be expired and without `refresh_token` the next 401 marks `needs_reauth` and adapter becomes `None`.

---

## 4. Salla adapter — coupon API requirements

**File:** `backend/store_adapters/salla_adapter.py`

| Operation | Method | Salla endpoint | Auth |
|-----------|--------|----------------|------|
| Import/list | `get_coupons()` | `GET /coupons` (paginated) | `_get()` → uses `api_key`; refreshes if `refresh_token` present |
| Create (Phase 2) | `create_coupon()` | `POST /coupons` | `_require_auth()` — needs `api_key`; refresh strongly recommended |

### Scopes

- Sync OAuth requests **`scope=offline_access`** only (`salla_oauth.py`).
- No separate `coupons.read` / `discounts.write` in codebase — coupons use standard Admin API v2 with the Sync app token.
- Missing `offline_access` → no `refresh_token` → `api_sync_enabled` stays false → Full API incomplete.
- Endpoint-level 403 from Salla on `/coupons` would surface as 502 `فشلت مزامنة...` (adapter exists), not the 400 guard.

---

## 5. Verifying the current tenant (production-safe)

No DB credentials in this audit. Use JWT-scoped endpoints as the merchant:

```bash
# Identity + basic Salla row
curl -H "Authorization: Bearer $JWT" https://api.nahlah.ai/salla/whoami

# Full API link fields (same source as /app/entry card)
curl -H "Authorization: Bearer $JWT" https://api.nahlah.ai/api/salla/integration-status
```

| Field | Meaning |
|-------|---------|
| `embedded_connected` | Row exists |
| `api_sync_enabled` | Full API link complete |
| `has_refresh_token` | Refresh present |
| `has_api_key` | Access token present |
| `easy_mode` | Legacy webhook path |
| `oauth_start_url` | `/api/salla/oauth/start` |

**Expected for reported production state:**

- `embedded_connected: true`
- `api_sync_enabled: false`
- `has_refresh_token: false` (or `api_sync_enabled` false despite token)
- `easy_mode: false`

Admin diagnostic (if available): `describe_integrations_for_tenant()` via admin Salla integration tools — shows which row `pick_active_salla_integration` selects and why.

---

## 6. Root-cause classification

| Hypothesis | Fits production? |
|------------|------------------|
| **Missing Sync OAuth completion** | **Primary** — matches API incomplete + needs_api_sync |
| Missing coupon-specific scopes | Unlikely — not modeled; would be 502 not 400 |
| Tenant/store mismatch | Possible if dashboard JWT ≠ embedded store; check `whoami` vs `embedded_store_id` |
| Missing credentials / needs_reauth | Possible if embedded token expired after 401 |
| Adapter capability gap | **No** — `get_coupons` exists on `SallaAdapter` |
| UI guard bug | **Partial** — embedded “connected” overstates readiness; sync error message is generic |

---

## 7. Minimum safe next PRs (recommended order)

### PR A — Connection completion (merchant action + docs)

**No code required for happy path:** merchant completes Sync OAuth from embedded **ربط المتجر** CTA.

Validation after OAuth:

1. `/app/entry` → **ربط API الكامل** shows complete.
2. `GET /api/salla/integration-status` → `api_sync_enabled: true`, `has_refresh_token: true`.
3. `/coupons` → **مزامنة كوبونات سلة** imports without 400.

### PR B — Visibility only (small, safe, coupons + embedded)

Improve `POST /coupons/sync-salla` 400 detail using integration row state, e.g.:

- `تطبيق سلة متصل، لكن ربط API الكامل غير مكتمل. أكمل الربط من تطبيق سلة لتفعيل مزامنة الكوبونات.`
- `صلاحيات سلة تحتاج إعادة ربط (needs_reauth).`
- `لا يوجد تكامل سلة لهذا المتجر.`

Optionally mirror `needs_api_sync` on `/coupons` sync button (read-only `integration-status`).

**Do not** implement push/retry/usage sync in this PR.

### PR C — Coupon Phase 2 (still blocked)

Only after PR A validation:

- Controlled manual coupon push to Salla (`create_coupon`).
- Separate PR; requires Full API link + successful import test.

---

## 8. Phase 2 status

**Coupon Phase 2 remains blocked** until:

1. Full API link complete (`api_sync_enabled` + `refresh_token` + enabled integration).
2. `POST /coupons/sync-salla` successfully imports Salla coupons (non-zero or confirmed empty store).
3. Controlled push PR merged and validated separately.

---

## Code references

| Topic | Path |
|-------|------|
| Embedded status UI | `dashboard/src/pages/SallaEntryScreen.tsx` |
| Integration status API | `backend/routers/salla_oauth.py` → `salla_integration_status` |
| Login flags | `backend/store_integration/salla_login_flags.py` |
| Adapter selection | `backend/store_integration/registry.py` |
| Coupon sync endpoint | `backend/routers/coupons.py` → `sync_salla_coupons` |
| Coupon import | `backend/services/store_sync.py` → `sync_coupons` |
| Salla coupon API | `backend/store_adapters/salla_adapter.py` → `get_coupons`, `create_coupon` |
| Dual integration tests | `tests/test_salla_dual_integration.py` |
