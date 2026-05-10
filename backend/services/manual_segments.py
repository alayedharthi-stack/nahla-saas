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
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from models import Customer, CustomerSegmentManual
from services.nahla_segments import SEGMENTS, all_segment_keys, get_segment

logger = logging.getLogger(__name__)


# ── Mode-column availability gate ──────────────────────────────────────
#
# Migration 0053 added ``customer_segments_manual.mode``. Production
# deployments that haven't run the migration yet (deploy in flight,
# Alembic step skipped, etc.) would otherwise 500 on EVERY query that
# references ``mode``, taking the customer list page down with them.
# We probe the column once per process and fall back to a mode-less
# code path when it's missing — every row is treated as ``include``,
# which is exactly what they were before 0053 anyway.
#
# The flag is set lazily on first use rather than at import time so a
# DB that's still booting / migrating doesn't crash the import.
_MODE_COLUMN_AVAILABLE: Optional[bool] = None


def _mode_column_available(db: Session) -> bool:
    """Return True iff ``customer_segments_manual.mode`` exists in the
    bound database. Caches the answer on the module so we don't run
    the probe on every helper call."""
    global _MODE_COLUMN_AVAILABLE
    if _MODE_COLUMN_AVAILABLE is not None:
        return _MODE_COLUMN_AVAILABLE
    try:
        # The cheapest way to check: SELECT mode FROM ... LIMIT 0.
        # If the column is missing the DB raises ProgrammingError /
        # OperationalError before scanning any row.
        db.query(CustomerSegmentManual.mode).limit(0).all()
        _MODE_COLUMN_AVAILABLE = True
    except (ProgrammingError, OperationalError):
        # Roll back so the session isn't left in a broken state.
        try:
            db.rollback()
        except Exception:
            pass
        _MODE_COLUMN_AVAILABLE = False
        logger.warning(
            "[manual_segments] mode column missing on customer_segments_manual; "
            "falling back to legacy (mode-less) helpers. Run alembic upgrade head."
        )
    except Exception:
        # Any other error: assume available; the per-helper try/except
        # will catch and roll back if it lies.
        _MODE_COLUMN_AVAILABLE = True
    return _MODE_COLUMN_AVAILABLE


# ── Validation ──────────────────────────────────────────────────────────


class UnknownSegmentError(ValueError):
    """Raised when a caller tries to add a segment_key that isn't part
    of the canonical Nahla registry. The router catches this and
    returns HTTP 422 with the list of accepted keys.
    """


class ModeColumnUnavailableError(RuntimeError):
    """Raised when a caller asks for a behaviour that requires the
    ``customer_segments_manual.mode`` column (e.g. creating an
    exclude row) but the database is still on a pre-0053 schema.

    The router translates this into a structured 200 response so the
    customer page can render "نحن نُحدّث المنصة الآن — أعد المحاولة
    خلال دقيقة" instead of a 500. Once the migration runs, the next
    process restart re-probes and this error becomes unreachable.
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


# ── Mode constants ───────────────────────────────────────────────────────
# Single source of truth for the two values stored in
# CustomerSegmentManual.mode. See migration 0053.
MODE_INCLUDE = "include"
MODE_EXCLUDE = "exclude"
ALLOWED_MODES = frozenset({MODE_INCLUDE, MODE_EXCLUDE})


class _LegacyManualSegmentRow:
    """Lightweight row representation returned from the legacy
    (pre-0053) write paths. Shape-compatible with the ORM model so
    routers can call ``row.segment_key``, ``row.source``, etc., but
    deliberately does NOT expose ``mode`` because the database
    doesn't store one yet."""

    __slots__ = ("id", "tenant_id", "customer_id", "segment_key",
                 "source", "created_by", "created_at", "mode")

    def __init__(self, *, id, tenant_id, customer_id, segment_key,
                 source, created_by, created_at):
        self.id = id
        self.tenant_id = tenant_id
        self.customer_id = customer_id
        self.segment_key = segment_key
        self.source = source
        self.created_by = created_by
        self.created_at = created_at
        # Always "include" on legacy schema — there's no other state.
        self.mode = MODE_INCLUDE


