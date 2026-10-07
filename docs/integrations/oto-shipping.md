# OTO shipping for Nahlah AI / نحلة

## State

The code is dormant until the `0117` migration is reviewed and applied, `OTO_TOKEN_ENC_KEY` is configured, and `OTO_EXTERNAL_EGRESS_ENABLED=1` is set. Production additionally requires `OTO_PRODUCTION_ENABLED=1`. These switches are deliberately independent of existing WhatsApp, Salla, AI, and commerce settings.

`OTO_EXTERNAL_EGRESS_ENABLED=1` is the single activation switch for every OTO surface. Until it is set: every merchant route under `/oto` answers `409 oto_integration_disabled` before any database read (so a deployment without migration `0117` or `OTO_TOKEN_ENC_KEY` stays inert instead of failing with 500s); the public webhook `/oto/webhooks/{environment}/{event_type}` answers `404` without reading the body or the database; the WhatsApp label notice is refused; and the order page keeps the existing internal shipment card — the OTO panel appears only when `GET /oto/availability` reports `enabled: true`. Covered by `tests/test_oto_dormant_by_default.py`.

When switched on, the webhook reads at most 32 KiB of request body: a declared `Content-Length` above that is refused with `413` before any body byte is read, a body without `Content-Length` (chunked) is streamed and refused as soon as it passes 32 KiB, a malformed `Content-Length` (anything but ASCII digits) is refused with `400`, and a client that disconnects mid-body gets `400 client_disconnected` rather than an unhandled error; every refusal carries `Connection: close`, so the server closes the connection instead of draining the rest. Covered against a real uvicorn server (h11 and httptools, bare and behind a pass-through `BaseHTTPMiddleware`) by `tests/test_oto_webhook_body_limit.py`. Responses from OTO itself are not size-capped yet; a limit needs OTO's documented maximum response sizes.

No Nahlah-specific Marketplace master token, merchant refresh token, or completed commercial activation was found during the October 2026 context review. A successful local build is not an OTO end-to-end test.

## Merchant flow

1. Merchant enters their OTO refresh token and pickup location code in an authenticated Nahlah session. Only encrypted values are stored. The connection remains disabled.
2. Merchant verifies the connection against OTO's pickup-location list. The displayed status becomes active only on a successful authenticated response.
3. For a WhatsApp or manual Nahlah order, the merchant enters destination city and package dimensions, requests live OTO rates, then selects a delivery option.
4. Nahlah checks its existing payment/address/duplicate-shipment gates. It reserves the order's single shipment row before calling OTO. If the external result is uncertain, the row stays in `oto_needs_reconciliation`; staff must inspect OTO before any retry.
5. `createShipment` acknowledgement is stored as `oto_shipment_requested`, never as delivered or shipped. Signed OTO webhook events or a merchant-triggered `orderStatus` refresh provide tracking and fulfillment evidence.
6. Merchant retrieves/prints an AWB URL. The merchant may explicitly send the AWB as a WhatsApp document with tracking details through the connected store number while the 24-hour service window is open. A Meta API acceptance is displayed as acceptance, not delivery.

Cancellation is guarded by an OTO shipment ID and the pre-pickup state. OTO's acknowledgement is stored as a request, not a completed cancellation. Return shipments and merchant commands sent to the platform WhatsApp number are not activated in this stage.

## Activation checklist

- Confirm the target DB revision and review migration `0117` because this repository has multiple Alembic branches. No migration is run by application startup.
- Configure a dedicated Fernet `OTO_TOKEN_ENC_KEY` in the deployment secret store. Never put it, a refresh token, Marketplace token, or webhook secret in Git or screenshots.
- Obtain a merchant staging token from OTO, staging pickup details, and a signed webhook test. Use staging before enabling production.
- Register `orderStatus` and `shipmentError` callbacks under `/oto/webhooks/{environment}/{event_type}` with an OTO-provided signing secret; confirm the exact signature algorithm with OTO because its documentation shows both a long RSA-like example and HMAC-SHA256 prose.
- Reconcile a test order across quote, create, tracking, AWB, WhatsApp document, cancellation, and any return path before production rollout.

## OTO clarifications needed

- Marketplace master token and merchant-account onboarding: after `register`, how does Nahlah receive or obtain each merchant's shipment-capable refresh token without manual copy/paste?
- Staging access for Nahlah AI, enabled services/carriers, price schedules, COD settlement, pickup rules, billing/fees, and production approval.
- Sample actual responses for `checkOTODeliveryFee`, `orderStatus`, `/print/:orderId`, and `cancelShipment`; identify the authoritative `shipmentId` field.
- The exact webhook signature algorithm/key format and whether delayed events can arrive beyond five minutes.
- Whether the OTO AWB URL is directly fetchable by Meta for a WhatsApp document and its expiration period.
- Return-shipment identity, tracking, and label behavior for merchants without Salla.

Official references: [Authorization](https://help.tryoto.com/en/support/solutions/articles/150000213805-authorization), [Marketplace](https://help.tryoto.com/en/support/solutions/articles/150000213807-marketplace-apis), [Orders](https://help.tryoto.com/en/support/solutions/articles/150000213808-orders-apis), [Shipments](https://help.tryoto.com/en/support/solutions/articles/150000213809-shipments-apis), [Tracking](https://help.tryoto.com/en/support/solutions/articles/150000213812-tracking-apis), [AWB](https://help.tryoto.com/en/support/solutions/articles/150000213811-shipping-label-awb-apis), [Webhook](https://help.tryoto.com/en/support/solutions/articles/150000213819-webhook), [Pickup](https://help.tryoto.com/en/support/solutions/articles/150000213814-pickup-locations), [Returns](https://help.tryoto.com/en/support/solutions/articles/150000213810-return-shipments-apis).
