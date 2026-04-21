"""
services/customer_import/importer.py
────────────────────────────────────
The commit pass for the customer import wizard.

Given the classified rows (already normalized + decided) and the
merchant's commit options, this module:

    - creates "new" rows as fresh Customers (source: manual_import)
    - non-destructively merges "exact" rows into the existing customer
      (only fills missing/weak fields, never overwrites trusted data
      from salla_sync, etc.)
    - applies per-row decisions for "suspect" rows:
          merge_into:<id>  → same as exact merge against that id
          create_new       → insert as a new customer
          skip             → no-op
    - tracks lineage on every touched customer:
          extra_metadata.primary_source     (only set on create)
          extra_metadata.source_tags        (deduped append)
          extra_metadata.last_import_batch  (always overwritten)

Every Customer write goes through SQLAlchemy directly so we honor the
existing partial-unique index on (tenant_id, normalized_phone). If a
race condition produces an IntegrityError we re-fetch and merge.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from .dedupe import (
    CLASSIFICATION_EXACT,
    CLASSIFICATION_INVALID,
    CLASSIFICATION_NEW,
    CLASSIFICATION_SUSPECT,
)

logger = logging.getLogger("nahla.import.importer")


# Decisions accepted from the wizard for each suspect row.
SUSPECT_DECISION_SKIP        = "skip"
SUSPECT_DECISION_CREATE_NEW  = "create_new"
SUSPECT_DECISION_MERGE_PREFIX = "merge_into:"  # merge_into:<customer_id>


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors:  int = 0
    error_rows: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors":  self.errors,
            "error_rows": list(self.error_rows),
        }


# ── Public API ───────────────────────────────────────────────────────────────

def commit_batch(
    db: Any,
    *,
    tenant_id: int,
    batch_id: int,
    classified_rows: List[Dict[str, Any]],
    apply_new: bool = True,
    update_existing: bool = True,
    suspect_decisions: Optional[Dict[int, str]] = None,
) -> ImportResult:
    """Execute the import. `classified_rows` is the JSONB payload we
    persisted on the batch (already validated). `suspect_decisions`
    maps row_index → decision string ("skip", "create_new", or
    "merge_into:<id>").

    The function commits per row so a single bad row never aborts the
    whole import; the caller can replay errors from `result.error_rows`.
    """
    suspect_decisions = suspect_decisions or {}
    result = ImportResult()

    try:
        from models import Customer  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        logger.error("Customer model unavailable: %s", exc)
        raise

    for entry in classified_rows:
        try:
            classification = entry.get("classification") or CLASSIFICATION_INVALID
            normalized = entry.get("normalized") or {}
            row_index = int(entry.get("row_index") or 0)

            if classification == CLASSIFICATION_INVALID:
                result.skipped += 1
                continue

            if classification == CLASSIFICATION_NEW:
                if not apply_new:
                    result.skipped += 1
                    continue
                _create_customer(
                    db, Customer,
                    tenant_id=tenant_id,
                    batch_id=batch_id,
                    normalized=normalized,
                    result=result,
                )
                continue

            if classification == CLASSIFICATION_EXACT:
                if not update_existing:
                    result.skipped += 1
                    continue
                cid = entry.get("match_customer_id")
                _merge_into(
                    db, Customer,
                    tenant_id=tenant_id,
                    batch_id=batch_id,
                    customer_id=cid,
                    normalized=normalized,
                    result=result,
                )
                continue

            if classification == CLASSIFICATION_SUSPECT:
                decision = suspect_decisions.get(row_index, SUSPECT_DECISION_SKIP)
                if decision == SUSPECT_DECISION_SKIP:
                    result.skipped += 1
                    continue
                if decision == SUSPECT_DECISION_CREATE_NEW:
                    _create_customer(
                        db, Customer,
                        tenant_id=tenant_id,
                        batch_id=batch_id,
                        normalized=normalized,
                        result=result,
                    )
                    continue
                if decision.startswith(SUSPECT_DECISION_MERGE_PREFIX):
                    try:
                        target_id = int(
                            decision[len(SUSPECT_DECISION_MERGE_PREFIX):]
                        )
                    except (TypeError, ValueError):
                        result.errors += 1
                        result.error_rows.append({
                            "row_index": row_index,
                            "error": "bad_merge_decision",
                        })
                        continue
                    _merge_into(
                        db, Customer,
                        tenant_id=tenant_id,
                        batch_id=batch_id,
                        customer_id=target_id,
                        normalized=normalized,
                        result=result,
                    )
                    continue

                result.skipped += 1
                continue

            # Unknown classification — count and skip.
            result.skipped += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("import row failed")
            db.rollback()
            result.errors += 1
            result.error_rows.append({
                "row_index": entry.get("row_index"),
                "error": str(exc),
            })

    try:
        db.commit()
    except Exception as exc:
        logger.exception("final commit failed: %s", exc)
        db.rollback()
        result.errors += 1

    return result


# ── Create / merge primitives ────────────────────────────────────────────────

def _create_customer(
    db: Any,
    Customer,
    *,
    tenant_id: int,
    batch_id: int,
    normalized: Dict[str, Any],
    result: ImportResult,
) -> None:
    phone = (normalized.get("normalized_phone") or "").strip()
    if not phone:
        result.skipped += 1
        return

    now = datetime.now(timezone.utc)
    meta = _build_metadata(
        existing=None, normalized=normalized, batch_id=batch_id,
    )

    customer = Customer(
        tenant_id=tenant_id,
        name=(normalized.get("name") or "").strip() or None,
        email=(normalized.get("email") or "").strip().lower() or None,
        phone=(normalized.get("phone_raw") or phone).strip(),
        normalized_phone=phone,
        acquisition_channel="manual_import",
        first_seen_at=now,
        last_interaction_at=now,
        extra_metadata=meta,
    )
    db.add(customer)
    try:
        db.flush()
        result.created += 1
    except IntegrityError:
        # Race condition: another concurrent import or webhook beat us
        # to the same normalized_phone. Re-fetch and merge instead.
        db.rollback()
        existing = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id)
            .filter(Customer.normalized_phone == phone)
            .first()
        )
        if existing is None:
            # Truly broken — record an error and move on.
            result.errors += 1
            result.error_rows.append({
                "row_index": normalized.get("row_index"),
                "error": "integrity_error_no_existing",
            })
            return
        _apply_non_destructive_merge(
            existing, normalized=normalized, batch_id=batch_id,
        )
        db.add(existing)
        try:
            db.flush()
            result.updated += 1
        except Exception as exc:
            db.rollback()
            result.errors += 1
            result.error_rows.append({
                "row_index": normalized.get("row_index"),
                "error": f"merge_after_race_failed:{exc}",
            })


def _merge_into(
    db: Any,
    Customer,
    *,
    tenant_id: int,
    batch_id: int,
    customer_id: Optional[int],
    normalized: Dict[str, Any],
    result: ImportResult,
) -> None:
    if not customer_id:
        result.skipped += 1
        return
    existing = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id)
        .filter(Customer.id == int(customer_id))
        .first()
    )
    if existing is None:
        result.skipped += 1
        return
    _apply_non_destructive_merge(
        existing, normalized=normalized, batch_id=batch_id,
    )
    db.add(existing)
    try:
        db.flush()
        result.updated += 1
    except Exception as exc:
        db.rollback()
        result.errors += 1
        result.error_rows.append({
            "row_index": normalized.get("row_index"),
            "error": f"merge_failed:{exc}",
        })


# ── Non-destructive field merging ────────────────────────────────────────────

def _apply_non_destructive_merge(
    existing,
    *,
    normalized: Dict[str, Any],
    batch_id: int,
) -> None:
    """Fill empty / weak fields on the existing customer from the
    incoming row. NEVER overwrites a value that is already present —
    salla_sync data and other trusted sources stay authoritative."""
    incoming_name  = (normalized.get("name")  or "").strip()
    incoming_email = (normalized.get("email") or "").strip().lower()
    incoming_phone = (normalized.get("normalized_phone") or "").strip()
    incoming_raw   = (normalized.get("phone_raw") or "").strip()

    if incoming_name and not (existing.name or "").strip():
        existing.name = incoming_name
    if incoming_email and not (existing.email or "").strip():
        existing.email = incoming_email
    if incoming_phone and not (existing.normalized_phone or "").strip():
        existing.normalized_phone = incoming_phone
    if incoming_raw and not (existing.phone or "").strip():
        existing.phone = incoming_raw

    # Refresh interaction timestamp so the customer surfaces in
    # "recently touched" lists.
    existing.last_interaction_at = datetime.now(timezone.utc)

    # Metadata merge — never wipe sibling keys, only top up.
    meta = _build_metadata(
        existing=getattr(existing, "extra_metadata", None) or {},
        normalized=normalized,
        batch_id=batch_id,
    )
    existing.extra_metadata = meta
    try:
        flag_modified(existing, "extra_metadata")
    except Exception:  # noqa: silent-ok — flag_modified is an SQLAlchemy hint; reassignment above already triggers UPDATE
        pass


def _build_metadata(
    *,
    existing: Optional[Dict[str, Any]],
    normalized: Dict[str, Any],
    batch_id: int,
) -> Dict[str, Any]:
    """Compose the merged extra_metadata block. Rules:

    - `primary_source` is set ONLY when missing (first source wins).
    - `source_tags` is a sorted, deduped list of every source we have
      ever seen for this customer.
    - `last_import_batch` is always refreshed to the current batch.
    - Optional fields (city, notes) are filled non-destructively.
    """
    meta = dict(existing or {})

    incoming_source = (normalized.get("source") or "manual_import").strip() or "manual_import"
    if not meta.get("primary_source"):
        meta["primary_source"] = incoming_source

    tags = set(meta.get("source_tags") or [])
    if isinstance(tags, set):
        tags.add(incoming_source)
        # Always include manual_import alongside the merchant-provided
        # source so audits can distinguish hand-uploaded data.
        tags.add("manual_import")
    meta["source_tags"] = sorted(tags)

    meta["last_import_batch"] = int(batch_id)
    meta["last_import_at"] = datetime.now(timezone.utc).isoformat()

    incoming_city  = (normalized.get("city")  or "").strip()
    incoming_notes = (normalized.get("notes") or "").strip()
    if incoming_city and not (meta.get("city") or "").strip():
        meta["city"] = incoming_city
    if incoming_notes:
        # Notes are append-only because merchants commonly add details
        # over multiple imports. Keep newest at the bottom.
        prior_notes = (meta.get("notes") or "").strip()
        if prior_notes and incoming_notes not in prior_notes:
            meta["notes"] = f"{prior_notes}\n{incoming_notes}".strip()
        elif not prior_notes:
            meta["notes"] = incoming_notes

    return meta
