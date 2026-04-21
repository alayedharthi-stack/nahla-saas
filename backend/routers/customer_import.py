"""
routers/customer_import.py
──────────────────────────
Tenant-scoped customer-import wizard endpoints.

Routes:
    POST   /customers/import/upload                — multipart upload, parse
    POST   /customers/import/{batch_id}/mapping    — submit column mapping
    GET    /customers/import/{batch_id}            — full batch state
    GET    /customers/import/{batch_id}/rows       — paged classified rows
    POST   /customers/import/{batch_id}/commit     — execute the import
    GET    /customers/import                       — list recent batches
    DELETE /customers/import/{batch_id}            — discard an uncommitted batch

Wizard contract (4 steps):
    1) Upload      → POST /upload                 → returns batch_id, headers, sample rows
    2) Mapping     → POST /{id}/mapping           → returns dedupe summary + sample classified
    3) Preview     → GET  /{id}/rows?status=...   → drill down into each bucket
    4) Commit      → POST /{id}/commit            → returns final created/updated/skipped/errors
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import CustomerImportBatch
from services.customer_import import (
    CLASSIFICATION_EXACT,
    CLASSIFICATION_INVALID,
    CLASSIFICATION_NEW,
    CLASSIFICATION_SUSPECT,
    SUPPORTED_FIELDS,
    classify_rows,
    commit_batch,
    normalize_row,
    parse_upload,
    suggest_column_mapping,
)
from services.customer_import.parser import ParseError

logger = logging.getLogger("nahla.routers.customer_import")

router = APIRouter(prefix="/customers/import", tags=["Customers Import"])


# ── Pydantic input models ────────────────────────────────────────────────────

class MappingIn(BaseModel):
    """Column mapping submitted on step 2.

    Maps each canonical Nahla field (name, phone, ...) to the spreadsheet
    header chosen by the merchant. `phone` is required because no row
    can become a customer without one.
    """
    mapping: Dict[str, str] = Field(default_factory=dict)
    default_region: Optional[str] = "SA"


class CommitIn(BaseModel):
    """Step 4 commit options."""
    apply_new: bool = True
    update_existing: bool = True
    suspect_decisions: Dict[int, str] = Field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_batch(db: Session, *, tenant_id: int, batch_id: int) -> CustomerImportBatch:
    batch = (
        db.query(CustomerImportBatch)
        .filter(CustomerImportBatch.tenant_id == tenant_id)
        .filter(CustomerImportBatch.id == batch_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="batch_not_found")
    return batch


def _serialize_batch(batch: CustomerImportBatch) -> Dict[str, Any]:
    return {
        "id":             batch.id,
        "filename":       batch.filename,
        "file_kind":      batch.file_kind,
        "status":         batch.status,
        "column_mapping": batch.column_mapping or {},
        "total_rows":     batch.total_rows,
        "summary": {
            "new":      batch.new_count,
            "matched":  batch.match_count,
            "suspects": batch.suspect_count,
            "invalid":  batch.invalid_count,
        },
        "result": {
            "created":  batch.created_count,
            "updated":  batch.updated_count,
            "skipped":  batch.skipped_count,
            "errors":   batch.error_count,
        },
        "created_at":    batch.created_at.isoformat() if batch.created_at else None,
        "committed_at":  batch.committed_at.isoformat() if batch.committed_at else None,
        "error_message": batch.error_message,
    }


def _persist_rows(batch: CustomerImportBatch, payload: List[Dict[str, Any]]) -> None:
    """Replace the batch's rows_payload, flagging the JSONB column dirty
    so SQLAlchemy actually emits the UPDATE."""
    batch.rows_payload = list(payload)
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        flag_modified(batch, "rows_payload")
    except Exception:
        pass


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/upload", summary="Upload a CSV/XLSX file (step 1)")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"upload_read_failed:{exc}")

    try:
        parsed = parse_upload(content=content, filename=file.filename or "")
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    suggestion = suggest_column_mapping(parsed.headers)
    sample = parsed.rows[:5]

    batch = CustomerImportBatch(
        tenant_id=tenant_id,
        filename=parsed.filename or (file.filename or ""),
        file_kind=parsed.kind,
        status="parsed",
        column_mapping=suggestion or {},
        total_rows=parsed.total_rows,
        rows_payload={
            "stage": "parsed",
            "headers": parsed.headers,
            "rows":    parsed.rows,
        },
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    return {
        "batch": _serialize_batch(batch),
        "headers": parsed.headers,
        "supported_fields": list(SUPPORTED_FIELDS),
        "suggested_mapping": suggestion,
        "sample_rows": sample,
    }


@router.post("/{batch_id}/mapping", summary="Submit column mapping (step 2)")
def submit_mapping(
    batch_id: int,
    body: MappingIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    batch = _load_batch(db, tenant_id=tenant_id, batch_id=batch_id)

    # Validate that the user gave us a phone column at minimum.
    if not (body.mapping.get("phone") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="phone_column_required",
        )

    parsed = batch.rows_payload or {}
    raw_rows = parsed.get("rows") or []
    if not raw_rows:
        raise HTTPException(status_code=400, detail="batch_has_no_rows")

    # Normalize every row using the chosen mapping.
    normalized_rows = []
    for idx, raw in enumerate(raw_rows, start=1):
        normalized_rows.append(
            normalize_row(
                row_index=idx, raw=raw,
                mapping=body.mapping,
                default_region=body.default_region or "SA",
            )
        )

    # Classify against the tenant's existing customer book.
    classified = classify_rows(db, tenant_id=tenant_id, rows=normalized_rows)
    payload = [c.to_dict() for c in classified]

    # Aggregate counters.
    counts = {k: 0 for k in (
        CLASSIFICATION_NEW, CLASSIFICATION_EXACT,
        CLASSIFICATION_SUSPECT, CLASSIFICATION_INVALID,
    )}
    for c in classified:
        counts[c.classification] = counts.get(c.classification, 0) + 1

    batch.column_mapping = dict(body.mapping)
    batch.status = "previewed"
    batch.total_rows    = len(payload)
    batch.new_count     = counts[CLASSIFICATION_NEW]
    batch.match_count   = counts[CLASSIFICATION_EXACT]
    batch.suspect_count = counts[CLASSIFICATION_SUSPECT]
    batch.invalid_count = counts[CLASSIFICATION_INVALID]
    _persist_rows(batch, payload)
    db.add(batch)
    db.commit()
    db.refresh(batch)

    return {
        "batch": _serialize_batch(batch),
        "sample": _sample_per_status(payload, limit=3),
    }


@router.get("/{batch_id}", summary="Get batch state")
def get_batch(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    batch = _load_batch(db, tenant_id=tenant_id, batch_id=batch_id)
    return {"batch": _serialize_batch(batch)}


@router.get("/{batch_id}/rows", summary="Paged classified rows (step 3)")
def list_rows(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = Query(
        None,
        description="Filter by classification: new | exact | suspect | invalid",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    tenant_id = resolve_tenant_id(request)
    batch = _load_batch(db, tenant_id=tenant_id, batch_id=batch_id)

    payload = batch.rows_payload or []
    if isinstance(payload, dict):
        # Means mapping has not been submitted yet — only raw rows exist.
        return {
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "stage": "awaiting_mapping",
        }

    if status:
        if status not in (
            CLASSIFICATION_NEW, CLASSIFICATION_EXACT,
            CLASSIFICATION_SUSPECT, CLASSIFICATION_INVALID,
        ):
            raise HTTPException(status_code=400, detail="invalid_status_filter")
        items = [r for r in payload if r.get("classification") == status]
    else:
        items = list(payload)

    total = len(items)
    start = (page - 1) * page_size
    end   = start + page_size

    return {
        "items":     items[start:end],
        "page":      page,
        "page_size": page_size,
        "total":     total,
    }


@router.post("/{batch_id}/commit", summary="Execute import (step 4)")
def commit_import(
    batch_id: int,
    body: CommitIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    batch = _load_batch(db, tenant_id=tenant_id, batch_id=batch_id)

    if batch.status not in ("previewed", "committed"):
        raise HTTPException(
            status_code=400,
            detail=f"batch_not_ready (status={batch.status})",
        )
    if batch.status == "committed":
        raise HTTPException(
            status_code=409,
            detail="batch_already_committed",
        )

    payload = batch.rows_payload or []
    if not isinstance(payload, list):
        raise HTTPException(
            status_code=400,
            detail="batch_payload_missing_classification",
        )

    result = commit_batch(
        db,
        tenant_id=tenant_id,
        batch_id=batch.id,
        classified_rows=payload,
        apply_new=body.apply_new,
        update_existing=body.update_existing,
        suspect_decisions={int(k): v for k, v in (body.suspect_decisions or {}).items()},
    )

    batch.status         = "committed"
    batch.committed_at   = datetime.now(timezone.utc)
    batch.created_count  = result.created
    batch.updated_count  = result.updated
    batch.skipped_count  = result.skipped
    batch.error_count    = result.errors
    if result.error_rows:
        # Persist a compact error summary; never blow JSONB size by
        # writing thousands of stack traces.
        batch.error_message = "; ".join(
            f"row {e.get('row_index')}: {e.get('error')}"
            for e in result.error_rows[:25]
        )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    return {
        "batch":  _serialize_batch(batch),
        "result": result.to_dict(),
    }


@router.get("", summary="List recent import batches")
def list_batches(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = resolve_tenant_id(request)
    rows = (
        db.query(CustomerImportBatch)
        .filter(CustomerImportBatch.tenant_id == tenant_id)
        .order_by(CustomerImportBatch.id.desc())
        .limit(limit)
        .all()
    )
    return {"items": [_serialize_batch(b) for b in rows]}


@router.delete("/{batch_id}", summary="Discard an uncommitted batch")
def delete_batch(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    batch = _load_batch(db, tenant_id=tenant_id, batch_id=batch_id)
    if batch.status == "committed":
        raise HTTPException(
            status_code=409,
            detail="cannot_delete_committed_batch",
        )
    db.delete(batch)
    db.commit()
    return {"ok": True}


# ── Sample helpers (small, embedded in mapping response) ─────────────────────

def _sample_per_status(payload: List[Dict[str, Any]], *, limit: int = 3) -> Dict[str, list]:
    out = {
        CLASSIFICATION_NEW:     [],
        CLASSIFICATION_EXACT:   [],
        CLASSIFICATION_SUSPECT: [],
        CLASSIFICATION_INVALID: [],
    }
    for row in payload:
        bucket = row.get("classification")
        if bucket in out and len(out[bucket]) < limit:
            out[bucket].append(row)
    return out
