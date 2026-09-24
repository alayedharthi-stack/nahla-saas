"""Minting and spending one page token, and refusing every other way to use it.

The contract is deliberately small. A browse too long for one list stores its
**whole ordered result** once and mints a token for the page after the one
being sent. When that token comes back on a tap, it is spent — once, in this
conversation, before it expires — and the next page is composed from the stored
order rather than from a second search.

Everything that is not exactly that fails closed and resolves to *no page*: the
turn then proceeds on whatever text the tap delivered, the model is told which
refusal it was, and the customer is still answered. No refusal ever becomes a
search, a product selection or a guess.

| what arrived | outcome |
|---|---|
| a token this conversation minted, unspent, unexpired | the page, and the next token |
| a token already spent | ``replayed`` — no page |
| a token past its expiry | ``expired`` — no page |
| a token minted in another conversation, or for another tenant | ``not_found`` — no page |
| a string nobody minted | ``not_found`` — no page |
| the store could not be read | ``unavailable`` — no page |

Spent with the reply, or not at all
===================================
Reading a token (``peek``) changes nothing. The token is spent — and the next
one minted — by ``apply``, which runs **inside the transaction that reserves
the reply showing the page**, on that transaction's own locked connection. So:

* a turn that stops before its reply is reserved (a provider failure, a crash,
  a lost lease) spends nothing, and the customer can tap again;
* a reply that is reserved has spent its token, and minted its successor, in
  the same commit — there is no window where one exists without the other;
* two turns racing on one token cannot both win. The spend is one conditional
  statement — ``UPDATE … WHERE consumed_at IS NULL AND expires_at > now()`` —
  and PostgreSQL arbitrates: the loser's reservation is refused whole.

Naming the refusal does not widen the lookup
============================================
Every read is scoped to the tenant, namespace and conversation the turn was
verified for, so a token belonging to somewhere else is indistinguishable from
one that never existed — the right answer, and the only one that leaks nothing.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.commerce_runtime import navigation_models as nm

logger = logging.getLogger("nahla.commerce_runtime.navigation")

# Closed outcomes, for the pilot log, the model's facts and tests.
RESOLVED = "resolved"              # a live token of this conversation's
NOT_FOUND = "not_found"            # forged, not this conversation's, or never minted
EXPIRED = "expired"
REPLAYED = "replayed"
UNAVAILABLE = "unavailable"        # the store could not be read, or does not exist here

# Bytes of entropy behind a token. Long enough that guessing is not a strategy;
# the scope check behind it means guessing right would still buy nothing.
_TOKEN_BYTES = 24

# The sweep: how many rows one pass removes at most, how many passes one
# scheduler tick may make, and how often the scheduler ticks.
SWEEP_BATCH = 500
SWEEP_MAX_BATCHES_PER_TICK = 20
SWEEP_INTERVAL_SECONDS = 3600
SWEEP_FIRST_DELAY_SECONDS = 120

_TABLE = nm.NavigationSnapshot.__table__


class NavigationNotPersisted(Exception):
    """The reservation's navigation writes could not be made; nothing was written."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# Why ``apply`` refused, closed.
CLAIM_LOST = "claim_lost"          # the token was spent or expired after it was read
NOT_STORED = "not_stored"          # the next page's token could not be written


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
    return token if 0 < len(token) <= 64 else None


def is_navigation_row(row: Any) -> bool:
    """Whether a composed or stored list row is the affordance rather than a product."""
    return isinstance(row, dict) and token_from_row_id(row.get("id")) is not None


# ── Page arithmetic ──────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class PageBounds:
    """Which slice of a stored result one page shows, and whether one follows."""

    start: int
    end: int
    has_next: bool

    @property
    def number(self) -> int:
        """The page's number as the customer counts it, from one.

        Every page but the last carries exactly ``PAGE_SIZE`` products, so a
        page always starts on a multiple of it.
        """
        return self.start // nm.PAGE_SIZE + 1


