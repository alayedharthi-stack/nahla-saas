"""
services/manual_segments.py
────────────────────────────
Add / remove merchant-curated segment tags on a Customer, plus the
two boolean flags that the campaign snapshot consults:

  * ``marketing_opt_out_manual``    → exclude from manual marketing
                                      campaigns (transactional / 24h
                                      service messages still flow).
  * ``is_campaign_test_recipient``  → "test list" — small group the
                                      merchant can target to dry-run a
                                      campaign before the real launch.
                                      Stored as a flag on
                                      ``Customer.extra_metadata`` so
                                      no merchant-visible tag exists
                                      and it never appears in the
                                      Nahla segment registry.

Why this lives in its own module
────────────────────────────────
The campaign dispatcher, the customers router, and the campaign
wizard all need the same primitives: "is this customer opted-out?",
"which manual segments does this customer have?", "list every
customer with manual segment X". Centralising them here keeps the
SQL identical across all three consumers and gives us one place to
test the rules.

Tenant safety
─────────────
Every write path takes ``tenant_id`` as a required argument and
asserts the target Customer belongs to that tenant before inserting.
There is NO code path here that resolves a customer by ID alone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Customer, CustomerSegmentManual
from services.nahla_segments import SEGMENTS, all_segment_keys, get_segment

logger = logging.getLogger(__name__)


# ── Validation ──────────────────────────────────────────────────────────


class UnknownSegmentError(ValueError):
    """Raised when a caller tries to add a segment_key that isn't part
    of the canonical Nahla registry. The router catches this and
    returns HTTP 422 with the list of accepted keys.
    """


def assert_known_segment(segment_key: str) -> str:
    """Normalise + validate ``segment_key`` against
    ``services.nahla_segments``. Returns the canonical (lowercased)
    key. Raises :class:`UnknownSegmentError` otherwise.
    """
    key = (segment_key or "").strip().lower()
    if not key or get_segment(key) is None:
        raise UnknownSegmentError(
            f"Unknown segment key: {segment_key!r}. "
            f"Allowed keys: {', '.join(all_segment_keys())}"
        )
    return key


# ── Manual segment CRUD ─────────────────────────────────────────────────


def add_manual_segment(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    segment_key: str,
    created_by: Optional[int] = None,
    commit: bool = True,
) -> CustomerSegmentManual:
    """Idempotently pin ``customer`` to ``segment_key``.

    Re-tagging an existing pair is a no-op — we return the existing
    row instead of raising. This is what lets the front-end safely
    retry on a flaky connection.

    Validates:
      * ``segment_key`` exists in the official Nahla registry.
      * The customer belongs to ``tenant_id`` (cross-tenant guard).
    """
    key = assert_known_segment(segment_key)

    cust = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        .first()
    )
    if cust is None:
        # We treat "wrong tenant" identically to "not found" so the
        # router never leaks the existence of customers in other
        # tenants. The caller surfaces a 404.
        raise LookupError(f"customer {customer_id} not found in tenant {tenant_id}")

    existing = (
        db.query(CustomerSegmentManual)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id == customer_id,
            CustomerSegmentManual.segment_key == key,
        )
        .first()
    )
    if existing is not None:
        return existing

    row = CustomerSegmentManual(
        tenant_id=tenant_id,
        customer_id=customer_id,
        segment_key=key,
        source="manual",
        created_by=created_by,
    )
    db.add(row)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        # Race window: another request just inserted the same pair.
        # Roll back and return the row that's now present — same UX
        # as the non-racy idempotent path above.
        db.rollback()
        return (
            db.query(CustomerSegmentManual)
            .filter(
                CustomerSegmentManual.tenant_id == tenant_id,
                CustomerSegmentManual.customer_id == customer_id,
                CustomerSegmentManual.segment_key == key,
            )
            .one()
        )
    if commit:
        db.refresh(row)
    return row


def remove_manual_segment(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    segment_key: str,
    commit: bool = True,
) -> bool:
    """Drop the (tenant, customer, segment) row. Returns ``True`` when
    something was removed, ``False`` when the pin was not present
    (still a 200 OK at the API layer — caller decides)."""
    key = (segment_key or "").strip().lower()
    if not key:
        return False
    deleted = (
        db.query(CustomerSegmentManual)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id == customer_id,
            CustomerSegmentManual.segment_key == key,
        )
        .delete(synchronize_session=False)
    )
    if commit:
        db.commit()
    return bool(deleted)


def list_manual_segments_for_customer(
    db: Session, tenant_id: int, customer_id: int,
) -> List[str]:
    """Return the canonical segment keys this customer is manually
    pinned to (sorted). Used by the drawer + by the campaign snapshot
    when applying include/exclude rules."""
    rows = (
        db.query(CustomerSegmentManual.segment_key)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id == customer_id,
        )
        .all()
    )
    return sorted({r[0] for r in rows})


def list_manual_segments_bulk(
    db: Session, tenant_id: int, customer_ids: List[int],
) -> Dict[int, List[str]]:
    """Bulk version — returns ``{customer_id: [segment_key, ...]}``.

    Used by the customers list endpoint so we don't fan out an N+1
    query per row when rendering a 200-row table.
    """
    if not customer_ids:
        return {}
    rows = (
        db.query(CustomerSegmentManual.customer_id, CustomerSegmentManual.segment_key)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id.in_(customer_ids),
        )
        .all()
    )
    out: Dict[int, Set[str]] = {}
    for cid, key in rows:
        out.setdefault(cid, set()).add(key)
    return {cid: sorted(keys) for cid, keys in out.items()}


def customer_ids_with_manual_segment(
    db: Session, tenant_id: int, segment_key: str,
) -> List[int]:
    """All customer IDs in this tenant that carry the given manual
    segment. Returned as a Python list (not a Query) so callers can
    feed it to ``Customer.id.in_(...)`` without leaking the SQLAlchemy
    object across module boundaries."""
    key = (segment_key or "").strip().lower()
    if not key:
        return []
    rows = (
        db.query(CustomerSegmentManual.customer_id)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.segment_key == key,
        )
        .all()
    )
    return [r[0] for r in rows]


# ── Marketing preference flags (stored on Customer.extra_metadata) ──────
#
# We keep these on the JSON column rather than promoting them to first
# class booleans so the customer page doesn't need a migration each
# time we add a new flag (test-recipient, future "VIP touchpoint
# only", "newsletter only", etc.). The flag set is small and rarely
# updated, so JSON read cost is negligible.


META_KEY_OPT_OUT          = "marketing_opt_out_manual"
META_KEY_OPT_OUT_AT       = "marketing_opt_out_manual_at"
META_KEY_TEST_RECIPIENT   = "is_campaign_test_recipient"
META_KEY_TEST_AT          = "campaign_test_recipient_at"


def _get_customer(db: Session, tenant_id: int, customer_id: int) -> Customer:
    cust = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        .first()
    )
    if cust is None:
        raise LookupError(f"customer {customer_id} not found in tenant {tenant_id}")
    return cust


def _set_customer_meta_flag(
    db: Session, *, tenant_id: int, customer_id: int,
    key: str, value: bool, timestamp_key: Optional[str] = None,
    commit: bool = True,
) -> Customer:
    """Toggle a boolean flag on ``Customer.extra_metadata`` while also
    stamping a related ``*_at`` timestamp so the audit trail has the
    "when". Always uses ``flag_modified`` so SQLAlchemy ships the JSON
    update to Postgres."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    cust = _get_customer(db, tenant_id, customer_id)
    meta = dict(cust.extra_metadata or {})
    meta[key] = bool(value)
    if timestamp_key:
        if value:
            meta[timestamp_key] = datetime.now(timezone.utc).isoformat()
        else:
            meta.pop(timestamp_key, None)
    cust.extra_metadata = meta
    flag_modified(cust, "extra_metadata")
    if commit:
        db.commit()
        db.refresh(cust)
    return cust


