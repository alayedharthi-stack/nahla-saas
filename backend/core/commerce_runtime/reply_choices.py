"""A tappable selector over an answer, offered by the model, composed by the platform.

The model decides whether this turn is one where a selector helps and which
products belong in it. It names them by **id** and nothing else. Everything the
customer then reads on a row — the title, the price, the option values — is
composed here from the merchant's own values **as this turn's tool results
returned them**, never from the model's words. That is the ownership split the
doctrine already draws: the reply's prose is the model's, the structured action
payload is the platform's.

So a row can only ever say what the turn established. A product the model names
without having looked it up this turn is refused in verification
(``choice_without_evidence``): the row would state a price the turn never read.

A selector is an affordance **over** the answer, never the answer itself. So
when the channel will not take the rows — fewer than two products to choose
between, more than it shows, a product with no usable title — the *selector*
is withheld and the *options* are not: they follow the model's own sentence as
lines of the merchant's values, the same title and description the rows would
have carried. A reply that says «اختر من القائمة» therefore never arrives with
nothing to choose from, and no option is ever trimmed to make a list fit. The
model's wording is never replaced, only followed; the reason rides on the
delivered payload as ``choices_withheld``.

A tap comes back as the row's id, and it is checked against the list that was
**sent**: the row ids each reply actually carried are persisted with it
(``recent_products.CHOICE_ROW_IDS_KEY``), and a tap must match one of those,
within the same per-product browsing-context lapse, *and* name a product still
carried and re-read in the merchant's catalogue. Being mentioned in a reply is
deliberately not enough — a product named in prose was never a row anyone could
tap. An id that does not verify is simply not a fact: the turn proceeds on the
row title the customer's tap sent as text, like any other message.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import choice_rows as cr

# Meta shows at most ten rows in one list. More than that is not a display
# detail to trim: trimming would take a real option out of the customer's
# answer, so the whole selector is withheld and the text carries them all.
MAX_CHOICES = 10
# One row is not a choice — the text has already named it.
MIN_CHOICES = 2

# The model's request, as the reply tool carries it, and the composed payload
# the transport reads. Two different keys on purpose: what was asked for and
# what the platform established are never the same field.
REQUESTED_KEY = "requested_choices"
CHOICES_KEY = "choices"
# Why a requested selector was not offered, carried on the delivered payload so
# the reason is auditable in production rather than only in a log line.
WITHHELD_KEY = "choices_withheld"

ROW_ID_PREFIX = "nahla:choice:"
PRODUCT_REF_PREFIX = "catalog:product:"

MAX_BUTTON_LABEL = 20
# Meta truncates a list row title. The affordance's word is bounded to what the
# channel renders whole, exactly as the rows' titles are.
MAX_ROW_TITLE = 24

# Closed reasons, for the pilot log and for tests.
NOT_REQUESTED = "not_requested"
TOO_FEW = "fewer_than_two_options"
TOO_MANY = "more_options_than_the_channel_shows"
NOT_OBSERVED = "not_observed_this_turn"
INCOMPLETE = "not_every_option_could_be_a_row"
OFFERED = "offered"
# The customer picked a product from a list this conversation sent, and this
# reply's selector does not offer that product back. Their own selection is
# answered first; the options here still follow the text as lines, so nothing
# the model meant to offer is lost.
TAP_ANSWERED_FIRST = "verified_tap_answered_first"
# A verified tap on a list's "More" row is being answered with the next page of
# that list. A selector the model asked for in the same reply stands down for
# it, and its options still follow the text as lines.
NAVIGATION_ANSWERED_FIRST = "navigation_page_answered_first"

# The one problem the loop raises when the platform's presentation decision
# found a list (several products this turn's search returned, no single focus)
# and the model's reply offered none. Asked once, through the verification
# feedback channel; the answer stays the model's, and a reply that still offers
# no selector goes as it was.
LIST_OFFER_NEEDED = "list_offer_needed"


def list_offer_detail(product_ids: Sequence[int], *, more_results: bool) -> str:
    """What the loop tells the model when it asks for the selector.

    Addressed to the model, never shown to a customer: which products the
    platform would list, what their rows show, which words the list needs, and
    that the decision is still the model's. Nothing about what to say.
    """
    ids = ", ".join(str(int(pid)) for pid in product_ids)
    words = "a button word" + (", and a more_label for the row that shows the next ones, "
                               "because the search matched more products than this list holds"
                               if more_results else "")
    return ("Your reply answers with several products this turn's search returned and offers no "
            "selector. If the answer offers them to the customer to choose from, submit the reply "
            f"again with choices naming them ({ids}) and {words}, in the customer's language. "
            "Each row shows the product's short name, current price and a few options from the "
            "merchant's records, so the text introduces them and does not list each one with its "
            "price. If the answer is not such an offer, submit it again unchanged.")


# The key a paged or platform-expanded list's own account of itself rides
# under, inside ``choices``: which page, how many shown, how many no longer
# available, whether another page follows, and whether the stored result was
# complete. Integers and booleans only — never a token, never a product value.
NAVIGATION_KEY = "navigation"
# The evidence references behind the values a platform-composed list's rows
# state, as the trusted read returned them. Audit, not citation: they are the
# platform's own reads, so they never become the reply's ``evidence_refs``.
ROW_EVIDENCE_KEY = "row_evidence_refs"


@dataclasses.dataclass(frozen=True)
class ChoiceSelection:
    """Rows a provider will accept, and the identities behind them."""

    rows: Tuple[Mapping[str, Any], ...]
    product_ids: Tuple[int, ...]
    button: str

    def __bool__(self) -> bool:
        return bool(self.rows)

    def as_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rows": [dict(row) for row in self.rows],
                                   "product_ids": list(self.product_ids)}
        if self.button:
            payload["button"] = self.button
        return payload


def product_ref(product_id: Any) -> str:
    return f"{PRODUCT_REF_PREFIX}{int(product_id)}"


def row_id(product_id: Any) -> str:
    return f"{ROW_ID_PREFIX}{int(product_id)}"


def product_id_from_row_id(value: Any) -> Optional[int]:
    """The product a tapped row names, or ``None`` if it names none.

    A row id is the platform's own token; anything else that arrives on this
    field — another surface's row, a tap on a list this runtime never sent —
    resolves to nothing, which is the honest answer.
    """
    text = str(value or "").strip()
    if not text.startswith(ROW_ID_PREFIX):
        return None
    try:
        product_id = int(text[len(ROW_ID_PREFIX):])
    except (TypeError, ValueError):
        return None
    return product_id if product_id > 0 else None


def requested_product_ids(draft: Any) -> Tuple[int, ...]:
    """The product ids the model asked to offer, de-duplicated, in its order."""
    payload = getattr(draft, "payload", None)
    if not isinstance(payload, Mapping):
        return ()
    request = payload.get(REQUESTED_KEY)
    if not isinstance(request, Mapping):
        return ()
    raw = request.get("product_ids")
    if not isinstance(raw, (list, tuple)):
        return ()
    out: List[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        try:
            product_id = int(item)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in out:
            out.append(product_id)
    return tuple(out)


def requested_button(draft: Any) -> str:
    """The word the model chose for the selector's button, if it chose one.

    Wording stays the model's here too: the platform supplies no default word
    of its own, and an absent label leaves the channel sender's existing one in
    place rather than introducing a second fixed phrase.
    """
    payload = getattr(draft, "payload", None)
    if not isinstance(payload, Mapping):
        return ""
    request = payload.get(REQUESTED_KEY)
    if not isinstance(request, Mapping):
        return ""
    return " ".join(str(request.get("button") or "").split())[:MAX_BUTTON_LABEL]


def requested_more_label(draft: Any) -> str:
    """The word the model chose for the affordance that reaches the next page.

    Customer-facing wording, so it is the model's, in the customer's own
    language — the same rule the card's button word follows, and for the same
    reason: the platform has no phrase of its own and will not invent one in
    whatever language it happens to be written in. Whether a list pages is not
    this word's to decide; a list that pages and lacks it is asked for it once,
    and without it has no "More" row.
    """
    payload = getattr(draft, "payload", None)
    if not isinstance(payload, Mapping):
        return ""
    request = payload.get(REQUESTED_KEY)
    if not isinstance(request, Mapping):
        return ""
    return " ".join(str(request.get("more_label") or "").split())[:MAX_ROW_TITLE]


def observed_products(observations: Sequence[Any]) -> Dict[int, Mapping[str, Any]]:
    """Every catalogue product this turn's tool results actually returned.

    Keyed by product id, with the merchant's values exactly as the tool
    projected them. A product read twice in one turn keeps the later read: it
    is the fresher statement of the same row.
    """
    found: Dict[int, Mapping[str, Any]] = {}
    for obs in observations or ():
        if not getattr(obs, "ok", False):
            continue
        result = getattr(obs, "result", None)
        if not isinstance(result, Mapping) or getattr(obs, "body_truncated", False):
            continue
        items: List[Any] = []
        listed = result.get("products")
        if isinstance(listed, (list, tuple)):
            items.extend(listed)
        single = result.get("product")
        if single is not None:
            items.append(single)
        for item in items:
            if not isinstance(item, Mapping):
                continue
            try:
                product_id = int(item.get("product_id") or 0)
            except (TypeError, ValueError):
                continue
            if product_id > 0:
                found[product_id] = item
    return found


def unobserved_choices(draft: Any, observations: Sequence[Any]) -> Tuple[int, ...]:
    """Requested products whose evidence this turn did not gather.

    Two things must hold, and the second is not implied by the first: the
    product's evidence reference is among what the tools returned **and** the
    draft cites it. A row states the merchant's price; a priced row nobody
    cited is a commerce claim with no reference behind it.
    """
    requested = requested_product_ids(draft)
    if not requested:
        return ()
    from core.commerce_runtime.agent_contracts import evidence_index  # noqa: PLC0415

    known = evidence_index(observations)
    cited = set(getattr(draft, "evidence_refs", ()) or ())
    observed = observed_products(observations)
    return tuple(product_id for product_id in requested
                 if product_id not in observed
                 or product_ref(product_id) not in known
                 or product_ref(product_id) not in cited)


def _wire_rows(products: Sequence[Mapping[str, Any]], *,
               start_position: int = 1) -> Tuple[List[Dict[str, Any]], bool]:
    """The rows these products compose to, and whether every one became a row.

    ``start_position`` is where this list begins in a longer one, so a numbered
    label on a later page states its place in the whole browse.
    """
    composed = cr.choice_rows(products, start_position=start_position)
    rows: List[Dict[str, Any]] = []
    for row in composed.rows:
        wire: Dict[str, Any] = {"id": row_id(row.product_id), "title": row.title}
        if row.description:
            wire["description"] = row.description
        rows.append(wire)
    return rows, bool(composed) and composed.complete


def _not_listed(products: Sequence[Mapping[str, Any]],
                already_listed: Sequence[int]) -> List[Mapping[str, Any]]:
    """The products that are not already rows on the customer's screen, by id."""
    listed = {int(product_id) for product_id in already_listed}
    return [product for product in products if int(product.get("product_id") or 0) not in listed]


