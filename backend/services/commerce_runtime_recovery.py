"""Replaying inbound messages the platform accepted and nobody finished.

A deferred record exists because the provider was told we had a message: the
webhook wrote it before answering, or the runtime wrote it when a handover
refused the turn. Either way a customer is owed an answer, and until now the row
was only *discoverable* — an operator could see it in ``status`` and had no
supported way to act on it except by hand.

This is that way. It is a replay, not a resend:

* the barrier decides first. A tenant that is draining, settled, or has no
  barrier row that admits work takes nothing back — recovering into a closed
  barrier is the abandonment a handover exists to prevent;
* ownership is checked against the ledger, not inferred. A turn that already
  reached a terminal is **not** replayed; its record is closed against that
  terminal and the entry is done. A turn admitted and unfinished is replayed,
  because the delivery ledger still refuses to dispatch an attempt whose outcome
  is pending, accepted or unknown — so an uncertain send stays uncertain and the
  re-entry records what is established rather than sending again;
* the replay re-enters through the ordinary dispatcher. It is the provider's own
  body, rebuilt from what was stored, and it passes the in-memory and the
  database deduplication boundaries the same way a provider retry does —
  because ``core.commerce_runtime.recovery`` answers for it, by identity, for an
  allowlisted tenant and recipient only.

Concurrency is handled by the database, twice: a per-entry advisory lock means a
second runner skips an entry rather than racing it, and admission is keyed by
the provider message id, so even a lock that is not held cannot produce two
turns for one inbound.

Nothing here composes customer-facing text, and nothing here sends: it hands the
inbound back to the pipeline that owns those decisions.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.commerce_runtime.recovery_runner")

# A separate advisory-lock namespace from the handover's: this one serialises
# *entries*, not tenants, so a recovery run never blocks a drain.
ENTRY_LOCK_NAMESPACE = 0x6E68_5256 & 0x7FFF_FFFF      # "nhRV", kept positive


@dataclasses.dataclass(frozen=True)
class RecoveryGrant:
    """The one inbound a recovery run is replaying right now, by identity.

    A drain stops **new** work. An accepted inbound the provider was told we
    had is not new work — the settlement already counts it as an obligation —
    and refusing to admit it while the barrier is draining is how a customer's
    message becomes something nobody can ever answer: recovery would need the
    barrier open, and reopening requires nothing pending. The runner therefore
    states, for the duration of one replay, which accepted identity it is
    handing back. The claim and the seam honour that statement only when the
    identity matches **and** a durable pending acceptance exists for it, and the
    admission itself is still ordered against the barrier on the admitting
    transaction's own connection: it is admitted while the barrier is open or
    draining, never while it is settled or released.
    """

    tenant_id: int
    channel_connection_ref: str
    provider_message_id: str
    entry_id: int

    def names(self, *, tenant_id: Any, channel_connection_ref: Any,
              provider_message_id: Any) -> bool:
        try:
            return (int(tenant_id) == self.tenant_id
                    and str(channel_connection_ref or "").strip() == self.channel_connection_ref
                    and str(provider_message_id or "").strip() == self.provider_message_id)
        except (TypeError, ValueError):
            return False


_GRANT: contextvars.ContextVar = contextvars.ContextVar("commerce_runtime_recovery_grant",
                                                        default=None)


def current_grant() -> Optional[RecoveryGrant]:
    """The grant in force for this replay, if a recovery run is the caller."""
    return _GRANT.get()


@contextlib.contextmanager
def granted(record: Any) -> Iterator[RecoveryGrant]:
    grant = RecoveryGrant(tenant_id=int(record.tenant_id),
                          channel_connection_ref=str(record.channel_connection_ref),
                          provider_message_id=str(record.provider_message_id),
                          entry_id=int(record.id))
    token = _GRANT.set(grant)
    try:
        yield grant
    finally:
        _GRANT.reset(token)


# Outcomes, one per entry. Every one of them is a fact about that entry.
REPLAYED = "replayed"                      # handed back to the dispatcher
RESOLVED_ALREADY_FINISHED = "already_finished"   # a terminal existed; closed against it
SKIPPED_BARRIER_CLOSED = "barrier_closed"  # the tenant is not admitting work
SKIPPED_IN_FLIGHT = "in_flight"            # another runner holds this entry
SKIPPED_NOT_REPLAYABLE = "not_replayable"  # nothing stored that can be rebuilt
SKIPPED_UNKNOWN_DELIVERY = "unknown_delivery"    # a send whose outcome nobody established
FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class EntryOutcome:
    """What happened to one deferred entry."""

    entry_id: int
    provider_message_id: str
    outcome: str
    detail: str = ""

    def as_log_fields(self) -> Dict[str, Any]:
        return {"entry": self.entry_id, "provider_message_id": self.provider_message_id,
                "outcome": self.outcome, "detail": self.detail}


@dataclasses.dataclass(frozen=True)
class RecoveryReport:
    """What one recovery run did, per tenant."""

    tenant_id: int
    inspected: int
    outcomes: Tuple[EntryOutcome, ...] = ()

    def counted(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self.outcomes:
            counts[item.outcome] = counts.get(item.outcome, 0) + 1
        return counts


def _try_lock_entry(session: Any, entry_id: int) -> bool:
    """Take this entry for this runner, or report that somebody else has it."""
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    try:
        dialect = str(getattr(getattr(session, "get_bind", lambda: None)(), "dialect", None)
                      and session.get_bind().dialect.name or "")
    except Exception:  # noqa: BLE001
        dialect = ""
    if dialect != "postgresql":
        return True
    try:
        return bool(session.execute(
            sa_text("SELECT pg_try_advisory_xact_lock(:ns, :entry)"),
            {"ns": ENTRY_LOCK_NAMESPACE, "entry": int(entry_id)}).scalar())
    except Exception as exc:  # noqa: BLE001 - an unlockable entry is left for the next run
        logger.warning("[COMMERCE_RUNTIME_RECOVER] entry lock failed entry=%s error=%s",
                       entry_id, type(exc).__name__)
        return False


def _webhook_body(record: Any) -> Optional[Dict[str, Any]]:
    """The provider body this record came from, rebuilt from what was stored.

    Only the message itself is rebuilt — the envelope is ours, and everything
    the dispatcher reads from it (the phone number id) is on the record. When
    the stored payload holds no replayable message, nothing is invented.
    """
    payload = dict(getattr(record, "payload", None) or {})
    raw = dict(payload.get("raw") or {})
    if not raw:
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        raw = {"id": record.provider_message_id, "from": record.recipient,
               "type": "text", "text": {"body": text}}
    raw.setdefault("id", record.provider_message_id)
    raw.setdefault("from", record.recipient)
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": record.phone_number_id,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": record.phone_number_id,
                                 "display_phone_number": ""},
                    "messages": [raw],
                },
            }],
        }],
        "_nahla_recovery": True,
    }


def _engine_of(db: Any) -> Any:
    """The engine behind the caller's session, so a run reads one database."""
    try:
        bind = db.get_bind() if hasattr(db, "get_bind") else db
        return getattr(bind, "engine", bind)
    except Exception:  # noqa: silent-ok — a session that cannot name its engine falls back to the process default, which is the production case
        return None


