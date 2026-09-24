"""A search's results, offered in pages the customer walks with a "More" row.

Three authorities meet here, and none of them does another's job:

* **The model interprets the customer.** It decides a reply is a browse the
  customer may want to continue — not a recommendation, not a comparison — and
  says so the only way it can: by giving the word for the "More" row, in the
  customer's language, beside a selector over products of one search. Without
  that word nothing here engages, and a focused answer stays exactly the
  products the model named.
* **The tools establish the candidates.** The search that produced the model's
  five-product window also handed the platform its whole ordered result, typed
  and scoped (``search_candidates``). That, and nothing the model wrote, is
  what the list pages through.
* **The platform selects the presentation.** Which products each page shows,
  where a page ends, whether another follows, and what every product row says
  — read from the merchant's catalogue at the moment it is shown.

What a list never does
======================
* It never re-runs the search. Page one is the stored result's head; every
  later page is the next slice of the same stored order.
* It never claims the end of a result it did not reach: a browse stored at the
  platform's cap says ``complete: false`` on every page, and its last page
  offers no "More" row it cannot back.
* It never makes a product a focus, a selection or a card. Rows are hydrated
  by ``read_list_rows``, which returns row views, not observations: nothing it
  reads enters the turn's evidence or the presentation policy's provenance.
* It never invents a word. Both words on a paged list that are not the
  merchant's — the list's button and the "More" row — are the model's, given
  when the browse was opened and carried on the stored row for later pages.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import navigation as nav
from core.commerce_runtime import reply_choices as rc
from core.commerce_runtime import search_candidates as sc

# Why a page the customer tapped for went out as lines rather than a list,
# on the delivered payload. Its own key: the model's selector standing down
# for the page records under ``choices_withheld`` beside it.
WITHHELD_KEY = "navigation_withheld"

# Closed outcomes a reply's list can record.
OPENED = "browse_opened"          # page one of a stored browse; "More" reaches page two
WHOLE = "browse_offered_whole"    # the search's whole result fits one list; nothing stored
PAGE = "browse_page"              # a later page, reached by a verified "More" tap
PAGE_EMPTY = "browse_page_empty"  # a later page none of whose products can be shown
# A later page whose token could not be spent with the reply: its products,
# read moments ago, follow the model's text as lines, and nothing is minted.
PAGE_AS_LINES = "browse_page_as_lines"
# Composing the list failed unexpectedly; the reply is shaped without paging.
BROWSE_FAILED = "browse_failed"

# Why a browse was not opened. The reply then offers exactly the selector the
# model asked for, as it always has.
NOT_ASKED = "paging_not_asked"
NO_BUTTON_WORD = "paging_without_button_word"
NO_CONTINUATION = "no_search_continuation"
NOTHING_MORE = "search_had_nothing_more"
ROWS_UNAVAILABLE = "browse_rows_unavailable"
NAMED_NOT_LISTED = "named_product_not_listable"

# The one key the model is told a "More" tap's outcome under, beside the other
# trusted facts in its context. Data, never instruction.
FACTS_KEY = "browse_page"

ReadRows = Callable[[Any, Sequence[int], float], Any]


@dataclasses.dataclass(frozen=True)
class BrowseRuntime:
    """What the loop needs to open a browse, handed in by the runtime entry.

    Present only when this database can store a continuation. ``read_rows`` is
    ``agent_live_tools.read_list_rows`` bound to the turn's trusted context.
    """

    read_rows: ReadRows
    search_tool_names: Tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Composed:
    """A list the platform composed, the account it gives of itself, and the
    navigation writes the reply's reservation must carry."""

    selection: Optional[rc.ChoiceSelection]
    reason: str
    navigation: Mapping[str, Any]
    plan: nav.Plan
    # The evidence behind every value the rows state: ``catalog:product:<id>``
    # as the trusted read returned it at composition. Carried on the delivered
    # payload so a price on a platform-composed row is as auditable as a price
    # in a cited reply.
    row_refs: Tuple[str, ...] = ()


def _listable(view: Mapping[str, Any]) -> bool:
    """Whether the platform may put this product on a list it composes itself.

    Only a product the customer can buy **now**, by the catalogue's own single
    rule (``orderable``: ``can_checkout``, which already includes a merchant's
    hide, a Meta archive and stock). The model may still name a product it was
    shown flagged unorderable — that is its judgement, as it always was — but
    the platform never adds one on its own.
    """
    return bool(view.get("orderable"))


def _row_refs(products: Sequence[Mapping[str, Any]], listed: Sequence[int]) -> List[str]:
    """The evidence reference of every product a composed list states values for."""
    wanted = set(int(pid) for pid in listed)
    return [str(view.get("evidence_ref") or rc.product_ref(view.get("product_id")))
            for view in products if int(view.get("product_id") or 0) in wanted]


def _meta(*, page: int, offset: int, shown: int, unavailable: int, has_next: bool,
          complete: bool, stored: int) -> Dict[str, Any]:
    """The list's own account of itself, for the delivered payload and the log."""
    return {"page": int(page), "offset": int(offset), "shown": int(shown),
            "unavailable": int(unavailable), "has_next": bool(has_next),
            "complete": bool(complete), "stored": int(stored)}


