# Navigation snapshot — paging a browse without asking the catalogue twice

WhatsApp shows ten rows. A merchant with thirty matching products cannot be
answered in one list, and the two honest ways to fail both cost the customer
something: trimming to fit takes a real option away, and withholding the whole
selector takes the tapping away. Paging is the third way, and it needs one thing
the runtime did not have — **a result set that outlives the turn that produced
it.**

## Why a relation of its own

The delivery ledger's `intent_payload` was measured against the seven properties
such a snapshot needs and failed five. The full table is in
`commerce-runtime-product-presentation.md`; the decisive ones are that the
sequence row is *deliberately mutable* while a snapshot must not be, that
retention there is the send audit record and cannot be pruned on a browsing
cadence, and that `UniqueConstraint("turn_id")` binds it to the one turn that
created it while a snapshot exists to be read by **later** turns.

## What one row is

One row is **one page token**.

| column | what it is |
|---|---|
| `token` | the opaque continuation string the next tap presents |
| `series` | the browse this page belongs to; every page of one browse shares it |
| `tenant_id`, `namespace`, `conversation_id` | the only scope the token is valid inside |
| `minted_by_turn_id` | provenance only; validity never depends on it |
| `product_ids` | the **whole ordered result**, immutable |
| `page_offset`, `page_size` | where this page starts, and how long it is |
| `expires_at` | when the token stops being usable |
| `consumed_at` | set the instant it is spent; a token is single use |

The ordered result is carried on each page of a series rather than referenced,
and that is the point: page two continues the order page one was composed
against **without asking the catalogue again**. A second search could return a
different set — something sold out, a price changed, a new arrival — and "the
next nine" would quietly mean something else. The rows a customer pages through
are the rows they were shown.

## Lifecycle

```
open_browse  →  [row: token T2, offset 9, expires now+24h]
   tap T2    →  claimed atomically, consumed_at set, page returned
             →  [row: token T3, offset 18, expires now+24h]   (if more remain)
   tap T2 again → replayed, no page
   sweep     →  rows older than 7 days removed
```

| stage | policy |
|---|---|
| **mint** | only when the result does not fit. A browse that fits stores nothing at all — no row to expire, no row to clean up |
| **expiry** | `TOKEN_LIFETIME_SECONDS` = 24h. A browse returned to tomorrow is a new question, not the next page of yesterday's answer. Deliberately shorter than the 72h browsing-context lapse, which governs what a product still *means*, not how long an unfinished list stays open |
| **single use** | `consumed_at` is set by the same statement that reads the row, conditional on it still being unset and unexpired |
| **retention** | `RETENTION_SECONDS` = 7 days from creation, then `sweep()` removes it. Rows outlive their expiry so an operator can still see what a customer was offered, and then they go. Retention is bounded, never open-ended |
| **rollback** | dropping the relation discards unfinished browses and nothing else: a customer mid-list simply gets a fresh answer |

`sweep()` is bounded (`limit`, default 1000) and never raises. It is not wired
to a scheduler in this change; it is called by an operator or a future job, and
until then rows accumulate only as fast as browses that do not fit.

## Spending is one statement

```sql
UPDATE commerce_runtime_navigation_snapshots
   SET consumed_at = now()
 WHERE token = :t AND tenant_id = :tenant AND namespace = :ns
   AND conversation_id = :conversation
   AND consumed_at IS NULL AND expires_at > now()
RETURNING series, product_ids, page_offset, page_size, minted_by_turn_id
```

The read and the claim are the same statement, so two taps racing on one token
cannot both win — PostgreSQL arbitrates, not the application. A check-then-act
would have let both through, which is the bug this shape makes impossible rather
than unlikely. A case drives it with two independent connections.

## Everything else fails closed

| what arrived | outcome |
|---|---|
| a token this conversation minted, unspent, unexpired | the page, and the next token |
| already spent | `replayed` |
| past expiry | `expired` |
| minted in another conversation, or for another tenant | `not_found` |
| a string nobody minted | `not_found` |
| the store could not be read or written | `unavailable` |

In every refusing case the turn proceeds on whatever text the tap delivered,
like any other message, and the customer is still answered.

**Naming the refusal does not widen the lookup.** When the conditional update
claims nothing, a second read — *scoped to the same tenant and conversation* —
says whether the token was spent, expired, or never here. It can only ever see
rows this conversation is already entitled to, so another tenant's token is
indistinguishable from one that never existed. That is the right answer, and the
only one that discloses nothing.

## A navigation row is not a product

Its id lives in `nahla:more:`, disjoint from the `nahla:choice:` namespace a
product row uses. `reply_choices.product_id_from_row_id` returns `None` for it
and `navigation.token_from_row_id` returns `None` for a product row, so neither
resolver can be fooled by the other's ids. Nothing here is a catalogue identity,
nothing here is commerce evidence, no reply may cite it, and **no fake product
id is invented** to carry the affordance.

## Composition

A page that has a successor spends one of its ten rows on the affordance that
reaches the successor, so **nine carry products and one is "More"**. A final page
needs no affordance and may carry ten.

## Two open decisions before this can be switched on

The durable substrate is complete and proven. Two *declaration* questions gate
whether a customer ever sees a paged list, and neither is mine to answer —
the approval for #1140 was explicitly scoped to that change alone.

1. **The affordance's word is customer-facing.** By the rule established for the
   card's button, it is the expression layer's, in the customer's own language,
   and the platform invents none. That needs an optional `choices.more_label` on
   the reply declaration. Without it, `requested_more_label` returns `""` and
   paging never engages.
2. **The model is currently told "two to ten products".** A browse can only be
   paged if the model may name more than ten, so `choices.product_ids` would
   need its stated range widened.

Until both land, `>10` keeps exactly today's behaviour: the selector is withheld
whole with `more_options_than_the_channel_shows`, and every option follows the
model's own sentence as a line of the merchant's values. Nothing is trimmed and
nothing is faked — the paging is simply not offered.

## Applying it

Dormant until applied. Production startup pins `alembic upgrade 0093` and
materialises only `models.Base`, so revision `0113` reaches no production
database on its own.

```
alembic upgrade 0113        # on a database at 0112
alembic downgrade 0113@-1   # this revision only
```

The branch-qualified downgrade matters wherever more than one head exists;
`0111` records why at length. After this revision the heads are `0111` and
`0113`, so `alembic upgrade head` stays ambiguous and every runbook names its
target.