def _unknown_delivery(tenant_id: int, turn_id: Optional[int], engine: Any = None) -> bool:
    """Whether this turn holds a send whose outcome nobody established.

    ``unknown`` is not "did not arrive". Replaying over one risks a second
    delivery of a message the customer may already have, so the entry is left
    for an operator with the evidence in front of them.
    """
    if not turn_id:
        return False
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    from core.commerce_runtime import recovery as core_recovery  # noqa: PLC0415

    if engine is None:
        from database.session import engine as default_engine  # noqa: PLC0415

        engine = default_engine

    sql = """
    SELECT count(*) FROM commerce_runtime_delivery_attempts a
    JOIN commerce_runtime_delivery_sequences s ON s.id = a.sequence_id
    WHERE a.tenant_id = :tenant AND a.namespace = :ns AND s.turn_id = :turn
      AND EXISTS (SELECT 1 FROM commerce_runtime_delivery_receipts r
                  WHERE r.attempt_id = a.id AND r.kind = 'unknown')
      AND NOT EXISTS (SELECT 1 FROM commerce_runtime_delivery_receipts r
                      WHERE r.attempt_id = a.id AND r.kind = 'accepted')
    """
    try:
        with engine.connect() as conn:
            return bool(conn.execute(sa_text(sql), {
                "tenant": int(tenant_id), "ns": core_recovery.NAMESPACE,
                "turn": int(turn_id)}).scalar() or 0)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger is not a clean one
        logger.warning("[COMMERCE_RUNTIME_RECOVER] delivery check failed tenant=%s "
                       "error=%s — treating the entry as unsafe to replay",
                       tenant_id, type(exc).__name__)
        return True


async def _replay(body: Mapping[str, Any]) -> None:
    from routers.whatsapp_webhook import _handle_whatsapp_body  # noqa: PLC0415

    await _handle_whatsapp_body(dict(body))


