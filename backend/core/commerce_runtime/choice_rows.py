"""Selectable rows for a set of products, labelled from the merchant's facts.

WhatsApp rejects an interactive payload whose visible titles repeat (HTTP 400,
``Duplicate button title``), and Tenant 1's catalogue makes that immediate: its
five dresses are all titled «فستان». A selector built from titles alone would
collapse to one row, or be refused outright.

So a row's label is composed by the platform from values the merchant's own
records carry — the price it sells at, the option values its variants are in
stock in — and only far enough to tell the rows apart. Nothing is invented: a
product that no fact distinguishes from one already in the set is **left out**
and named to the caller, rather than given a made-up label or an internal id.

This is a structured action payload, which the platform owns; the sentence the
customer reads around it stays the model's. Labels here are values and
separators, never prose: no greeting, no verb, no connective wording.

A selector is an affordance over the answer, never the answer itself. So when
a set cannot be offered **whole** — ``complete`` is false — the caller sends
the model's own text and no selector, rather than a list quietly missing a
product. The customer still hears about every option, in the model's words;
only the tapping is withheld. That is why this returns what it could not
render instead of silently shortening the set.

Meta's caps are the contract: 24 characters for a row title, 72 for its
description, and titles compared the way the provider compares them.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

MAX_ROW_TITLE = 24
MAX_ROW_DESCRIPTION = 72
MAX_DESCRIPTION_OPTIONS = 2
MAX_DESCRIPTION_VALUES = 3

# Punctuation, not wording: a separator carries no language and states nothing.
FIELD_SEPARATOR = " · "
VALUE_SEPARATOR = "/"


@dataclasses.dataclass(frozen=True)
class ChoiceRow:
    """One selectable product: the identity, and what the customer sees."""

    product_id: int
    title: str
    description: str


@dataclasses.dataclass(frozen=True)
class ChoiceRows:
    """Rows a provider will accept, and what could not be rendered as one."""

    rows: Tuple[ChoiceRow, ...]
    indistinguishable: Tuple[int, ...]
    offered: int = 0

    def __bool__(self) -> bool:
        return bool(self.rows)

    @property
    def complete(self) -> bool:
        """Whether every product asked for became a row.

        False when anything was left out — an indistinguishable pair, or a
        product with no usable identity or title. A caller offering a selector
        checks this: an incomplete set is sent as the model's text alone, so a
        real option never disappears from the customer's answer because of a
        display limit.
        """
        return len(self.rows) == int(self.offered)


def _title_key(value: str) -> str:
    """Compare titles the way the provider does, so a clash is caught here."""
    try:
        from core.product_button_label import normalize_button_title_key  # noqa: PLC0415

        return str(normalize_button_title_key(value) or "")
    except Exception:  # noqa: BLE001 - a missing helper must not hide a clash
        return " ".join(str(value or "").split()).casefold()


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _effective_price(product: Mapping[str, Any]) -> str:
    """What the merchant sells it at now, with the currency it stores."""
    for key in ("sale_price", "price"):
        amount = str(product.get(key) or "").strip()
        if amount:
            currency = str(product.get("currency") or "").strip()
            return f"{amount} {currency}".strip()
    return ""


def _option_values(product: Mapping[str, Any]) -> List[Tuple[str, List[str]]]:
    options = product.get("variant_options")
    if not isinstance(options, Mapping):
        return []
    out: List[Tuple[str, List[str]]] = []
    for name, values in options.items():
        label = _text(name, 40)
        if not label or not isinstance(values, (list, tuple)):
            continue
        cleaned = [_text(v, 40) for v in values]
        cleaned = [v for v in cleaned if v]
        if cleaned:
            out.append((label, cleaned))
    return out


def _distinguishers(product: Mapping[str, Any], others: Sequence[Mapping[str, Any]]) -> List[str]:
    """Facts that set this product apart, most useful first.

    The price the merchant sells it at comes first: every product has one and
    it is what a customer comparing two similar items reads. Then an option
    value none of the others is in stock in — a colour or a size that is
    genuinely this one's. Nothing else is offered, because nothing else here
    would be a fact about the product.
    """
    out: List[str] = []
    price = _effective_price(product)
    if price and all(_effective_price(o) != price for o in others):
        out.append(price)
    theirs = {value for other in others for _n, values in _option_values(other) for value in values}
    for _name, values in _option_values(product):
        for value in values:
            if value not in theirs and value not in out:
                out.append(value)
    return out


def _description(product: Mapping[str, Any]) -> str:
    parts: List[str] = []
    price = _effective_price(product)
    if price:
        parts.append(price)
    for _name, values in _option_values(product)[:MAX_DESCRIPTION_OPTIONS]:
        parts.append(VALUE_SEPARATOR.join(values[:MAX_DESCRIPTION_VALUES]))
    text = FIELD_SEPARATOR.join(parts)
    while text and len(text) > MAX_ROW_DESCRIPTION and parts:
        parts.pop()
        text = FIELD_SEPARATOR.join(parts)
    return text[:MAX_ROW_DESCRIPTION]


def choice_rows(products: Sequence[Mapping[str, Any]]) -> ChoiceRows:
    """Rows for these products, in the order given, with distinct titles.

    A product is dropped only when its title clashes with one already accepted
    and no fact in its own record tells the two apart. The caller is told which,
    so "several of these look the same to the customer" stays a fact it can act
    on rather than something the platform papers over — see ``complete``.
    """
    offered = len(list(products))
    considered: List[Tuple[int, str, Mapping[str, Any]]] = []
    dropped: List[int] = []
    for product in products:
        product_id = _int(product.get("product_id") or product.get("id"))
        if product_id is None:
            continue
        base = _text(product.get("title"), MAX_ROW_TITLE)
        if not base:
            dropped.append(product_id)
            continue
        considered.append((product_id, base, product))

    # Products whose visible titles the provider would read as one. Every
    # member of such a group is labelled, not just the later ones: a set where
    # one row says «فستان» and the next «فستان · 114 SAR» asks the customer to
    # infer what the first one costs from the absence of a number.
    groups: Dict[str, List[int]] = {}
    for position, (_pid, base, _p) in enumerate(considered):
        groups.setdefault(_title_key(base), []).append(position)

    rows: List[ChoiceRow] = []
    taken: Dict[str, int] = {}
    for position, (product_id, base, product) in enumerate(considered):
        group = groups.get(_title_key(base)) or [position]
        if len(group) == 1:
            title: Optional[str] = base if _title_key(base) not in taken else None
        else:
            siblings = [considered[i][2] for i in group if i != position]
            title = _first_free_title(base, _distinguishers(product, siblings), taken)
        if title is None:
            dropped.append(product_id)
            continue
        taken[_title_key(title)] = product_id
        rows.append(ChoiceRow(product_id=product_id, title=title,
                              description=_description(product)))
    return ChoiceRows(rows=tuple(rows), indistinguishable=tuple(dropped), offered=offered)


def _first_free_title(base: str, distinguishers: Sequence[str], taken: Mapping[str, int]) -> Optional[str]:
    """The title plus the first fact that tells it apart, or nothing.

    A fact that would not fit is made to fit by shortening the *title*, not by
    cutting the fact: a price or a colour truncated halfway states something
    the merchant's record does not. When no fact distinguishes this product
    from the ones it shares a title with, there is no honest label and the
    caller is told so instead.
    """
    for fact in distinguishers:
        budget = MAX_ROW_TITLE - len(FIELD_SEPARATOR) - len(fact)
        if budget < 1:
            continue
        candidate = _text(f"{base[:budget].rstrip()}{FIELD_SEPARATOR}{fact}", MAX_ROW_TITLE)
        if candidate and _title_key(candidate) not in taken:
            return candidate
    return None


def _int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = ["MAX_ROW_DESCRIPTION", "MAX_ROW_TITLE", "ChoiceRow", "ChoiceRows", "choice_rows"]
