# Checkout Owner & Local Draft Authority

## 1. Who owns checkout turns after catalog selection?

**OrderFlowV2** (`try_handle_order_flow_v2`) is the single checkout owner for WhatsApp
catalog / local-draft checkout. It runs before Brain in the webhook and may call Brain
only for safe phrasing when explicitly delegated.

Brain must not independently route active-checkout turns to catalog browse, `track_order`,
or payment fallbacks.

## 2. How local draft DB becomes active checkout evidence

`checkout_authority.load_local_draft_evidence()` loads open `nahla-wa-{tenant}-{conversation}`
orders from the local Order DB. When present:

- `local_draft_authoritative` is stamped in `order_prep`
- line items / totals are rehydrated when volatile `order_prep` is empty
- `draft_order_reference` / `order_creation_status=created` when `external_order_number` exists

Active checkout is true when DB draft exists **even if** `order_prep.line_items` is empty.

## 3. How Brain is prevented from overriding checkout owner

1. Webhook runs OrderFlowV2 first; when **operational** (see §5), V2 sends the reply.
2. `active_whatsapp_checkout()` gates V2 handlers (name, catalog ack, order number, delivery).
3. Brain fallbacks (`commerce_reply_quality_guard`, `resolve_track_order_fallback`,
   `has_active_commerce_from_state`) consult local-draft evidence so name-like text,
   catalog-selection ack, and order-number questions do not escape to browse / `no_orders`.

## 4. How order reference is surfaced from persisted DB

`build_checkout_order_number_reply` and `resolve_track_order_fallback` read
`external_order_number` from the persisted Order row — never invent a reference.
Creation ACK (`build_order_created_reply`) is sent only when a persisted reference exists
and `creation_ack_sent` is not yet set.

## 5. How V2 is enforced only for canary/test traffic

`order_flow_v2.enforcement.resolve_order_flow_v2_operational()`:

| Condition | V2 sends replies |
|-----------|------------------|
| `ORDER_FLOW_V2_ENABLED=true` | All allowed traffic |
| Shadow + `store_ai_mode=test` + allowlisted phone + billing + not paused/handoff | Canary path only |
| Otherwise shadow | Logs only, `handled=False` |

No global unsafe flip; tenant 33 is not hardcoded (only existing test allowlist applies).

## 6. How payment credential claims remain truth-bound

- `payment_credential_guard` blocks fake IBAN / wrong bank.
- Webhook payment hard-override replaces reply with “هذه بيانات التحويل” **only** when a
  sendable payment asset (media/url) is actually attached — never on bank name alone.
- OrderFlowV2 `build_payment_bank_mismatch_reply` when requested bank is unavailable.