def _legacy_upsert_include_row(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    segment_key: str,
    created_by: Optional[int],
) -> _LegacyManualSegmentRow:
    """Idempotent insert-or-fetch on a pre-0053 schema using raw SQL
    that does not reference the ``mode`` column.

    Returns a ``_LegacyManualSegmentRow`` (shape-compatible with the
    ORM model) so callers can render ``row.segment_key`` / etc.
    without caring whether they got an ORM row or this stub.
    """
    from sqlalchemy import text  # noqa: PLC0415
    existing = db.execute(
        text(
            """
            SELECT id, tenant_id, customer_id, segment_key, source,
                   created_by, created_at
            FROM customer_segments_manual
            WHERE tenant_id = :tid AND customer_id = :cid
              AND segment_key = :seg
            LIMIT 1
            """
        ),
        {"tid": tenant_id, "cid": customer_id, "seg": segment_key},
    ).fetchone()
    if existing is None:
        db.execute(
            text(
                """
                INSERT INTO customer_segments_manual
                  (tenant_id, customer_id, segment_key, source, created_by, created_at)
                VALUES
                  (:tid, :cid, :seg, 'manual', :cby, CURRENT_TIMESTAMP)
                """
            ),
            {"tid": tenant_id, "cid": customer_id, "seg": segment_key, "cby": created_by},
        )
        db.commit()
        existing = db.execute(
            text(
                """
                SELECT id, tenant_id, customer_id, segment_key, source,
                       created_by, created_at
                FROM customer_segments_manual
                WHERE tenant_id = :tid AND customer_id = :cid
                  AND segment_key = :seg
                LIMIT 1
                """
            ),
            {"tid": tenant_id, "cid": customer_id, "seg": segment_key},
        ).fetchone()
    return _LegacyManualSegmentRow(
        id=existing[0],
        tenant_id=existing[1],
        customer_id=existing[2],
        segment_key=existing[3],
        source=existing[4],
        created_by=existing[5],
        created_at=existing[6],
    )


