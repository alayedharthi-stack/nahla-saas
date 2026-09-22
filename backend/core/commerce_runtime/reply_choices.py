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

A selector is an affordance **over** the answer, never the answer itself, so
everything short of a truth problem degrades quietly to the model's text alone:
fewer than two products to choose between, more rows than the channel shows, a
product with no usable identity or title. The customer still hears about every
option in the model's own words; only the tapping is withheld, and the reason
is logged rather than fed back, because none of it makes the reply wrong.
Nothing is ever dropped from a list to make it fit — a list that cannot carry
every option is not sent at all.

A tap comes back as the row's id. It is resolved to a product id here and then
**re-checked** against what this conversation's replies actually showed
(``recent_products``), which is also what applies the browsing-context lapse.
An id that does not verify is simply not a fact: the turn proceeds on the row
title the customer's tap sent as text, like any other message.
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

ROW_ID_PREFIX = "nahla:choice:"
PRODUCT_REF_PREFIX = "catalog:product:"

MAX_BUTTON_LABEL = 20

# Closed reasons, for the pilot log and for tests.
NOT_REQUESTED = "not_requested"
TOO_FEW = "fewer_than_two_options"
TOO_MANY = "more_options_than_the_channel_shows"
NOT_OBSERVED = "not_observed_this_turn"
INCOMPLETE = "not_every_option_could_be_a_row"
OFFERED = "offered"


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


def selection(draft: Any, observations: Sequence[Any]) -> Tuple[Optional[ChoiceSelection], str]:
    """The selector this draft may carry, and why there is none when there is not.

    Called after verification, so anything refused here is a display limit
    rather than a truth problem, and the caller sends the model's text alone.
    """
    requested = requested_product_ids(draft)
    if not requested:
        return None, NOT_REQUESTED
    if len(requested) < MIN_CHOICES:
        return None, TOO_FEW
    if len(requested) > MAX_CHOICES:
        return None, TOO_MANY
    observed = observed_products(observations)
    products = [observed[product_id] for product_id in requested if product_id in observed]
    if len(products) != len(requested):
        return None, NOT_OBSERVED
    composed = cr.choice_rows(products)
    if not composed or not composed.complete:
        return None, INCOMPLETE
    rows: List[Dict[str, Any]] = []
    for row in composed.rows:
        wire: Dict[str, Any] = {"id": row_id(row.product_id), "title": row.title}
        if row.description:
            wire["description"] = row.description
        rows.append(wire)
    return ChoiceSelection(rows=tuple(rows),
                           product_ids=tuple(row.product_id for row in composed.rows),
                           button=requested_button(draft)), OFFERED


def finalize(draft: Any, observations: Sequence[Any]) -> Tuple[Any, str]:
    """The draft as it will be delivered, with its selector or without one.

    The text is returned untouched in every case. What changes is the delivery
    kind and the structured payload beside it: a verified selector makes the
    reply ``rich``, and everything else leaves it the plain text the model
    wrote. The one bounded rich-to-text recovery the ledger permits still
    applies afterwards, so a list the provider definitively refuses costs the
    tapping and not the answer.
    """
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415
    from core.commerce_runtime import ledger_contracts as lc  # noqa: PLC0415

    chosen, reason = selection(draft, observations)
    if chosen is None:
        # A draft that asked for nothing is left exactly as it is; only the
        # request itself is dropped, because a request the platform declined is
        # not part of what gets delivered.
        payload = dict(getattr(draft, "payload", None) or {})
        if REQUESTED_KEY not in payload:
            return draft, reason
        payload.pop(REQUESTED_KEY)
        return dataclasses.replace(draft, kind=lc.DeliveryKind.TEXT.value,
                                   payload=ac.public_copy(payload)), reason
    offered: Dict[str, Any] = {key: value for key, value in dict(getattr(draft, "payload", None) or {}).items()
                               if key != REQUESTED_KEY}
    offered[CHOICES_KEY] = chosen.as_payload()
    return dataclasses.replace(draft, kind=lc.DeliveryKind.RICH.value,
                               payload=ac.public_copy(offered)), reason


def text_only_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The same reply without its selector, for the bounded recovery attempt."""
    out = {key: value for key, value in dict(payload or {}).items() if key != CHOICES_KEY}
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
    "CHOICES_KEY", "ChoiceSelection", "INCOMPLETE", "MAX_BUTTON_LABEL", "MAX_CHOICES",
    "MIN_CHOICES", "NOT_OBSERVED", "NOT_REQUESTED", "OFFERED", "PRODUCT_REF_PREFIX",
    "REQUESTED_KEY", "ROW_ID_PREFIX", "TOO_FEW", "TOO_MANY", "finalize", "observed_products",
    "payload_rows", "product_id_from_row_id", "product_ref", "requested_button",
    "requested_product_ids", "row_id", "selection", "text_only_payload", "unobserved_choices",
]
