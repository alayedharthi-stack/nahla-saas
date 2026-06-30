# WhatsApp Token Durability — Root Cause Notes

## Why a demo connection can look "disconnected" after weeks

Most Nahla demo / manual connects that stop working after ~30–60 days trace to **Meta access token expiry**, not webhook routing or `phone_number_id` drift.

### Typical failure chain

1. Admin or embedded signup stores a **User token** or **60-day System User token** in `WhatsAppConnection.access_token`.
2. Token works initially → `status=connected`, `sending_enabled=true`, webhooks arrive.
3. After expiry Meta returns Graph **error 190** on send / debug_token.
4. Without proactive validation, the merchant sees failed sends or "needs reauth" while the DB row may still show `connected` until a health pass runs.

### What usually does *not* cause silent full disconnect

| Signal | Usually stays stable |
|--------|---------------------|
| `phone_number_id` | Yes — routing key unchanged |
| Webhook subscription | May lapse but guardian re-subscribes if token still valid |
| WABA ownership | Unchanged unless merchant revokes app |

### Cron / sync that affects operational state

- `run_wa_token_refresh_scheduler` — refreshes **embedded** long-lived user tokens only.
- `run_webhook_guardian` — re-subscribes webhooks; fails if token dead.
- **No cron** should set `status=disconnected` without an explicit merchant/admin action.

## Production onboarding SOP (first real merchant)

**Do not use:**

- Temporary Access Token from Meta API Setup page
- Short-lived User OAuth token from a personal Facebook login

**Use:**

- **Permanent System User Access Token** from Meta Business Manager → System Users → Generate Token, scoped to the merchant WABA (`whatsapp_business_management`, `whatsapp_business_messaging`).

**Before enabling production sending:**

1. Run admin force-connect or set-token with the permanent token.
2. Confirm `debug_token` shows `type=SYSTEM_USER`, `is_valid=true`, no near-term `expires_at`.
3. Confirm `production_ready=true` and `health_status=healthy` in admin response / DB metadata.
4. If token expires within 60 days, plan renewal **before** go-live — merchant must not manage monthly token rotation.

## This PR mitigations

- Encrypt tokens at rest (`enc1:` + Fernet, `WA_TOKEN_ENC_KEY`).
- Validate on admin write + `commit_connection`; block `sending_enabled` for non-production tokens.
- Periodic `wa_token_health` job: updates `health_status`, disables sending on expiry, **never** silent `disconnected`.
- Admin warnings at 14 / 7 days before expiry.
