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
* **Nothing outside the pilot is affected.** While the pilot is off, or for a
  tenant or recipient it is not configured for, the answer is ``None`` without
  a single query.
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
# owner-only; a list longer than this is a misconfiguration, not a workload.
MAX_TENANTS_CONSIDERED = 8


@dataclasses.dataclass(frozen=True)
class UnfinishedTurn:
    """An admitted commerce-runtime turn with no terminal recorded."""

    tenant_id: int
    turn_id: int
    conversation_id: int          # the runtime conversation, not the application one
    provider_message_id: str


def channel_connection_ref(phone_number_id: Any) -> str:
    """The channel reference the runtime admits turns under."""
    return f"{CHANNEL_REF_PREFIX}:{str(phone_number_id or '').strip()}"


def unfinished_turn_for(
    *,
    tenant_id: int,
    phone_number_id: Any,
    provider_message_id: Any,
    engine: Any = None,
) -> Optional[UnfinishedTurn]:
    """The runtime's unfinished turn for this inbound identity, or ``None``.

    Read-only and never raises: anything it cannot establish is ``None``, which
    keeps every caller on its existing behaviour.
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
        if not runtime_schema_available(engine):
            return None
        foundation = LedgerRepository(engine).foundation
        admitted = foundation.find_admitted_turn(
            tenant_id=int(tenant_id), namespace=NAMESPACE,
            channel_connection_ref=channel, provider_message_id=pmid)
        if admitted is None:
            return None
        terminal = foundation.get_terminal(
            tenant_id=int(tenant_id), namespace=NAMESPACE, turn_id=admitted.turn_id)
        if terminal is not None:
            # Finished. Its duplicates are duplicates, exactly as before.
            return None
        return UnfinishedTurn(tenant_id=int(tenant_id), turn_id=int(admitted.turn_id),
                              conversation_id=int(admitted.conversation_id),
                              provider_message_id=pmid)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger establishes nothing
        logger.warning("[COMMERCE_RUNTIME_RECOVERY] lookup failed tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return None


def duplicate_carries_unfinished_work(
    *,
    phone_number_id: Any,
    customer_phone: Any,
    provider_message_id: Any,
    engine: Any = None,
) -> Optional[UnfinishedTurn]:
    """Whether a duplicate the platform is about to drop must be let through.

    Asked at the deduplication boundary, where the tenant has not necessarily
    been resolved yet. The candidate tenants are therefore the pilot's own
    allowlist — which is also what makes this ownership-checked: a message can
    only ever be let through for a tenant and a recipient the pilot is
    explicitly configured for, and never for anything else on the platform.
    """
    try:
        # Draining counts: handing back is exactly when unfinished work must
        # still be reachable. Only a fully disabled pilot stops recovering.
        if not pg.pilot_owns_open_work() or not pg.pilot_model():
            return None
        tenants = sorted(pg.tenant_allowlist())[:MAX_TENANTS_CONSIDERED]
        if not tenants:
            return None
        recipients = pg.recipient_allowlist()
        normalized = pg.normalize_recipient(customer_phone)
        if not recipients or not normalized or normalized not in recipients:
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
    return None


# ── Handover ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class HandoverState:
    """What one tenant still has in flight in the commerce runtime."""

    tenant_id: int
    open_turns: int                 # admitted, no terminal recorded
    reserved_undispatched: int      # a reply reserved that was never sent
    unresolved_attempts: int        # a send whose outcome is not established

    @property
    def settled(self) -> bool:
        """Whether switching the pilot off would abandon nothing."""
        return (self.open_turns == 0 and self.reserved_undispatched == 0
                and self.unresolved_attempts == 0)

    def as_log_fields(self) -> dict:
        return {"tenant_id": self.tenant_id, "open_turns": self.open_turns,
                "reserved_undispatched": self.reserved_undispatched,
                "unresolved_attempts": self.unresolved_attempts, "settled": self.settled}


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


def handover_state(*, tenant_ids: Any = None, engine: Any = None) -> tuple:
    """What each tenant still has in flight, for the handover procedure.

    Switching the pilot off is not a rollback while it still holds work: an
    admitted turn with no terminal is a customer owed an answer or an honest
    record, a reserved intent that was never dispatched is a reply nobody sent,
    and an attempt with no receipt is a send whose outcome nobody established.
    This counts all three so the decision to switch off is taken against
    evidence rather than by flipping a flag.

    Read-only. Raises only if the database itself cannot be read, because an
    operator asking whether it is safe to stop must not be told "yes" by a
    failure.
    """
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    if engine is None:
        from database.session import engine as default_engine  # noqa: PLC0415

        engine = default_engine
    tenants = sorted(int(t) for t in (tenant_ids if tenant_ids is not None
                                      else pg.tenant_allowlist()))
    states = []
    with engine.connect() as conn:
        for tenant_id in tenants[:MAX_TENANTS_CONSIDERED]:
            params = {"tenant": tenant_id, "ns": NAMESPACE}
            states.append(HandoverState(
                tenant_id=tenant_id,
                open_turns=int(conn.execute(sa_text(_OPEN_TURNS_SQL), params).scalar() or 0),
                reserved_undispatched=int(
                    conn.execute(sa_text(_RESERVED_UNDISPATCHED_SQL), params).scalar() or 0),
                unresolved_attempts=int(
                    conn.execute(sa_text(_UNRESOLVED_ATTEMPTS_SQL), params).scalar() or 0),
            ))
    return tuple(states)


def handover_settled(states: Any) -> bool:
    """Whether every tenant in ``states`` has nothing left in flight."""
    states = tuple(states)
    return bool(states) and all(state.settled for state in states)


__all__ = [
    "CHANNEL_REF_PREFIX", "HandoverState", "MAX_TENANTS_CONSIDERED", "NAMESPACE",
    "UnfinishedTurn", "channel_connection_ref", "duplicate_carries_unfinished_work",
    "handover_settled", "handover_state", "unfinished_turn_for",
]
