# Dormant Shopify install-first owner onboarding

The existing connection flow requires a Nahlah merchant session before OAuth. This Draft adds a separate path: a Shopify owner opens the app, authorizes it, and receives a new Nahlah tenant and a short authenticated session without signup. Repeat installation resolves the same explicit shop/owner mapping.

Development only. No production migration, deployment, credentials, app dashboard changes, distribution selection, payments, or new hosted environment are authorized by this change. Production remains on the previously reviewed dormant release. Revision 0124 and its two new tables are outside application startup `Base.create_all`; the prior S3 inventory/expectations remain frozen and do not approve this revision.

## Authentication choice

Recommend a standalone authorization-code flow because Nahlah already serves an independent dashboard. The app's current embedding setting alone is not the architecture decision. Embedded managed installation would require an embedded frontend, App Bridge ID-token validation and a different user/session boundary. Current Shopify guidance distinguishes standalone authorization codes, online user identity and offline background authority. This implementation requests an online grant (`grant_options[]=per-user`) for owner proof, then a separate expiring offline grant for a new/reinstalled connection. The sole scope remains `read_products`. [Standalone authentication](https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps), [access tokens](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens).

Owner-only entry is a Nahlah security policy: the current merchant role has tenant-wide authority. Staff/collaborators are refused, even when permitted to install Shopify apps. Account ownership changes require a separately reviewed recovery policy; they never silently transfer Nahlah history or other integrations.

## Protocol and ownership

1. The fixed public dashboard entry `/app/shopify?shop=...` scrubs launch parameters before telemetry. The shop parameter is an untrusted hint, not authentication. With both UI gates enabled it generates a browser secret, retains it with an expiring hashed Nahlah-session binding in sessionStorage, and submits a top-level form to the API. The API requires the exact dashboard Origin and sets a Secure, HttpOnly, host-only, SameSite=Lax cookie. This avoids dependence on third-party cookies when dashboard and API use different sites.
2. The API records a hashed state, browser binding, canonical shop, exact callback fingerprint, phase and deadline. The callback requires Shopify HMAC, freshness, unique parameters, correct state and the original cookie. Both phases are single-use and consumed before network calls.
3. The online exchange requires integer `associated_user.id`, strict `account_owner=true`, `collaborator=false`, and the exact read-only scope. An authenticated GraphQL identity check binds the grant to the expected shop GID/domain. No email, display name, supplied tenant ID, existing browser tenant, or first merchant lookup establishes ownership.
4. The offline phase shares the existing per-shop exchange/refresh lease and checks generation fences. Tenant, passwordless merchant principal, owner mapping, encrypted expiring credentials and completion state commit atomically. Internal subjects use the reserved `.invalid` domain; no email or password-setup message is sent. An existing connection without a mapping fails closed. A concurrent loser reuses the winning mapping without acquiring another offline token.
5. A short opaque handoff reaches the fixed dashboard route in the fragment. Redemption requires the independently retained browser secret and unchanged browser Nahlah session. It verifies the online token again, preserves existing Nahlah TOTP, rechecks identity/activity/generation and MFA enrollment before issuing a scoped merchant JWT.

The online token is temporarily encrypted with flow/shop-specific authenticated data and erased on completion/failure/expiry. No pre-authentication JWT is issued. The existing recovery scheduler sweeps expired transactions only under the new gate, quarantining uncertain offline exchange outcomes. Logout/revocation follows existing JWT revocation, with additional database validation of every Shopify session.

## Session boundary and remaining product choices

Authority ends at most five minutes after the online owner proof (not five minutes after a delayed handoff). This intentionally conservative Draft reauthenticates through Shopify instead of native rolling refresh. Database checks reject disconnected/quarantined shops, old generations, inactive/demoted identities and expired flows. A Shopify logout/owner change without an immediately processed webhook can remain effective until that short absolute deadline; a shop identity query does not refresh owner status.

Generic refresh and Salla session reissuers cannot turn this scoped session into a normal long-lived JWT. Generic authentication dependencies reject purpose-specific setup/challenge tokens; their dedicated decoders remain responsible for those flows. Normal Salla sessions keep their existing path.

Before activation, review the owner-only/ownership-transfer policy, five-minute renewal experience, MFA recovery, transaction retention, same-app scope/distribution requirements and a new migration/release gate. Catalog import, inventory permissions, Meta/WhatsApp and AI behavior are outside this slice. This is not an App Store readiness claim.

New Nahlah TOTP enrollment is explicitly refused for passwordless Shopify sessions until a scoped recovery/disable path is implemented. Native MFA currently depends on a password for disabling, and its recovery-code login assumes native credentials. Offering enrollment now would trap a merchant who lost their authenticator. Existing mapped TOTP remains mandatory at redemption; it is never silently bypassed. Normal password-backed accounts retain existing MFA behavior.

## Verification record

Initial local protocol/router checks passed (25 cases). Existing Salla authentication/identity/OAuth regressions: 46 passed, 3 explicitly skipped; existing coroutine/deprecation warnings remained. Local dashboard TypeScript and production build passed after the approved normal build process ran outside a filesystem restriction. Return-capture VM checks passed. More regression cases and independent review fixes were added afterward; final results are recorded in the PR and follow-up execution evidence.

Local PostgreSQL cases are skipped because no disposable PostgreSQL server is available and Docker's daemon is stopped. The branch's dedicated GitHub Actions proof runs actual PostgreSQL, the existing connection lifecycle suite, a dashboard build and real Chromium navigation with synthetic credentials and an isolated local endpoint. Skips are not evidence of passing database behavior.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