def add_manual_segment(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    segment_key: str,
    mode: str = MODE_INCLUDE,
    created_by: Optional[int] = None,
    commit: bool = True,
) -> CustomerSegmentManual:
    """Pin ``customer`` to ``segment_key`` in the requested ``mode``.

    Idempotent — re-tagging the same pair updates the existing row's
    mode (so a merchant can flip include⇄exclude without us having
    to delete + recreate). The unique index keeps us at one row per
    (tenant, customer, segment) tuple.

    Validates:
      * ``segment_key`` exists in the official Nahla registry.
      * ``mode`` is one of {include, exclude}.
      * The customer belongs to ``tenant_id`` (cross-tenant guard).

    Pre-0053 behaviour
    ──────────────────
    * ``mode='include'`` works normally (raw-SQL insert without the
      mode column).
    * ``mode='exclude'`` cannot be honoured on a legacy schema — we
      raise ``ModeColumnUnavailableError`` so the caller surfaces a
      structured error to the merchant ("the platform is upgrading,
      please retry in a moment") instead of a 500. The router maps
      this to a 503-shaped 200 with a clear message.
    """
    if mode not in ALLOWED_MODES:
        raise ValueError(
            f"Unknown mode {mode!r}. Allowed: {sorted(ALLOWED_MODES)}",
        )
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

    mode_available = _mode_column_available(db)

    if not mode_available and mode == MODE_EXCLUDE:
        # Legacy schema can't store exclude rows. Surface a typed
        # error so the API layer renders a clear merchant message.
        raise ModeColumnUnavailableError(
            "exclude mode requires migration 0053; please retry shortly"
        )

    if not mode_available:
        # Legacy path — never load the ORM row (SQLAlchemy would
        # SELECT ``mode`` in the projection). The helper handles
        # the upsert idempotently.
        return _legacy_upsert_include_row(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
            segment_key=key,
            created_by=created_by,
        )

    # ── Modern path (0053+) — full ORM is fine.
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
        # Idempotent + mode-flip — update if the merchant clicked
        # the opposite mode (e.g. previously excluded, now wants
        # to include). Same row, different mode value.
        if existing.mode != mode:
            existing.mode = mode
            if commit:
                db.commit()
                db.refresh(existing)
        return existing

    row = CustomerSegmentManual(
        tenant_id=tenant_id,
        customer_id=customer_id,
        segment_key=key,
        mode=mode,
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
    except (ProgrammingError, OperationalError):
        # The probe lied — DB doesn't actually have the column.
        # Roll back, mark unavailable, retry without the column.
        db.rollback()
        global _MODE_COLUMN_AVAILABLE
        _MODE_COLUMN_AVAILABLE = False
        if mode == MODE_EXCLUDE:
            raise ModeColumnUnavailableError(
                "exclude mode requires migration 0053; please retry shortly"
            )
        return _legacy_upsert_include_row(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
            segment_key=key,
            created_by=created_by,
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
    (still a 200 OK at the API layer — caller decides).

    Note — this is the *unconditional* delete. The router uses
    ``smart_remove_manual_segment`` for the merchant-facing UX which
    automatically converts an include into an exclude when the auto
    classifier still matches (so the customer doesn't pop back into
    the segment one second after the merchant removed them).
    """
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
    """Return the *included* segment keys this customer is manually
    pinned to (sorted). Used by the legacy callers that only care
    about positive tags. ``list_manual_sources_for_customer`` returns
    the include + exclude breakdown for the new sources UI.

    Pre-0053 fallback: returns every tagged segment as if mode were
    ``include`` (the legacy semantic).
    """
    base = (
        db.query(CustomerSegmentManual.segment_key)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id == customer_id,
        )
    )
    if _mode_column_available(db):
        try:
            rows = base.filter(CustomerSegmentManual.mode == MODE_INCLUDE).all()
            return sorted({r[0] for r in rows})
        except (ProgrammingError, OperationalError):
            try:
                db.rollback()
            except Exception:
                pass
            global _MODE_COLUMN_AVAILABLE
            _MODE_COLUMN_AVAILABLE = False
    rows = base.all()
    return sorted({r[0] for r in rows})


def list_manual_sources_for_customer(
    db: Session, tenant_id: int, customer_id: int,
) -> Dict[str, str]:
    """Return ``{segment_key: mode}`` for this customer.

    Used by the drawer to render the per-segment source label
    ("VIP يدوي + تلقائي" vs "مستبعد يدويًا من VIP" vs "VIP تلقائي").

    Pre-0053 fallback: every row is reported as ``include``.
    """
    if _mode_column_available(db):
        try:
            rows = (
                db.query(CustomerSegmentManual.segment_key, CustomerSegmentManual.mode)
                .filter(
                    CustomerSegmentManual.tenant_id == tenant_id,
                    CustomerSegmentManual.customer_id == customer_id,
                )
                .all()
            )
            return {k: m for (k, m) in rows}
        except (ProgrammingError, OperationalError):
            try:
                db.rollback()
            except Exception:
                pass
            global _MODE_COLUMN_AVAILABLE
            _MODE_COLUMN_AVAILABLE = False
    rows_legacy = (
        db.query(CustomerSegmentManual.segment_key)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id == customer_id,
        )
        .all()
    )
    return {r[0]: MODE_INCLUDE for r in rows_legacy}


def list_manual_segments_bulk(
    db: Session, tenant_id: int, customer_ids: List[int],
) -> Dict[int, List[str]]:
    """Bulk version of ``list_manual_segments_for_customer`` —
    returns ``{customer_id: [included_segment_key, ...]}``.

    Excludes are deliberately filtered out so callers that only
    want the positive-tag list don't have to re-filter. For the
    full include + exclude breakdown use
    ``list_manual_sources_bulk``.

    Defensive against pre-0053 schemas: if the ``mode`` column is
    missing, we treat every row as ``include`` (the legacy semantic)
    instead of 500-ing the caller. The customers page must keep
    working during a Railway deploy where the migration runs after
    the new code reaches the worker.
    """
    if not customer_ids:
        return {}
    base = (
        db.query(CustomerSegmentManual.customer_id, CustomerSegmentManual.segment_key)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id.in_(customer_ids),
        )
    )
    if _mode_column_available(db):
        try:
            rows = base.filter(CustomerSegmentManual.mode == MODE_INCLUDE).all()
        except (ProgrammingError, OperationalError):
            # Probe lied (e.g. column dropped after probe). Re-probe
            # next call by clearing the cache, and fall back to
            # mode-less for THIS call.
            try:
                db.rollback()
            except Exception:
                pass
            global _MODE_COLUMN_AVAILABLE
            _MODE_COLUMN_AVAILABLE = False
            rows = base.all()
    else:
        rows = base.all()
    out: Dict[int, Set[str]] = {}
    for cid, key in rows:
        out.setdefault(cid, set()).add(key)
    return {cid: sorted(keys) for cid, keys in out.items()}


def list_manual_sources_bulk(
    db: Session, tenant_id: int, customer_ids: List[int],
) -> Dict[int, Dict[str, str]]:
    """Bulk include + exclude breakdown — ``{customer_id: {key: mode}}``.

    Powers the ``segment_sources`` field in the customers list
    response so the drawer can render labels without an N+1 fetch.

    Pre-0053 fallback: when the ``mode`` column doesn't exist yet we
    return every row as ``include``. This keeps the customers list
    response shape stable during a deploy where the worker beats
    the migration to the start line.
    """
    if not customer_ids:
        return {}
    if _mode_column_available(db):
        try:
            rows = (
                db.query(
                    CustomerSegmentManual.customer_id,
                    CustomerSegmentManual.segment_key,
                    CustomerSegmentManual.mode,
                )
                .filter(
                    CustomerSegmentManual.tenant_id == tenant_id,
                    CustomerSegmentManual.customer_id.in_(customer_ids),
                )
                .all()
            )
            out: Dict[int, Dict[str, str]] = {}
            for cid, key, mode in rows:
                out.setdefault(cid, {})[key] = mode
            return out
        except (ProgrammingError, OperationalError):
            try:
                db.rollback()
            except Exception:
                pass
            global _MODE_COLUMN_AVAILABLE
            _MODE_COLUMN_AVAILABLE = False
    rows_legacy = (
        db.query(
            CustomerSegmentManual.customer_id,
            CustomerSegmentManual.segment_key,
        )
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.customer_id.in_(customer_ids),
        )
        .all()
    )
    out: Dict[int, Dict[str, str]] = {}
    for cid, key in rows_legacy:
        out.setdefault(cid, {})[key] = MODE_INCLUDE
    return out


def customer_ids_with_manual_segment(
    db: Session, tenant_id: int, segment_key: str,
    *, mode: str = MODE_INCLUDE,
) -> List[int]:
    """Customer IDs in this tenant tagged ``segment_key`` in ``mode``.

    Default ``mode='include'`` keeps the legacy behaviour. Pass
    ``mode='exclude'`` to get the negative set (for the
    ``(auto ∪ include) − exclude`` filter formula).
    """
    if mode not in ALLOWED_MODES:
        raise ValueError(
            f"Unknown mode {mode!r}. Allowed: {sorted(ALLOWED_MODES)}",
        )
    key = (segment_key or "").strip().lower()
    if not key:
        return []
    base = (
        db.query(CustomerSegmentManual.customer_id)
        .filter(
            CustomerSegmentManual.tenant_id == tenant_id,
            CustomerSegmentManual.segment_key == key,
        )
    )
    # Pre-0053 fallback: with no mode column, every row is treated
    # as ``include``. ``mode='exclude'`` queries return [] instead
    # of crashing — there can't be any exclude rows on a schema
    # that doesn't know what excludes are.
    if not _mode_column_available(db):
        if mode == MODE_EXCLUDE:
            return []
        return [r[0] for r in base.all()]
    try:
        rows = base.filter(CustomerSegmentManual.mode == mode).all()
        return [r[0] for r in rows]
    except (ProgrammingError, OperationalError):
        try:
            db.rollback()
        except Exception:
            pass
        global _MODE_COLUMN_AVAILABLE
        _MODE_COLUMN_AVAILABLE = False
        if mode == MODE_EXCLUDE:
            return []
        return [r[0] for r in base.all()]


def smart_remove_manual_segment(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    segment_key: str,
    auto_match: bool,
    commit: bool = True,
) -> str:
    """Merchant-facing "remove this customer from segment X" action.

    The merchant sees one button — "أزل هذا التصنيف" — and expects
    the customer to disappear from the segment immediately AND stay
    out. There are two distinct DB outcomes depending on whether
    the auto classifier matched:

      * Auto classifier matched (``auto_match=True``):
        We can't delete the auto match (it's derived from RFM and
        will re-compute next cycle). Instead we INSERT/UPDATE a
        manual ``exclude`` row, which the filter formula subtracts.
        Returns ``"excluded"``.

      * Auto classifier did NOT match (``auto_match=False``):
        The customer was in the segment only because of the manual
        include row. Plain delete; nothing else needed.
        Returns ``"deleted"``.

    If neither row exists, returns ``"noop"``.

    Pre-0053 fallback
    ─────────────────
    On a legacy schema we cannot create an exclude row at all, so
    even ``auto_match=True`` collapses to a plain delete and we
    return ``"deleted_legacy"`` so the API layer can surface a
    one-time hint that the merchant may need to retry once the
    platform finishes upgrading. The customer will pop back into
    the auto-classified set on the next RFM cycle, but at least
    the click doesn't 500.
    """
    from sqlalchemy import text  # noqa: PLC0415
    key = assert_known_segment(segment_key)
    mode_available = _mode_column_available(db)

    if not mode_available:
        # ── Legacy path (pre-0053): NEVER load the ORM row, because
        # SQLAlchemy would emit ``SELECT customer_segments_manual.mode``
        # in the projection and the DB doesn't have that column. We
        # use raw SQL the whole way through the legacy branch.
        existing_id_row = db.execute(
            text(
                """
                SELECT id FROM customer_segments_manual
                WHERE tenant_id = :tid AND customer_id = :cid
                  AND segment_key = :seg
                LIMIT 1
                """
            ),
            {"tid": tenant_id, "cid": customer_id, "seg": key},
        ).fetchone()
        if existing_id_row is not None:
            db.execute(
                text(
                    "DELETE FROM customer_segments_manual WHERE id = :rid"
                ),
                {"rid": existing_id_row[0]},
            )
            if commit:
                db.commit()
            # When auto_match is True we'd ideally write an exclude
            # row, but the column doesn't exist yet. Return a typed
            # action so the API surfaces the degraded state.
            return "deleted_legacy" if auto_match else "deleted"
        return "noop"

    # ── Modern path (0053+) — full ORM is fine.
    try:
        existing = (
            db.query(CustomerSegmentManual)
            .filter(
                CustomerSegmentManual.tenant_id == tenant_id,
                CustomerSegmentManual.customer_id == customer_id,
                CustomerSegmentManual.segment_key == key,
            )
            .first()
        )
    except (ProgrammingError, OperationalError):
        # Probe lied — column actually missing. Recurse via the
        # legacy path now that the cache is updated.
        db.rollback()
        global _MODE_COLUMN_AVAILABLE
        _MODE_COLUMN_AVAILABLE = False
        return smart_remove_manual_segment(
            db, tenant_id=tenant_id, customer_id=customer_id,
            segment_key=segment_key, auto_match=auto_match, commit=commit,
        )

    if auto_match:
        try:
            if existing is None:
                new_row = CustomerSegmentManual(
                    tenant_id=tenant_id, customer_id=customer_id,
                    segment_key=key, mode=MODE_EXCLUDE, source="manual",
                )
                db.add(new_row)
            else:
                existing.mode = MODE_EXCLUDE
            if commit:
                db.commit()
            return "excluded"
        except (ProgrammingError, OperationalError):
            db.rollback()
            _MODE_COLUMN_AVAILABLE = False
            return smart_remove_manual_segment(
                db, tenant_id=tenant_id, customer_id=customer_id,
                segment_key=segment_key, auto_match=auto_match, commit=commit,
            )

    # auto_match=False — only the manual include row matters.
    if existing is None:
        return "noop"
    db.delete(existing)
    if commit:
        db.commit()
    return "deleted"


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
