"""
core/customer_name_provenance.py
────────────────────────────────
Durable persistence for customer-name authority decisions.

``core.customer_name_authority`` decides; this module remembers.

One row per ``(tenant_id, customer_id)`` in ``customer_name_provenance``.
The identity keys on ``Customer.extra_metadata`` are still written (many
readers depend on them) but they are now a **cache** of this table, not
the record of truth.

Every function here is crash-safe. Provenance is an audit concern —
a failure to record it must never break message ingestion or a store
sync. Failures are logged and swallowed deliberately.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.customer_name_authority import (
    AMBIGUOUS,
    NameAuthority,
    NameAuthorityDecision,
)

logger = logging.getLogger("nahla.customer_name_provenance")


def _session_for(customer: Any):
    """Best-effort ORM session lookup for an attached Customer row."""
    try:
        from sqlalchemy.orm import object_session  # noqa: PLC0415

        return object_session(customer)
    except Exception:  # noqa: BLE001 — provenance must never break callers
        logger.debug("[NAME_PROVENANCE] no session for customer", exc_info=True)
        return None


def record_name_decision(
    customer: Any,
    decision: NameAuthorityDecision,
    *,
    source: Optional[str] = None,
    profile_hint: str = "",
    merchant_locked: bool = False,
) -> bool:
    """
    Persist a resolver decision to ``customer_name_provenance``.

    Returns True when a row was written or updated. Never raises.

    No-ops (returns False) when the customer is detached, has no id yet,
    or the provenance table is unavailable — the JSONB mirror written by
    the resolver still carries the decision in that case.
    """
    if customer is None or decision is None:
        return False

    customer_id = getattr(customer, "id", None)
    tenant_id = getattr(customer, "tenant_id", None)
    if not customer_id or not tenant_id:
        return False

    session = _session_for(customer)
    if session is None:
        return False

    try:
        from models import CustomerNameProvenance  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        try:
            from database.models import CustomerNameProvenance  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            logger.debug("[NAME_PROVENANCE] model unavailable", exc_info=True)
            return False

    try:
        row = (
            session.query(CustomerNameProvenance)
            .filter(
                CustomerNameProvenance.tenant_id == tenant_id,
                CustomerNameProvenance.customer_id == customer_id,
            )
            .one_or_none()
        )
        if row is None:
            row = CustomerNameProvenance(
                tenant_id=tenant_id,
                customer_id=customer_id,
            )
            session.add(row)

        row.canonical_name = decision.canonical_name or None
        row.authority = decision.authority.label
        row.source = (source or "") or None
        row.decision = decision.decision
        row.classification = decision.classification or None
        row.evidence_kind = decision.evidence_kind or None
        if profile_hint:
            row.profile_hint = profile_hint
        row.merchant_locked = bool(merchant_locked)
        row.previous_name = decision.previous_name or None
        row.previous_authority = decision.previous_authority.label
        row.reason = decision.reason or None
        session.flush()
        return True
    except Exception:  # noqa: BLE001 — audit write must never break ingestion
        logger.warning(
            "[NAME_PROVENANCE] write failed customer=%s tenant=%s",
            customer_id,
            tenant_id,
            exc_info=True,
        )
        try:
            session.rollback()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — rollback best effort
            pass
        return False


def read_name_authority(customer: Any) -> NameAuthority:
    """
    Current authority behind ``Customer.name``.

    Reads the durable row when available and falls back to the JSONB
    mirror, so this is correct both before and after the backfill.
    """
    if customer is None:
        return NameAuthority.UNKNOWN

    if not str(getattr(customer, "name", None) or "").strip():
        return NameAuthority.UNKNOWN

    session = _session_for(customer)
    customer_id = getattr(customer, "id", None)
    tenant_id = getattr(customer, "tenant_id", None)

    if session is not None and customer_id and tenant_id:
        try:
            from models import CustomerNameProvenance  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            CustomerNameProvenance = None  # type: ignore[assignment]
            try:
                from database.models import (  # noqa: PLC0415
                    CustomerNameProvenance,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — JSONB fallback below
                pass

        if CustomerNameProvenance is not None:
            try:
                row = (
                    session.query(CustomerNameProvenance)
                    .filter(
                        CustomerNameProvenance.tenant_id == tenant_id,
                        CustomerNameProvenance.customer_id == customer_id,
                    )
                    .one_or_none()
                )
                if row is not None and row.canonical_name:
                    from core.customer_name_authority import (  # noqa: PLC0415
                        authority_from_label,
                    )

                    return authority_from_label(row.authority)
            except Exception:  # noqa: BLE001  # noqa: silent-ok — best-effort read; JSONB mirror below is the fallback
                logger.debug("[NAME_PROVENANCE] read failed", exc_info=True)

    return _authority_from_metadata(customer)


def _authority_from_metadata(customer: Any) -> NameAuthority:
    """Derive authority from the legacy JSONB identity keys."""
    from core.customer_name_authority import (  # noqa: PLC0415
        authority_for_source,
        authority_from_label,
    )

    meta = dict(getattr(customer, "extra_metadata", None) or {})

    explicit = meta.get("customer_name_authority")
    if explicit:
        resolved = authority_from_label(explicit)
        if resolved != NameAuthority.UNKNOWN:
            return resolved

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


def profile_hint_is_displayable(customer: Any) -> bool:
    """
    True only when the retained profile hint was classified
    ``PERSON_NAME``.

    ``AMBIGUOUS`` hints are kept for merchant review but must never be
    shown as the customer's name or used operationally — this is the
    gate that stopped "الحمد لله" reaching invoices and shipping labels.
    """
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    hint = str(meta.get("proposed_name") or "").strip()
    if not hint:
        return False

    stored = str(meta.get("proposed_name_classification") or "").strip().upper()
    if stored:
        return stored != AMBIGUOUS and stored != "NOT_PERSON_NAME"

    # Legacy rows written before the classifier existed: classify now
    # rather than trusting an unclassified hint.
    from core.customer_name_authority import (  # noqa: PLC0415
        classify_whatsapp_profile_name,
    )

    return classify_whatsapp_profile_name(hint).is_person_name