def _search_for(requested: Sequence[int], observations: Sequence[Any], scope: Any,
                search_tool_names: Sequence[str]) -> Tuple[Optional[sc.SearchCandidates], str]:
    """The one search whose model window holds every product the model named.

    The most recent such search wins. Products named from two different
    searches belong to no single result, so there is nothing to page.
    """
    wanted = set(int(pid) for pid in requested)
    for obs in reversed(list(observations or ())):
        candidates, _why = sc.from_observation(
            obs, tenant_id=scope.tenant_id, namespace=scope.namespace,
            conversation_id=scope.conversation_id, turn_id=scope.turn_id,
            search_tool_names=search_tool_names)
        if candidates is not None and wanted <= set(candidates.window_ids):
            return candidates, str(getattr(obs, "call_id", "") or "")
    return None, ""


def open_browse(draft: Any, observations: Sequence[Any], *, scope: Any,
                runtime: BrowseRuntime, timeout_seconds: float) -> Tuple[Optional[Composed], str]:
    """Page one of the search the model offered a selector over, or why not.

    Engages only when the model asked for it — a "More" word and a button word,
    beside a selector over products of one search whose result extends beyond
    what the model was shown. Page one is the stored result's head, in its
    order: the products the model named are all on it, and the rest of it is
    hydrated from the catalogue now. When anything is missing — a word, the
    typed result, a readable catalogue, a row for a product the model named —
    this returns ``None`` and the reply offers exactly what the model asked.
    """
    label = rc.requested_more_label(draft)
    requested = rc.requested_product_ids(draft)
    if not label or not requested:
        return None, NOT_ASKED
    button = rc.requested_button(draft)
    if not button:
        # The list's own button is the other word a customer reads on it. The
        # channel sender has a fixed phrase for an empty one; a list the
        # platform composes must never reach it.
        return None, NO_BUTTON_WORD
    candidates, call_id = _search_for(requested, observations, scope, runtime.search_tool_names)
    if candidates is None:
        return None, NO_CONTINUATION
    if not candidates.extends_beyond_window:
        return None, NOTHING_MORE
    stored = candidates.product_ids
    bounds = nav.page_bounds(len(stored), 0)
    page_ids = stored[bounds.start:bounds.end]
    observed = rc.observed_products(observations)
    unseen = [pid for pid in page_ids if pid not in observed]
    read = runtime.read_rows(scope, unseen, timeout_seconds)
    if not getattr(read, "ok", False):
        return None, ROWS_UNAVAILABLE
    by_id: Dict[int, Mapping[str, Any]] = {pid: observed[pid] for pid in page_ids if pid in observed}
    for view in getattr(read, "products", ()) or ():
        by_id[int(view["product_id"])] = view
    named = set(int(pid) for pid in requested)
    products = [by_id[pid] for pid in page_ids
                if pid in by_id and (pid in named or _listable(by_id[pid]))]
    rows, _dropped = rc.wire_rows(products, start_position=1)
    listed = [rc.product_id_from_row_id(row["id"]) for row in rows]
    if any(int(pid) not in listed for pid in requested):
        return None, NAMED_NOT_LISTED
    mint: Optional[nav.Mint] = None
    if bounds.has_next:
        token = nav.new_token()
        rows.append({"id": nav.row_id(token), "title": label})
        mint = nav.Mint(
            token=token, series=nav.new_token(), product_ids=stored, page_offset=bounds.end,
            complete=candidates.complete, more_label=label, button_label=button,
            origin_turn_id=int(scope.turn_id), origin_call_id=call_id,
            search_method=candidates.method, query_digest=candidates.query_digest)
    selection = rc.ChoiceSelection(rows=tuple(rows), product_ids=tuple(int(p) for p in listed),
                                   button=button)
    meta = _meta(page=1, offset=0, shown=len(listed),
                 unavailable=len([pid for pid in page_ids if pid not in listed]),
                 has_next=bounds.has_next, complete=candidates.complete, stored=len(stored))
    reason = OPENED if bounds.has_next else WHOLE
    return Composed(selection=selection, reason=reason, navigation=meta, plan=nav.Plan(mint=mint),
                    row_refs=tuple(_row_refs(products, listed))), reason


@dataclasses.dataclass(frozen=True)
class BrowsePage:
    """A later page, verified in the store and read from the catalogue now.

    Built before the model runs, so what the model is told about the page —
    how many products are on it, how many are gone — is what the list shows.
    """

    continuation: nav.Continuation
    products: Tuple[Mapping[str, Any], ...]   # listable row views, in stored order
    missing: Tuple[int, ...]                  # the page's products that cannot be shown now

    @property
    def bounds(self) -> nav.PageBounds:
        return self.continuation.bounds()

    def facts(self) -> Dict[str, Any]:
        bounds = self.bounds
        return {"navigation": "next_page_of_an_earlier_list", "status": nav.RESOLVED,
                "page_number": bounds.number,
                "products_on_this_page": len(self.products),
                "no_longer_available": len(self.missing),
                "more_pages_after_this": bounds.has_next,
                "more_matches_than_the_list_holds": not self.continuation.complete}


