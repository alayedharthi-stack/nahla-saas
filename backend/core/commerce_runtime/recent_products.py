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

Nothing here is deleted or rewritten. Lapsing changes only what the platform
volunteers as context for one turn: the conversation, the customer's profile
and their real orders are untouched, and a customer who asks to go back to a
product simply has it looked up again.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.commerce_runtime.recent_products")

# How long a browsing context survives the conversation going quiet, measured
# from this conversation's last reply. Provisional: the owner's policy decision
# is pending, and this is the one number it changes. It is deliberately not
# WhatsApp's 24h service window — that governs sending, not memory.
BROWSING_CONTEXT_LAPSE_SECONDS = 72 * 3600

# How far back to read, and how much to carry. A follow-up refers to something
# recent; an unbounded set would be noise the model has to wade through.
MAX_REPLIES_READ = 5
MAX_PRODUCTS = 10

PRODUCT_REF_PREFIX = "catalog:product:"

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
    seconds_since_last_reply: Optional[int] = None

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


def products_shown_earlier(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: int,
    now: Optional[datetime] = None,
    lapse_seconds: int = BROWSING_CONTEXT_LAPSE_SECONDS,
) -> ShownProducts:
    """Products this conversation's recent replies cited, if still current.

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

    last_reply_at = getattr(rows[0], "created_at", None)
    elapsed: Optional[int] = None
    if isinstance(last_reply_at, datetime):
        moment = now if isinstance(now, datetime) else datetime.utcnow()
        if last_reply_at.tzinfo is not None and moment.tzinfo is None:
            moment = moment.replace(tzinfo=last_reply_at.tzinfo)
        elif last_reply_at.tzinfo is None and moment.tzinfo is not None:
            last_reply_at = last_reply_at.replace(tzinfo=moment.tzinfo)
        elapsed = max(0, int((moment - last_reply_at).total_seconds()))
        if moment - last_reply_at > timedelta(seconds=int(lapse_seconds)):
            return ShownProducts(products=(), reason=LAPSED,
                                 seconds_since_last_reply=elapsed)

    cited: List[int] = []
    for row in rows:
        for product_id in _product_ids_cited(getattr(row, "extra_metadata", None)):
            if product_id not in cited:
                cited.append(product_id)
        if len(cited) >= MAX_PRODUCTS:
            break
    cited = cited[:MAX_PRODUCTS]
    if not cited:
        return ShownProducts(products=(), reason=NO_PRODUCTS_CITED,
                             seconds_since_last_reply=elapsed)

    products = _still_in_catalog(db, tenant_id=tenant_id, product_ids=cited)
    if not products:
        return ShownProducts(products=(), reason=NO_PRODUCTS_CITED,
                             seconds_since_last_reply=elapsed)
    return ShownProducts(products=tuple(products), reason=CARRIED,
                         seconds_since_last_reply=elapsed)


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
    "BROWSING_CONTEXT_LAPSE_SECONDS", "CARRIED", "LAPSED", "MAX_PRODUCTS", "MAX_REPLIES_READ",
    "NO_EARLIER_REPLY", "NO_PRODUCTS_CITED", "PRODUCT_REF_PREFIX", "ShownProduct",
    "ShownProducts", "UNAVAILABLE", "products_shown_earlier",
]
