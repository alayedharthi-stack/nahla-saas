# WhatsApp provider: Meta only

Meta WhatsApp Cloud API is the only supported WhatsApp provider. The 360dialog
integration was removed by owner decision; this document records what went,
what stayed, and what is left for a separate decision.

## The rule the code now enforces

A `whatsapp_connections.provider` value is read, never guessed:

| stored value | answer | why |
|---|---|---|
| `meta` | `meta` | the supported provider |
| `''` / `NULL` | `meta` | every Meta row written before the column existed |
| anything else | `unsupported` | a row from an integration this code no longer speaks |

There is deliberately no "default to Meta". Answering `meta` for a leftover row
would send one provider's credential to Meta's API, on a number Meta may never
have had. `services/whatsapp_platform/provider_utils.py` is the single place
that decides, and every path that acts on a connection asks it first.

## The token boundary

A connection row left over from the retired provider still holds a stored
credential. On the reviewed head, a send for such a row resolved its token
**before** the provider refusal ran: the credential was read, and — because it
was expired — exchanged at Meta's OAuth endpoint as if it were a Meta token,
the row's expiry rewritten on Meta's `190`, and token state committed onto the
row. Only then did the request wrapper refuse. Each of those is now impossible
by construction:

| Boundary | For a row naming a retired provider |
| --- | --- |
| `get_token_for_operation` | raises `UnsupportedWhatsAppProvider` before any candidate is built, before any refresh, before any persistence |
| `get_token_context` / `get_token_candidates` / `build_token_context` / `get_oauth_session_state` | answer an `unsupported_provider` context without reading the stored credential |
| `_refresh_merchant_long_lived_token` | returns `None` without reading the credential; nothing is exchanged at `graph.facebook.com/oauth/access_token` |
| `update_token_state` / `persist_token_context` | write nothing and commit nothing |
| the scheduled refresh (`_refresh_all_wa_tokens`) | skips the row |
| `provider_send_message`, `graph_get`, `graph_post`, `provider_submit_template` and the other wrappers | refuse **before** token resolution and record the attempt as a definite provider failure (`provider_error_field`, no status) — never the ambiguous transport exception that invites a resend |

The reviewer's reproduction — an expired retired-provider credential driven
through the real `provider_send_message` — is
`tests/test_whatsapp_meta_only_provider.py::test_an_expired_retired_credential_is_neither_refreshed_nor_rewritten_by_a_send`:
no HTTP client is constructed, the stored credential is never read, the row's
metadata and expiry are byte-for-byte unchanged, and nothing is committed.
Meta rows and the pre-column rows (empty `provider`) resolve and persist exactly
as before.

## Executable surfaces removed

| surface | what happened |
|---|---|
| `POST /webhook/whatsapp/360dialog{,/coexistence,/status}` | still mounted, answer **410 Gone**. No body read, no tenant resolved, no acceptance recorded, no background work scheduled, no path into the Meta pipeline. |
| `_handle_360dialog_body` and its scope/field classification | deleted (949 lines) |
| per-endpoint webhook receipt bookkeeping, coexistence/status/batch event recorders | deleted — the 360dialog loop was their only caller |
| `dialog360_*` Partner/Channel API client (webhook config, WABA webhook, API-key generation, channel metadata, live-verify probes) | deleted |
| `D360-API-KEY` token source, `D360_*` settings | deleted |
| `/whatsapp/coexistence/*`, `/whatsapp/admin/coexistence/*` | deleted (1612 lines) |
| admin `inbound-trace`, `recent-webhook-events`, `core/wa_webhook_observability.py` | deleted — they read only the 360dialog routing ring buffer |
| `core/d360_dispatch_telemetry.py`, `scripts/probe_d360_forwarding.py`, `scripts/triage_inbound_drop.py`, the two back-fill scripts | deleted |
| dashboard `AdminCoexistence` page, route, sidebar entry and API client | deleted |
| inbound media download over `waba-v2.360dialog.io` | deleted (separate PR — see below) |

Meta Embedded Signup **coexistence** — the WhatsApp Business App onboarding
mode, which shares the word and nothing else — is untouched.

## How a leftover row behaves now

| asked | answer |
|---|---|
| send / read through the provider | refused **before the request exists**, recorded as a definite provider failure, never an ambiguous transport exception — so nothing resends on the strength of it |
| `validate_connection_health` | `unsupported_provider`, `is_valid=False` — never "valid" |
| webhook Guardian | skipped, and **not** stamped `webhook_verified` |
| `GET /whatsapp/status`, `/connection/health` | an explicit `unsupported_provider` payload |
| `POST /whatsapp/connection/reconnect` | refused with the same payload; the merchant connects through Meta instead |
| inbound media download | refused before a token is resolved |

## Kept on purpose

* **Every historical row.** No message, connection record, customer or webhook
  archive was deleted; no credential revoked; no external account cancelled.
* `core/coexistence_client_id.py` + `core/coexistence_repair.py` — they sanitise
  `client_id` strings already stored on historical rows.
* The `ROUTE_*` / `EVENT_*` telemetry vocabulary in `core/inbound_lifecycle.py` —
  historical log lines and dashboards still name it.
* `d360-api-key` in the log-redaction and webhook-audit deny lists — historical
  logs can still contain one.
* The acceptance contracts that *detect* `D360_*` environment variables and
  block: with the provider gone they are a safety net against a stale
  environment, and they never satisfy readiness.

## Historical configuration needing a separate decision

Not done here, and each needs its own authorization:

1. `whatsapp_connections` rows whose `provider` column names the retired
   integration, whose `token_type` names its channel-key kind, and which still
   hold a stored channel key.
2. `extra_metadata.coexistence.*` and `provider_details`
   (`channel_id`, `client_id`, webhook secrets) on those rows.
3. Connect requests of kind `coexistence` still in the admin queue.
4. `D360_*` variables in any deployed environment. Unread by this code; the
   acceptance contracts will block on them until they are removed.

## Integration order

The removal is two pull requests because
`tests/test_whatsapp_connection_finalization.py::test_wa_life_09_and_10_no_ai_settings_or_customer_ai_files`
forbids one PR from touching both a WhatsApp connection/trial lifecycle owner
and anything under `backend/modules/ai/`:

1. **`claude/nahla-meta-only-media-normalizer`** — the media-download half.
   Independent, green on its own. **Merge first.**
2. **`claude/nahla-meta-only-remove-360dialog`** — everything else. It deletes
   the provider constants the media half still imports on `main`, so its CI is
   red until (1) merges.