def refusal_facts(status: str) -> Dict[str, Any]:
    """What the model is told when a "More" tap opens nothing — and only that."""
    return {"navigation": "next_page_of_an_earlier_list", "status": str(status)}


def resolve_tap(*, peek: Callable[[], nav.Continuation], read_rows: ReadRows, scope: Any,
                timeout_seconds: float) -> Tuple[Optional[BrowsePage], Dict[str, Any]]:
    """Read the page a verified token opens, without spending the token.

    A refusal — forged, replayed, expired, another conversation's, an
    unreadable store or catalogue — is a named fact for the model and no page.
    It never becomes a search, a product selection or a guess.
    """
    continuation = peek()
    if not continuation.resolved:
        return None, refusal_facts(continuation.status)
    page_ids = continuation.page_ids()
    read = read_rows(scope, page_ids, timeout_seconds)
    if not getattr(read, "ok", False):
        return None, refusal_facts(nav.UNAVAILABLE)
    by_id = {int(view["product_id"]): view for view in getattr(read, "products", ()) or ()}
    # Membership is the stored result's; what may be shown is decided now. A
    # product hidden, archived or sold out since the search is counted as no
    # longer available, never listed and never replaced.
    found = [by_id[pid] for pid in page_ids if pid in by_id and _listable(by_id[pid])]
    rows, dropped = rc.wire_rows(found, start_position=continuation.bounds().start + 1)
    listed = {rc.product_id_from_row_id(row["id"]) for row in rows}
    products = tuple(view for view in found if int(view["product_id"]) in listed)
    missing = tuple(pid for pid in page_ids if pid not in listed)
    page = BrowsePage(continuation=continuation, products=products, missing=missing)
    return page, page.facts()


def continue_browse(page: BrowsePage) -> Composed:
    """The page a verified "More" tap opened, and the token for the one after.

    The stored order decides membership and boundaries; the catalogue, read
    moments ago, decides what each row says. A product the catalogue no longer
    holds is counted, never replaced, and the page boundaries do not move for
    it — so no product is shown twice and none is skipped.
    """
    continuation = page.continuation
    bounds = page.bounds
    rows, _dropped = rc.wire_rows(list(page.products), start_position=bounds.start + 1)
    listed = [int(rc.product_id_from_row_id(row["id"])) for row in rows]
    mint: Optional[nav.Mint] = None
    if bounds.has_next:
        token = nav.new_token()
        rows.append({"id": nav.row_id(token), "title": continuation.more_label})
        mint = nav.Mint(
            token=token, series=continuation.series, product_ids=continuation.product_ids,
            page_offset=bounds.end, complete=continuation.complete,
            more_label=continuation.more_label, button_label=continuation.button_label,
            origin_turn_id=continuation.origin_turn_id,
            origin_call_id=continuation.origin_call_id,
            search_method=continuation.search_method,
            query_digest=continuation.query_digest)
    meta = _meta(page=bounds.number, offset=bounds.start, shown=len(listed),
                 unavailable=bounds.end - bounds.start - len(listed), has_next=bounds.has_next,
                 complete=continuation.complete, stored=len(continuation.product_ids))
    plan = nav.Plan(spend=continuation.token, mint=mint)
    if not rows:
        # Nothing on this page can be shown and nothing follows it. The token
        # is still spent — it named this page — and the answer goes as text.
        return Composed(selection=None, reason=PAGE_EMPTY, navigation=meta, plan=plan)
    selection = rc.ChoiceSelection(rows=tuple(rows), product_ids=tuple(listed),
                                   button=continuation.button_label)
    return Composed(selection=selection, reason=PAGE, navigation=meta, plan=plan,
                    row_refs=tuple(_row_refs(page.products, listed)))


def page_as_lines(draft: Any, page: BrowsePage) -> Any:
    """The model's text with the page's products under it as lines, and no list.

    For the one case a page was read but could not be spent with the reply.
    The lines are the same merchant values the rows would have carried — the
    platform's structured facts where the channel is not carrying rows — and
    the model's own wording is only followed, never replaced.
    """
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415

    rows, _dropped = rc.wire_rows(list(page.products), start_position=page.bounds.start + 1)
    if not rows:
        return draft
    payload = dict(getattr(draft, "payload", None) or {})
    payload[WITHHELD_KEY] = PAGE_AS_LINES
    return dataclasses.replace(draft, text=rc.options_as_text(getattr(draft, "text", ""), rows),
                               payload=ac.public_copy(payload))


__all__ = [
    "BROWSE_FAILED", "BrowsePage", "BrowseRuntime", "Composed", "FACTS_KEY", "NAMED_NOT_LISTED", "NOTHING_MORE",
    "NOT_ASKED", "NO_BUTTON_WORD", "NO_CONTINUATION", "OPENED", "PAGE", "PAGE_AS_LINES",
    "PAGE_EMPTY", "ROWS_UNAVAILABLE", "WHOLE", "WITHHELD_KEY", "continue_browse", "open_browse",
    "page_as_lines", "refusal_facts", "resolve_tap",
]
