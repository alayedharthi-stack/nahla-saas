# Phase 1A Security Hardening — Operator Runbook

This document captures every **manual** step needed to complete Phase 1A in
production. The code changes shipped with this PR are useless until each
control is also enabled at the platform / DNS / dashboard level. Work
through the sections in order.

---

## 1. Railway environment variables

Set the following in **Railway → your-service → Variables** before
redeploying. The `preflight_check.py` script invoked from `start.sh`
will refuse to start the worker when any of these are missing or
matching a known placeholder.

| Variable | Required | Notes |
|---|---|---|
| `JWT_SECRET` | yes | 64-char random. `openssl rand -hex 32` |
| `ADMIN_EMAIL` | yes | The platform owner's email |
| `ADMIN_PASSWORD` | yes | ≥ 12 chars, NOT `nahla-admin-2026` / `12345678` |
| `WHATSAPP_VERIFY_TOKEN` | yes | ≥ 16 chars, NOT `nahla2025`. Update Meta webhook config to match. |
| `DATABASE_URL` | yes | Postgres URL (with `?sslmode=require` recommended) |
| `REDIS_URL` | recommended | Enables shared rate limiting + JWT revocation across workers. Use the Railway Redis plugin. |
| `SENTRY_DSN` | recommended | Backend DSN. No-op when empty. |
| `VITE_SENTRY_DSN` | recommended | Dashboard DSN (separate Sentry project). Set in the dashboard service. |
| `SENTRY_TRACES_SAMPLE_RATE` | optional | Default `0.1`. |
| `ENABLE_ADMIN_DEBUG` | NO | MUST stay `false` in production. |

After saving, **redeploy** so the new vars take effect. The deploy log
should show:

```
[start.sh] running preflight checks…
[ ok ] JWT_SECRET
[ ok ] ADMIN_PASSWORD
[ ok ] WHATSAPP_VERIFY_TOKEN
[ ok ] ADMIN_EMAIL
[ ok ] DATABASE_URL
[preflight] all production checks passed.
```

If any line is `[FAIL]`, fix the variable and redeploy.

> **Emergency boot.** If a variable is genuinely missing and the deploy
> must come up RIGHT NOW (e.g. mid-incident), set `NAHLA_SKIP_PREFLIGHT=1`
> in Railway. Restore it to unset / `0` immediately after the incident.

---

## 2. Rotate the secrets that previously had defaults

The following values shipped with placeholders that may have leaked into
docs / past deploys. Treat them as compromised and rotate.

1. **`JWT_SECRET`** — generate a new value, set in Railway, redeploy.
   Every existing JWT becomes invalid (clients re-login automatically).
2. **`WHATSAPP_VERIFY_TOKEN`** — generate, set in Railway, redeploy,
   then go to **Meta Business → WhatsApp → Configuration → Webhook**
   and update the verify token. Re-verify the webhook.
3. **`ADMIN_PASSWORD`** — set a new password in Railway. The env-fallback
   admin login uses this value directly. Do NOT keep
   `nahla-admin-2026`.
4. **`DEBUG_ADMIN_TOKEN`** (only if you have ever set it) — also rotate.

Optional but encouraged: rotate `API_SECRET_KEY`, `SALLA_WEBHOOK_SECRET`,
`ZID_WEBHOOK_SECRET`, `D360_WEBHOOK_INTERNAL_SECRET`.

---

## 3. Recover an admin password (replacement for the deleted endpoint)

The `GET/POST /admin/debug/reset-admin` HTTP endpoint was removed.
The replacement is offline:

```sh
railway run python scripts/reset_admin_password.py \
    --email admin@nahlah.ai \
    --password "$(openssl rand -base64 24)"
```

The script writes a bcrypt hash directly into `users.password_hash` via
`DATABASE_URL`. It refuses passwords shorter than 12 chars and refuses
known placeholders.

---

## 4. Cloudflare — manual setup

Code can't enable Cloudflare; do this in the Cloudflare dashboard.

### 4.1 Add the zone

1. Add `nahlah.ai` to Cloudflare. Switch the registrar's nameservers
   to Cloudflare's. Wait for "Active" status.
2. **Free plan + WAF Managed Rules** is the recommended starting tier.

### 4.2 DNS records (proxy through Cloudflare)

| Type | Name | Target | Proxy |
|---|---|---|---|
| `CNAME` | `api` | `<railway-app>.up.railway.app` | **Proxied (orange cloud)** |
| `CNAME` | `app` | `<railway-app-dashboard>.up.railway.app` | **Proxied** |

Confirm `dig api.nahlah.ai +short` returns a Cloudflare IP (e.g.
`104.x.x.x`), not the Railway IP.

### 4.3 SSL / TLS

* **SSL/TLS → Overview → Full (strict)**.
* **Edge Certificates → Always Use HTTPS = ON**.
* **Edge Certificates → HSTS** — enable with `max-age=31536000`,
  include subdomains, preload (only after confirming all subdomains
  serve HTTPS).
