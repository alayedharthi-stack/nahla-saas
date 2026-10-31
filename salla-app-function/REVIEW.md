# Salla Communication WhatsApp: local review candidate

Action binding: `communication.whatsapp.send` in Partner app `2067202718`.
The ordinary `POST /webhook/salla` subscription is observation-only. The
Function's sole send route is `POST /webhook/salla/communication-function`.
It uses an independent HMAC key, timestamp and nonce; the backend reserves a
nonce and an event send attempt durably before calling Meta. A repeated or
unknown outcome cannot trigger another provider send automatically.

## Proposed App Settings schema (nothing has been saved)

| Field | Type | Initial state | Purpose |
| --- | --- | --- | --- |
| `pilotStoreId` | text | unset | Exact demo merchant ID; no general store access. |
| `pilotRecipient` | text | unset | Exact owner test number; other recipients receive an explicit error. |
| `handoffMode` | enum (`preview`, `live`) | `preview` | Network calls are disabled unless explicitly set to `live`. |
| `handoffUrl` | URL | unset | Must be HTTPS on `api.nahlah.ai`, exact Function path, no credentials/query/fragment. |
| `handoffSecret` | secret | unset | Must match backend `SALLA_COMM_FUNCTION_HMAC_SECRET`; it must differ from the ordinary `SALLA_WEBHOOK_SECRET`. No value is included here. |

Backend pilot flags `SALLA_COMM_WHATSAPP_ENABLED`, `..._TENANTS`,
`..._STORES`, and `..._RECIPIENTS` also default to disabled/empty. No
secret was generated, copied, or entered.

## Receipt semantics

The Function returns `Resp.success` only after the backend records a Meta
provider message ID; the data says `provider_accepted` and `delivery` remains
`unconfirmed` until a verified Meta status moves it to delivered/read. A
network timeout or unknown provider outcome returns an error and is blocked
from automatic retry. In contrast, ordinary webhook HTTP 200 is merely an
inbox receipt and cannot cause a send.

## Activation blockers

1. Salla's Partner Portal lists no saved Function and its App Settings builder
   has no fields. The demo store still selects Salla's default WhatsApp
   provider. Nothing was changed there.
2. Salla's documented Preview runs code against real demo-store data. No
   reliable Preview flag in `context` is documented. For review, use the
   separate `communication-whatsapp-preview.mjs`: it contains no network or
   key path and returns `delivery=not_attempted` even if live settings are
   accidentally supplied. It is a replacement preview artifact, not a second
   action handler. The live file's default `handoffMode=preview` also fails
   closed, but previewing the live file after changing settings to `live`
   could call production. Do not run that combination or claim intrinsic
   Preview isolation. A verified runtime mode signal or an approved staging
   endpoint would be needed to remove this operational restriction.
3. Selecting this App as the WhatsApp handler routes the entire store's
   WhatsApp channel to it. The owner-only pilot explicitly rejects all other
   recipients. Verify there are no other active recipients or design an
   authorized full-channel handler before selection; do not silently drop
   notifications.
4. Only existing Meta service-window text is supported. No approved order
   status template is verified, and a closed window produces a hard error.
5. The isolated PostgreSQL concurrency and migration 0125 upgrade/downgrade
   proofs used local PostgreSQL 17.10, not production PostgreSQL 18. Alembic
   history shows `0124 -> 0125` with no second `0125` revision; existing other
   migration heads remain. PostgreSQL 18 is not installed on this computer,
   and Docker's local daemon is unavailable. An isolated migration/integration run on 18,
   Salla Function runtime preview, full review, and approved owner-only live
   test remain before any publish or handler selection.

No Salla Function was saved, no provider handler switched, no key configured,
and no WhatsApp message sent in this preparation.
