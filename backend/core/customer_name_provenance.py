"""
core/customer_name_provenance.py
────────────────────────────────
Durable persistence for customer-name authority decisions.

``core.customer_name_authority`` decides; this module remembers.

One row per ``(tenant_id, customer_id)`` in ``customer_name_provenance``.
The row has two halves:

  * CANONICAL fields (``canonical_name``, ``authority``, ``source``,
    ``evidence_kind``, ``evidence_ref``, ``merchant_locked``,
    ``previous_*``, ``canonical_updated_at``) describe the customer's
    CURRENT canonical identity. They change only when a decision
    actually applies a name — a rejected or blocked attempt never
    touches them, so a Salla-verified name keeps its VERIFIED_ECOMMERCE
    provenance no matter how many "الحمد لله" profile updates arrive.
  * ATTEMPT fields (``last_decision``, ``last_attempt_*``) and HINT
    fields (``profile_hint``, ``profile_hint_classification``) record
    what was tried most recently, for audit and merchant review.

The identity keys on ``Customer.extra_metadata`` are still written by the
resolver (many readers depend on them) but they are a **cache** of this
table, not the record of truth.

Transaction safety
==================
Provenance is an audit concern. A failed provenance write must never
break message ingestion or a store sync — and it must never roll back
the caller's unrelated pending work either. Every write here runs inside
a SAVEPOINT (``Session.begin_nested``); on failure only the savepoint is
rolled back and the outer transaction stays usable and committable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.customer_name_authority import (
    DECISION_HINT_ONLY,
    MERCHANT_OVERRIDE_LABEL,
    PERSON_NAME,
    NameAuthority,
    NameAuthorityDecision,
    authority_from_label,
)

logger = logging.getLogger("nahla.customer_name_provenance")

DECISION_MANUAL_CLEARED = "manual_cleared"
DECISION_MERCHANT_WRITE = "merchant_write"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _session_for(customer: Any):
    """Best-effort ORM session lookup for an attached Customer row."""
    try:
        from sqlalchemy.orm import object_session  # noqa: PLC0415

        return object_session(customer)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — detached stand-ins have no session
        return None


def _model():
    try:
        from models import CustomerNameProvenance  # noqa: PLC0415

        return CustomerNameProvenance
    except Exception:  # noqa: BLE001
        try:
            from database.models import CustomerNameProvenance  # noqa: PLC0415

            return CustomerNameProvenance
        except Exception:  # noqa: BLE001  # noqa: silent-ok — provenance table optional at import time
            return None


def _get_or_create_row(session: Any, model: Any, *, tenant_id: int, customer_id: int):
    row = (
        session.query(model)
        .filter(model.tenant_id == tenant_id, model.customer_id == customer_id)
        .one_or_none()
    )
    if row is None:
        row = model(tenant_id=tenant_id, customer_id=customer_id)
        session.add(row)
    return row


def _write_isolated(customer: Any, mutate) -> bool:
    """
    Run ``mutate(session, model, row)`` inside a SAVEPOINT.

    Returns True on success. On any failure only the savepoint is rolled
    back: the caller's session, its pending business mutations and its
    ability to commit are untouched. Never raises.
    """
    if customer is None:
        return False
    customer_id = getattr(customer, "id", None)
    tenant_id = getattr(customer, "tenant_id", None)
    if not customer_id or not tenant_id:
        return False
    session = _session_for(customer)
    if session is None:
        return False
    model = _model()
    if model is None:
        return False

    try:
        with session.begin_nested():
            row = _get_or_create_row(
                session, model, tenant_id=tenant_id, customer_id=customer_id,
            )
            mutate(session, model, row)
            session.flush()
        return True
    except Exception:  # noqa: BLE001 — savepoint already rolled back; outer txn intact
        logger.warning(
            "[NAME_PROVENANCE] write failed (savepoint rolled back) customer=%s tenant=%s",
            customer_id,
            tenant_id,
            exc_info=True,
        )
        return False


def record_name_decision(
    customer: Any,
    decision: NameAuthorityDecision,
    *,
    source: Optional[str] = None,
    evidence_ref: Optional[Dict[str, Any]] = None,
    profile_hint: str = "",
    merchant_locked: bool = False,
) -> bool:
    """
    Persist a resolver decision.

    Canonical fields change only for a canonical-changing decision;
    attempt/hint fields change on every call. Never raises.
    """
    if decision is None:
        return False
    now = _utcnow()

    def _mutate(_session: Any, _model: Any, row: Any) -> None:
        # ── Attempt half: always ──────────────────────────────────
        row.last_decision = decision.decision
        row.last_attempt_name = decision.incoming_name or None
        row.last_attempt_authority = decision.incoming_authority.label
        row.last_attempt_source = (source or "") or None
        row.last_attempt_classification = decision.classification or None
        row.last_attempt_reason = decision.reason or None
        row.last_attempt_at = now

        # ── Hint half: profile hints only ─────────────────────────
        if profile_hint:
            row.profile_hint = profile_hint
            row.profile_hint_classification = decision.classification or None
        elif decision.decision == DECISION_HINT_ONLY and decision.incoming_name:
            row.profile_hint = decision.incoming_name
            row.profile_hint_classification = decision.classification or None

        # ── Canonical half: only when a name was actually applied ─
        if decision.changed_canonical:
            row.previous_name = decision.previous_name or None
            row.previous_authority = decision.previous_authority.label
            row.canonical_name = decision.canonical_name or None
            row.authority = decision.effective_authority_label
            row.source = (source or "") or None
            row.evidence_kind = decision.evidence_kind or None
            row.evidence_ref = dict(evidence_ref or {}) or None
            row.merchant_locked = bool(merchant_locked)
            row.canonical_updated_at = now
            if decision.authority == NameAuthority.WHATSAPP_PROFILE:
                row.profile_hint = decision.canonical_name or None
                row.profile_hint_classification = PERSON_NAME

    return _write_isolated(customer, _mutate)


def record_merchant_name_write(
    customer: Any,
    new_name: Optional[str],
    *,
    source: str,
    previous_name: Optional[str] = None,
) -> bool:
    """Provenance for a merchant/manual write (lock path). Never raises."""
    cleaned = str(new_name or "").strip()
    now = _utcnow()

    def _mutate(_session: Any, _model: Any, row: Any) -> None:
        row.last_decision = DECISION_MERCHANT_WRITE if cleaned else DECISION_MANUAL_CLEARED
        row.last_attempt_name = cleaned or None
        row.last_attempt_authority = MERCHANT_OVERRIDE_LABEL
        row.last_attempt_source = source
        row.last_attempt_reason = "merchant_lock_write" if cleaned else "merchant_cleared"
        row.last_attempt_at = now
        row.previous_name = (previous_name if previous_name is not None else row.canonical_name) or None
        row.previous_authority = row.authority or NameAuthority.UNKNOWN.label
        row.canonical_name = cleaned or None
        row.authority = MERCHANT_OVERRIDE_LABEL if cleaned else NameAuthority.UNKNOWN.label
        row.source = source
        row.evidence_kind = None
        row.evidence_ref = None
        row.merchant_locked = bool(cleaned)
        row.canonical_updated_at = now

    return _write_isolated(customer, _mutate)


def read_name_authority(customer: Any) -> NameAuthority:
    """
    Current authority behind ``Customer.name``.

    Reads the durable row when available and falls back to the JSONB
    mirror, so this is correct both before and after any backfill.
    """
    if customer is None or not str(getattr(customer, "name", None) or "").strip():
        return NameAuthority.UNKNOWN

    session = _session_for(customer)
    customer_id = getattr(customer, "id", None)
    tenant_id = getattr(customer, "tenant_id", None)
    model = _model()
    if session is not None and model is not None and customer_id and tenant_id:
        row = _read_row_isolated(session, model, tenant_id=tenant_id, customer_id=customer_id)
        if row is not None and row.canonical_name:
            return authority_from_label(row.authority)

    return _authority_from_metadata(customer)


def _read_row_isolated(session: Any, model: Any, *, tenant_id: int, customer_id: int):
    """
    SELECT the provenance row inside a SAVEPOINT.

    On PostgreSQL a failed statement (e.g. ``UndefinedTable`` while
    migration 0107 has not been applied yet) aborts the *enclosing*
    transaction; catching the Python exception alone would leave the
    caller's transaction in ``InFailedSqlTransaction`` and its next
    business write would fail. Running the read inside
    ``Session.begin_nested()`` confines the failure to the savepoint —
    the same isolation the write path already uses — so the caller's
    session, pending mutations and ability to commit are untouched.

    Autoflush note: ``begin_nested()`` flushes pending session state
    *before* it emits SAVEPOINT (exactly as any ORM query's autoflush
    would). Those rows therefore land in the outer transaction and are
    never undone by a savepoint rollback. This is covered by
    ``backend/tests/test_customer_name_provenance_read_isolation_pg.py``.

    Returns the row, or ``None`` on any failure. Never raises.
    """
    try:
        with session.begin_nested():
            return (
                session.query(model)
                .filter(model.tenant_id == tenant_id, model.customer_id == customer_id)
                .one_or_none()
            )
    except Exception:  # noqa: BLE001 — savepoint already rolled back; outer txn intact
        logger.warning(
            "[NAME_PROVENANCE] read failed (savepoint rolled back) customer=%s tenant=%s",
            customer_id,
            tenant_id,
            exc_info=True,
        )
        return None


def _authority_from_metadata(customer: Any) -> NameAuthority:
    """Derive authority from the legacy JSONB identity keys."""
    from core.customer_name_authority import authority_for_source  # noqa: PLC0415

    meta = dict(getattr(customer, "extra_metadata", None) or {})
    explicit = meta.get("customer_name_authority")
    if explicit:
        return authority_from_label(explicit)
    source = str(
        meta.get("customer_name_source")
        or meta.get("name_source")
        or getattr(customer, "acquisition_channel", "")
        or ""
    ).strip()
    return authority_for_source(source)


def read_profile_hint(customer: Any) -> str:
    """Non-canonical WhatsApp profile hint retained for merchant review."""
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    return str(meta.get("proposed_name") or "").strip()
