"""The products this conversation's earlier replies were grounded on.

A customer who was shown a few dresses and asks, four turns later, about "the
first one" is not starting from nothing — the platform already knows exactly
which products that reply rested on, because each reply persists the evidence
references it used. What was missing is the bridge: product identity is
otherwise acquired only inside the turn that looked it up, so the follow-up
turn met a refusal (``product_id_not_discovered_in_this_run``) and a catalog
search on the referring phrase, which matches nothing.

Tenant 1, 2026-09-22 07:12Z: exactly that. ``get_product_details`` was refused,
``search_products`` returned no row, the turn produced no fact, and the reply
honestly said it had none — while the merchant's variant rows sat in the
catalog the whole time.

This module reads that evidence back:

* only from **this** conversation's own outbound rows,
* only references of the form ``catalog:product:<id>``,
* only products the tenant's catalog still holds, re-read now,
* only while the browsing context has not lapsed.

What it deliberately does **not** do is claim an order. The stored references
are the order the reply *cited*, which is not provably the order the customer
*saw*; the model has its own earlier message in the transcript and resolves
"the first" from that. A provable presentation order needs the reply itself to
carry structured choices, which is a later step.

The clock belongs to the **product**, not to the conversation. A customer who
chats every day about other things must not keep a dress from three weeks ago
alive: what is asked is "is *this product* still what 'the first one' means",
and the honest measure of that is how long ago a reply last showed it. A
product that is still being discussed refreshes itself, because the reply that
discusses it cites it — relevance is carried by evidence, never by guessing at
the subject of a message.

Nothing here is deleted or rewritten. Lapsing changes only what the platform
volunteers as context for one turn: the conversation, the customer's profile
and their real orders are untouched, and a customer who asks to go back to a
product simply has it looked up again — which the PostgreSQL proofs exercise
rather than assume.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.commerce_runtime.recent_products")

# How long a product stays current after the reply that last showed it.
# Adopted by the owner as an experimental starting point: one number, measured
# per product rather than from the conversation's last activity, and to be set
# from the ``seconds_since_last_product_shown`` the pilot log records. It is
# deliberately not WhatsApp's 24h service window — that governs sending, not
# memory — and it expires nothing else: the conversation, the customer's
# profile and their real orders are untouched.
BROWSING_CONTEXT_LAPSE_SECONDS = 72 * 3600

# Resource guards, not policy. The lapse above decides what is current; these
# only bound the read and the size of what one turn is handed.
MAX_REPLIES_READ = 20
MAX_PRODUCTS = 10

PRODUCT_REF_PREFIX = "catalog:product:"

# The metadata key an outbound row carries when that reply offered selectable
# rows. It is the evidence that *this* list was sent to *this* conversation,
# which is what a tap has to be checked against — a product merely named in
# prose was never a row anyone could tap.
CHOICE_ROW_IDS_KEY = "choice_row_ids"

# The provider's own id for the reply that carried those rows. A tap names the
# message it was made in, so this is what binds a tap to the one list it came
# from rather than to any list this conversation still holds.
PROVIDER_MESSAGE_ID_KEY = "provider_message_id"

# The product of the card an outbound reply actually **delivered**. Written only
# when the provider accepted the send and identified it, exactly as the row ids
# beside it are, and for the same reason: a reserved payload that carried a card
# the provider refused is not a card the customer saw. Recent-card suppression
# rests on this and on nothing else.
CARD_PRODUCT_ID_KEY = "card_product_id"

_OUTBOUND_DIRECTIONS = ("out", "outbound", "internal_e2e_outbound")

# Closed reasons, for the pilot log and for tests.
NO_EARLIER_REPLY = "no_earlier_reply"
LAPSED = "lapsed"
NO_PRODUCTS_CITED = "no_products_cited"
CARRIED = "carried"
UNAVAILABLE = "unavailable"


@dataclasses.dataclass(frozen=True)
class ShownProduct:
    """One product an earlier reply in this conversation was grounded on."""

    product_id: int
    title: str
    price: Optional[str]
    sale_price: Optional[str]
    currency: Optional[str]
    in_stock: Optional[bool]

    def as_fact(self) -> dict:
        fact: dict = {"product_id": self.product_id, "title": self.title}
        for key, value in (("price", self.price), ("sale_price", self.sale_price),
                           ("currency", self.currency)):
            if value not in (None, ""):
                fact[key] = value
        if self.in_stock is not None:
            fact["in_stock"] = bool(self.in_stock)
        return fact


@dataclasses.dataclass(frozen=True)
class ShownProducts:
    """What this conversation's recent replies showed, and why, if nothing."""

    products: Tuple[ShownProduct, ...]
    reason: str
    # Age of the most recent product citation found, carried or lapsed. The one
    # number the policy turns on, logged every turn so it can be set from
    # evidence rather than opinion.
    seconds_since_last_product_shown: Optional[int] = None
    # Products this conversation actually offered as selectable rows, within
    # the same per-product lapse. Strictly narrower than ``products``: being
    # mentioned in a reply is not being offered as a row, and only a row that
    # was sent can honestly be said to have been tapped.
    offered_as_rows: Tuple[int, ...] = ()
    # The same rows, kept per reply, keyed by the provider message id that
    # carried them. A tap that names its message is resolved against that one
    # list; the flat set above is the fallback for a tap that names none.
    offered_by_message: Mapping[str, Tuple[int, ...]] = dataclasses.field(default_factory=dict)
    # The product of the most recent card this conversation actually delivered,
    # within the same lapse. ``None`` means no card was delivered recently —
    # never "a card was delivered for some product we could not name".
    last_card_product_id: Optional[int] = None

    def rows_offered_in(self, provider_message_id: Any) -> Tuple[int, ...]:
        """The rows one named reply carried, or nothing if it carried none."""
        key = str(provider_message_id or "").strip()
        return tuple(self.offered_by_message.get(key, ())) if key else ()

    def __bool__(self) -> bool:
        return bool(self.products)

    @property
    def product_ids(self) -> List[int]:
        return [item.product_id for item in self.products]

    @property
    def titles(self) -> dict:
        return {item.product_id: item.title for item in self.products if item.title}

    def as_facts(self) -> List[dict]:
        return [item.as_fact() for item in self.products]


