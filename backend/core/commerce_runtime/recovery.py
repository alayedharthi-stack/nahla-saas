"""Whether a redelivered inbound message is work this runtime has not finished.

Providers retry inbound webhooks, and the platform drops those retries — which
is right, because a duplicate must never produce a second answer. It is also
the only way an unfinished commerce-runtime turn can ever be resumed: the turn
is keyed by the inbound provider message id, so the retry carrying that id is
the one event that can reach it.

A turn is left unfinished when the worker holding it stopped between admitting
it and recording its terminal — a process that died, a lease that expired, a
completion the ledger refused because a reserved delivery had not been
dispatched. The customer is owed either an answer or an honest record, and
neither exists yet.

This module answers one question and takes no action:

    is there an admitted commerce-runtime turn for this exact inbound identity
    that has no terminal?

What it is **not**: a retry mechanism. Nothing here sends anything, and letting
a retry through resends nothing either — the delivery ledger still refuses to
dispatch an attempt whose outcome is pending, accepted or unknown, so an
uncertain send stays uncertain and the re-entry only records what is already
established.

Two properties hold by construction:

* **Completed work is still refused.** A turn with a terminal answers ``None``
  here, so its duplicates are dropped exactly as they are today.
* **Nothing outside the runtime's own scope is affected.** While it is off, or
  for a tenant or recipient it is not configured for, the answer is ``None``
  without a single query. Under ``store_gated`` and ``global`` the scope is the
  tenant that owns the connection, and the answer is still ``None`` unless this
  runtime admitted a turn for this exact inbound identity.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from core.commerce_runtime import contracts as c
from core.commerce_runtime import pilot_guard as pg

logger = logging.getLogger("nahla.commerce_runtime.recovery")

NAMESPACE = c.Namespace.LIVE.value
CHANNEL_REF_PREFIX = "wa"

# How many allowlisted tenants one lookup will consider. The pilot is
# owner-only; a list longer than this is a misconfiguration, not a workload —
# and it is refused rather than truncated, because silently ignoring the ninth
# tenant is how work in it gets left behind while a handover reports settled.
MAX_TENANTS_CONSIDERED = 8


class TooManyTenants(ValueError):
    """More tenants are configured than this owner-only pilot will inspect."""

    def __init__(self, count: int) -> None:
        self.count = int(count)
        super().__init__(f"{count} tenants configured; at most "
                         f"{MAX_TENANTS_CONSIDERED} are inspected")


def _connection_owner_tenants(phone_number_id: Any) -> list:
    """The one tenant whose verified connection owns this number, or ``[]``.

    Used where there is no allowlist to walk. A failed lookup answers ``[]`` and
    the caller drops the duplicate exactly as the platform does today — the same
    outcome an empty allowlist already produced, so an unreadable connection
    never becomes a *wider* answer than a readable one.
    """
    try:
        from database.session import SessionLocal  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - no session, no candidates
        logger.warning("[COMMERCE_RUNTIME_RECOVERY] session factory unavailable error=%s",
                       type(exc).__name__)
        return []
    session = None
    try:
        session = SessionLocal()
        scope = pg.resolve_pilot_scope(session, phone_number_id=phone_number_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable scope names no tenant
        logger.warning("[COMMERCE_RUNTIME_RECOVERY] connection owner lookup failed error=%s",
                       type(exc).__name__)
        return []
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                logger.warning("[COMMERCE_RUNTIME_RECOVERY] session close failed")
    return [int(scope.tenant_id)] if scope.resolved else []


def _scoped_tenants(tenant_ids: Any = None) -> list:
    """The tenants to inspect, or a refusal. Never a silent subset."""
    tenants = sorted({int(t) for t in (tenant_ids if tenant_ids is not None
                                       else pg.tenant_allowlist())})
    if len(tenants) > MAX_TENANTS_CONSIDERED:
        raise TooManyTenants(len(tenants))
    return tenants


@dataclasses.dataclass(frozen=True)
class AdmittedInbound:
    """A commerce-runtime turn this runtime admitted for one inbound identity.

    ``finished`` says whether it has a terminal. Both states matter, for
    different decisions: only an **unfinished** turn is work to resume, but an
    admitted turn is this runtime's either way, and neither may become new work
    for the legacy path.
    """

    tenant_id: int
    turn_id: int
    conversation_id: int          # the runtime conversation, not the application one
    provider_message_id: str
    finished: bool

    @property
    def unfinished(self) -> bool:
        return not self.finished


# The historical name. An unfinished turn is the recovery case; callers that
# only care about work to resume keep reading this.
UnfinishedTurn = AdmittedInbound


def channel_connection_ref(phone_number_id: Any) -> str:
    """The channel reference the runtime admits turns under."""
    return f"{CHANNEL_REF_PREFIX}:{str(phone_number_id or '').strip()}"


def unfinished_turn_for(
    *,
    tenant_id: int,
    phone_number_id: Any,
    provider_message_id: Any,
    engine: Any = None,
) -> Optional[AdmittedInbound]:
    """The runtime's **unfinished** turn for this inbound identity, or ``None``.

    This is the recovery question: is there work left to do. A finished turn
    answers ``None`` here, which is what keeps its duplicates duplicates.
    """
    admitted = admitted_turn_for(tenant_id=tenant_id, phone_number_id=phone_number_id,
                                 provider_message_id=provider_message_id, engine=engine)
    return admitted if admitted is not None and admitted.unfinished else None


class EvidenceUnavailable(RuntimeError):
    """The runtime's records could not be read. That is not "no turn"."""


