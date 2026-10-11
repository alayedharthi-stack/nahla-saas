# Shopify regression and acceptance plan

Status: **test plan.** It defines what every Shopify slice must prove and how
results are recorded. It does not change any test, implementation or
existing document. The slice list and permission gates are in
[`shopify-roadmap.md`](shopify-roadmap.md).

## How results are recorded

Every slice records, for **baseline** (the exact base SHA it stacks on) and
**candidate** (its exact tested SHA):

* the affected paths;
* each command as executed, its exit code, and the passed / failed /
  skipped / errored counts (a skip in a strict suite is a failure, never a
  pass);
* the PostgreSQL versions used (16 and 18) and the authentication mode (TCP
  SCRAM, as in CI);
* which GitHub checks actually ran on the Draft (on a stacked base the full
  CI is filtered; only checks that ran may be called passed).

No assertion is removed or weakened to make a candidate pass. A baseline
failure that also fails on the candidate is reported as pre-existing, with
its node id.

### Evidence levels

| Level | Meaning |
|---|---|
| **U** | unit test, no database, mocks / fakes |
| **D** | integration on disposable PostgreSQL, or a local browser harness |
| **R** | observed on a real Shopify development store |

Status values: **implemented** (with a link to the proof), **planned**, or
**NOT RUN — pending permissions** for real-store journeys. Nothing in level
R is authorized now.

### Columns used below

Each case lists its initial state, the events, the expected Shopify and
Nahlah state, the expected messages with an **exact count**, the forbidden
side effects, the evidence level it must reach, and its status today.

## A. Protect the baseline (Shopify flags off)