def _requested_products(draft: Any, observations: Sequence[Any]) -> List[Mapping[str, Any]]:
    observed = observed_products(observations)
    return [observed[product_id] for product_id in requested_product_ids(draft)
            if product_id in observed]


def selection(draft: Any, observations: Sequence[Any]) -> Tuple[Optional[ChoiceSelection], str]:
    """The selector this draft may carry, and why there is none when there is not.

    Called after verification, so anything refused here is a display limit
    rather than a truth problem. The options themselves are not refused with
    it: see ``finalize``, which carries them into the text instead.
    """
    requested = requested_product_ids(draft)
    if not requested:
        return None, NOT_REQUESTED
    if len(requested) < MIN_CHOICES:
        return None, TOO_FEW
    if len(requested) > MAX_CHOICES:
        return None, TOO_MANY
    products = _requested_products(draft, observations)
    if len(products) != len(requested):
        return None, NOT_OBSERVED
    rows, complete = _wire_rows(products)
    if not rows or not complete:
        return None, INCOMPLETE
    return ChoiceSelection(rows=tuple(rows),
                           product_ids=tuple(int(str(row["id"])[len(ROW_ID_PREFIX):])
                                             for row in rows),
                           button=requested_button(draft)), OFFERED


def option_line(row: Mapping[str, Any]) -> str:
    """One option as a line of the merchant's own values, and nothing else.

    The same title and description the row would have carried, joined by the
    same separator. No heading, no verb, no connective: this is the platform's
    structured facts rendered where the channel would not take a row, not a
    sentence the platform wrote on the model's behalf. A description the title
    already states is not repeated.
    """
    title = " ".join(str(row.get("title") or "").split())
    description = " ".join(str(row.get("description") or "").split())
    if not title:
        return ""
    if not description or description in title:
        return title
    return f"{title}{cr.FIELD_SEPARATOR}{description}"


