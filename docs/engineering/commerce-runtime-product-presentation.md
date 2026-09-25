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

1. **A verified fresh product-row selection** → hydrate → **Card**.
1b. **A verified "More" tap** on a list this conversation sent → the **next
   page** of that list, from its stored order
   (`commerce-runtime-navigation-snapshot.md`).
2. **A valid model-requested shape** that is consistent with evidence and
   policy.
3. **A focused product** from a deliberate `get_product_details` → **Card**,
   subject to recent-card suppression when there is no new selection.
4. **Multiple candidates and no focus** → **List**.
5. Otherwise → **Text**.

**The tap is first, and that is the point of it.** A tap on a row this
conversation sent is the strongest structured product selection the channel
gives us: stronger than a model request, which is a suggestion, and stronger
than suppression, which is a guess about repetition. An unrelated selector in
the same reply must not be able to turn an explicit choice into a different
list — that answers a question the customer did not ask.

Exactly one structured fact returns such a turn to multi-product selection: the
model asks for a selector that **itself offers the tapped product back**, among
others. That is the agent deliberately presenting the customer's own choice as
one of several, and it is read off the requested product ids — never off
anything anyone wrote. A selector that does not carry the tapped product is
about something else: the Card goes out, the selector stands down with
`verified_tap_answered_first`, and its options still follow the model's own
sentence as lines, so nothing it meant to offer is lost. The selector only
stands down for a Card that will actually be there — asked of the composer
before anything is changed — so a tapped product that turns out to have no photo
leaves the customer with the selector rather than with neither shape.

Where a model request conflicts with structured facts or evidence, the existing
guards win — verification runs before any of this, and a card or row whose
product this turn did not observe and cite never reaches step 2.

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

### The button word is the expression layer's, in the customer's language

The button is the one thing on a Card the customer *reads*. Everything else —
the product, its photo, the page the button opens — is the merchant's, read
through the tools. So the two are owned separately, and neither can stand in
for the other:

| | authority |
|---|---|
| product identity, image, URL | **merchant / tool** — read through `get_product_details`, never from the model's words |
| button wording | **expression layer** — the model, in the customer's own language |

The platform has no word of its own and will not invent one: **no word, no
Card**, whoever chose the product. The answer still goes out, with
`no_button_label_offered` on the payload.

This is not a formality. The channel sender substitutes a fixed Arabic phrase
for an empty label —

```python
"display_text": str(btn_label or "عرض المنتج")[:20]
```

— so a Card composed without a word would arrive carrying wording nobody wrote,
in one language whatever language the customer is writing in. Refusing to
compose one is what makes that substitution unreachable from this path;
`payload_card` refuses the same on the read side, for a payload written by an
older release. `build_cta_url_payload` itself is untouched, because other
senders share it; a case pins that its fallback is still there for the paths
that own it, and that this one never reaches it.

Localization was considered and rejected on evidence: the only customer-language
signal in the codebase is `preferred_language` in the legacy Brain profile,
which defaults to `"ar"` at every call site and yields a *language code*, not a
word. Turning it into a button label would need a language→phrase table — a
customer-facing constant per language rather than one, which is the same
violation multiplied.

So the reply contract makes `button_label` the only **required** field of
`card`, and `product_id` optional. The model may give the wording without
naming a product, and the platform then decides which product the Card shows —
from the customer's verified selection, not from anything the model wrote. A
card request carrying only wording is therefore not a request for a *shape*: it
never stands in front of the customer's own selection.

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

Where revision 0113 is not applied, `>10` keeps its honest behaviour: the
selector is withheld whole (`more_options_than_the_channel_shows`) and
**every** option follows the model's sentence as a line of the merchant's own
values. No option is trimmed, and nothing is faked. Where it is applied, a
search's whole result is paged by the platform — the model still names only
what it was shown — as `commerce-runtime-navigation-snapshot.md` describes.

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
step 1 and suppression at step 3. A customer who taps a product again is asking
for it again; answering that with suppressed presentation would be the platform
overruling an explicit selection.

## The text beside a shape, and the language it is written in

Tenant 1, 25 September 2026 (turns 47–49): the text beside a list restated
every row's price and sizes, the text beside a card carried the photo's raw
address, and replies drifted out of Saudi Arabic («هسع», «شنو»). None was the
model's divergence first:

* **What a shape shows was never declared.** The reply tool said the text
  "carries the answer by itself" and that each shape is an addition; it never
  said a row shows the product's title, price and options, or that a card shows
  the photo and opens the page. The declaration now states those facts, that the
  text need not repeat them (and still introduces, explains, compares or answers
  in as much detail as the turn needs), that a selector which cannot be shown
  becomes lines under the text, and that a card needs a photo, an https page
  link and a button word — otherwise the text is all the customer receives. A
  link in the text stays the model's choice whenever the customer asks for one.
* **The merchant's language was never delivered.** Like the assistant name
  before #1147, ``default_language`` and ``reply_tone`` stayed on the legacy
  path. ``commerce_runtime_pilot._context_preamble`` now reads them once per
  turn with the name and hands the model their platform meaning
  (``tenant_overlay.LANGUAGE_MAP`` / ``TONE_MAP``) as ``reply_language`` and
  ``reply_tone`` in ``conversation_context``. ``arabic`` means Saudi colloquial
  Arabic, as it always has on the legacy path; ``english`` and ``bilingual``
  mean exactly that — nothing is inferred from a country. ``reply_length``
  (every meaning is a line cap) and the free-text ``owner_instructions`` /
  ``assistant_role`` are not carried; carrying them needs its own reviewed
  scope.

Nothing rewrites, trims or replaces the model's text on any path. Evidence:
``tests/test_commerce_runtime_reply_style.py`` and the declaration cases in
``tests/commerce_reliability/test_commerce_runtime_agent_provider.py``; the
real-model before/after comparison is recorded on the PR.

## What this does not touch

Model, prompt, persona, tool schema and the model's text are all unchanged. The
policy runs after verification, chooses a shape, and composes structured
payloads from the merchant's own values. Every reason it can reach is a closed
constant, carried on the delivered payload and in the pilot log, so the shape of
any production turn is auditable without a transcript.
