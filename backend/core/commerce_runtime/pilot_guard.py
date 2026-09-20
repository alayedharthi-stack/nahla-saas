"""Who the commerce runtime may answer, decided once and fail-closed.

The runtime is off. It answers a turn only when **every** one of these is true,
and the first that is not decides the outcome:

1. the pilot switch is on;
2. the tenant is named in an explicit, non-empty tenant allowlist;
3. the recipient is named in an explicit, non-empty recipient allowlist;
4. the WhatsApp connection the message arrived on really belongs to that
   tenant, checked against the database rather than taken from the payload;
5. the legacy path has not already answered, and the turn is not one the
   existing AI gates told us to skip.

An empty allowlist permits nothing. A tenant is never enabled wholesale: the
recipient list is required as well, so the runtime reaches the owner's own test
conversations and nothing else. Nothing here reads a display name, a store
title or any other label — authorisation comes from configured ids and from the
verified connection, never from what something is called.

This mirrors the established `commerce_lifecycle.canary_guard` shape so the
pilot is configured the same way the platform already configures a canary.
No customer-facing text, no model call, no write.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any, FrozenSet, Optional, Tuple

logger = logging.getLogger("nahla.commerce_runtime.pilot_guard")

ENV_ENABLED = "COMMERCE_RUNTIME_PILOT_ENABLED"
ENV_DRAINING = "COMMERCE_RUNTIME_PILOT_DRAINING"
ENV_TENANT_ALLOWLIST = "COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST"
ENV_RECIPIENT_ALLOWLIST = "COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST"
ENV_MODEL = "COMMERCE_RUNTIME_PILOT_MODEL"
ENV_MAX_STEPS = "COMMERCE_RUNTIME_PILOT_MAX_STEPS"
ENV_MAX_TOOL_CALLS = "COMMERCE_RUNTIME_PILOT_MAX_TOOL_CALLS"
ENV_DEADLINE_SECONDS = "COMMERCE_RUNTIME_PILOT_DEADLINE_SECONDS"
ENV_PROVIDER_TIMEOUT_SECONDS = "COMMERCE_RUNTIME_PILOT_PROVIDER_TIMEOUT_SECONDS"
ENV_TOOL_TIMEOUT_SECONDS = "COMMERCE_RUNTIME_PILOT_TOOL_TIMEOUT_SECONDS"

# Closed reasons. Exactly one is reported per decision.
PERMITTED = "permitted"
PILOT_DISABLED = "pilot_disabled"
PILOT_DRAINING = "pilot_draining"
TENANT_NOT_ALLOWLISTED = "tenant_not_allowlisted"
RECIPIENT_MISSING = "recipient_missing"
RECIPIENT_UNNORMALIZABLE = "recipient_unnormalizable"
RECIPIENT_NOT_ALLOWLISTED = "recipient_not_allowlisted"
CONNECTION_NOT_VERIFIED = "connection_not_verified"
MODEL_NOT_CONFIGURED = "model_not_configured"
LEGACY_ALREADY_ANSWERED = "legacy_already_answered"
AI_GATE_SKIPPED = "ai_gate_skipped"
EMPTY_INBOUND = "empty_inbound"
GUARD_ERROR = "guard_error"

# Hard ceilings. A configured value may lower these, never raise them.
MAX_STEPS_CEILING = 6
MAX_TOOL_CALLS_CEILING = 8
DEADLINE_CEILING_SECONDS = 120.0
PROVIDER_TIMEOUT_CEILING_SECONDS = 60.0
TOOL_TIMEOUT_CEILING_SECONDS = 20.0

DEFAULT_MAX_STEPS = 4
DEFAULT_MAX_TOOL_CALLS = 6
DEFAULT_DEADLINE_SECONDS = 75.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 35.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0


@dataclasses.dataclass(frozen=True)
class PilotDecision:
    """One routing decision. ``permitted`` is true for exactly one runtime."""

    permitted: bool
    reason: str
    tenant_id: int
    recipient: Optional[str] = None
    connection_ref: Optional[str] = None   # the runtime's opaque channel reference
    connection_id: Optional[str] = None    # the verified WhatsAppConnection row id
    model: Optional[str] = None            # the explicitly configured pilot model

    @property
    def legacy_owns_turn(self) -> bool:
        return not self.permitted


def _flag(name: str, default: str = "false") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def pilot_enabled() -> bool:
    return _flag(ENV_ENABLED)


def pilot_draining() -> bool:
    """Whether *this process* is handing back: no new turns, its own work finished.

    Draining is the step a rollback goes through instead of switching the pilot
    off underneath work it has not finished. While it is set this process takes
    no new turn, and the turns this runtime already admitted stay reachable
    until they have a terminal.

    It does **not** mean the affected conversations go to the legacy path. A
    runtime that is handing over may still have a send in flight, and a second
    answer from a second runtime is the one outcome a handover must not
    produce; ``commerce_runtime_claims_inbound`` therefore withholds such an
    inbound from every owner and records it for the operator instead.

    This flag is per process. It cannot say the same word to every replica at
    the same moment, so it is not the mechanism a handover is performed with —
    ``core.commerce_runtime.handover``'s shared barrier is. It remains here so a
    single process can be taken out of rotation without releasing anything.
    """
    return _flag(ENV_ENABLED) and _flag(ENV_DRAINING)


def pilot_owns_open_work() -> bool:
    """Whether unfinished runtime work may still be finished by this deployment.

    True while the pilot is on, and while it is draining. It is only false once
    the pilot is fully off, which is why switching it off is a step the handover
    procedure takes *after* draining reports nothing left.
    """
    return _flag(ENV_ENABLED)


def _int_allowlist(name: str) -> FrozenSet[int]:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return frozenset()
    allowed = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError:
            continue
        if value > 0:
            allowed.add(value)
    return frozenset(allowed)


def _normalize(phone: Any) -> str:
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    raw = str(phone or "").strip()
    if not raw:
        return ""
    return str(normalize_phone(raw) or "").strip()


def _phone_allowlist(name: str) -> FrozenSet[str]:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return frozenset()
    allowed = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        normalized = _normalize(piece)
        if normalized:
            allowed.add(normalized)
    return frozenset(allowed)


def normalize_recipient(phone: Any) -> str:
    """The platform's own normalisation of a recipient, or ``""``."""
    return _normalize(phone)