def _row_product_ids_offered(metadata: Any) -> List[int]:
    """The catalog products a stored reply offered as selectable rows.

    Read from the row ids the platform itself minted and persisted when it
    sent the list, so the answer is "this reply offered this row", never "this
    product came up somewhere".
    """
    if not isinstance(metadata, Mapping):
        return []
    raw = metadata.get(CHOICE_ROW_IDS_KEY)
    if isinstance(raw, str):
        raw = [part for part in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return []
    from core.commerce_runtime.reply_choices import product_id_from_row_id  # noqa: PLC0415

    out: List[int] = []
    for value in raw:
        product_id = product_id_from_row_id(value)
        if product_id is not None and product_id not in out:
            out.append(product_id)
    return out


def _card_product_delivered(metadata: Any) -> Optional[int]:
    """The product of the card a stored reply delivered, or ``None``.

    Read from the key the send path writes only on an accepted send, so the
    answer is "this reply put this card on the wire", never "this reply's
    payload once held a card".
    """
    if not isinstance(metadata, Mapping):
        return None
    try:
        product_id = int(metadata.get(CARD_PRODUCT_ID_KEY) or 0)
    except (TypeError, ValueError):
        return None
    return product_id if product_id > 0 else None


def _product_ids_cited(metadata: Any) -> List[int]:
    """The catalog product ids a stored reply's evidence references name."""
    if not isinstance(metadata, Mapping):
        return []
    refs = metadata.get("evidence_refs")
    if isinstance(refs, str):
        refs = [part for part in refs.split(",")]
    if not isinstance(refs, (list, tuple)):
        return []
    out: List[int] = []
    for ref in refs:
        text = str(ref or "").strip()
        if not text.startswith(PRODUCT_REF_PREFIX):
            continue
        try:
            product_id = int(text[len(PRODUCT_REF_PREFIX):])
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in out:
            out.append(product_id)
    return out


def _recent_outbound_rows(db: Any, *, tenant_id: int, conversation_id: int) -> Sequence[Any]:
    from models import MessageEvent  # noqa: PLC0415

    return (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == int(tenant_id),
            MessageEvent.conversation_id == int(conversation_id),
            MessageEvent.direction.in_(_OUTBOUND_DIRECTIONS),
        )
        .order_by(MessageEvent.created_at.desc(), MessageEvent.id.desc())
        .limit(MAX_REPLIES_READ)
        .all()
    )


def _age_seconds(moment: datetime, shown_at: Any) -> Optional[int]:
    """How long ago a stored reply was written, or ``None`` if it cannot say."""
    if not isinstance(shown_at, datetime):
        return None
    when = shown_at
    if when.tzinfo is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=when.tzinfo)
    elif when.tzinfo is None and moment.tzinfo is not None:
        when = when.replace(tzinfo=moment.tzinfo)
    return max(0, int((moment - when).total_seconds()))


