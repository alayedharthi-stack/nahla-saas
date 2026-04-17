# WhatsApp Half-Bootstrapped Connection — RCA

**Status:** open
**Severity:** medium (no message-send breakage; merchant UX + admin observability degraded)
**Owner:** platform team
**First seen:** 2026-04-16, surfaced via `/admin/troubleshooting/tenants/1/whatsapp`

## 1. Snapshot — what production told us

Captured 2026-04-17 23:19:21 KSA (commit `b12d1eb`, before security restore in `ecc0dd7`):

```jsonc
{
  "tenant_id": 1,
  "tenant_name": "متجر تجريبي 1",
  "connection": {
    "status": "connected",
    "phone_number": null,                  // ← BUG #1
    "business_display_name": null,         // ← BUG #1
    "connection_type": "cloud_api",
    "provider": "meta",
    "webhook_verified": true,              // ← misleading: webhook OK, but
    "sending_enabled": true,               //    user OAuth session is dead
    "extra_metadata": {
      "oauth_debug": {
        "is_valid": false,                 // ← BUG #2 — masked by platform fallback
        "expires_at": 1776164400           //    (= 2026-04-15 19:00 UTC, expired)
      },
      "token_health": "healthy",           // ← reports the *platform* token,
      "active_token_source": "platform",   //    not the merchant's
      "oauth_session_status": "invalid",
      "oauth_session_needs_reauth": true,  // ← never surfaced in admin UI
      "active_graph_token_source": "platform"
    },
    "updated_at": "2026-04-17T17:47:43.476707"
  },
  "usage": []
}
```

## 2. Root causes

### BUG #1 — half-bootstrapped row: `phone_number` / `business_display_name` are `NULL`

**Where:** `backend/services/whatsapp_connection_service.py::commit_connection`, lines 229–232.

```python
if phone_number:
    conn.phone_number = phone_number
if display_name:
    conn.business_display_name = display_name
```

`commit_connection` is the **only canonical write path** that flips a row to
`status="connected"` (per the docstring at the top of the file). It marks the
row connected unconditionally, but persists the display fields **only when
the caller passes them explicitly**.

Two callers do not always pass them:

- `backend/routers/admin.py::admin_force_connect_whatsapp` reads from a request
  body where both fields are optional. When admins use the force-connect endpoint
  to bootstrap a number quickly (paste-only the three IDs + token), the columns
  stay `NULL` even though the row flips to `connected`.
- `backend/routers/whatsapp_connect.py::_finish_connect` (line 412) only forwards
  the metadata it already has. Some legacy entry points that route through
  `commit_connection` do not look up `display_phone_number` / `verified_name`
  from Meta first.

We already have the helper that *can* fetch them — `resolve_waba_for_phone`
in the same file fetches `id,display_phone_number,verified_name,whatsapp_business_account`
in one Graph call. We just don't use the first two fields after the call.

**Why it matters**

- Merchant Settings page renders `business_display_name · phone_number` as
  the connected-channel label. A `NULL` here forces it back to "متصل" with no
  identifier; merchants cannot tell *which* number is connected.
- Admin troubleshooting page hides the number column when `phone_number` is
  null, so support staff also cannot tell which number this tenant is on.
- `WhatsAppUsage` has no rows because nothing ever attributed conversations
  back to a phone the system "knows" the display name for.

### BUG #2 — broken merchant OAuth session is masked by the platform token

**Where:** the **token-pick chain** in `backend/services/whatsapp_platform/token_manager.py`.

The merchant's user-OAuth token expired on **2026-04-15 19:00 UTC**, two days
before the snapshot was taken. The token manager correctly detected this
(`oauth_session_status: "invalid"`, `oauth_session_needs_reauth: true`) and
gracefully **fell back to the platform-level token** so message-send keeps
working (`active_token_source: "platform"`).

However:

1. The fallback decision is invisible to merchants — Settings already shows
   the amber "needs reauth" banner, but only when `connected === true`. With
   the platform fallback, `sending_enabled` stays `true` and most merchants
   will never notice anything is wrong until something user-scoped fails
   (re-subscribe, template sync, business-account read).
2. The admin troubleshooting endpoint returns `oauth_session_*` fields
   nested inside `extra_metadata.oauth_debug` — they are not promoted to
   top-level fields and are not displayed by `AdminTroubleshooting.tsx`.
   Support staff have to inspect the JSON manually to find them.

**Why it matters**

The longer the merchant runs on the platform token, the harder it is to
recover later: when the platform token also rotates / is revoked, the only
way back is a full re-auth — and at that point we already lost confidence
in their conversion windows because their OAuth-required actions silently
returned partial data.

## 3. Blast radius

Tenants potentially affected: every `WhatsAppConnection` where
`status='connected'` AND (`phone_number IS NULL` OR `business_display_name IS NULL`).

A one-off SQL audit:

```sql
SELECT tenant_id, id, phone_number_id, status, sending_enabled,
       phone_number, business_display_name, updated_at
FROM   whatsapp_connections
WHERE  status = 'connected'
  AND  (phone_number IS NULL OR business_display_name IS NULL);
```

For OAuth session masking, the audit is:

```sql
SELECT tenant_id, id, phone_number_id, status,
       extra_metadata->>'oauth_session_status'        AS oauth_session_status,
       extra_metadata->>'oauth_session_needs_reauth'  AS needs_reauth,
       extra_metadata->>'active_graph_token_source'   AS token_source
FROM   whatsapp_connections
WHERE  status = 'connected'
  AND  extra_metadata->>'oauth_session_needs_reauth' = 'true';
```

## 4. Fix plan (delivered alongside this RCA)

| # | Change | File |
|---|---|---|
| 1 | After the canonical write, if `phone_number` / `display_name` are still missing, fetch them from Meta via `fetch_phone_metadata(phone_number_id, access_token)` and persist. Failures log a warning but never block the connection. | `backend/services/whatsapp_connection_service.py` |
| 2 | New CLI: `scripts/backfill_whatsapp_phone_metadata.py --tenant <id>` (or `--all`) — finds half-bootstrapped rows and fills them in idempotently. | `scripts/backfill_whatsapp_phone_metadata.py` |
| 3 | Admin troubleshoot endpoint promotes `oauth_session_status`, `oauth_session_message`, `oauth_session_needs_reauth`, `active_graph_token_source` to top-level fields on the response, so dashboards don't have to dig into `extra_metadata`. | `backend/routers/admin.py` |
| 4 | Admin troubleshooting dashboard (`AdminTroubleshooting.tsx`) renders an amber **"OAuth session expired — needs re-auth"** banner when `oauth_session_needs_reauth === true`, regardless of `sending_enabled`. | `dashboard/src/pages/AdminTroubleshooting.tsx` |
| 5 | Tests: parity test that `commit_connection` always populates `phone_number` and `business_display_name`, plus a unit test for the backfill walking a fixture row. | `tests/test_whatsapp_phone_metadata.py` |

## 5. Merchant impact while waiting on re-auth

| Action | Works? |
|---|---|
| Inbound conversations from customers | ✅ webhook is verified |
| Outbound message-send (templates, free-form, payment-link nudges) | ✅ falls back to platform token |
| Salla / Zid commerce sync | ✅ unrelated to WhatsApp OAuth |
| **Re-subscribing webhooks** (`POST /{waba_id}/subscribed_apps`) | ❌ requires user token |
| **Template list refresh / template create** | ❌ requires user token |
| Reading WABA business asset metadata (display name, quality rating) | ⚠️ may serve stale values |
| Adding / switching phone numbers under the same WABA | ❌ requires user token |

In short: send keeps working today; **anything that mutates the WABA itself
will fail until the merchant re-authorises**.