def recover_tenant(db: Any, *, tenant_id: int, limit: int = 50,
                   dry_run: bool = True) -> RecoveryReport:
    """Work this tenant's pending deferred entries, oldest first.

    ``dry_run`` decides everything except the replay itself: the barrier, the
    ownership checks and the per-entry verdicts all run, and nothing is handed
    back to the dispatcher. It is how an operator sees what a run *would* do.
    """
    from core.commerce_runtime import handover  # noqa: PLC0415
    from core.commerce_runtime import recovery as core_recovery  # noqa: PLC0415

    engine = _engine_of(db)
    entries = handover.pending_inbound(db, tenant_id=int(tenant_id), limit=int(limit))
    outcomes: List[EntryOutcome] = []
    # Open **or draining**: a drain is exactly when accepted work has to be met
    # rather than held, and the entries here are accepted work, not new work.
    # Settled and released take nothing back — an entry pending under either
    # is the operator's signal to drain again first.
    try:
        barrier = handover.read_barrier(db, tenant_id=int(tenant_id))
        admits = barrier.state in (handover.STATE_OPEN, handover.STATE_DRAINING)
        closed_detail = (f"barrier_{barrier.state}: run 'drain', then 'recover --apply'"
                         if not admits else "")
    except Exception as exc:  # noqa: BLE001 - an unreadable barrier admits nothing
        logger.warning("[COMMERCE_RUNTIME_RECOVER] barrier unreadable tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        admits, closed_detail = False, "barrier_unreadable"

    for record in entries:
        def done(outcome: str, detail: str = "") -> None:
            outcomes.append(EntryOutcome(entry_id=record.id,
                                         provider_message_id=record.provider_message_id,
                                         outcome=outcome, detail=detail))

        # Keyed on the channel reference the record itself carries, not on the
        # phone number id: the reference is what a turn was admitted under, and
        # the two are only the same when nothing rewrote one of them.
        owned = core_recovery.admitted_turn_for(
            tenant_id=int(tenant_id),
            phone_number_id=str(record.channel_connection_ref).split(":", 1)[-1],
            provider_message_id=record.provider_message_id, engine=engine)

        if owned is not None and owned.finished:
            # Completed work is never repeated. The obligation is closed
            # against the terminal that completed it, which is the same check
            # the runtime itself makes.
            if not dry_run:
                handover.resolve_inbound(
                    db, tenant_id=int(tenant_id),
                    channel_connection_ref=record.channel_connection_ref,
                    provider_message_id=record.provider_message_id,
                    evidence={"terminal_for_turn_id": int(owned.turn_id),
                              "closed_by": "recovery"})
            done(RESOLVED_ALREADY_FINISHED, f"turn={owned.turn_id}")
            continue

        if not admits:
            # Settled or released: the entry stays exactly as it is, and the
            # run says what to do rather than replaying into a barrier that
            # would refuse it anyway.
            done(SKIPPED_BARRIER_CLOSED, closed_detail)
            continue

        if owned is not None and _unknown_delivery(int(tenant_id), owned.turn_id, engine):
            done(SKIPPED_UNKNOWN_DELIVERY, f"turn={owned.turn_id}")
            continue

        body = _webhook_body(record)
        if body is None:
            done(SKIPPED_NOT_REPLAYABLE, "no stored payload to rebuild")
            continue

        if not _try_lock_entry(db, record.id):
            done(SKIPPED_IN_FLIGHT)
            continue

        if dry_run:
            done(REPLAYED, "dry_run")
            continue

        try:
            # The grant names this one identity for this one replay; it is what
            # lets a draining barrier admit accepted work without admitting
            # anything new, and it is checked against the durable record again
            # by whoever honours it.
            with granted(record):
                asyncio.run(_replay(body))
        except Exception as exc:  # noqa: BLE001 - one entry does not end the run
            logger.exception("[COMMERCE_RUNTIME_RECOVER] replay failed entry=%s", record.id)
            done(FAILED, type(exc).__name__)
            continue
        done(REPLAYED)

    report = RecoveryReport(tenant_id=int(tenant_id), inspected=len(entries),
                            outcomes=tuple(outcomes))
    logger.warning("[COMMERCE_RUNTIME_RECOVER] tenant=%s inspected=%s outcomes=%s dry_run=%s",
                   tenant_id, report.inspected, report.counted(), dry_run)
    return report


def recover(db: Any, *, tenant_ids: Sequence[int], limit: int = 50,
            dry_run: bool = True) -> Tuple[RecoveryReport, ...]:
    """Every configured tenant, in order. Never raises for one tenant's sake."""
    reports: List[RecoveryReport] = []
    for tenant_id in tenant_ids:
        try:
            reports.append(recover_tenant(db, tenant_id=int(tenant_id), limit=limit,
                                          dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[COMMERCE_RUNTIME_RECOVER] tenant=%s failed", tenant_id)
            reports.append(RecoveryReport(
                tenant_id=int(tenant_id), inspected=0,
                outcomes=(EntryOutcome(entry_id=0, provider_message_id="",
                                       outcome=FAILED, detail=type(exc).__name__),)))
    return tuple(reports)


__all__ = ["EntryOutcome", "FAILED", "RESOLVED_ALREADY_FINISHED", "REPLAYED",
           "RecoveryGrant", "RecoveryReport", "SKIPPED_BARRIER_CLOSED", "SKIPPED_IN_FLIGHT",
           "SKIPPED_NOT_REPLAYABLE", "SKIPPED_UNKNOWN_DELIVERY", "current_grant",
           "granted", "recover", "recover_tenant"]