def products_shown_earlier(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: int,
    now: Optional[datetime] = None,
    lapse_seconds: int = BROWSING_CONTEXT_LAPSE_SECONDS,
) -> ShownProducts:
    """Products this conversation showed recently enough to still mean.

    Each product is judged by the age of the reply that last showed **it**, not
    by how recently anything at all was said. A conversation that has carried
    on daily about other subjects therefore carries nothing from three weeks
    ago, while a product still being discussed stays current on its own, since
    the reply discussing it cites it.

    Never raises: a turn that cannot read its own history still runs, with no
    carried context rather than a failure.
    """
    try:
        rows = _recent_outbound_rows(db, tenant_id=tenant_id, conversation_id=conversation_id)
    except Exception as exc:  # noqa: BLE001 - context is an aid, never a precondition
        logger.warning("[COMMERCE_RUNTIME] earlier replies unreadable tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return ShownProducts(products=(), reason=UNAVAILABLE)

    rows = [row for row in rows if row is not None]
    if not rows:
        return ShownProducts(products=(), reason=NO_EARLIER_REPLY)

    moment = now if isinstance(now, datetime) else datetime.utcnow()
    lapse = max(0, int(lapse_seconds))
    current: List[int] = []
    offered: List[int] = []
    by_message: Dict[str, Tuple[int, ...]] = {}
    newest_citation: Optional[int] = None
    cited_anything = False
    last_card: Optional[int] = None
    for row in rows:
        metadata = getattr(row, "extra_metadata", None)
        cited = _product_ids_cited(metadata)
        offered_here = _row_product_ids_offered(metadata)
        delivered_card = _card_product_delivered(metadata)
        if delivered_card is not None and last_card is None:
            # Rows arrive newest first, so the first card found is the most
            # recent one, and only that one can be "the card we just sent".
            # It is held to the same lapse as everything else: a card from
            # last week is not a repeat, it is a new answer.
            card_age = _age_seconds(moment, getattr(row, "created_at", None))
            if card_age is None or card_age <= lapse:
                last_card = delivered_card
        if not cited and not offered_here:
            continue
        # A reply whose timestamp cannot be read is not evidence of staleness;
        # the age is simply unknown and the products it showed are carried.
        age = _age_seconds(moment, getattr(row, "created_at", None))
        if cited:
            cited_anything = True
            if age is not None and (newest_citation is None or age < newest_citation):
                newest_citation = age
        if age is not None and age > lapse:
            continue
        # A row that was offered is tappable whether or not this reply also
        # cited it; the two are collected side by side under the one clock.
        if offered_here:
            named = str((metadata or {}).get(PROVIDER_MESSAGE_ID_KEY) or "").strip() \
                if isinstance(metadata, Mapping) else ""
            if named and named not in by_message:
                by_message[named] = tuple(offered_here)
        for product_id in offered_here:
            if product_id not in offered:
                offered.append(product_id)
        if len(current) >= MAX_PRODUCTS:
            continue
        for product_id in cited:
            if product_id not in current:
                current.append(product_id)
    current = current[:MAX_PRODUCTS]

    rows_offered = tuple(offered)
    offered_map: Dict[str, Tuple[int, ...]] = dict(by_message)
    if not cited_anything:
        return ShownProducts(products=(), reason=NO_PRODUCTS_CITED, offered_as_rows=rows_offered,
                             offered_by_message=offered_map, last_card_product_id=last_card)
    if not current:
        return ShownProducts(products=(), reason=LAPSED, offered_as_rows=rows_offered,
                             offered_by_message=offered_map, last_card_product_id=last_card,
                             seconds_since_last_product_shown=newest_citation)

    products = _still_in_catalog(db, tenant_id=tenant_id, product_ids=current)
    if not products:
        return ShownProducts(products=(), reason=NO_PRODUCTS_CITED, offered_as_rows=rows_offered,
                             offered_by_message=offered_map, last_card_product_id=last_card,
                             seconds_since_last_product_shown=newest_citation)
    return ShownProducts(products=tuple(products), reason=CARRIED, offered_as_rows=rows_offered,
                         offered_by_message=offered_map, last_card_product_id=last_card,
                         seconds_since_last_product_shown=newest_citation)


def _still_in_catalog(db: Any, *, tenant_id: int, product_ids: Sequence[int]) -> List[ShownProduct]:
    """Re-read each cited product in the tenant's own catalog, now.

    A reference is only ever as good as the row behind it: a product the
    merchant has since removed, or one that was never this tenant's, must not
    become an identity this turn may read.
    """
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    builder = CatalogContextBuilder(db, int(tenant_id))
    out: List[ShownProduct] = []
    for product_id in product_ids:
        try:
            row = builder.get_by_id(int(product_id))
        except Exception as exc:  # noqa: BLE001 - one unreadable row is not the turn's failure
            logger.warning("[COMMERCE_RUNTIME] cited product unreadable tenant=%s error=%s",
                           tenant_id, type(exc).__name__)
            continue
        if not isinstance(row, Mapping) or int(row.get("id") or 0) != int(product_id):
            continue
        in_stock = row.get("in_stock")
        out.append(ShownProduct(
            product_id=int(product_id),
            title=str(row.get("title") or "").strip()[:120],
            price=_scalar(row.get("price")),
            sale_price=_scalar(row.get("sale_price")),
            currency=_scalar(row.get("currency")),
            in_stock=None if in_stock is None else bool(in_stock),
        ))
    return out


def _scalar(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "BROWSING_CONTEXT_LAPSE_SECONDS", "CARD_PRODUCT_ID_KEY", "CHOICE_ROW_IDS_KEY", "CARRIED",
    "PROVIDER_MESSAGE_ID_KEY", "LAPSED", "MAX_PRODUCTS", "MAX_REPLIES_READ",
    "NO_EARLIER_REPLY", "NO_PRODUCTS_CITED", "PRODUCT_REF_PREFIX", "ShownProduct",
    "ShownProducts", "UNAVAILABLE", "products_shown_earlier",
]
