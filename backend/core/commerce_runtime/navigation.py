"""Minting and spending one page token, and refusing every other way to use it.

The contract is deliberately small. A browse that does not fit one list stores
its **whole ordered result** once and mints a token for the page after the one
being sent. When that token comes back on a tap, it is spent — once, in this
conversation, before it expires — and the next page is composed from the stored
order rather than from a second search.

Everything that is not exactly that fails closed and resolves to *no
navigation*: the turn then proceeds on whatever text the tap delivered, like any
other message, and the customer is still answered.

| what arrived | outcome |
|---|---|
| a token this conversation minted, unspent, unexpired | the page, and the next token |
| a token already spent | ``replayed`` — no page |
| a token past its expiry | ``expired`` — no page |
| a token minted in another conversation, or for another tenant | ``not_found`` — no page |
| a string nobody minted | ``not_found`` — no page |
| the store could not be read or written | ``unavailable`` — no page |

Spending is one statement
=========================
``UPDATE … SET consumed_at = now() WHERE token = :t AND tenant = … AND
conversation = … AND consumed_at IS NULL AND expires_at > now() RETURNING …``

The read and the claim are the same statement, so two taps racing on one token
cannot both win: PostgreSQL arbitrates, and exactly one of them gets a page. A
check-then-act would have let both through, which is the bug this shape exists
to make impossible rather than unlikely.

Naming the refusal does not widen the lookup
============================================
When the conditional update claims nothing, a second read — **scoped to the same
tenant and conversation** — says whether the token was spent, expired, or never
here. It can only ever see rows this conversation is already entitled to, so a
token belonging to somewhere else is indistinguishable from one that never
existed, which is the right answer to give and the only one that leaks nothing.
"""
from __future__ import annotations

import dataclasses
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

from core.commerce_runtime import navigation_models as nm

logger = logging.getLogger("nahla.commerce_runtime.navigation")

# Closed outcomes, for the pilot log and for tests.
OPENED = "opened"                  # a continuation was stored and a token minted
NO_NEXT_PAGE = "no_next_page"      # everything fits; nothing was stored
RESOLVED = "resolved"              # a token was spent and a page returned
NOT_FOUND = "not_found"            # forged, or not this conversation's, or never minted
EXPIRED = "expired"
REPLAYED = "replayed"
UNAVAILABLE = "unavailable"

# Bytes of entropy behind a token. Long enough that guessing is not a strategy;
# the scope check behind it means guessing right would still buy nothing.
_TOKEN_BYTES = 24