def page_bounds(total: int, offset: int) -> PageBounds:
    """The page starting at ``offset`` in a result of ``total`` products.

    A page that has a successor spends one of the ten rows on the affordance,
    so it carries nine products. A final page needs none and carries up to ten
    — so a result of ten is one list, and a result of nineteen is two.
    """
    total, offset = max(0, int(total)), max(0, int(offset))
    if offset >= total:
        return PageBounds(start=total, end=total, has_next=False)
    if total - offset <= nm.MAX_ROWS:
        return PageBounds(start=offset, end=total, has_next=False)
    return PageBounds(start=offset, end=offset + nm.PAGE_SIZE, has_next=True)


# ── Reading a token ──────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Continuation:
    """What a token opens, read and not yet spent — or why it opens nothing."""

    status: str
    token: str = ""
    series: str = ""
    product_ids: Tuple[int, ...] = ()
    page_offset: int = 0
    page_size: int = nm.PAGE_SIZE
    complete: bool = False
    more_label: str = ""
    button_label: str = ""
    origin_turn_id: int = 0
    origin_call_id: str = ""
    search_method: str = ""
    query_digest: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == RESOLVED

    def bounds(self) -> PageBounds:
        return page_bounds(len(self.product_ids), self.page_offset)

    def page_ids(self) -> Tuple[int, ...]:
        bounds = self.bounds()
        return self.product_ids[bounds.start:bounds.end]


def _stored_ids(raw: Any) -> Tuple[int, ...]:
    out: List[int] = []
    for item in raw or ():
        if isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in out:
            out.append(number)
    return tuple(out)