def set_marketing_opt_out_manual(
    db: Session, *, tenant_id: int, customer_id: int,
    opted_out: bool, commit: bool = True,
) -> Customer:
    """Toggle the merchant-controlled marketing opt-out.

    Distinct from the customer-driven ``is_unsubscribed`` flag: this
    one is set explicitly by the merchant from the drawer UI
    ("استبعاد من الحملات التسويقية") to keep a customer out of
    *manual marketing* campaigns without breaking transactional /
    automation messages or the 24h service window.
    """
    return _set_customer_meta_flag(
        db, tenant_id=tenant_id, customer_id=customer_id,
        key=META_KEY_OPT_OUT, value=opted_out,
        timestamp_key=META_KEY_OPT_OUT_AT, commit=commit,
    )


def set_test_recipient(
    db: Session, *, tenant_id: int, customer_id: int,
    is_test: bool, commit: bool = True,
) -> Customer:
    """Add / remove a customer from the internal "test recipient" set.

    No merchant-visible tag is created — this is purely an internal
    flag. The wizard reads it via ``test_recipient_customer_ids`` to
    let the merchant target a small dry-run audience before launching
    the real campaign.
    """
    return _set_customer_meta_flag(
        db, tenant_id=tenant_id, customer_id=customer_id,
        key=META_KEY_TEST_RECIPIENT, value=is_test,
        timestamp_key=META_KEY_TEST_AT, commit=commit,
    )


def is_marketing_opted_out(customer: Customer) -> bool:
    """Read-only convenience for hot paths (snapshot loop). Tolerates
    a missing ``extra_metadata`` (legacy rows have ``None`` here)."""
    meta = customer.extra_metadata or {}
    return bool(meta.get(META_KEY_OPT_OUT))


def is_test_recipient(customer: Customer) -> bool:
    meta = customer.extra_metadata or {}
    return bool(meta.get(META_KEY_TEST_RECIPIENT))


def list_test_recipient_customer_ids(db: Session, tenant_id: int) -> List[int]:
    """Return the IDs of every customer in this tenant currently
    marked as a test recipient. Used by the wizard's "send to test
    list" quick action.

    Named ``list_*`` (not ``test_*``) deliberately so pytest does not
    try to collect it as a test case during discovery — pytest treats
    every module-level ``test_`` function as a test, which would
    require fixtures we don't have.
    """
    # SQLite vs Postgres JSON access — fall back to a Python filter
    # on the small result set to keep the query portable across both
    # backends used in tests + production.
    rows = (
        db.query(Customer.id, Customer.extra_metadata)
        .filter(Customer.tenant_id == tenant_id)
        .all()
    )
    out: List[int] = []
    for cid, meta in rows:
        if (meta or {}).get(META_KEY_TEST_RECIPIENT):
            out.append(cid)
    return out


__all__ = [
    "META_KEY_OPT_OUT",
    "META_KEY_OPT_OUT_AT",
    "META_KEY_TEST_RECIPIENT",
    "META_KEY_TEST_AT",
    "UnknownSegmentError",
    "add_manual_segment",
    "assert_known_segment",
    "customer_ids_with_manual_segment",
    "is_marketing_opted_out",
    "is_test_recipient",
    "list_manual_segments_bulk",
    "list_manual_segments_for_customer",
    "remove_manual_segment",
    "list_test_recipient_customer_ids",
    "set_marketing_opt_out_manual",
    "set_test_recipient",
]