| ID | Initial state | Events | Expected state | Messages (exact) | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| A1 | Salla tenant, Shopify flags off | Salla product, order and shipping flows | identical to baseline | as baseline | any Shopify table read or written | U + D | partly implemented: existing Salla suites run beside every Shopify Draft (baseline vs candidate recorded in each validation document) |
| A2 | Meta catalog connected | item creation and updates | Meta item ids and updates unchanged | 0 extra | Shopify rows reaching Meta | U + D | partly implemented: existing Meta catalog suites run beside each Draft |
| A3 | two tenants | any Shopify slice active for one | the other tenant unchanged | 0 | cross-tenant read or write | D | implemented per slice (composite tenant keys; #1202–#1205 proofs) |
| A4 | billing subscriptions | Shopify flows | billing unchanged | 0 | billing side effects | U | partly implemented: billing suites in CI steps |
| A5 | agent tools and conversations | Shopify flows | agent behaviour unchanged | as baseline | prompt / persona / model / dialect change | U | implemented: GOV-002 scanner and constitution suite per Draft |
| A6 | same SKU / order number at Salla and Shopify | both providers sync | each provider row keyed by its own identity | 0 | one provider's row overwritten by the other | D | catalog: implemented (#1204 colliding raw ids); orders: planned |
| A7 | mixed-provider tenant | Shopify connected, Salla active | both work independently | as baseline | provider confusion in shared tables | D | planned |
| A8 | Shopify enabled, then disabled | flags turned off | routes 404, workers idle, existing work unaffected | 0 | dangling jobs or writes | U + D | implemented for #1202–#1205 (flag-off tests) |
| A9 | heavy Shopify backlog | existing workers busy | existing work not starved | as baseline | Shopify jobs monopolising workers or the shared pool | D | planned (#1205 uses a dedicated bounded pool for receipts) |

## B. Connection and privacy

| ID | Initial state | Events | Expected state | Messages (exact) | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| B1 | two development stores, two tenants | connect both | two isolated connections | 0 | cross-store data | D / R | D implemented (#1202); R NOT RUN — pending permissions |
| B2 | connected store | token expiry, refresh, revocation | refresh or quarantine; never a stale credential | 0 | use of a revoked credential | D | implemented (#1202) |
| B3 | any | invalid webhook signature | 401, nothing stored | 0 | any write | U + D | implemented (#1202 uninstall; #1205 privacy topics) |
| B4 | unknown shop | signed delivery | receipt without tenant; manual review | 0 | attribution to a tenant by guess | D | implemented (#1205) |
| B5 | shop owned by tenant A | claim attempt by tenant B | refused; ownership unchanged | 0 | rebinding | D | implemented (#1202) |
| B6 | buyer fields not approved | any flow needing them | field absent, flow degrades honestly | 0 | reading or storing an unapproved field | U + D | planned (protected customer data gate) |
| B7 | uninstall queued | late events after uninstall | quarantine then confirmed tombstone; late events change nothing | 0 | revival from a replayed event | D | implemented (#1202) |
| B8 | connected store | `customers/data_request`, `customers/redact`, `shop/redact` | durable receipt; bounded assessment; blocked-policy proposal; no erasure without approved policy | 0 | fulfilment claims; unapproved erasure | U + D | implemented (#1205) |
| B9 | notification records exist (later slices) | `customers/redact` | derived notification data handled under the approved retention | 0 | retention beyond policy | D | planned |

## C. Orders (planned)

| ID | Initial state | Events | Expected state | Messages (exact) | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| C1 | order mirror empty | paid order | paid | per template table | — | D / R | planned; R NOT RUN |
| C2 | — | authorized / pending / partially paid | each its own financial state | per table | treating any of them as paid | D | planned |
| C3 | existing order | edit, cancel, delete | mirror follows; cancelled flows stop | per table | messages for a cancelled order | D | planned |
| C4 | paid order | partial / full / pending / failed refund | refund reported successful **only** after its transactions are verified | per table | "refunded" from `refunds/create` alone | D | planned |
| C5 | COD order | buyer accepts | acceptance recorded | 1 confirmation request, then 0 | inferring shipment or payment | D / R | planned; R NOT RUN |
| C6 | COD order | buyer rejects / ambiguous reply / no response / duplicate reply | rejection, clarification or reminder per owner-set deadline | exact per case | double counting a duplicate reply | D | planned |
| C7 | COD order modified or cancelled | buyer responds afterwards | response bound to the order version it answers | per table | confirming a cancelled order | D | planned |
| C8 | several open orders for one phone | buyer replies | reply bound to the right order or clarified | per table | confirming the wrong order | D | planned |
| C9 | manual payment gateway | order created | **not** COD proof | 0 | treating manual payment as COD acceptance | U | planned |
| C10 | order with `confirmed = true` | — | inventory reservation only | 0 | treating it as buyer acceptance | U | planned |
| C11 | COD delivered, not collected | delivery event | delivered, unpaid | per table | `orderMarkAsPaid` or "paid" claim | D | planned |

## D. Shipping (planned)

| ID | Initial state | Events | Expected state | Messages (exact) | Forbidden | Level | Status |
|---|---|---|---|---|---|---|---|
| D1 | paid order | fulfillment without tracking | fulfilled, tracking unknown | per table | invented tracking | D | planned |
| D2 | — | label printed vs carrier acceptance | distinct states | per table | "shipped" from a label alone | D | planned |
| D3 | — | in transit / out for delivery / delivered | ordered, evidence-based | per table | regressions from stale events | D | planned |
| D4 | — | delay / failed attempt / failure | reported honestly | per table | silent loss | D | planned |
| D5 | multi-parcel order | one parcel delivered | parcel delivered, order **not** delivered | per table | marking the whole order delivered | D | planned |
| D6 | — | carrier or tracking number changed | latest authoritative tracking | per table | duplicate notifications | D | planned |
| D7 | — | fulfillment cancelled and recreated | state follows the live fulfillment | per table | messages for the cancelled one | D | planned |
| D8 | — | stale or missing carrier events | no regression; reconciliation flagged | 0 | invented delivery | D | planned |
| D9 | pickup or digital goods | completion | appropriate state, no carrier flow | per table | carrier messages | D | planned |
| D10 | delivered order | return | return state | per table | reopening payment | D | planned |

## E. Notifications (planned)

Every notification is specified in one table with these columns:
**recipient** (buyer or merchant), **trigger**, **channel**, **language**,
**opt-in**, **template** (approved class), **idempotency key**,
**retry / escalation**, and **exact expected count**.

Cases to cover: disabled preferences; missing or invalid phone; Arabic and
English; time zone and currency; withdrawn consent; the WhatsApp 24-hour
window and template constraints; provider states accepted / delivered /
read / failed / unknown (message delivery is distinct from parcel delivery);
Shopify native notifications, including `notifyCustomer`, accounted for so
that no buyer receives an unintended double; and **no fallback to an
unapproved channel**.

## F. Resilience (planned unless noted)

| ID | Events | Expected | Forbidden | Level | Status |
|---|---|---|---|---|---|
| F1 | duplicate and cross-topic duplicate deliveries | one effect | double effects | D | implemented for #1202 / #1205 topics; planned for orders |
| F2 | out-of-order and late events (after cancel, uninstall, delete) | no regression, no revival | stale writes | D | implemented for #1202 / #1205; planned for orders |
| F3 | crash before / after the database commit or provider acceptance | exactly-once effect, idempotent retry | lost or duplicated effects | D | implemented for #1205 receipts (late commit, blocked commit) |
| F4 | rate limits, GraphQL errors, 5xx, token expiry | bounded retries | unbounded retries | U + D | implemented for #1204 import |
| F5 | missing events reconciled | state repaired | historical message floods | D | planned |
| F6 | two stores | isolation | cross-store effects | D / R | D implemented; R NOT RUN |
| F7 | independent Shopify, Meta and WhatsApp outages | each degrades alone | cascading failure | D | planned |

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
| [#1205](https://github.com/alayedharthi-stack/nahla-saas/pull/1205) | `docs/engineering/shopify-privacy-webhooks-validation.md` on its branch (to be linked here once its final-head runs are recorded) |

Final test links for later slices are added here only when those runs exist.

## Official references

The same parent-verified references as the roadmap; this environment's own
fetch of shopify.dev is blocked and was not bypassed. See
[`shopify-roadmap.md`](shopify-roadmap.md#official-references).