def new_token() -> str:
    """An opaque continuation token. Random, and meaningless outside the table."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def row_id(token: Any) -> str:
    """The row id a navigation affordance carries on the wire."""
    return f"{nm.NAVIGATION_ROW_PREFIX}{str(token or '').strip()}"


def token_from_row_id(value: Any) -> Optional[str]:
    """The token a tapped row names, or ``None`` if it names none.

    A product row resolves to nothing here, and a navigation row resolves to
    nothing in ``reply_choices.product_id_from_row_id``. The two namespaces are
    disjoint, so neither resolver can be fooled by the other's ids.
    """
    text = str(value or "").strip()
    if not text.startswith(nm.NAVIGATION_ROW_PREFIX):
        return None
    token = text[len(nm.NAVIGATION_ROW_PREFIX):]
    return token or None


@dataclasses.dataclass(frozen=True)
class Page:
    """One page of a stored browse, and the way to the next one."""

    product_ids: Tuple[int, ...]
    # The token that reaches the page after this one, or "" when this is the
    # last. An empty token is how "no More row" is said; nothing is invented to
    # stand in for a page that does not exist.
    next_token: str
    offset: int
    total: int
    reason: str

    @property
    def has_next(self) -> bool:
        return bool(self.next_token)

    def __bool__(self) -> bool:
        return bool(self.product_ids)


def _now(value: Optional[datetime]) -> datetime:
    return value if isinstance(value, datetime) else datetime.now(timezone.utc)


def _clean_ids(product_ids: Sequence[Any]) -> Tuple[int, ...]:
    """The browse's order, de-duplicated, with anything unusable dropped.

    Order is the caller's and is never re-sorted: it is the order the customer
    is being shown, and page two means "what came after what you saw".
    """
    out: List[int] = []
    for item in product_ids or ():
        if isinstance(item, bool):
            continue
        try:
            product_id = int(item)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in out:
            out.append(product_id)
    return tuple(out)


def open_browse(db: Any, *, tenant_id: int, namespace: str, conversation_id: int, turn_id: int,
                product_ids: Sequence[Any], page_size: int = nm.PAGE_SIZE,
                now: Optional[datetime] = None,
                lifetime_seconds: int = nm.TOKEN_LIFETIME_SECONDS) -> Page:
    """Store a browse that does not fit, and mint the token for its next page.

    Returns the first page either way. When everything fits, nothing is stored
    at all — a browse with no continuation leaves no row to expire or clean up.

    Never raises: a store that cannot be written costs the customer the paging
    and nothing else, and says so as ``unavailable``.
    """
    ordered = _clean_ids(product_ids)
    size = max(1, min(int(page_size), nm.MAX_ROWS))
    if len(ordered) <= size:
        # It fits. No token, no row, no "More" — and no page that does not exist.
        return Page(product_ids=ordered, next_token="", offset=0, total=len(ordered),
                    reason=NO_NEXT_PAGE)

    first, rest_offset = ordered[:size], size
    token, series = new_token(), new_token()
    moment = _now(now)
    try:
        db.add(nm.NavigationSnapshot(
            token=token, series=series, tenant_id=int(tenant_id), namespace=str(namespace),
            conversation_id=int(conversation_id), minted_by_turn_id=int(turn_id),
            product_ids=list(ordered), page_offset=rest_offset, page_size=size,
            expires_at=moment + timedelta(seconds=max(1, int(lifetime_seconds))),
        ))
        db.flush()
    except Exception as exc:  # noqa: BLE001 - paging is an affordance, never a precondition
        logger.warning("[COMMERCE_RUNTIME] navigation snapshot not stored tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("[COMMERCE_RUNTIME] navigation rollback failed after a store error")
        return Page(product_ids=first, next_token="", offset=0, total=len(ordered),
                    reason=UNAVAILABLE)
    return Page(product_ids=first, next_token=token, offset=0, total=len(ordered), reason=OPENED)


def continue_browse(db: Any, *, token: str, tenant_id: int, namespace: str, conversation_id: int,
                    now: Optional[datetime] = None,
                    lifetime_seconds: int = nm.TOKEN_LIFETIME_SECONDS) -> Page:
    """Spend a page token and return its page, or refuse and say exactly why.

    The claim is the read: one conditional statement, scoped to this tenant and
    this conversation, so a replay, an expiry and somebody else's token are all
    simply not claimed — and a race between two taps has one winner.
    """
    wanted = str(token or "").strip()
    empty = Page(product_ids=(), next_token="", offset=0, total=0, reason=NOT_FOUND)
    if not wanted:
        return empty
    moment = _now(now)
    table = nm.NavigationSnapshot.__table__
    scope = [table.c.token == wanted,
             table.c.tenant_id == int(tenant_id),
             table.c.namespace == str(namespace),
             table.c.conversation_id == int(conversation_id)]
    try:
        claimed = db.execute(
            table.update()
            .where(*scope, table.c.consumed_at.is_(None), table.c.expires_at > moment)
            .values(consumed_at=moment)
            .returning(table.c.series, table.c.product_ids, table.c.page_offset,
                       table.c.page_size, table.c.minted_by_turn_id)
        ).first()
    except Exception as exc:  # noqa: BLE001 - an unreadable store is never a page
        logger.warning("[COMMERCE_RUNTIME] navigation token unreadable tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return dataclasses.replace(empty, reason=UNAVAILABLE)

    if claimed is None:
        return dataclasses.replace(empty, reason=_why_not(db, scope, moment))

    series, stored, offset, size, minted_by = claimed
    ordered = _clean_ids(stored or ())
    page = ordered[offset:offset + size]
    if not page:
        # The stored order ran out. Nothing to show and nothing to mint; the
        # token is spent either way, which is right — it named this page.
        return Page(product_ids=(), next_token="", offset=int(offset), total=len(ordered),
                    reason=NO_NEXT_PAGE)
    following = int(offset) + int(size)
    if following >= len(ordered):
        return Page(product_ids=page, next_token="", offset=int(offset), total=len(ordered),
                    reason=RESOLVED)
    nxt = new_token()
    try:
        db.add(nm.NavigationSnapshot(
            token=nxt, series=series, tenant_id=int(tenant_id), namespace=str(namespace),
            conversation_id=int(conversation_id), minted_by_turn_id=int(minted_by),
            product_ids=list(ordered), page_offset=following, page_size=int(size),
            expires_at=moment + timedelta(seconds=max(1, int(lifetime_seconds))),
        ))
        db.flush()
    except Exception as exc:  # noqa: BLE001 - this page still goes out; the next one simply cannot
        logger.warning("[COMMERCE_RUNTIME] next navigation page not stored tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return Page(product_ids=page, next_token="", offset=int(offset), total=len(ordered),
                    reason=RESOLVED)
    return Page(product_ids=page, next_token=nxt, offset=int(offset), total=len(ordered),
                reason=RESOLVED)


def _why_not(db: Any, scope: Sequence[Any], moment: datetime) -> str:
    """Why a token was not claimed, read inside the scope that already applied.

    This second look can only see rows this conversation is entitled to, so
    another tenant's token and a token nobody ever minted give the same answer —
    which is the honest one, and the one that discloses nothing.
    """
    table = nm.NavigationSnapshot.__table__
    try:
        row = db.execute(
            table.select().with_only_columns(table.c.consumed_at, table.c.expires_at).where(*scope)
        ).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COMMERCE_RUNTIME] navigation refusal unreadable error=%s",
                       type(exc).__name__)
        return UNAVAILABLE
    if row is None:
        return NOT_FOUND
    consumed_at, expires_at = row
    if consumed_at is not None:
        return REPLAYED
    return EXPIRED if expires_at is not None and expires_at <= moment else NOT_FOUND


def sweep(db: Any, *, now: Optional[datetime] = None,
          retention_seconds: int = nm.RETENTION_SECONDS, limit: int = 1000) -> int:
    """Remove page tokens old enough that nobody is still reading them.

    Retention is bounded and deliberate: a token stays a while past its expiry
    so an operator can still see what a customer was offered, and then it goes.
    Returns how many rows were removed; never raises.
    """
    moment = _now(now)
    cutoff = moment - timedelta(seconds=max(1, int(retention_seconds)))
    table = nm.NavigationSnapshot.__table__
    try:
        stale = db.execute(
            table.select().with_only_columns(table.c.id)
            .where(table.c.created_at < cutoff).limit(max(1, int(limit)))
        ).fetchall()
        ids = [row[0] for row in stale]
        if not ids:
            return 0
        db.execute(table.delete().where(table.c.id.in_(ids)))
        db.commit()
        return len(ids)
    except Exception as exc:  # noqa: BLE001 - cleanup is maintenance, never a turn's business
        logger.warning("[COMMERCE_RUNTIME] navigation sweep failed error=%s", type(exc).__name__)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("[COMMERCE_RUNTIME] navigation sweep rollback failed")
        return 0


__all__ = [
    "EXPIRED", "NOT_FOUND", "NO_NEXT_PAGE", "OPENED", "Page", "REPLAYED", "RESOLVED",
    "UNAVAILABLE", "continue_browse", "new_token", "open_browse", "row_id", "sweep",
    "token_from_row_id",
]