def pilot_model() -> str:
    """The model this pilot is configured to use, or ``""``.

    There is deliberately no default. The loop is model-neutral and the
    repository's own fallback exists for the legacy path; inheriting it here
    would mean activating a pilot on a model nobody chose for it.
    """
    return str(os.environ.get(ENV_MODEL, "") or "").strip()


def tenant_allowlist() -> FrozenSet[int]:
    return _int_allowlist(ENV_TENANT_ALLOWLIST)


def recipient_allowlist() -> FrozenSet[str]:
    return _phone_allowlist(ENV_RECIPIENT_ALLOWLIST)


def _bounded_int(name: str, default: int, ceiling: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return min(default, ceiling)
    try:
        value = int(raw)
    except ValueError:
        return min(default, ceiling)
    return max(1, min(value, ceiling))


def _bounded_float(name: str, default: float, ceiling: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return min(default, ceiling)
    try:
        value = float(raw)
    except ValueError:
        return min(default, ceiling)
    if value <= 0:
        return min(default, ceiling)
    return min(value, ceiling)


def pilot_budget() -> Any:
    """The turn's finite attempt, timeout and wall-clock limits.

    Configuration may only make a limit smaller. A missing, unparsable or
    larger-than-ceiling value falls back to the bounded default, so a
    mis-set variable can never widen what one turn is allowed to spend.
    """
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415

    return ac.LoopBudget(
        max_steps=_bounded_int(ENV_MAX_STEPS, DEFAULT_MAX_STEPS, MAX_STEPS_CEILING),
        max_tool_calls=_bounded_int(ENV_MAX_TOOL_CALLS, DEFAULT_MAX_TOOL_CALLS, MAX_TOOL_CALLS_CEILING),
        tool_timeout_seconds=_bounded_float(ENV_TOOL_TIMEOUT_SECONDS, DEFAULT_TOOL_TIMEOUT_SECONDS,
                                            TOOL_TIMEOUT_CEILING_SECONDS),
        provider_timeout_seconds=_bounded_float(ENV_PROVIDER_TIMEOUT_SECONDS,
                                                DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                                                PROVIDER_TIMEOUT_CEILING_SECONDS),
        deadline_seconds=_bounded_float(ENV_DEADLINE_SECONDS, DEFAULT_DEADLINE_SECONDS,
                                        DEADLINE_CEILING_SECONDS),
    )


def _refused(reason: str, tenant_id: Any, recipient: Optional[str] = None) -> PilotDecision:
    try:
        tenant = int(tenant_id)
    except (TypeError, ValueError):
        tenant = 0
    return PilotDecision(permitted=False, reason=reason, tenant_id=tenant, recipient=recipient)


def evaluate_pilot_route(
    db: Any,
    *,
    tenant_id: Any,
    customer_phone: Any,
    phone_number_id: Any,
    inbound_text: Any,
    legacy_already_answered: bool = False,
    ai_gate_skipped: bool = False,
    finishing_open_work: bool = False,
) -> PilotDecision:
    """Decide, once, whether the commerce runtime owns this inbound turn.

    Returning ``permitted=False`` means the legacy path owns it exactly as it
    does today; returning ``permitted=True`` means the legacy path must not run
    for this turn. There is no third answer, so the two runtimes can never both
    reply to one inbound message.
    """
    try:
        if not pilot_enabled():
            return _refused(PILOT_DISABLED, tenant_id)
        tenants = tenant_allowlist()
        try:
            tenant = int(tenant_id)
        except (TypeError, ValueError):
            return _refused(TENANT_NOT_ALLOWLISTED, 0)
        if not tenants or tenant not in tenants:
            return _refused(TENANT_NOT_ALLOWLISTED, tenant)

        raw_phone = str(customer_phone or "").strip()
        if not raw_phone:
            return _refused(RECIPIENT_MISSING, tenant)
        recipient = _normalize(raw_phone)
        if not recipient:
            return _refused(RECIPIENT_UNNORMALIZABLE, tenant)
        recipients = recipient_allowlist()
        if not recipients or recipient not in recipients:
            return _refused(RECIPIENT_NOT_ALLOWLISTED, tenant, recipient)

        if pilot_draining() and not finishing_open_work:
            # Handing back: this process takes no new turn. Asked *after* the
            # allowlists on purpose — ``PILOT_DRAINING`` then names only traffic
            # this pilot would otherwise own, so the caller can tell an affected
            # conversation (which must not be released to another owner) from
            # traffic that was never the runtime's and keeps the behaviour it
            # has today. ``finishing_open_work`` is the caller stating it has
            # already established that this exact inbound message is a turn this
            # runtime admitted and never finished; draining finishes those,
            # which is what makes it a handover rather than an abandonment.
            return _refused(PILOT_DRAINING, tenant, recipient)

        model = pilot_model()
        if not model:
            return _refused(MODEL_NOT_CONFIGURED, tenant, recipient)

        verified = verified_connection(db, tenant_id=tenant, phone_number_id=phone_number_id)
        if verified is None:
            return _refused(CONNECTION_NOT_VERIFIED, tenant, recipient)
        connection_ref, connection_id = verified

        if legacy_already_answered:
            return _refused(LEGACY_ALREADY_ANSWERED, tenant, recipient)
        if ai_gate_skipped:
            return _refused(AI_GATE_SKIPPED, tenant, recipient)
        if not str(inbound_text or "").strip():
            return _refused(EMPTY_INBOUND, tenant, recipient)

        return PilotDecision(permitted=True, reason=PERMITTED, tenant_id=tenant,
                             recipient=recipient, connection_ref=connection_ref,
                             connection_id=connection_id, model=model)
    except Exception as exc:  # noqa: BLE001 - a guard that cannot decide refuses
        logger.warning("[COMMERCE_RUNTIME_PILOT] guard error tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return _refused(GUARD_ERROR, tenant_id)


def verified_connection(db: Any, *, tenant_id: int,
                        phone_number_id: Any) -> Optional[Tuple[str, str]]:
    """``(channel_reference, connection_row_id)``, only when the database agrees
    the connection is this tenant's.

    The inbound payload names a phone number id. That is a claim, not proof of
    ownership, so it is resolved against the tenant's own stored WhatsApp
    connection and refused when the two do not agree. Authorisation never comes
    from a display name or any other label on the row.
    """
    identifier = str(phone_number_id or "").strip()
    if not identifier or db is None:
        return None
    try:
        from database.models import WhatsAppConnection  # noqa: PLC0415

        connection = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == int(tenant_id))
            .filter(WhatsAppConnection.phone_number_id == identifier)
            .first()
        )
    except Exception as exc:  # noqa: BLE001 - an unverifiable connection is not a verified one
        logger.warning("[COMMERCE_RUNTIME_PILOT] connection lookup failed tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return None
    if connection is None:
        return None
    return f"wa:{identifier}", str(connection.id)


# What a scope lookup can conclude. The three are not interchangeable, and the
# difference between the last two is the whole point: "this is not ours" is a
# fact about the traffic, "I could not find out" is a fact about us.
SCOPE_RESOLVED = "resolved"        # an allowlisted tenant owns this connection
SCOPE_NOT_OURS = "not_ours"        # the platform knows: no allowlisted tenant owns it
SCOPE_AMBIGUOUS = "ambiguous"      # more than one allowlisted tenant claims the number
SCOPE_UNAVAILABLE = "unavailable"  # the lookup itself failed


@dataclasses.dataclass(frozen=True)
class ScopeLookup:
    """Who owns a phone number id, or why that could not be established."""

    status: str
    tenant_id: int = 0
    connection_ref: str = ""
    connection_id: str = ""
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == SCOPE_RESOLVED

    @property
    def decided(self) -> bool:
        """Whether this is an answer about the traffic rather than about us."""
        return self.status in {SCOPE_RESOLVED, SCOPE_NOT_OURS}


def resolve_pilot_scope(db: Any, *, phone_number_id: Any) -> ScopeLookup:
    """Which allowlisted tenant owns this connection — or why we cannot say.

    The reverse of :func:`verified_connection`, and deliberately narrower than a
    plain lookup: the connection row says which tenant owns the number, and the
    answer is given only when that tenant is one the pilot is configured for. A
    phone number id alone never selects a tenant for the runtime.

    A failed lookup is **not** "not ours". Answering that would let an
    unreachable database turn pilot-owned traffic into traffic nobody has to
    keep, and the caller that acknowledges an inbound needs to tell the two
    apart. Two allowlisted tenants claiming one number is likewise refused
    rather than resolved to whichever row came back first.
    """
    identifier = str(phone_number_id or "").strip()
    tenants = tenant_allowlist()
    if not identifier:
        return ScopeLookup(status=SCOPE_NOT_OURS, detail="no_phone_number_id")
    if not tenants:
        return ScopeLookup(status=SCOPE_NOT_OURS, detail="no_tenant_allowlist")
    if db is None:
        return ScopeLookup(status=SCOPE_UNAVAILABLE, detail="no_session")
    try:
        from database.models import WhatsAppConnection  # noqa: PLC0415

        connections = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.phone_number_id == identifier)
            .filter(WhatsAppConnection.tenant_id.in_(sorted(tenants)))
            .limit(2)
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - a failed lookup decides nothing
        logger.error("[COMMERCE_RUNTIME_PILOT] scope lookup failed phone_number_id=%s "
                     "error=%s — scope unavailable, not 'unrelated'",
                     identifier, type(exc).__name__)
        return ScopeLookup(status=SCOPE_UNAVAILABLE, detail=type(exc).__name__)
    if not connections:
        return ScopeLookup(status=SCOPE_NOT_OURS, detail="no_allowlisted_tenant_owns_it")
    if len(connections) > 1:
        logger.error("[COMMERCE_RUNTIME_PILOT] scope ambiguous phone_number_id=%s — more "
                     "than one allowlisted tenant claims it", identifier)
        return ScopeLookup(status=SCOPE_AMBIGUOUS, detail="more_than_one_allowlisted_tenant")
    connection = connections[0]
    return ScopeLookup(status=SCOPE_RESOLVED, tenant_id=int(connection.tenant_id),
                       connection_ref=f"wa:{identifier}", connection_id=str(connection.id))


def tenant_for_phone_number_id(db: Any, *, phone_number_id: Any
                               ) -> Optional[Tuple[int, str, str]]:
    """``(tenant_id, channel_reference, connection_row_id)``, or ``None``.

    The two-valued form, kept for callers that genuinely cannot act on the
    difference. Anything that acknowledges an inbound must use
    :func:`resolve_pilot_scope` instead: this one cannot tell "not ours" from
    "could not find out".
    """
    found = resolve_pilot_scope(db, phone_number_id=phone_number_id)
    if not found.resolved:
        return None
    return found.tenant_id, found.connection_ref, found.connection_id


__all__ = [
    "AI_GATE_SKIPPED", "CONNECTION_NOT_VERIFIED", "DEADLINE_CEILING_SECONDS", "EMPTY_INBOUND",
    "ENV_DRAINING", "PILOT_DRAINING", "pilot_draining", "pilot_owns_open_work",
    "ENV_ENABLED", "ENV_RECIPIENT_ALLOWLIST", "ENV_TENANT_ALLOWLIST", "GUARD_ERROR",
    "ENV_MODEL", "LEGACY_ALREADY_ANSWERED", "MAX_STEPS_CEILING", "MAX_TOOL_CALLS_CEILING",
    "MODEL_NOT_CONFIGURED", "PERMITTED", "normalize_recipient",
    "PILOT_DISABLED", "PilotDecision", "RECIPIENT_MISSING", "RECIPIENT_NOT_ALLOWLISTED",
    "RECIPIENT_UNNORMALIZABLE", "TENANT_NOT_ALLOWLISTED", "evaluate_pilot_route", "pilot_budget",
    "pilot_enabled", "pilot_model", "recipient_allowlist", "tenant_allowlist",
    "tenant_for_phone_number_id", "verified_connection",
    "SCOPE_AMBIGUOUS", "SCOPE_NOT_OURS", "SCOPE_RESOLVED", "SCOPE_UNAVAILABLE",
    "ScopeLookup", "resolve_pilot_scope",
]
