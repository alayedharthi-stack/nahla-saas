# Shopify regression and acceptance plan

Status: **test plan.** It defines what every Shopify slice must prove and how
results are recorded. It does not change any test, implementation or
existing document. The slice list and permission gates are in
[`shopify-roadmap.md`](shopify-roadmap.md).

## How results are recorded

Every slice records, for **baseline** (the exact base SHA it stacks on) and
**candidate** (its exact tested SHA):

* the affected paths;
* each command as executed, its real exit code, and the passed / failed /
  skipped / errored counts (a skip in a strict suite is a failure, never a
  pass);
* the PostgreSQL versions used (16 and 18) and the authentication mode (TCP
  SCRAM, as in CI);
* which GitHub checks actually ran on the Draft (on a stacked base the full
  CI is filtered; only checks that ran may be called passed).

No assertion is removed or weakened to make a candidate pass. A baseline
failure that also fails on the candidate is reported as pre-existing, with
its node id.

### Evidence levels and status values

| Level | Meaning |
|---|---|
| **U** | unit test, no database, mocks / fakes |
| **D** | integration on disposable PostgreSQL, or a local browser harness (a local dashboard with mocked routes is still D, never R) |
| **R** | observed on a real Shopify development store |

| Status | Meaning |
|---|---|
| **implemented [Pn]** | an automated proof exists; `[Pn]` points to its exact node ids below |
| **partial [Pn]** | a proof covers part of the case; the remainder is named and pending |
| **planned** | no proof exists yet |
| **blocked: owner choice** | the expected outcome depends on a named owner decision; not runnable as an exact expectation yet |
| **NOT RUN — pending permissions** | a real-store journey; nothing at level R is authorized now |

Every case lists its initial state, the events, the expected Shopify and
Nahlah state, the expected messages as exact **buyer / merchant** counts, the
forbidden side effects, the level it must reach, and its status. Counts in
sections C–F are expectations **under the proposed synthetic fixture policy
in section E** — not live product behaviour — and cover only the events of
that row (messages sent before its initial state are not counted).

### Count metrics

Four metrics are kept apart and never substituted for one another:

| Metric | Meaning |
|---|---|
| **logical** | distinct notifications decided to be sent (one per idempotency key) |
| **attempts** | provider send attempts made for them |
| **accepted** | attempts the provider positively acknowledged |
| **delivered** | messages the provider reported delivered (message delivery, never parcel delivery) |

An attempt with an unknown outcome is counted under **unknown**, never as
accepted or delivered. Unless a row states otherwise, the **buyer /
merchant** column gives **logical** counts.

## A. Protect the baseline (Shopify flags off)

| ID | Initial state | Events | Expected state | Buyer / merchant | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| A1 | Salla tenant, Shopify flags off | Salla product, order and shipping flows | identical to baseline | baseline counts | any Shopify table read or written | U + D | partial: the existing Salla suites run beside each Shopify Draft (baseline vs candidate in each validation record); a dedicated "flags off, Salla untouched" end-to-end proof is planned |
| A2 | Meta catalog connected | item creation and updates | Meta item ids and updates unchanged | 0 / 0 extra | Shopify rows reaching Meta | U + D | partial: existing Meta catalog suites run beside each Draft; no Shopify-to-Meta path exists to test |
| A3 | two tenants, one Shopify shop each | each slice's operations | the other tenant unchanged | 0 / 0 | cross-tenant read or write | D | implemented [P1] |
| A4 | billing subscriptions | Shopify flows | billing unchanged | 0 / 0 | billing side effects | U | partial: billing suites run beside each Draft; no Shopify billing path exists |
| A5 | agent tools and conversations | Shopify flows | agent behaviour unchanged | baseline counts | prompt / persona / model / dialect change | U | partial [P2]: the GOV-002 scanner and the constitution suite prove that no prohibited source changed — they do not prove conversation equivalence; baseline vs candidate agent regression runs are still required per Draft |
| A6 | same raw numeric id at Salla and in the Shopify mirror | both writers run | each provider row keyed by its own identity | 0 / 0 | one provider overwriting the other | D | partial [P3]: proves raw-id collisions for **products**; same-SKU and same order-number collisions, and every order case, are planned |
| A7 | mixed-provider tenant (Salla and Shopify) | both active | both independent | baseline counts | provider confusion in shared tables | D | planned |
| A8a | Shopify flags off from the start | requests and scheduler ticks | routes 404, workers idle | 0 / 0 | any Shopify work | U | implemented [P4] |
| A8b | Shopify enabled with queued work (receipts, reconciles, imports) | flags turned off | queued work neither runs nor corrupts state; existing work unaffected | 0 / 0 | dangling writes after disable | D | planned |
| A9 | heavy Shopify backlog | existing workers busy | existing work not starved | baseline counts | Shopify jobs monopolising workers or the shared pool | D | partial [P5]: privacy receipts use a dedicated bounded pool and slots; a whole-system starvation proof is planned |

