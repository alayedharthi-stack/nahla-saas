# Product presentation — content is the agent's, presentation is the platform's

Which **shape** a commerce reply takes — a tappable List, a Product Card, or
plain Text — is a platform decision, made from structured provenance the turn
already established. It is never read off the customer's words, never inferred
from a keyword, and it never changes a single character of what the model
wrote.

```
customer language → LLM understanding → commerce tools
                  → verified structured product identity → presentation policy
```

The last arrow is this document. Everything to its left already exists; nothing
here reaches back across it.

## What the policy is allowed to look at

Only these, and they are all structured:

| Input | Where it comes from | Why it is trustworthy |
|---|---|---|
| the model's requested shape | `requested_choices` / `requested_card` on the draft payload | a field, not prose |
| this turn's observations | `session.observations` | the tools' own results |
| the tool that produced each one | `ToolObservation.tool_name` | the registry's declared provenance |
| a verified product-row tap | `inbound` preamble `customer_tapped` | `runtime_entry._tapped_product` already proved the row was sent to *this* conversation, inside the lapse, and re-read the product |
| whether a Card was actually sent before | the accepted send's own persisted evidence | a receipt, not a payload |

It may **not** look at: the customer's text, product or category names, a
phrase map, a regex over anything a customer wrote, or the model's prose.

### Provenance classes

Taken from the tool registry's declared result kind (`agent_live_tools.py`),
never from a name match:

| Tool | Result kind | Class |
|---|---|---|
| `get_product_details` | `product` | **focus** — one product, read deliberately |
| `search_products` | `product_list` | **candidates** |
| `resolve_customer_order`, `get_order_details`, `get_order_shipment`, `get_customer_addresses`, `list_shareable_promotions`, `search_merchant_knowledge` | `order_summary`, `shipment`, … | **incidental** — never drives a List or a Card |

A product that appears *incidentally* — the item inside an order summary, say —
is a fact the answer may state. It is not a product the customer asked to
browse, so it never becomes a Card or a row on its own.

## Precedence

Exactly this order, first match wins:

1. **A valid model-requested shape** that is consistent with evidence and
   policy. The model may legitimately decide this turn offers alternatives,
   even right after a tap.
2. **A verified fresh product-row selection** → hydrate → **Card**.
3. **A focused product** from a deliberate `get_product_details` → **Card**,
   subject to recent-card suppression when there is no new selection.
4. **Multiple candidates and no focus** → **List**.
5. Otherwise → **Text**.

Where a model request conflicts with structured facts or evidence, the existing
guards win — verification runs before any of this, and a card or row whose
product this turn did not observe and cite never reaches step 1.

## Rule 1 — a verified tap leads to a Card deterministically

A customer who taps a product row has made the most explicit product selection
the channel allows. The Card that follows must not depend on the model
happening to call `get_product_details` in the same turn: that would make a
customer-visible guarantee conditional on a non-deterministic choice.

```
verified product-row tap → selected_product_id
                         → fresh platform hydration from the merchant catalogue
                         → Product Card
```

**Hydration goes through the tools' own contract, not a copy of it.** The
policy builds an ordinary `ToolRequest` for `get_product_details` and runs it
through the same `registry.execute(...)` the model's own calls go through. So
it inherits, without restating any of it:

* `context.assert_scope()` and `_assert_catalog_rows_belong_to_tenant` — tenant
  isolation;
* `context.require_authorized_product(...)` — the product-identity guard;
* `CatalogContextBuilder(...).get_by_id(...)` — the merchant's own catalogue,
  read **now**, so availability is current;
* `context.register_evidence(...)` — a real evidence reference, so the Card is
  evidenced exactly like a model-requested one.

No business logic is copied into the presentation layer. If the contract
changes, hydration changes with it, because it *is* the contract.

**Fail closed, with a name.** Hydration that does not return a product, or a
product whose image or link cannot be proven `https`, yields **Text** and a
named reason on the delivered payload — never a Card built from a guess:

| reason | meaning |
|---|---|
| `tap_hydration_unavailable` | the read did not succeed |
| `tap_product_not_found` | the read succeeded and the product is gone |
| `product_has_no_image` / `product_has_no_link` / `product_link_not_https` | the merchant's own values will not make a Card |

**Not applied to a single search candidate.** Rule 2 requires a *tap*. One
product coming back from `search_products` is a candidate, not a selection, and
does not become a Card by being alone.

### The button label adds no new fixed wording

A model-requested Card still needs the model's own button word
(`no_button_label_offered` when it offers none) — unchanged. A
**platform-determined** Card after a tap was never offered one, so it carries an
empty label and the send path's existing `display_text` default stands, exactly
as `reply_choices` leaves the list button to the channel sender. This PR
introduces no customer-facing constant.

## Rule 2 — the pagination snapshot: an explicit lifecycle proof

Before choosing the delivery ledger's `intent_payload` as the store for a
paginated result set, it was checked against the seven properties such a
snapshot needs. **It fails five of them**, so it is not the store.

`RuntimeDeliverySequence` (`ledger_models.py`) is the record of *one turn's
outbound delivery*.

| Property | `intent_payload` | |
|---|---|---|
| opaque token lookup | no token column; the only keys are `id` and `UniqueConstraint("turn_id")`. A token would live inside JSONB with no index — a scan, not a lookup | ❌ |
| tenant binding | `tenant_id` + FK to `tenants` + scope unique constraint | ✅ |
| conversation binding | `conversation_id` + composite FK | ✅ |
| expiry | no expiry column, no lifecycle state meaning "stale" | ❌ |
| immutable ordered result set | the table is **deliberately mutable** — `outcome`, `attempt_count`, `updated_at` all change during a send. It is not in `APPEND_ONLY_TABLES`; only attempts, results and receipts carry the immutability trigger | ❌ |
| replay / expired rejection | no notion of a snapshot being consumed or timed out | ❌ |
| cleanup / bounded retention | this row *is* the send audit record. Pruning it on a navigation cadence would destroy delivery evidence — the two purposes are in direct conflict | ❌ |

There is a further purpose mismatch independent of the table's columns:
`UniqueConstraint("turn_id")` binds a sequence to the single turn that created
it, while a browsing snapshot exists precisely to be read by **later** turns.

**Conclusion:** a small dedicated navigation snapshot entity, as instructed.
It is specified in `commerce-runtime-navigation-snapshot.md` and delivered
separately, because it needs a schema revision that an owner must apply to
production explicitly (production startup pins `alembic upgrade 0093` and
materialises only `models.Base`, so a new `RuntimeBase` relation is dormant
until applied). Bundling a dormant table into this policy change would let CI
prove a behaviour production does not yet have.

Until it lands, `>10` keeps its current honest behaviour: the selector is
withheld whole (`more_options_than_the_channel_shows`) and **every** option
follows the model's sentence as a line of the merchant's own values. No option
is trimmed, and nothing is faked.

## Rule 3 — recent-card suppression rests on a send, and loses to a tap

Suppression exists so the same product's Card is not sent twice in a row when
nothing new was selected. It must therefore rest on evidence that a Card was
**actually sent** — not on a payload having once carried one. A reserved intent
that the provider refused, or that a crash left undispatched, is not a Card the
customer saw.

The evidence is the accepted send's own record: the product id of the card is
persisted on the outbound message **only when the provider accepted it and
returned a message id**, alongside the row ids that are already persisted the
same way and for the same reason. Read back, the question is exact: *was the
most recent Card this conversation actually delivered for this same product?*

And it is beaten by a verified fresh tap, because precedence puts the tap at
step 2 and suppression at step 3. A customer who taps a product again is asking
for it again; answering that with suppressed presentation would be the platform
overruling an explicit selection.

## What this does not touch

Model, prompt, persona, tool schema and the model's text are all unchanged. The
policy runs after verification, chooses a shape, and composes structured
payloads from the merchant's own values. Every reason it can reach is a closed
constant, carried on the delivered payload and in the pilot log, so the shape of
any production turn is auditable without a transcript.