def options_as_text(text: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """The model's sentence, with the options it meant under it as fact lines.

    A reply that says «اختر من القائمة» must not arrive with no list. The
    options are the platform's own composed merchant facts; when the channel
    will not carry them as rows they are carried as lines, so the customer sees
    every option and the sentence refers to something that is there. The
    model's own wording is never replaced, only followed.
    """
    lines = [line for line in (option_line(row) for row in rows or ()) if line]
    if not lines:
        return str(text or "")
    body = str(text or "").rstrip()
    return "\n".join([body, *lines]) if body else "\n".join(lines)


def finalize(draft: Any, observations: Sequence[Any], *,
             withhold: str = "", already_listed: Sequence[int] = ()) -> Tuple[Any, str]:
    """The draft as it will be delivered, with its selector or with its options.

    A verified selector makes the reply ``rich`` and leaves the text exactly as
    the model wrote it. When the channel will not carry the rows, the **options
    are not withheld with them**: they follow the model's own sentence as lines
    of the merchant's values, so a reply that says «اختر من القائمة» never
    arrives with nothing to choose from. Only a reply that asked for no
    selector is returned untouched.

    The model's wording is never replaced — only followed by facts the platform
    already owns and had already composed for the rows.
    """
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415
    from core.commerce_runtime import ledger_contracts as lc  # noqa: PLC0415

    chosen, reason = selection(draft, observations)
    if withhold and chosen is not None:
        # The platform decided this turn answers the customer's own selection
        # instead. The rows are not offered; the options still are, below.
        chosen, reason = None, withhold
    payload: Dict[str, Any] = {key: value
                               for key, value in dict(getattr(draft, "payload", None) or {}).items()
                               if key != REQUESTED_KEY}
    if chosen is not None:
        payload[CHOICES_KEY] = chosen.as_payload()
        return dataclasses.replace(draft, kind=lc.DeliveryKind.RICH.value,
                                   payload=ac.public_copy(payload)), reason
    if reason == NOT_REQUESTED:
        return (draft if REQUESTED_KEY not in (getattr(draft, "payload", None) or {})
                else dataclasses.replace(draft, kind=lc.DeliveryKind.TEXT.value,
                                         payload=ac.public_copy(payload))), reason

    # The selector was asked for and cannot be offered. The options themselves
    # are still the answer, so they are carried as text rather than lost —
    # except those already on the customer's screen as rows (``already_listed``,
    # given only when a "More" page answered first), which a second copy would
    # only repeat.
    rows, _complete = _wire_rows(_not_listed(_requested_products(draft, observations),
                                             already_listed))
    text = options_as_text(getattr(draft, "text", ""), rows)
    if rows:
        payload[WITHHELD_KEY] = reason
    return dataclasses.replace(draft, kind=lc.DeliveryKind.TEXT.value, text=text,
                               payload=ac.public_copy(payload)), reason


def _is_navigation_row(row: Mapping[str, Any]) -> bool:
    from core.commerce_runtime import navigation as nav  # noqa: PLC0415

    return nav.token_from_row_id(row.get("id")) is not None


def wire_rows(products: Sequence[Mapping[str, Any]], *,
              start_position: int = 1) -> Tuple[List[Dict[str, Any]], List[int]]:
    """Rows for products the platform chose to list, and the ids that became none.

    The platform-composed counterpart of the model's selector, for a list the
    platform decided the shape of — a page of a stored browse. The same rows
    as ``selection`` composes, from the merchant's values as a trusted read
    returned them. A product that cannot be a row is named, not hidden.
    """
    rows, _complete = _wire_rows(products, start_position=start_position)
    listed = {product_id_from_row_id(row["id"]) for row in rows}
    dropped: List[int] = []
    for product in products:
        try:
            product_id = int(product.get("product_id") or 0)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in listed and product_id not in dropped:
            dropped.append(product_id)
    return rows, dropped


def finalize_composed(draft: Any, observations: Sequence[Any], chosen: ChoiceSelection, reason: str, *,
                      navigation: Optional[Mapping[str, Any]] = None,
                      stand_down: str = "",
                      row_refs: Sequence[str] = (),
                      already_listed: Sequence[int] = ()) -> Tuple[Any, str]:
    """The draft as it will be delivered, carrying a list the platform composed.

    The model's text is not replaced. When the model asked for a selector of
    its own and the platform is answering something else first — the next
    page the customer tapped for — ``stand_down`` names why, the model's
    selector is not offered, and its options follow the text as lines exactly
    as ``finalize`` carries any withheld selector's options — except those this
    customer already has as rows (``already_listed``: this page's rows and the
    rows this conversation sent earlier, by id). A second copy as lines is the
    duplication Tenant 1 saw on 25 September, and it carries nothing the rows do
    not; a product that was never a row still follows as a line.
    """
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415
    from core.commerce_runtime import ledger_contracts as lc  # noqa: PLC0415

    payload: Dict[str, Any] = {key: value
                               for key, value in dict(getattr(draft, "payload", None) or {}).items()
                               if key != REQUESTED_KEY}
    text = getattr(draft, "text", "")
    if stand_down and requested_product_ids(draft):
        withheld_rows, _complete = _wire_rows(
            _not_listed(_requested_products(draft, observations), already_listed))
        text = options_as_text(text, withheld_rows)
        # Recorded whether or not any line followed: a selector that stood
        # down with every option already on screen is still a stand-down, and
        # production must be able to tell it from a reply that asked for none.
        payload[WITHHELD_KEY] = stand_down
    composed = chosen.as_payload()
    if navigation:
        composed[NAVIGATION_KEY] = dict(navigation)
    if row_refs:
        composed[ROW_EVIDENCE_KEY] = [str(ref) for ref in row_refs]
    payload[CHOICES_KEY] = composed
    return dataclasses.replace(draft, kind=lc.DeliveryKind.RICH.value, text=text,
                               payload=ac.public_copy(payload)), reason


def text_only_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The same reply without its selector, for the bounded recovery attempt.

    The rows do not simply disappear: the provider refused to render them, not
    to carry their content, so they follow the model's sentence as lines. A
    customer whose list was rejected still reads every option, and the
    sentence that pointed at the list still points at something.
    """
    out = {key: value for key, value in dict(payload or {}).items() if key != CHOICES_KEY}
    rows, _button = payload_rows(payload)
    # The affordance that reached another page is not an option: as a line it
    # would be a word with nothing behind it. Only products become lines.
    rows = [row for row in rows if not _is_navigation_row(row)]
    if rows:
        out["text"] = options_as_text(out.get("text", ""), rows)
        out[WITHHELD_KEY] = "provider_rejected_the_list"
    return out


def payload_rows(payload: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """The rows and button a stored delivery payload carries, if it carries any."""
    choices = (payload or {}).get(CHOICES_KEY)
    if not isinstance(choices, Mapping):
        return [], ""
    raw = choices.get("rows")
    if not isinstance(raw, (list, tuple)):
        return [], ""
    rows = [dict(row) for row in raw if isinstance(row, Mapping)]
    return rows, " ".join(str(choices.get("button") or "").split())[:MAX_BUTTON_LABEL]


__all__ = [
    "CHOICES_KEY", "ChoiceSelection", "INCOMPLETE", "LIST_OFFER_NEEDED", "MAX_BUTTON_LABEL",
    "MAX_CHOICES", "list_offer_detail",
    "MAX_ROW_TITLE", "MIN_CHOICES", "NAVIGATION_ANSWERED_FIRST", "NAVIGATION_KEY",
    "NOT_OBSERVED", "NOT_REQUESTED", "OFFERED", "PRODUCT_REF_PREFIX", "ROW_EVIDENCE_KEY",
    "finalize_composed",
    "requested_more_label", "wire_rows",
    "TAP_ANSWERED_FIRST",
    "REQUESTED_KEY", "ROW_ID_PREFIX", "TOO_FEW", "TOO_MANY", "WITHHELD_KEY", "finalize",
    "observed_products", "option_line", "options_as_text",
    "payload_rows", "product_id_from_row_id", "product_ref", "requested_button",
    "requested_product_ids", "row_id", "selection", "text_only_payload", "unobserved_choices",
]