## B. Connection and privacy

| ID | Initial state | Events | Expected state | Buyer / merchant | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| B1 | two tenants, two shops | connect both | two isolated connections | 0 / 0 | cross-store data | D / R | D implemented [P1]; R NOT RUN — pending permissions |
| B2 | connected store | token expiry, rotation, revoked refresh | refresh, or quarantine and confirmed uninstall; never a stale credential | 0 / 0 | use of a revoked credential | D | implemented [P6] |
| B3 | any | webhook with an invalid signature | 401, nothing stored | 0 / 0 | any write | U + D | implemented [P7] |
| B4 | unknown shop | signed delivery | no tenant; open manual review | 0 / 0 | attribution by guess | D | implemented [P8] |
| B5 | shop owned by tenant A | claim by tenant B | refused; ownership unchanged | 0 / 0 | rebinding | D | implemented [P9] |
| B6 | buyer fields not approved | a flow that would need them | field absent; flow degrades honestly | 0 / 0 | reading or storing an unapproved field | U + D | planned |
| B7 | uninstall queued | late events after the uninstall | quarantine then confirmed tombstone; late events change nothing | 0 / 0 | revival from a replay | D | implemented [P10] |
| B8 | connected store | `customers/data_request`, `customers/redact`, `shop/redact` | durable receipt; bounded assessment; blocked-policy proposal; no erasure without an approved policy | 0 / 0 | fulfilment claims; unapproved erasure | U + D | implemented [P11] |
| B9 | notification records exist (later slices) | `customers/redact` | derived notification data handled under the approved retention | 0 / 0 | retention beyond policy | D | planned; blocked: owner choice (retention) |

## C. Orders (all planned; counts under the section E fixture)