* **Edge Certificates → Minimum TLS Version = 1.2**.

### 4.4 WAF + bots

* **Security → WAF → Managed Rules** — enable the Cloudflare Managed
  Ruleset on `nahlah.ai`. Action: Block.
* **Security → WAF → Tools → Rate limiting rules** — add:
  - `(http.request.uri.path eq "/auth/login" or http.request.uri.path eq "/auth/login-form")` → 10 req / 1 min per IP, action: Block 5 min.
  - `(http.request.uri.path eq "/auth/forgot-password")` → 5 req / 5 min per IP, action: Block 15 min.
* **Security → Bots → Bot Fight Mode = ON** (free tier).

### 4.5 Backend allowlist

Inside Railway, add an outbound check that rejects any traffic NOT
coming from Cloudflare. Two simple options:

* **Cloudflare Authenticated Origin Pulls** — Cloudflare presents its
  client cert; Railway / your origin verifies it. This is the
  cleanest option.
* **IP allowlist** — restrict Railway's public networking to
  Cloudflare IP ranges from <https://www.cloudflare.com/ips/>.

Document the chosen option in this runbook so the next incident
responder knows which knob to look at.

---

## 5. GitHub repository security

Code in this PR adds Dependabot config and a gitleaks workflow. The
following toggles must be enabled by a repo admin:

1. **Settings → Code security and analysis**:
   * Dependency graph: **Enable**
   * Dependabot alerts: **Enable**
   * Dependabot security updates: **Enable**
   * Secret scanning: **Enable**
   * Push protection: **Enable** (rejects commits containing leaked secrets at push time)
2. **Settings → Branches → Branch protection rule** for `main`:
   * Require status checks to pass: include `gitleaks` and `lint-and-test`.
   * Require pull request reviews before merging.
   * Require signed commits (recommended).
3. After merge, watch the **Security** tab for any backlog finding
   from gitleaks / Dependabot and triage.

---

## 6. Sentry projects

Create two projects in the Nahla Sentry org:

* **`nahla-backend`** (Python / FastAPI) — DSN ⇒ `SENTRY_DSN` in the
  backend Railway service.
* **`nahla-dashboard`** (JavaScript / React) — DSN ⇒ `VITE_SENTRY_DSN`
  in the dashboard Railway service. (`VITE_*` vars are baked into the
  static build, so a redeploy is required after setting.)

In each project:

* **Settings → Data Scrubbing** — enable all default scrubbers AND add
  custom rules for `password`, `access_token`, `refresh_token`,
  `whatsapp_token`, `salla_access_token` to be safe in case the
  before-send hook regresses.
* **Alerts → Issue alerts** — set a high-priority alert for any
  `level:error` event in production. Route to the team's preferred
  channel (Slack / email).

---

## 7. Smoke tests after deploy

Run these BEFORE handing the deploy back to merchants:

```sh
# 1. Liveness — should return 200 in < 100ms
curl -i https://api.nahlah.ai/alive

# 2. Auth ping (no preflight)
curl -i https://api.nahlah.ai/auth/ping

# 3. Login rate limit — 6th attempt within 15min should 429
for i in 1 2 3 4 5 6; do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST https://api.nahlah.ai/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"x@x","password":"wrong"}'
done
# Expect: 401, 401, 401, 401, 401, 429

# 4. Logout revocation — second auth call should 401
TOKEN=$(curl -s -X POST https://api.nahlah.ai/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"<your-email>","password":"<your-password>"}' \
  | jq -r .access_token)
curl -i -X POST https://api.nahlah.ai/auth/logout -H "Authorization: Bearer $TOKEN"
curl -i https://api.nahlah.ai/auth/me -H "Authorization: Bearer $TOKEN"
# Expect: 401 invalid_token

# 5. Removed admin recovery endpoint — should be 410
curl -i https://api.nahlah.ai/admin/debug/reset-admin
# Expect: 410 Gone

# 6. Public debug surface in production — should be 404
curl -i "https://api.nahlah.ai/debug/version?debug_token=anything"
# Expect: 404
```

Triage any deviation before announcing the deploy.

---

## 8. Rollback plan

Every change in Phase 1A is additive or fail-soft:

* **Rate limit issues** — set `REDIS_URL` to empty string to fall back
  to the in-process limiter. Counts will reset on each worker restart
  but logins keep working.
* **Preflight false positive** — set `NAHLA_SKIP_PREFLIGHT=1` to boot
  through; fix the root cause within the same maintenance window.
* **Sentry noise** — set `SENTRY_DSN=""` to immediately stop event
  delivery without a redeploy (the SDK reads the DSN once at startup,
  so a process restart is required for the empty value to take
  effect).
* **JWT shortened to 24h** — set `JWT_EXPIRE_HOURS=72` if a wave of
  re-logins overwhelms the dashboard. Restore to 24 within a week.
* **Cloudflare WAF false positive** — disable the offending Managed
  Rule via Cloudflare dashboard, file a bug, re-enable after fix.