def _turn_table_present(engine: Any) -> bool:
    """Whether this database holds the runtime's turn table — read now, on
    the engine handed in, raising when the catalogue cannot be read. The
    cached schema probe is for admission, where a failed probe keeps the
    runtime off; a decision that *closes* something must not read a cached
    failure as absence."""
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

    with engine.begin() as conn:
        return bool(sa_inspect(conn).has_table("commerce_runtime_turns"))


def admitted_turn_for(
    *,
    tenant_id: int,
    phone_number_id: Any,
    provider_message_id: Any,
    engine: Any = None,
    strict: bool = False,
) -> Optional[AdmittedInbound]:
    """Any turn this runtime admitted for this inbound identity, finished or not.

    This is the ownership question, and it is deliberately wider than the
    recovery one. A turn can be completed by another invocation between the
    moment deduplication lets its retry through and the moment routing looks at
    it; asking only "is there unfinished work" then answers no, and the inbound
    the runtime already owns and already answered becomes new work for the
    legacy path.

    Read-only. By default it never raises: anything it cannot establish is
    ``None``, which keeps every routing caller on its existing behaviour — a
    turn nobody can see is not routed to the runtime. A caller that is about
    to **close** an obligation on the answer asks with ``strict=True``: then
    ``None`` is answered only after the turn table was found and read and
    holds no turn for this identity, and a read that fails raises
    :class:`EvidenceUnavailable` instead — a failed read is not absence.
    """
    pmid = str(provider_message_id or "").strip()
    channel = channel_connection_ref(phone_number_id)
    if not pmid or channel == f"{CHANNEL_REF_PREFIX}:":
        return None
    try:
        from core.commerce_runtime.ledgers import LedgerRepository  # noqa: PLC0415
        from core.commerce_runtime.runtime_entry import runtime_schema_available  # noqa: PLC0415

        if engine is None:
            from database.session import engine as default_engine  # noqa: PLC0415

            engine = default_engine
        if strict:
            if not _turn_table_present(engine):
                return None
        elif not runtime_schema_available(engine):
            return None
        foundation = LedgerRepository(engine).foundation
        admitted = foundation.find_admitted_turn(
            tenant_id=int(tenant_id), namespace=NAMESPACE,
            channel_connection_ref=channel, provider_message_id=pmid)
        if admitted is None:
            return None
        terminal = foundation.get_terminal(
            tenant_id=int(tenant_id), namespace=NAMESPACE, turn_id=admitted.turn_id)
        return AdmittedInbound(tenant_id=int(tenant_id), turn_id=int(admitted.turn_id),
                               conversation_id=int(admitted.conversation_id),
                               provider_message_id=pmid, finished=terminal is not None)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger establishes nothing
        logger.warning("[COMMERCE_RUNTIME_RECOVERY] lookup failed tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        if strict:
            raise EvidenceUnavailable(type(exc).__name__) from exc
        return None


@dataclasses.dataclass(frozen=True)
class Recoverable:
    """Why a duplicate the platform would drop has to be let through."""

    tenant_id: int
    provider_message_id: str
    basis: str                     # unfinished_turn | accepted_not_admitted
    turn_id: Optional[int] = None


# The two bases. Both mean "a customer is owed something and this event is the
# only one that can reach it"; they differ in how far the work got.
BASIS_UNFINISHED_TURN = "unfinished_turn"
BASIS_ACCEPTED_NOT_ADMITTED = "accepted_not_admitted"


def accepted_but_unadmitted(*, tenant_id: int, phone_number_id: Any,
                            provider_message_id: Any, session: Any = None) -> bool:
    """Whether a durable acceptance for this identity is still pending.

    The acceptance record is written before the webhook answers, and it is
    resolved only against a terminal. One that is still pending therefore means
    exactly this: the provider was told we had the message, and nothing has
    finished it. That is work to recover even though no turn was ever admitted —
    the very case a "is there an unfinished turn" check answers ``no`` to.
    """
    pmid = str(provider_message_id or "").strip()
    channel = channel_connection_ref(phone_number_id)
    if not pmid or channel == f"{CHANNEL_REF_PREFIX}:":
        return False
    owns_session = session is None
    try:
        from core.commerce_runtime import handover  # noqa: PLC0415

        if owns_session:
            from database.session import SessionLocal  # noqa: PLC0415

            session = SessionLocal()
        record = handover.accepted_inbound(
            session, tenant_id=int(tenant_id), channel_connection_ref=channel,
            provider_message_id=pmid)
        return record is not None and record.pending
    except Exception as exc:  # noqa: BLE001 - an unreadable record establishes nothing
        logger.warning("[COMMERCE_RUNTIME_RECOVERY] acceptance lookup failed tenant=%s "
                       "error=%s", tenant_id, type(exc).__name__)
        return False
    finally:
        if owns_session and session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                logger.warning("[COMMERCE_RUNTIME_RECOVERY] session close failed")


def duplicate_carries_unfinished_work(
    *,
    phone_number_id: Any,
    customer_phone: Any,
    provider_message_id: Any,
    engine: Any = None,
) -> Optional[UnfinishedTurn]:
    """Whether a duplicate the platform is about to drop must be let through.

    Asked at the deduplication boundary, where the tenant has not necessarily
    been resolved yet. In ``pilot`` mode the candidate tenants are the pilot's
    own allowlist — which is also what makes this ownership-checked: a message
    can only ever be let through for a tenant and a recipient the pilot is
    explicitly configured for, and never for anything else on the platform.

    Once the merchant's own setting is the authority there is no operator
    recipient list, and in ``global`` no allowlist either, so the candidate is
    the single tenant whose verified connection owns the number. Ownership is
    then checked by the thing that actually establishes it: only a turn this
    runtime **admitted** for this exact inbound identity is ever returned, and
    an admission is not something an allowlist could have granted or withheld.
    """
    try:
        # Draining counts: handing back is exactly when unfinished work must
        # still be reachable. Only a fully disabled pilot stops recovering.
        if not pg.pilot_owns_open_work() or not pg.pilot_model():
            return None
        if pg.store_gate_decides_recipient():
            # Once the merchant's own setting decides who may be spoken to,
            # neither an operator recipient list nor an allowlist enumeration is
            # the right pre-filter, and in ``global`` there is no list to
            # enumerate at all: the connection row names the one tenant that can
            # own this number, which is narrower than any list would have been.
            #
            # The recipient is deliberately **not** re-checked against the store
            # gate here. What is being asked is whether this runtime already
            # admitted a turn for this exact inbound identity, and an admission
            # is stronger evidence of ownership than a setting read now — a
            # merchant who switches AI off between admission and the retry must
            # not thereby strand a turn the customer is already owed an answer
            # or an honest record for.
            tenants = _connection_owner_tenants(phone_number_id)
        else:
            tenants = _scoped_tenants()
            recipients = pg.recipient_allowlist()
            normalized = pg.normalize_recipient(customer_phone)
            if not recipients or not normalized or normalized not in recipients:
                return None
        if not tenants:
            return None
    except TooManyTenants as too_many:
        # Fail closed and loudly: with an oversized allowlist this cannot say
        # which tenants it did not look at.
        logger.error("[COMMERCE_RUNTIME_RECOVERY] refusing to inspect %s tenants; "
                     "the pilot allowlist is oversized", too_many.count)
        return None
    except Exception as exc:  # noqa: BLE001 - an undecidable configuration permits nothing
        logger.warning("[COMMERCE_RUNTIME_RECOVERY] configuration unreadable error=%s",
                       type(exc).__name__)
        return None

    for tenant_id in tenants:
        found = unfinished_turn_for(tenant_id=tenant_id, phone_number_id=phone_number_id,
                                    provider_message_id=provider_message_id, engine=engine)
        if found is not None:
            logger.warning(
                "[COMMERCE_RUNTIME_RECOVERY] duplicate inbound carries unfinished runtime work "
                "tenant=%s turn=%s provider_message_id=%s — allowing it through to be finished",
                found.tenant_id, found.turn_id, found.provider_message_id)
            return found
        # No turn — but the acknowledgement may still have promised one. An
        # accepted inbound that was never admitted is the case a turn lookup
        # cannot see, and it is precisely what a recovery replay carries.
        if accepted_but_unadmitted(tenant_id=tenant_id, phone_number_id=phone_number_id,
                                   provider_message_id=provider_message_id):
            logger.warning(
                "[COMMERCE_RUNTIME_RECOVERY] duplicate inbound carries an accepted inbound "
                "nobody admitted tenant=%s provider_message_id=%s — allowing it through",
                tenant_id, provider_message_id)
            return AdmittedInbound(tenant_id=int(tenant_id), turn_id=0,
                                   conversation_id=0,
                                   provider_message_id=str(provider_message_id or "").strip(),
                                   finished=False)
    return None


# ── Handover ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class HandoverState:
    """What one tenant still has in flight in the commerce runtime."""

    tenant_id: int
    open_turns: int                 # admitted, no terminal recorded
    reserved_undispatched: int      # a reply reserved that was never sent
    unresolved_attempts: int        # a send with no outcome receipt at all
    unknown_outcomes: int = 0       # a send whose recorded outcome is *unknown*
    deferred_pending: int = 0       # accepted inbound nobody has finished or disposed of

    @property
    def settled(self) -> bool:
        """Whether switching the pilot off would abandon nothing.

        An ``unknown`` outcome counts against it. The ledger records unknown
        precisely when nobody established what the provider did with the send —
        the request may still be in flight and may still deliver — so a recorded
        unknown is not proof of non-delivery and a handover that treats it as
        resolved is claiming something no evidence supports.
        """
        return (self.open_turns == 0 and self.reserved_undispatched == 0
                and self.unresolved_attempts == 0 and self.unknown_outcomes == 0
                and self.deferred_pending == 0)

    def as_log_fields(self) -> dict:
        return {"tenant_id": self.tenant_id, "open_turns": self.open_turns,
                "reserved_undispatched": self.reserved_undispatched,
                "unresolved_attempts": self.unresolved_attempts,
                "unknown_outcomes": self.unknown_outcomes,
                "deferred_pending": self.deferred_pending, "settled": self.settled}


_OPEN_TURNS_SQL = """
SELECT count(*) FROM commerce_runtime_turns t
LEFT JOIN commerce_runtime_turn_terminals x ON x.turn_id = t.id
WHERE t.tenant_id = :tenant AND t.namespace = :ns AND x.turn_id IS NULL
"""

_RESERVED_UNDISPATCHED_SQL = """
SELECT count(*) FROM commerce_runtime_delivery_sequences s
LEFT JOIN commerce_runtime_turn_terminals x ON x.turn_id = s.turn_id
WHERE s.tenant_id = :tenant AND s.namespace = :ns
  AND s.attempt_count = 0 AND x.turn_id IS NULL
"""

_UNRESOLVED_ATTEMPTS_SQL = """
SELECT count(*) FROM commerce_runtime_delivery_attempts a
WHERE a.tenant_id = :tenant AND a.namespace = :ns
  AND NOT EXISTS (
      SELECT 1 FROM commerce_runtime_delivery_receipts r
      WHERE r.attempt_id = a.id AND r.kind IN ('accepted', 'rejected', 'unknown')
  )
"""

# An attempt whose established outcome is 'unknown' and which no later
# acceptance resolved. The send may have reached the provider; nothing here
# knows, and nothing later will without an operator.
_UNKNOWN_OUTCOMES_SQL = """
SELECT count(*) FROM commerce_runtime_delivery_attempts a
WHERE a.tenant_id = :tenant AND a.namespace = :ns
  AND EXISTS (
      SELECT 1 FROM commerce_runtime_delivery_receipts r
      WHERE r.attempt_id = a.id AND r.kind = 'unknown'
  )
  AND NOT EXISTS (
      SELECT 1 FROM commerce_runtime_delivery_receipts r
      WHERE r.attempt_id = a.id AND r.kind = 'accepted'
  )
"""


# Accepted inbound nobody has finished or accounted for. The provider was told
# we had these messages, so an unresolved one is a customer owed an answer just
# as much as an admitted turn with no terminal is.
_DEFERRED_PENDING_SQL = """
SELECT count(*) FROM commerce_runtime_deferred_inbound d
WHERE d.tenant_id = :tenant AND d.namespace = :ns AND d.state = 'pending'
"""


def handover_state(*, tenant_ids: Any = None, engine: Any = None) -> tuple:
    """What each tenant still has in flight, for the handover procedure.

    Switching the pilot off is not a rollback while it still holds work: an
    admitted turn with no terminal is a customer owed an answer or an honest
    record, a reserved intent that was never dispatched is a reply nobody sent,
    and an attempt with no receipt is a send whose outcome nobody established.
    This counts all three so the decision to switch off is taken against
    evidence rather than by flipping a flag.

    It counts what this database can show and **nothing about other processes**:
    an admission that has been decided but not yet written is invisible here, so
    an empty count proves no *recorded* work outstanding, never that no replica
    is still admitting. The operator job is what turns the two into a verdict.

    Read-only. Raises if the database cannot be read, or if more tenants are
    configured than it will inspect, because an operator asking whether it is
    safe to stop must not be told "yes" by a failure or by a silent subset.
    """
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    if engine is None:
        from database.session import engine as default_engine  # noqa: PLC0415

        engine = default_engine
    tenants = _scoped_tenants(tenant_ids)
    with engine.connect() as conn:
        return handover_state_on(conn, tenant_ids=tenants)


def handover_state_on(conn: Any, *, tenant_ids: Any = None) -> tuple:
    """The same counts, taken on a connection the caller already holds.

    This is the form a settlement uses: counting inside the transaction that
    performs the transition is what stops work committing between the count and
    the decision it justified.
    """
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    states = []
    for tenant_id in _scoped_tenants(tenant_ids):
        params = {"tenant": tenant_id, "ns": NAMESPACE}
        states.append(HandoverState(
            tenant_id=tenant_id,
            open_turns=int(conn.execute(sa_text(_OPEN_TURNS_SQL), params).scalar() or 0),
            reserved_undispatched=int(
                conn.execute(sa_text(_RESERVED_UNDISPATCHED_SQL), params).scalar() or 0),
            unresolved_attempts=int(
                conn.execute(sa_text(_UNRESOLVED_ATTEMPTS_SQL), params).scalar() or 0),
            unknown_outcomes=int(
                conn.execute(sa_text(_UNKNOWN_OUTCOMES_SQL), params).scalar() or 0),
            deferred_pending=int(
                conn.execute(sa_text(_DEFERRED_PENDING_SQL), params).scalar() or 0),
        ))
    return tuple(states)


def handover_settled(states: Any) -> bool:
    """Whether every tenant in ``states`` has nothing left in flight."""
    states = tuple(states)
    return bool(states) and all(state.settled for state in states)


__all__ = ["EvidenceUnavailable", 
    "AdmittedInbound", "BASIS_ACCEPTED_NOT_ADMITTED", "BASIS_UNFINISHED_TURN",
    "CHANNEL_REF_PREFIX", "HandoverState", "MAX_TENANTS_CONSIDERED", "Recoverable",
    "accepted_but_unadmitted",
    "NAMESPACE", "TooManyTenants", "UnfinishedTurn", "admitted_turn_for",
    "channel_connection_ref", "duplicate_carries_unfinished_work",
    "handover_settled", "handover_state", "handover_state_on", "unfinished_turn_for",
]