def peek(engine: Any, *, token: str, tenant_id: int, namespace: str,
         conversation_id: int) -> Continuation:
    """What ``token`` opens in this conversation, read without spending it.

    Scoped to the tenant, namespace and **runtime** conversation the turn was
    verified for, so another conversation's token reads exactly as a forged
    one. Expiry is judged on the database clock. Never raises.
    """
    wanted = str(token or "").strip()
    if not wanted:
        return Continuation(status=NOT_FOUND)
    from sqlalchemy import func, select  # noqa: PLC0415

    statement = (
        select(_TABLE.c.series, _TABLE.c.product_ids, _TABLE.c.page_offset, _TABLE.c.page_size,
               _TABLE.c.complete, _TABLE.c.more_label, _TABLE.c.button_label,
               _TABLE.c.origin_turn_id, _TABLE.c.origin_call_id, _TABLE.c.search_method,
               _TABLE.c.query_digest, _TABLE.c.consumed_at,
               (_TABLE.c.expires_at > func.now()).label("live"))
        .where(_TABLE.c.token == wanted, _TABLE.c.tenant_id == int(tenant_id),
               _TABLE.c.namespace == str(namespace),
               _TABLE.c.conversation_id == int(conversation_id))
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(statement).first()
    except Exception as exc:  # noqa: BLE001 - an unreadable store is never a page
        logger.warning("[COMMERCE_RUNTIME] navigation token unreadable tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return Continuation(status=UNAVAILABLE)
    if row is None:
        return Continuation(status=NOT_FOUND)
    if row.consumed_at is not None:
        return Continuation(status=REPLAYED)
    if not row.live:
        return Continuation(status=EXPIRED)
    return Continuation(
        status=RESOLVED, token=wanted, series=str(row.series),
        product_ids=_stored_ids(row.product_ids), page_offset=int(row.page_offset),
        page_size=int(row.page_size), complete=bool(row.complete),
        more_label=str(row.more_label), button_label=str(row.button_label),
        origin_turn_id=int(row.origin_turn_id), origin_call_id=str(row.origin_call_id),
        search_method=str(row.search_method), query_digest=str(row.query_digest),
    )


# ── Writing, inside the reservation ──────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Mint:
    """One token to store: the page after the one being sent."""

    token: str
    series: str
    product_ids: Tuple[int, ...]
    page_offset: int
    complete: bool
    more_label: str
    button_label: str
    origin_turn_id: int
    origin_call_id: str
    search_method: str
    query_digest: str


@dataclasses.dataclass(frozen=True)
class Plan:
    """The navigation writes one reply reservation carries.

    ``spend`` is the token the customer tapped to reach this page (empty for
    the first page of a browse); ``mint`` is the token the page's own "More"
    row carries (``None`` on a final page).
    """

    spend: str = ""
    mint: Optional[Mint] = None

    def __bool__(self) -> bool:
        return bool(self.spend) or self.mint is not None


def apply(conn: Any, plan: Plan, *, tenant_id: int, namespace: str, conversation_id: int,
          turn_id: int, db_now: datetime,
          lifetime_seconds: int = nm.TOKEN_LIFETIME_SECONDS) -> None:
    """Spend and mint, on the reservation transaction's own connection.

    Called as that transaction's precondition, after the conversation row lock
    and before the reply is written, so everything here commits with the reply
    or not at all. Raises ``NavigationNotPersisted`` when the tapped token can
    no longer be spent or the next one cannot be stored; the caller then
    reserves the answer without the navigation instead.
    """
    if not plan:
        return
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    moment = db_now
    scope = [_TABLE.c.tenant_id == int(tenant_id), _TABLE.c.namespace == str(namespace),
             _TABLE.c.conversation_id == int(conversation_id)]
    if plan.spend:
        claimed = conn.execute(
            _TABLE.update()
            .where(_TABLE.c.token == plan.spend, *scope, _TABLE.c.consumed_at.is_(None),
                   _TABLE.c.expires_at > moment)
            .values(consumed_at=moment, consumed_by_turn_id=int(turn_id))
            .returning(_TABLE.c.id)
        ).first()
        if claimed is None:
            raise NavigationNotPersisted(CLAIM_LOST)
    mint = plan.mint
    if mint is None:
        return
    try:
        # A savepoint, so a refused write is named here rather than leaving the
        # reservation's transaction aborted under the caller.
        with conn.begin_nested():
            conn.execute(_TABLE.insert().values(
                token=mint.token, series=mint.series, tenant_id=int(tenant_id),
                namespace=str(namespace), conversation_id=int(conversation_id),
                minted_by_turn_id=int(turn_id), origin_turn_id=int(mint.origin_turn_id),
                origin_call_id=str(mint.origin_call_id)[:128],
                search_method=str(mint.search_method)[:64],
                query_digest=str(mint.query_digest)[:64],
                product_ids=list(mint.product_ids), complete=bool(mint.complete),
                page_offset=int(mint.page_offset), page_size=nm.PAGE_SIZE,
                more_label=mint.more_label, button_label=mint.button_label,
                expires_at=moment + timedelta(seconds=max(1, int(lifetime_seconds))),
            ))
    except SQLAlchemyError as exc:
        logger.warning("[COMMERCE_RUNTIME] next page token not stored tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        raise NavigationNotPersisted(NOT_STORED) from exc


# ── Whether this database can hold a browse at all ───────────────────────────

_schema_state: Dict[str, bool] = {}
_schema_lock = threading.Lock()


def _engine_key(engine: Any) -> str:
    try:
        return str(engine.url)
    except Exception:  # noqa: BLE001 - an engine that cannot name itself is probed every time
        return f"unknown:{id(engine)}"


def schema_available(engine: Any) -> bool:
    """Whether revision 0113's relation exists here, with the columns this code writes.

    Probed once per database. Absent — the production default until an owner
    applies the revision — paging is simply off: no candidate is read, the
    model is told nothing about more results, and no token is ever minted.
    """
    key = _engine_key(engine)
    with _schema_lock:
        cached = _schema_state.get(key)
    if cached is not None:
        return cached
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    available = False
    try:
        with engine.connect() as conn:
            present = conn.execute(sa_text("SELECT to_regclass(:t)"),
                                   {"t": f"public.{nm.NAVIGATION_TABLE}"}).scalar()
            if present:
                columns = {row[0] for row in conn.execute(sa_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"),
                    {"t": nm.NAVIGATION_TABLE})}
                available = {c.name for c in _TABLE.columns} <= columns
    except Exception as exc:  # noqa: BLE001 - an unprobeable store is not an available one
        logger.warning("[COMMERCE_RUNTIME] navigation schema probe failed error=%s",
                       type(exc).__name__)
        available = False
    with _schema_lock:
        _schema_state[key] = available
    logger.info("[COMMERCE_RUNTIME] navigation schema available=%s", available)
    return available


def reset_schema_probe() -> None:
    """Forget every probed engine. For tests and for an operator re-check."""
    with _schema_lock:
        _schema_state.clear()


# ── Cleanup ──────────────────────────────────────────────────────────────────


def sweep(engine: Any, *, batch: int = SWEEP_BATCH,
          retention_after_expiry_seconds: int = nm.RETENTION_AFTER_EXPIRY_SECONDS) -> int:
    """Remove tokens that stopped being usable long enough ago. One bounded pass.

    Deletes at most ``batch`` rows whose expiry passed more than the retention
    ago, judged on the database clock and found through the expiry index. A
    live token, and a spent one still inside its retention, is never touched.
    Returns how many rows were removed; never raises. A database without the
    relation has nothing to sweep.
    """
    if not schema_available(engine):
        return 0
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    try:
        with engine.begin() as conn:
            removed = conn.execute(sa_text(
                f"DELETE FROM {nm.NAVIGATION_TABLE} WHERE id IN ("
                f"  SELECT id FROM {nm.NAVIGATION_TABLE}"
                f"   WHERE expires_at < now() - make_interval(secs => :retention)"
                f"   ORDER BY expires_at LIMIT :batch)"),
                {"retention": max(0, int(retention_after_expiry_seconds)),
                 "batch": max(1, int(batch))}).rowcount
        return int(removed or 0)
    except Exception as exc:  # noqa: BLE001 - cleanup is maintenance, never a turn's business
        logger.warning("[COMMERCE_RUNTIME] navigation sweep failed error=%s", type(exc).__name__)
        return 0


def sweep_until_clean(engine: Any, *, batch: int = SWEEP_BATCH,
                      max_batches: int = SWEEP_MAX_BATCHES_PER_TICK) -> int:
    """Sweep in bounded passes until a pass comes back short, or the bound is hit."""
    total = 0
    for _ in range(max(1, int(max_batches))):
        removed = sweep(engine, batch=batch)
        total += removed
        if removed < max(1, int(batch)):
            break
    return total


_scheduler_state: Dict[str, Any] = {"ticks": 0, "removed": 0, "last_error": None}


async def run_navigation_sweep_scheduler(
    *, engine: Any = None, interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    first_delay_seconds: float = SWEEP_FIRST_DELAY_SECONDS,
    max_ticks: Optional[int] = None, sleep: Any = None,
) -> None:
    """The scheduled owner of cleanup: one bounded sweep every interval.

    Registered with the application's background tasks at startup. Each tick
    runs ``sweep_until_clean`` off the event loop, so a slow database never
    stalls a webhook; a tick that fails is logged and the next one runs as
    usual. ``engine``, ``max_ticks`` and ``sleep`` exist for tests.
    """
    pause = sleep or asyncio.sleep
    if engine is None:
        from database.session import engine as app_engine  # noqa: PLC0415

        engine = app_engine
    await pause(first_delay_seconds)
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        _scheduler_state["ticks"] += 1
        try:
            removed = await asyncio.to_thread(sweep_until_clean, engine)
            _scheduler_state["removed"] += removed
            _scheduler_state["last_error"] = None
            if removed:
                logger.info("[COMMERCE_RUNTIME] navigation sweep removed=%d", removed)
        except Exception as exc:  # noqa: BLE001 - the next tick runs regardless
            _scheduler_state["last_error"] = type(exc).__name__
            logger.warning("[COMMERCE_RUNTIME] navigation sweep tick failed error=%s",
                           type(exc).__name__)
        if max_ticks is not None and ticks >= max_ticks:
            break
        await pause(interval_seconds)


def scheduler_state() -> Dict[str, Any]:
    return dict(_scheduler_state)


__all__ = [
    "CLAIM_LOST", "Continuation", "EXPIRED", "Mint", "NOT_FOUND", "NOT_STORED",
    "NavigationNotPersisted", "PageBounds", "Plan", "REPLAYED", "RESOLVED", "SWEEP_BATCH",
    "SWEEP_INTERVAL_SECONDS", "UNAVAILABLE", "apply", "is_navigation_row", "new_token",
    "page_bounds", "peek", "reset_schema_probe", "row_id", "run_navigation_sweep_scheduler",
    "scheduler_state", "schema_available", "sweep", "sweep_until_clean", "token_from_row_id",
]
