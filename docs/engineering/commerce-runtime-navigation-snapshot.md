# Paging a search's results — "More" through the real commerce runtime

WhatsApp shows ten rows. A merchant with thirty matching products cannot be
answered in one list, and the two honest ways to fail both cost the customer
something: trimming to fit takes a real option away, and withholding the whole
selector takes the tapping away. Paging is the third way.

This document is the design of record. It supersedes the first draft in PR
#1143, whose store is reused here in revised form and whose model-facing
approach is not (see *What changed from the #1143 draft*).

## The constraint that shapes everything

The model reads a search through a **five-product window**
(`agent_live_tools.MAX_SEARCH_LIMIT`, and `_GENERAL_BROWSE_EVIDENCE_LIMIT` in the
shared catalogue tool). That bound is deliberate — it keeps the model's context
and its turn budget small — and it is not widened. So pagination cannot depend
on the model accumulating, repeating or ordering more than ten product ids, and
it does not: the model never sees the rest of the result, never names it, and
never handles an offset, an order or a token.

## Three authorities, none doing another's job

| | owns | how |
|---|---|---|
| **model** | whether this reply is a browse the customer may continue | gives the word for the "More" row (`choices.more_label`) and the list's button word (`choices.button`), in the customer's language, beside a selector over products of one search |
| **tools** | which products are candidates, and in what order | the search that produced the five-product window also hands the platform the whole ordered result, typed and scoped (`SearchCandidates`) |
| **platform** | the presentation | which products each page shows, where a page ends, whether another follows, and what every row says — read from the catalogue at the moment it is shown |

Without the model's "More" word nothing engages, so a focused recommendation
or a comparison stays exactly the products the model named, however many more
the search matched.

## The typed search-result contract

`core/commerce_runtime/search_candidates.py`. One frozen dataclass per search:

| field | meaning |
|---|---|
| `tenant_id`, `namespace`, `conversation_id`, `turn_id` | stamped from the loop's trusted `ToolScope`, never from an argument |
| `query_digest`, `method` | which search it was (a digest, never the customer's words) and which strategy matched |
| `product_ids` | every match up to `CANDIDATE_CAP` = 50, in the search's total order |
| `complete` | **proven**: true only when the read got back fewer matches than it asked for |
| `window_ids` | the products the model's tool result carried |

It travels on `ToolObservation.platform`, a field the provider never sees:
`provider_observations()` does not copy it, checkpoints do not persist it, the
observation digest does not read it, and a restored observation has none.
`from_observation` refuses candidates from anything but a successful,
untruncated search, from another scope, or whose window is not the head of the
stored result — every refusal a named reason. An untyped list in the same field
is refused as `no_candidates_attached`.

### Completeness and order

`CatalogContextBuilder._search_steps` is now the **one** search strategy. The
row search (`search_products`, unchanged for every caller) and the candidate
read (`search_product_candidates`) walk the same steps and stop at the same
first step that matches, so they cannot disagree about which search ran or how
it ordered. The candidate read asks for `cap + 1` ids; `exhausted` is true only
when fewer came back. Reaching the cap is never reaching the end.

Two ordering defects were fixed on the way, and both are proven on PostgreSQL
with the heap deliberately shuffled:

* the full-text path selected ids `ORDER BY in_stock DESC, id` and hydrated them
  with `id IN (...)`, which has no order: rows came back in heap order. They
  are now returned in the selected order (`_rows_in_order`);
* the ILIKE and Arabic-normalised paths ordered by `in_stock` alone, on which
  every stocked product ties. `id` is now the tie-breaker
  (`CATALOG_SEARCH_ORDER_SQL = "in_stock DESC, id"` everywhere).

The general browse (empty query) offers only orderable products, and
orderability is decided per product in Python; its candidate read formats a
bounded window (`cap + 1 + 20`, variants loaded in one `selectinload`) and is
exhaustive only when that window held every product.

## The flow, end to end

```
search_products ─► model: ≤5 products + "more_results": true
               └► platform: SearchCandidates (≤50 ids, complete?)
reply(choices: ids, button, more_label)
   └► browse.open_browse: page 1 = stored head (9) + "More" row
        rows 6–9 hydrated by read_list_rows (trusted, tenant-scoped, NOT an observation)
        reservation transaction: reply intent + mint token(page 2)        ── one commit
tap "More"  (list_reply_id = nahla:more:<token>)
   └► runtime_entry: peek(token) in tenant/namespace/runtime conversation — spends nothing
        read_list_rows(page 2 ids) — the catalogue now
        model told: browse_page {status, page_number, products_on_this_page,
                                 no_longer_available, more_pages_after_this,
                                 more_matches_than_the_list_holds}
   └► policy step 1b: verified navigation tap → List
   └► browse.continue_browse: page 2 (9) + "More" with the stored words
        reservation transaction: reply intent + spend(token) + mint(page 3)  ── one commit
tap "More" … final page: up to 10, no "More", token spent, nothing minted
```

Page boundaries: a page with a successor carries nine products and one "More"
row; a final page carries up to ten. Ten matches are one list and store nothing;
eleven are nine and two; nineteen are nine and ten.

### Spent with the reply, or not at all

Reading a token (`navigation.peek`) changes nothing. The spend and the next mint
(`navigation.apply`) run as the reservation's `precondition`, on the
transaction's own locked connection, after the conversation lock and before the
reply is written. So:

* a turn that stops before its reply is reserved spends nothing, and the
  customer can tap again;
* a reserved reply has spent its token and minted its successor in the same
  commit — no row ever points at a page that was never sent;
* two turns racing on one token cannot both win: the spend is one conditional
  `UPDATE … WHERE consumed_at IS NULL AND expires_at > now()`.

If the store refuses (`claim_lost`, `not_stored`), nothing was written; the
loop shapes the reply again without navigation and reserves once more. For a
later page that means its products, read moments ago, follow the model's text
as lines (`browse_page_as_lines`) — the answer to "More" is never an empty
sentence.

### Every refusal fails closed, by name

| what arrived | model is told | outcome |
|---|---|---|
| unspent, unexpired token of this conversation | `resolved` + page counts | the page |
| already spent | `replayed` | no page |
| past expiry (database clock) | `expired` | no page |
| another conversation's or tenant's token | `not_found` | no page |
| a string nobody minted | `not_found` | no page |
| unreadable store or catalogue | `unavailable` | no page |

A refusal never becomes a search, a product selection or a card: the platform
runs no fallback, and the turn is still answered on the model's own text.

### Presentation authority preserved (#1140)

Precedence is now: verified product tap → Card; **verified "More" tap → next
page**; model-requested shape; focused product → Card; candidates → List; text.

* A product tap on any row — including one the platform composed and nobody
  cited — verifies against the rows actually sent, re-reads the product now,
  and becomes a Card through the same hydration as before.
* List-row hydration returns row views, never observations: it cannot become
  a focused read, a selection or a card.
* A selector the model asks for on a "More" turn stands down for the page
  (`navigation_page_answered_first`); its options follow the text as lines.
* Order, shipment, address and coupon products are incidental as before.

### Words the platform does not have

The "More" row's title and the list's button are customer-facing words, and the
platform has none of its own. Both are the model's, given when the browse was
opened, and stored with the browse for later pages. A browse is **not opened**
without both (`paging_without_button_word`): the channel sender substitutes a
fixed Arabic phrase for an empty list button, and a list the platform composes
must never reach that fallback. Titles are bounded to what WhatsApp renders
(24 and 20 characters).

## The store: revision 0113

One relation, `commerce_runtime_navigation_snapshots`, on `RuntimeBase`. One row
is one page token.

| column | |
|---|---|
| `token` | opaque, 24 random bytes, `UNIQUE` |
| `series` | every page of one browse |
| `tenant_id`, `namespace`, `conversation_id` | scope; FK to `tenants` and to the runtime conversation `(id, tenant_id, namespace)` |
| `minted_by_turn_id`, `origin_turn_id`, `origin_call_id`, `search_method`, `query_digest` | provenance |
| `product_ids` JSONB | the whole stored result, 1–50 ids |
| `complete` | proven completeness of the stored result |
| `page_offset`, `page_size` | the page this token opens |
| `more_label`, `button_label` | the model's words |
| `expires_at`, `consumed_at`, `consumed_by_turn_id`, `created_at` | lifecycle |

Checks: namespace; `product_ids` is an array of 1–50; `1 ≤ page_offset <
len(product_ids)` (a token opens a page after the first, and one that exists);
`1 ≤ page_size ≤ 9`; both words non-empty; `consumed_at` and
`consumed_by_turn_id` set together. Unique `(series, page_offset)`. One index,
on `expires_at`, for the sweep; the token is found by its own unique index.

Applying: `alembic upgrade 0113` on a database at `0112`. Rolling back this
revision only: `alembic downgrade 0113@-1`. After it the repository heads are
`{0092, 0111, 0113}` and bootstrap stays pinned to `0093`. A pre-existing
relation is compared **by definition** (`database/runtime_schema_guarantees.py`,
lifted from `0111` with one foreign-key correction that a case pins) and an
incompatible one — including the #1143 draft's shape — stops the upgrade.

## Lifecycle and cleanup

| stage | policy |
|---|---|
| mint | only when a page follows; a result that fits stores nothing |
| expiry | 24h (`TOKEN_LIFETIME_SECONDS`), judged on the database clock |
| single use | spent inside the reservation that shows the page |
| retention | 6 days after expiry (`RETENTION_AFTER_EXPIRY_SECONDS`), ≈ 7 days in all |
| cleanup | `run_navigation_sweep_scheduler`, registered in `backend/main.py` as `commerce_runtime_navigation_sweep`; every hour, passes of ≤500 rows, ≤20 passes a tick, off the event loop, never raising |
| rollback | dropping the relation discards unfinished browses and nothing else |

## Dormant until 0113 is applied

`navigation.schema_available(engine)` — the relation exists with every column
this code writes — is probed once per process. Where it is absent (production
today: startup pins `alembic upgrade 0093`):

* the search reads no candidates and its result carries no `more_results`;
* the `search_products` declaration and the reply declaration are byte-for-byte
  what they were (`more_label` is not offered);
* no browse runtime is handed to the loop, and a "More" tap is `unavailable`.

Applying 0113 takes effect at the next process start.

## Governance

```
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=YES   — tool/reply declarations, only where 0113 exists:
                       choices.more_label (reply declaration) and one sentence
                       on search_products about more_results
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```

No customer-facing text is composed by the platform on any path this adds: row
titles and descriptions are the merchant's values; the "More" row and the
button are the model's words; the body is the model's text; the only
transformation is option lines under the model's text on the two degraded
paths, the same pattern a withheld selector already uses.

## What changed from the #1143 draft

| #1143 | now |
|---|---|
| the model had to name more than ten products for paging to engage | the model names only what it saw; the platform pages the typed search result |
| `open_browse`/`continue_browse` flushed on the tool session, which is closed without commit — nothing would have persisted | writes run on the reservation transaction's connection and commit with the reply |
| the token was spent when read; a turn that failed after reading lost the page | read with `peek`, spent only with the reserved reply |
| `sweep()` had no caller | a startup-registered scheduler runs it |
| no provenance, no completeness, no stored words, no spender | all four, plus integrity checks |
| every page nine; a final page could not carry ten | final page up to ten |
| page-one rows were the model's selection | page one is the stored head; rows 6–9 hydrated by a trusted read |

## Limitations

* A customer who *types* about a row the model never saw ("the seventh one")
  relies on the model's own context, which carries the products replies cited,
  not every row sent. Taps are exact; typed positional references to
  platform-composed rows are not resolved by the platform.
* The words on later pages are the ones given when the browse opened; a
  customer who switches language mid-browse sees the first language's words.
* A general browse (empty query) that is not exhaustive within its formatting
  window is stored as not complete, even when the remaining rows are all
  unorderable.
* Candidate reads add one statement to every search while paging is on — see
  the performance section of the PR.