| ID | Initial state | Events | Expected Shopify / Nahlah state | Buyer / merchant | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| C1 | no order | prepaid order, `financial_status = paid` | paid | 0 / 0 (Shopify's own order confirmation; N0) | a Nahlah duplicate of the native confirmation | D / R | planned; R NOT RUN |
| C2a | no order | card order `authorized` | authorized, not paid | 0 / 0 | treating it as paid | D | planned |
| C2b | no order | order `pending` (non-COD manual) | pending | 0 / 0 | treating it as COD or paid | D | planned |
| C2c | no order | `partially_paid` | partially paid | 0 / 0 | treating it as paid | D | planned |
| C3a | open COD order, request sent | edit that is **not** a meaningful revision (for example a note or a tag) | mirror updated; the pending request stays valid | 0 / 0 | a new confirmation request (meaningful revisions are C7a) | D | planned |
| C3b | open COD order, request sent | order cancelled | cancelled; pending COD flow stopped | 1 / 0 (N11) unless natively notified | reminders after cancellation | D | planned |
| C3c | order mirrored | order deleted | mirror tombstone | 0 / 0 | messages for a deleted order | D | planned |
| C4a | paid order | partial refund, transactions verified `success` | partially refunded | 1 / 0 (N10) unless natively notified | — | D | planned |
| C4b | paid order | full refund, transactions verified `success` | refunded | 1 / 0 (N10) unless natively notified | — | D | planned |
| C4c | paid order | `refunds/create` with pending transactions | refund pending | 0 / 0 | a "refunded" claim | D | planned |
| C4d | paid order | refund with a failed transaction | refund failed | 0 / 1 (N12) | a "refunded" claim | D | planned |
| C5 | COD order created, buyer opted in | — | awaiting buyer acceptance | 1 / 0 (N1) | inferring shipment or payment | D / R | planned; R NOT RUN |
| C6a | COD request sent | buyer accepts | accepted | 0 / 1 (N3) | inferring shipment or payment | D | planned |
| C6b | COD request sent | buyer rejects | rejected | 0 / 1 (N4) | shipping a rejected order | D | planned |
| C6c | COD request sent | ambiguous reply | still awaiting; no acceptance recorded | 0 / 0 (clarification is the conversation's job, not a template) | recording acceptance | D | planned |
| C6d | COD request sent | no response by the fixture deadline | reminder once, then manual follow-up | 1 / 0 (N2), at most one | more than one reminder | D | planned; blocked: owner choice for the real deadline |
| C6e | COD accepted | the same acceptance delivered twice | accepted once | 0 / 1 total (N3 idempotent) | a second merchant message | D | planned |
| C7a | COD request sent, order then modified | buyer accepts | acceptance bound to the old version → re-confirmation needed | 1 / 0 (N1 for the new version) | confirming the modified order with the old answer | D | planned |
| C7b | COD request sent, order then cancelled | buyer accepts | stays cancelled | 0 / 0 | reviving the order | D | planned |
| C8a | two open COD orders for one phone, requests sent for both | one reply that cannot be bound to either order | both still awaiting acceptance | 0 / 0 | confirming either order | D | planned |
| C8b | as C8a | one reply correlated unambiguously to order 1 (for example, an answer to order 1's request) | order 1 accepted; order 2 still awaiting | 0 / 1 (N3 for order 1) | confirming order 2 | D | planned; blocked: owner choice (which correlation evidence counts as unambiguous) |
| C9 | order with a manual payment gateway | created | not COD proof | 0 / 0 | starting the COD flow from the gateway alone | U | planned |
| C10 | order with `confirmed = true` | — | inventory reservation only | 0 / 0 | treating it as buyer acceptance | U | planned |
| C11 | COD order delivered, not collected | delivery event | delivered, unpaid | 1 / 0 (N7) | `orderMarkAsPaid`; a "paid" claim | D | planned |
| C12 | COD order delivered | collection verified by the owner-chosen source | paid by verified collection | 0 / 1 (N9) | `orderMarkAsPaid` without explicit merchant authority | D | planned; blocked: owner choice (collection source, write authority) |

## D. Shipping (all planned; counts under the section E fixture)

| ID | Initial state | Events | Expected state | Buyer / merchant | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| D1 | paid order | fulfillment without tracking | fulfilled, tracking unknown | 0 / 0 | invented tracking; a dispatch message | D | planned |
| D2a | fulfilled order | label printed only | label created, not shipped | 0 / 0 | "shipped" from a label | D | planned |
| D2b | label printed | carrier acceptance | in carrier hands | 1 / 0 (N5) unless natively notified | — | D | planned |
| D3a | in carrier hands | in transit | in transit | 0 / 0 | — | D | planned |
| D3b | in transit | out for delivery | out for delivery | 0 / 0 (fixture sends none) | — | D | planned |
| D3c | single-parcel order out for delivery | delivered | delivered | 1 / 0 (N7) | "paid" | D | planned |
| D4a | in transit | carrier delay | delayed | 0 / 0 | silent state loss | D | planned |
| D4b | out for delivery | failed delivery attempt | attempt failed | 1 / 1 (N8) | "delivered" | D | planned |
| D4c | in transit | delivery failure (return to sender) | failed | 1 / 1 (N8) | "delivered" | D | planned |
| D5a | two-parcel order | first parcel delivered | parcel 1 delivered; order not delivered | 0 / 0 | marking the order delivered | D | planned |
| D5b | D5a | second parcel delivered | order delivered | 1 / 0 (N7, once per order) | a second delivered message | D | planned |
| D6 | in carrier hands | carrier or tracking number changed | latest authoritative tracking | 0 / 0 | a second dispatch message | D | planned |
| D7 | fulfillment exists | fulfillment cancelled and recreated | state follows the live fulfillment | 1 / 0 (N5 for the recreated one only, if carrier-accepted) | messages for the cancelled one | D | planned |
| D8 | in transit | stale or missing carrier events | no regression; reconciliation flagged | 0 / 0 | an invented delivery | D | planned |
| D9a | pickup order | ready for pickup | ready | 0 / 0 | carrier messages | D | planned; blocked: owner choice (pickup notifications) |
| D9b | digital goods | fulfilled | fulfilled | 0 / 0 | carrier messages | D | planned |
| D10 | delivered order | return requested and received | returned | 0 / 0 | reopening payment | D | planned |

## E. Notifications

### Proposed synthetic notification test policy

**PROPOSED — pending owner approval. A test fixture, not live product
policy.** No channel, template, recipient or send is authorized by it. It
exists so that cases have fixed, runnable expected counts. Anything absent
from it, or unapproved, sends **0** messages.

Fixture rules:

* **Channel:** WhatsApp approved templates only. No SMS, email or other
  fallback; a missing or invalid phone sends 0.
* **Language:** the buyer's recorded language (`ar` or `en`), default `ar`;
  the merchant's dashboard language.
* **Opt-in:** buyer messages require a recorded WhatsApp opt-in that has not
  been withdrawn; merchant messages go to the merchant's verified staff
  number.
* **Templates:** synthetic fixture template ids (`fx_*`); real templates come
  from the Nahla Templates Library, the merchant, or Meta-approved templates,
  per AGENTS.md.
* **Idempotency key:** notification-specific — see the `Key` column of the
  table below. Only the business fact a notification reports is part of its
  key: COD confirmation and reconfirmation use the **meaningful order
  revision** (in this fixture: a change to line items, quantities, total or
  delivery address); dispatch uses the **fulfillment identity** plus its
  carrier-acceptance fact; whole-order delivery uses the **order** plus its
  delivery-completion fact; refunds use the **refund identity**. Unrelated
  order edits, tracking number or carrier changes and repeated events do not
  change any key and therefore send nothing. The live identity choices
  (which order changes are meaningful, which carrier event ids exist) are
  **pending owner and design decisions**.
* **Retry / escalation:** at most 3 provider attempts on a retryable failure
  with backoff; after the last failure, a dashboard flag only — no extra
  message.
* **Native suppression:** when Shopify notified the buyer natively for the
  same event (`notifyCustomer = true` on the mutation, or the store's own
  notification for that event), the fixture sends 0 for it. If whether
  Shopify notified is unknown, the case is **blocked: owner choice**.
* **WhatsApp constraints:** business-initiated messages outside the 24-hour
  window use approved templates only.

Keys below omit the common prefix `tenant : shop :`.

| ID | Recipient | Trigger (fact) | Template | Key | Buyer | Merchant |
|---|---|---|---|---|---|---|
| N0 | buyer | order created and paid | none (Shopify's native confirmation) | — | 0 | 0 |
| N1 | buyer | COD order created, or meaningfully revised while awaiting acceptance | `fx_cod_confirm_request` | `order GID : N1 : meaningful revision` | 1 | 0 |
| N2 | buyer | no COD response by the fixture deadline (24 h) | `fx_cod_reminder` | `order GID : N2 : meaningful revision` | 1 (max once per revision) | 0 |
| N3 | merchant | buyer accepted COD | `fx_merchant_cod_accepted` | `order GID : N3 : meaningful revision` | 0 | 1 |
| N4 | merchant | buyer rejected COD | `fx_merchant_cod_rejected` | `order GID : N4 : meaningful revision` | 0 | 1 |
| N5 | buyer | carrier accepted a fulfillment's parcel (not natively notified) | `fx_dispatched` | `fulfillment GID : N5 : carrier-accepted` | 1 | 0 |
| N7 | buyer | whole order delivered (all parcels) | `fx_delivered` | `order GID : N7 : delivery-complete` | 1 (once per order) | 0 |
| N8 | buyer and merchant | failed delivery attempt, or delivery failure | `fx_delivery_issue` / `fx_merchant_delivery_issue` | `fulfillment GID : N8 : attempt-failed` or `: delivery-failed` | 1 | 1 |
| N9 | merchant | collection verified by the owner-chosen source | `fx_merchant_collected` | `order GID : N9 : collected` | 0 | 1 |
| N10 | buyer | refund transactions verified `success` (not natively notified) | `fx_refunded` | `refund GID : N10` | 1 | 0 |
| N11 | buyer | order cancelled (not natively notified) | `fx_cancelled` | `order GID : N11 : cancelled` | 1 | 0 |
| N12 | merchant | refund transaction failed | `fx_merchant_refund_failed` | `refund GID : N12` | 0 | 1 |

(N6 is intentionally absent: the fixture sends nothing for out-for-delivery.)

### Notification cases (all planned)

| ID | Initial state | Events | Expected state | Buyer / merchant | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| E1 | COD order, buyer opted in, `ar` | created | request recorded with its key | 1 / 0 | a second request for the same key | D | planned |
| E2 | as E1 | the same webhook delivered twice | one request | 1 / 0 | a duplicate send | D | planned |
| E3 | buyer opted out (preference disabled) | COD order created | manual follow-up flag | 0 / 0 | any buyer message | D | planned |
| E4 | phone missing | COD order created | flagged; no send | 0 / 0 | a send to a guessed number | D | planned |
| E5 | phone invalid | COD order created | flagged; no send | 0 / 0 | a send | D | planned |
| E6 | buyer language `en` | COD order created | English template chosen | 1 / 0 | the Arabic template | D | planned |
| E7 | shop time zone and currency differ from the platform's | COD order created | amounts and dates rendered in the shop's currency and time zone | 1 / 0 | converted or mis-dated values | D | planned |
| E8 | buyer withdrew consent after the request | reminder due | no reminder | 0 / 0 | a reminder | D | planned |
| E9 | outside the 24-hour window | carrier acceptance | template message only | 1 / 0 | a free-form message | D | planned |
| E10 | one buyer notification due | provider: accepted, then delivered, then read | message states recorded | buyer: logical 1, attempts 1, accepted 1, delivered 1; merchant 0 | treating message delivery as parcel delivery | D | planned |
| E11 | one buyer notification due | provider: retryable failure on all three attempts | dashboard flag | buyer: logical 1, attempts 3, accepted 0, delivered 0; merchant 0 | a fourth attempt; another channel | D | planned |
| E12 | one buyer notification due | provider outcome unknown (timeout without acknowledgement) | state "unknown"; no automatic resend | buyer: logical 1, attempts 1, accepted 0 claimed, delivered 0 claimed, unknown 1; merchant 0 | claiming accepted or delivered; an automatic duplicate | D | planned |
| E13 | fulfillment created with `notifyCustomer = true` | carrier acceptance | native notification relied on | 0 / 0 | a Nahlah double | D | planned |
| E14 | refund created with Shopify notifying the buyer | transactions verified | native notification relied on | 0 / 0 | a Nahlah double | D | planned |
| E15 | unknown whether Shopify notified | carrier acceptance | — | — | — | D | blocked: owner choice (how native notification is known) |
| E16 | channel not approved (e.g. SMS) | any trigger | nothing sent | 0 / 0 | any send on an unapproved channel | U | planned |

## F. Resilience

| ID | Initial state | Events | Expected state | Buyer / merchant | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| F1a | connected shop | identical privacy delivery repeated, concurrently and with new webhook ids | one open request; counters only | 0 / 0 | a second request | D | implemented [P12] |
| F1b | no order (later slice) | COD order created (`orders/create`), then the same creation state again via `orders/updated` and a redelivered `orders/create` | one COD request | 1 / 0 total (N1 once) | a second N1; a second order row | D | planned |
| F2a | uninstalled then reinstalled shop | old uninstall / privacy body replayed after the reinstall | new generation retained, never erased | 0 / 0 | revival, erasure of the new installation | D | implemented [P13] |
| F2b | cancelled / deleted order (later slice) | late event after cancel, uninstall or delete | no regression | 0 / 0 | messages or state revival | D | planned |
| F3a | connected shop | receipt COMMIT in flight at the HTTP deadline; receipt blocked past it | 503; one durable request; idempotent redelivery | 0 / 0 | a lost or doubled receipt | D | implemented [P14] — proves receipt recovery only |
| F3b-1 | COD order event (later slice), provider available | crash **before** the commit that records the notification decision; event redelivered | decision recorded once on retry | buyer: logical 1, attempts 1, accepted 1 | a lost decision; two sends | D | planned |
| F3b-2 | as F3b-1 | crash **after** that commit, **before** the provider send; recovery runs | one send from the recorded decision | buyer: logical 1, attempts 1, accepted 1 | a lost send; a second logical notification | D | planned |
| F3b-3 | as F3b-1 | provider **accepted** the message, then a crash **before** the acceptance was recorded | acceptance unknown to Nahlah until reconciled | buyer: logical 1, attempts 1, accepted 0 claimed, unknown 1 until reconciliation | a blind resend | D | blocked: design — provider-side reconciliation (status lookup or provider idempotency) is not designed |
| F3b-4 | as F3b-1 | provider outcome unknown (timeout, provider unavailable) | state "unknown"; no automatic resend | buyer: logical 1, attempts 1, accepted 0 claimed, unknown 1 | a blind resend; claiming delivery | D | blocked: design — unavailable-provider reconciliation is not designed |
| F4a | catalog import running | 429 / throttling, GraphQL errors, 5xx, timeouts | bounded retries, then a classified failure; last good catalog kept | 0 / 0 | unbounded retries; partial publication | U + D | implemented [P15] |
| F4b | catalog import running | access token rotated mid-import | new credential followed, old one never reused | 0 / 0 | reuse of a retired token | D | implemented [P15] |
| F5 | events missed during an outage (later slice) | reconciliation | state repaired | 0 / 0 for historical events | historical message floods | D | planned |
| F6 | two shops, two tenants | operations on both | isolated | 0 / 0 | cross-store effects | D / R | D implemented [P1]; R NOT RUN — pending permissions |
| F7 | all integrations configured (later slices) | Shopify, Meta and WhatsApp outages, each alone | each degrades alone | 0 / 0 during the outage | cascading failure; silent loss | D | planned |

General exactly-once external messaging is **not implemented and not
guaranteed** by any Draft; F3a covers only the durable privacy receipt.

## Proof references

Node ids are pinned: #1202–#1204 at
[`08428cb`](https://github.com/alayedharthi-stack/nahla-saas/tree/08428cb7edf9666a0b31874e8f0256764fd11c90)
(the #1204 head, which contains #1202 and #1203), #1205 at
[`3681ebd`](https://github.com/alayedharthi-stack/nahla-saas/tree/3681ebdb4f6f922e4f2f4bd415d8963c45ef3f6c)
— the **pinned proof source**; its final-head execution evidence is
recorded in [`shopify-privacy-webhooks-validation.md`](https://github.com/alayedharthi-stack/nahla-saas/blob/3d802df44054db8e85b377ebc2805752fbe6c94d/docs/engineering/shopify-privacy-webhooks-validation.md)
(docs-only commit `3d802df`; tested source `3681ebd`, baseline `08428cb`),
with its sanitized machine-readable subset
[`shopify-privacy-webhooks-validation-evidence.json`](https://github.com/alayedharthi-stack/nahla-saas/blob/3d802df44054db8e85b377ebc2805752fbe6c94d/docs/engineering/shopify-privacy-webhooks-validation-evidence.json).

* **[P1] two tenants / shops isolated** —
  `backend/tests/test_shopify_connection_pg.py::test_connect_two_tenants_isolated`;
  `backend/tests/test_shopify_catalog_import_pg.py::test_identical_raw_ids_in_two_tenants_and_shops_stay_isolated`;
  `backend/tests/test_shopify_catalog_import_pg.py::test_constraints_refuse_cross_tenant_parents_and_malformed_identities`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_two_tenants_with_identical_raw_ids_stay_isolated`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_composite_key_isolates_tenants_and_checks_refuse_fabricated_states`.
* **[P2] no prohibited source change** — the GOV-002 scanner
  (`scripts/lint_intelligence_non_interference.py`, run from the merge base)
  and `backend/tests/test_constitution_compliance.py` +
  `backend/tests/test_intelligence_non_interference_guard.py`; results in each
  Draft's validation record.
* **[P3] product raw-id collisions with the Salla writer** —
  `backend/tests/test_shopify_catalog_import_pg.py::test_salla_writers_with_colliding_raw_ids_never_touch_the_mirror`.
* **[P4] flags off** —
  `tests/test_shopify_connection_foundation.py::test_every_route_is_off_by_default`;
  `tests/test_shopify_connection_foundation.py::test_recovery_tick_is_dormant_without_configuration`;
  `tests/test_shopify_catalog_import.py::test_routes_are_off_by_default_and_with_only_one_flag`;
  `tests/test_shopify_privacy_webhooks.py::test_routes_are_absent_while_the_intake_flag_is_off`;
  `tests/test_shopify_privacy_webhooks.py::test_worker_is_dormant_without_its_flag`.
* **[P5] dedicated bounded receipt pool** —
  `tests/test_shopify_privacy_webhooks.py::test_exhausted_receipt_slots_answer_503_at_once_without_backlog`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_real_dedicated_pool_exhaustion_is_a_bounded_503_and_recovers`.
* **[P6] token expiry, rotation, revocation** —
  `backend/tests/test_shopify_connection_pg.py::test_refresh_rotation_and_concurrent_refresh`;
  `backend/tests/test_shopify_connection_pg.py::test_expired_access_token_and_revoked_refresh_confirms_uninstall`;
  `backend/tests/test_shopify_connection_pg.py::test_old_event_with_expired_access_token_refreshes_before_concluding`.
* **[P7] invalid signatures** —
  `tests/test_shopify_connection_foundation.py::test_webhook_hmac_over_raw_body`;
  `tests/test_shopify_connection_foundation.py::test_webhook_refusals_before_any_database_access`;
  `tests/test_shopify_privacy_webhooks.py::test_wrong_or_malformed_signatures_are_401`;
  `tests/test_shopify_privacy_webhooks.py::test_hmac_covers_the_raw_bytes_not_the_json_value`.
* **[P8] unknown shop** —
  `backend/tests/test_shopify_connection_pg.py::test_unknown_shop_changes_nothing`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_unknown_and_conflicting_identity_never_mutate_or_rebind`.
* **[P9] cross-tenant claim refused** —
  `backend/tests/test_shopify_connection_pg.py::test_verified_ownership_survives_disconnect_and_blocks_other_tenant`.
* **[P10] uninstall and late events** —
  `backend/tests/test_shopify_connection_pg.py::test_genuine_uninstall_is_confirmed_and_keeps_the_tombstone`;
  `backend/tests/test_shopify_connection_pg.py::test_delayed_old_uninstall_after_same_tenant_reinstall_is_retained`;
  `backend/tests/test_shopify_connection_pg.py::test_identical_signed_body_across_generations_is_never_suppressed`.
* **[P11] privacy topics** —
  `tests/test_shopify_privacy_webhooks.py::test_valid_signed_delivery_is_acknowledged_after_the_receipt`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_http_receipts_store_no_raw_customer_value_header_or_body`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_matched_customer_request_persists_the_bounded_assessment`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_confirmed_uninstall_yields_a_counts_only_blocked_proposal`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_executor_refuses_without_flag_or_policy_and_changes_nothing`.
* **[P12] duplicate privacy deliveries** —
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_concurrent_identical_deliveries_resolve_to_one_request`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_changed_unsigned_headers_attach_and_never_suppress`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_signed_replay_flood_is_bounded_and_lifecycle_still_progresses`.
* **[P13] replay across reinstall** —
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_reinstall_never_erases_the_new_generation_and_never_suppresses_a_later_request`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_retained_replay_never_swallows_a_later_genuine_uninstall_with_the_same_body`;
  `backend/tests/test_shopify_connection_pg.py::test_same_delivery_id_and_body_across_reinstall_requests_a_probe`.
* **[P14] receipt recovery at the deadline** —
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_late_commit_or_blocked_commit_answers_503_and_the_redelivery_is_idempotent`;
  `backend/tests/test_shopify_privacy_webhooks_pg.py::test_late_delivery_waiting_behind_a_later_one_keeps_timestamps_monotone`.
* **[P15] import resilience** —
  `tests/test_shopify_catalog_import.py::test_http_failures_are_bounded_and_classified`;
  `tests/test_shopify_catalog_import.py::test_throttle_is_retried_with_bounded_waits_then_refused`;
  `tests/test_shopify_catalog_import.py::test_graphql_errors_refuse_even_partial_data`;
  `tests/test_shopify_catalog_import.py::test_timeouts_and_transport_errors_are_retried_then_refused`;
  `backend/tests/test_shopify_catalog_import_pg.py::test_throttle_and_429_during_an_import_still_commit`;
  `backend/tests/test_shopify_catalog_import_pg.py::test_a_rotated_credential_mid_import_is_followed_never_reused`.

## Real development-store journeys (launch gate)

| Journey | Status |
|---|---|
| paid order | NOT RUN — pending permissions and owner approval |
| COD confirmation, shipping, delivery and verified collection | NOT RUN — pending permissions and owner approval |
| rejection and cancellation | NOT RUN — pending permissions and owner approval |
| partial shipments | NOT RUN — pending permissions and owner approval |
| partial and failed refunds | NOT RUN — pending permissions and owner approval |
| retries and recovery | NOT RUN — pending permissions and owner approval |
| two-store isolation | NOT RUN — pending permissions and owner approval |
| re-run of the existing regressions on the activated stack | NOT RUN — pending permissions and owner approval |

## Recorded results

| Draft | Validation record |
|---|---|
| [#1202](https://github.com/alayedharthi-stack/nahla-saas/pull/1202) | design and runbook `docs/engineering/shopify-connection-foundation.md`; validation recorded on the Draft |
| [#1204](https://github.com/alayedharthi-stack/nahla-saas/pull/1204) | `docs/engineering/shopify-catalog-import-validation.md` |
| [#1205](https://github.com/alayedharthi-stack/nahla-saas/pull/1205) | [`shopify-privacy-webhooks-validation.md`](https://github.com/alayedharthi-stack/nahla-saas/blob/3d802df44054db8e85b377ebc2805752fbe6c94d/docs/engineering/shopify-privacy-webhooks-validation.md) at `3d802df` (tested source `3681ebd`, baseline `08428cb`): strict PG16.15 and PG18.6 SCRAM **PROVEN 815/815**; 17 `lint-and-test` steps RC 0 at both SHAs; owner 65-file regression gate **FAILED** at candidate and baseline alike (RC 1 — 2 failed, 1075 passed, 41 skipped; identical JUnit identities: reproduced baseline debt, not a pass) |

Final test links for later slices are added here only when those runs exist.

## Official references

The same parent-verified references as the roadmap; this environment's own
fetch of shopify.dev is blocked and was not bypassed. See
[`shopify-roadmap.md`](shopify-roadmap.md#official-references).
