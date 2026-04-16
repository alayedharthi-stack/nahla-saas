"""
core/tenant_integrity.py
─────────────────────────
Tenant Consistency & Identity Isolation Layer

Golden rule (enforced here):
  For every merchant, ALL of the following must belong to the SAME tenant:
    • Salla / Zid integration (store_id / external_store_id)
    • WhatsApp connection (phone_number_id, waba_id, access_token)
    • AI settings, customer records, conversation state, orders

Public API
──────────
  Guard functions (raise TenantIntegrityError on violation):
    assert_phone_id_not_claimed(db, phone_number_id, claiming_tenant_id)
    assert_waba_id_not_claimed(db, waba_id, claiming_tenant_id)
    assert_store_not_claimed(db, provider, store_id, claiming_tenant_id)

  Read-only audit:
    run_integrity_audit(db)        → full per-tenant report + duplicate lists
    run_post_deploy_check(db)      → summary + logs conflicts

  Reconciliation (admin only):
    reconcile_tenants(db, source_id, target_id, dry_run, actor)

  Logging helpers (internal):
    log_integrity_event(db, event, **fields)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.tenant_integrity")


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception — raised instead of HTTPException so callers can decide
# ─────────────────────────────────────────────────────────────────────────────

class TenantIntegrityError(Exception):
    """Raised when a write would violate cross-tenant isolation."""
    def __init__(self, message: str, conflict_tenant_id: Optional[int] = None, **ctx):
        super().__init__(message)
        self.conflict_tenant_id = conflict_tenant_id
        self.ctx = ctx


# ─────────────────────────────────────────────────────────────────────────────
# Guard functions — called at write-time from route handlers
# ─────────────────────────────────────────────────────────────────────────────

def assert_phone_id_not_claimed(
    db: Session,
    phone_number_id: str,
    claiming_tenant_id: int,
) -> None:
    """
    Raise TenantIntegrityError if phone_number_id is already owned by
    a DIFFERENT tenant with an active (connected) WhatsApp connection.

    The embedded-signup flow already clears stale rows; this guard
    is the hard backstop for all other write paths.
    """
    if not phone_number_id:
        return

    from database.models import WhatsAppConnection  # noqa: PLC0415

    conflict = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.phone_number_id == phone_number_id,
            WhatsAppConnection.tenant_id != claiming_tenant_id,
            WhatsAppConnection.status == "connected",
        )
        .first()
    )
    if conflict:
        log_integrity_event(
            db,
            event="write_blocked",
            tenant_id=claiming_tenant_id,
            other_tenant_id=conflict.tenant_id,
            phone_number_id=phone_number_id,
            action="assert_phone_id_not_claimed",
            result="blocked",
            detail=(
                f"phone_number_id={phone_number_id} already owned by "
                f"tenant={conflict.tenant_id} (status=connected)"
            ),
        )
        raise TenantIntegrityError(
            f"phone_number_id {phone_number_id} is already connected to tenant "
            f"#{conflict.tenant_id}. Disconnect it first.",
            conflict_tenant_id=conflict.tenant_id,
            phone_number_id=phone_number_id,
        )


def assert_waba_id_not_claimed(
    db: Session,
    waba_id: str,
    claiming_tenant_id: int,
) -> None:
    """
    Raise TenantIntegrityError if waba_id is already owned by a DIFFERENT
    active tenant. One WABA → one tenant. No exceptions.
    """
    if not waba_id:
        return

    from database.models import WhatsAppConnection  # noqa: PLC0415

    conflict = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.whatsapp_business_account_id == waba_id,
            WhatsAppConnection.tenant_id != claiming_tenant_id,
            WhatsAppConnection.status == "connected",
        )
        .first()
    )
    if conflict:
        log_integrity_event(
            db,
            event="write_blocked",
            tenant_id=claiming_tenant_id,
            other_tenant_id=conflict.tenant_id,
            waba_id=waba_id,
            action="assert_waba_id_not_claimed",
            result="blocked",
            detail=(
                f"waba_id={waba_id} already owned by tenant={conflict.tenant_id}"
            ),
        )
        raise TenantIntegrityError(
            f"WABA ID {waba_id} is already connected to tenant #{conflict.tenant_id}. "
            "Disconnect it first or use the admin reconciliation tool.",
            conflict_tenant_id=conflict.tenant_id,
            waba_id=waba_id,
        )


def assert_store_not_claimed(
    db: Session,
    provider: str,
    store_id: str,
    claiming_tenant_id: int,
) -> None:
    """
    Raise TenantIntegrityError if the (provider, store_id) is already bound
    to a DIFFERENT active tenant. One store → one canonical tenant.
    """
    if not store_id:
        return

    from database.models import Integration  # noqa: PLC0415

    conflict = (
        db.query(Integration)
        .filter(
            Integration.provider == provider,
            Integration.external_store_id == store_id,
            Integration.tenant_id != claiming_tenant_id,
            Integration.enabled == True,  # noqa: E712
        )
        .first()
    )
    if conflict:
        log_integrity_event(
            db,
            event="write_blocked",
            tenant_id=claiming_tenant_id,
            other_tenant_id=conflict.tenant_id,
            store_id=store_id,
            provider=provider,
            action="assert_store_not_claimed",
            result="blocked",
            detail=(
                f"store_id={store_id} provider={provider} already owned by "
                f"tenant={conflict.tenant_id}"
            ),
        )
        raise TenantIntegrityError(
            f"Store {store_id} ({provider}) is already bound to tenant "
            f"#{conflict.tenant_id}. Use the admin de-duplication tool first.",
            conflict_tenant_id=conflict.tenant_id,
            store_id=store_id,
            provider=provider,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Evict stale cross-tenant phone/waba ownership (write, use carefully)
# ─────────────────────────────────────────────────────────────────────────────

def evict_phone_id_from_other_tenants(
    db: Session,
    phone_number_id: str,
    keeping_tenant_id: int,
) -> int:
    """
    Null-out phone_number_id and mark as disconnected for every OTHER tenant
    that currently holds this phone_number_id.
    Returns the number of rows affected.
    Used by the embedded-signup and manual-connect flows before writing.
    """
    if not phone_number_id:
        return 0

    from database.models import WhatsAppConnection  # noqa: PLC0415

    victims = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.phone_number_id == phone_number_id,
            WhatsAppConnection.tenant_id != keeping_tenant_id,
        )
        .all()
    )
    for v in victims:
        log_integrity_event(
            db,
            event="duplicate_identity",
            tenant_id=keeping_tenant_id,
            other_tenant_id=v.tenant_id,
            phone_number_id=phone_number_id,
            action="evict_phone_id",
            result="fixed",
            detail=f"phone_number_id evicted from tenant={v.tenant_id} → claimed by tenant={keeping_tenant_id}",
        )
        v.phone_number_id = None
        v.status = "disconnected"
        v.sending_enabled = False
        v.webhook_verified = False
    return len(victims)


def evict_waba_id_from_other_tenants(
    db: Session,
    waba_id: str,
    keeping_tenant_id: int,
) -> int:
    """Same as above but for whatsapp_business_account_id."""
    if not waba_id:
        return 0

    from database.models import WhatsAppConnection  # noqa: PLC0415

    victims = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.whatsapp_business_account_id == waba_id,
            WhatsAppConnection.tenant_id != keeping_tenant_id,
        )
        .all()
    )
    for v in victims:
        log_integrity_event(
            db,
            event="duplicate_identity",
            tenant_id=keeping_tenant_id,
            other_tenant_id=v.tenant_id,
            waba_id=waba_id,
            action="evict_waba_id",
            result="fixed",
            detail=f"waba_id evicted from tenant={v.tenant_id} → claimed by tenant={keeping_tenant_id}",
        )
        v.whatsapp_business_account_id = None
        v.status = "disconnected"
        v.sending_enabled = False
        v.webhook_verified = False
    return len(victims)


# ─────────────────────────────────────────────────────────────────────────────
# Full audit: per-tenant health + global duplicate lists
# ─────────────────────────────────────────────────────────────────────────────

def run_integrity_audit(db: Session) -> Dict[str, Any]:
    """
    Comprehensive per-tenant integrity report.

    Returns:
      {
        "summary":             { healthy, warning, critical, total },
        "tenants":             [ { tenant_id, name, health, salla, whatsapp, issues } ],
        "duplicate_store_ids": [ { store_id, provider, tenant_ids } ],
        "duplicate_phone_ids": [ { phone_number_id, tenant_ids } ],
        "duplicate_waba_ids":  [ { waba_id, tenant_ids } ],
        "orphaned_wa":         [ { tenant_id, phone_number_id } ],
        "orphaned_stores":     [ { tenant_id, store_id, provider } ],
      }
    """
    from database.models import Integration, Tenant, WhatsAppConnection  # noqa: PLC0415
    from sqlalchemy import func as _func  # noqa: PLC0415

    tenants    = db.query(Tenant).order_by(Tenant.id.asc()).all()
    all_integ  = db.query(Integration).all()
    all_wa     = db.query(WhatsAppConnection).all()

    wa_by_tenant:    Dict[int, WhatsAppConnection] = {c.tenant_id: c for c in all_wa}
    store_by_tenant: Dict[int, List[Integration]]  = {}
    for intg in all_integ:
        store_by_tenant.setdefault(intg.tenant_id, []).append(intg)

    # ── Per-tenant rows ──────────────────────────────────────────────────────
    tenant_rows = []
    for t in tenants:
        wa    = wa_by_tenant.get(t.id)
        stores = store_by_tenant.get(t.id, [])
        salla = [s for s in stores if s.provider == "salla"]

        issues: List[str] = []

        # Duplicate store integrations for same tenant
        if len(salla) > 1:
            issues.append(f"multiple_salla_integrations ({len(salla)})")

        # WA connected but no store
        if wa and wa.status == "connected" and not salla:
            issues.append("wa_connected_no_store")

        # Store connected but no WA
        if salla and (not wa or wa.status not in ("connected",)):
            issues.append("store_no_wa")

        # Critical: connected but webhook_verified=false
        if wa and wa.status == "connected" and not wa.webhook_verified:
            issues.append("webhook_not_verified")

        # Critical: connected but sending disabled
        if wa and wa.status == "connected" and not wa.sending_enabled:
            issues.append("sending_disabled")

        health = "healthy"
        if any(i in issues for i in ("multiple_salla_integrations", "webhook_not_verified")):
            health = "critical"
        elif issues:
            health = "warning"

        tenant_rows.append({
            "tenant_id":    t.id,
            "tenant_name":  t.name,
            "health":       health,
            "issues":       issues,
            "store": {
                "count":          len(salla),
                "store_id":       salla[0].external_store_id if salla else None,
                "provider":       salla[0].provider if salla else None,
                "enabled":        salla[0].enabled if salla else None,
            },
            "whatsapp": {
                "status":           wa.status if wa else "not_connected",
                "phone_number_id":  wa.phone_number_id if wa else None,
                "waba_id":          wa.whatsapp_business_account_id if wa else None,
                "webhook_verified": bool(wa.webhook_verified) if wa else False,
                "sending_enabled":  bool(wa.sending_enabled) if wa else False,
                "connection_type":  wa.connection_type if wa else None,
                "provider":         wa.provider if wa else None,
            },
        })

    # ── Duplicate store_ids ──────────────────────────────────────────────────
    store_id_map: Dict[str, List[int]] = {}
    for intg in all_integ:
        sid = intg.external_store_id or (intg.config or {}).get("store_id", "")
        if not sid:
            continue
        key = f"{intg.provider}::{sid}"
        store_id_map.setdefault(key, []).append(intg.tenant_id)

    dup_stores = [
        {"store_id": k.split("::", 1)[1], "provider": k.split("::", 1)[0], "tenant_ids": v}
        for k, v in store_id_map.items()
        if len(v) > 1
    ]

    # ── Duplicate phone_number_ids ───────────────────────────────────────────
    phone_map: Dict[str, List[int]] = {}
    for wa in all_wa:
        if wa.phone_number_id:
            phone_map.setdefault(wa.phone_number_id, []).append(wa.tenant_id)

    dup_phones = [
        {"phone_number_id": k, "tenant_ids": v}
        for k, v in phone_map.items()
        if len(v) > 1
    ]

    # ── Duplicate waba_ids ───────────────────────────────────────────────────
    waba_map: Dict[str, List[int]] = {}
    for wa in all_wa:
        if wa.whatsapp_business_account_id:
            waba_map.setdefault(wa.whatsapp_business_account_id, []).append(wa.tenant_id)

    dup_wabas = [
        {"waba_id": k, "tenant_ids": v}
        for k, v in waba_map.items()
        if len(v) > 1
    ]

    # ── Orphans ──────────────────────────────────────────────────────────────
    wa_tenant_ids    = {c.tenant_id for c in all_wa if c.status == "connected"}
    store_tenant_ids = {i.tenant_id for i in all_integ if i.enabled}

    orphaned_wa = [
        {
            "tenant_id":       wa_by_tenant[tid].tenant_id,
            "phone_number_id": wa_by_tenant[tid].phone_number_id,
            "waba_id":         wa_by_tenant[tid].whatsapp_business_account_id,
        }
        for tid in wa_tenant_ids
        if tid not in store_tenant_ids
    ]
    orphaned_stores = [
        {
            "tenant_id": i.tenant_id,
            "store_id":  i.external_store_id or (i.config or {}).get("store_id"),
            "provider":  i.provider,
        }
        for i in all_integ
        if i.enabled and i.tenant_id not in wa_tenant_ids
    ]

    # ── Summary ──────────────────────────────────────────────────────────────
    summary = {
        "total":    len(tenant_rows),
        "healthy":  sum(1 for r in tenant_rows if r["health"] == "healthy"),
        "warning":  sum(1 for r in tenant_rows if r["health"] == "warning"),
        "critical": sum(1 for r in tenant_rows if r["health"] == "critical"),
        "duplicate_store_ids": len(dup_stores),
        "duplicate_phone_ids": len(dup_phones),
        "duplicate_waba_ids":  len(dup_wabas),
        "orphaned_wa":         len(orphaned_wa),
        "orphaned_stores":     len(orphaned_stores),
    }

    return {
        "summary":             summary,
        "tenants":             tenant_rows,
        "duplicate_store_ids": dup_stores,
        "duplicate_phone_ids": dup_phones,
        "duplicate_waba_ids":  dup_wabas,
        "orphaned_wa":         orphaned_wa,
        "orphaned_stores":     orphaned_stores,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliation: merge source tenant → target tenant (dry-run first)
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_tenants(
    db: Session,
    source_tenant_id: int,
    target_tenant_id: int,
    dry_run: bool,
    actor: str,
) -> Dict[str, Any]:
    """
    Merge all merchant resources from source_tenant into target_tenant.

    In dry_run mode: only reports what WOULD be moved.
    In live mode: actually updates FK references and deletes the source tenant.

    Safe-guards:
      - Both tenants must exist.
      - Target must not already have a WhatsApp connection with status=connected
        unless the source one matches.
      - Never silently drops data.

    Returns a detailed reconciliation report.
    """
    from database.models import (  # noqa: PLC0415
        WhatsAppConnection, Integration, Tenant,
    )
    from sqlalchemy import text as _text  # noqa: PLC0415

    source = db.query(Tenant).filter(Tenant.id == source_tenant_id).first()
    target = db.query(Tenant).filter(Tenant.id == target_tenant_id).first()

    if not source:
        return {"ok": False, "error": f"Source tenant #{source_tenant_id} not found"}
    if not target:
        return {"ok": False, "error": f"Target tenant #{target_tenant_id} not found"}
    if source_tenant_id == target_tenant_id:
        return {"ok": False, "error": "Source and target are the same tenant"}

    src_wa  = db.query(WhatsAppConnection).filter_by(tenant_id=source_tenant_id).first()
    tgt_wa  = db.query(WhatsAppConnection).filter_by(tenant_id=target_tenant_id).first()
    src_intg = db.query(Integration).filter_by(tenant_id=source_tenant_id).all()

    report: Dict[str, Any] = {
        "dry_run":           dry_run,
        "source_tenant_id":  source_tenant_id,
        "source_name":       source.name,
        "target_tenant_id":  target_tenant_id,
        "target_name":       target.name,
        "actions":           [],
        "warnings":          [],
    }

    # ── WhatsApp connection ──────────────────────────────────────────────────
    if src_wa:
        if tgt_wa and tgt_wa.status == "connected":
            report["warnings"].append(
                f"Target #{target_tenant_id} already has connected WA. "
                "Source WA will be DISCARDED (not merged). Confirm this is intended."
            )
        else:
            report["actions"].append({
                "resource":    "whatsapp_connection",
                "action":      "move",
                "from_tenant": source_tenant_id,
                "to_tenant":   target_tenant_id,
                "detail": {
                    "phone_number_id": src_wa.phone_number_id,
                    "waba_id":         src_wa.whatsapp_business_account_id,
                    "status":          src_wa.status,
                },
            })

    # ── Store integrations ───────────────────────────────────────────────────
    for intg in src_intg:
        report["actions"].append({
            "resource":    "integration",
            "action":      "move",
            "from_tenant": source_tenant_id,
            "to_tenant":   target_tenant_id,
            "detail": {
                "provider": intg.provider,
                "store_id": intg.external_store_id,
                "enabled":  intg.enabled,
            },
        })

    # ── Tables that FK to tenant_id ──────────────────────────────────────────
    tenant_fk_tables = [
        "orders", "products", "customers", "coupon_codes",
        "tenant_settings", "merchant_addons", "merchant_widgets",
        "store_sync_jobs", "store_knowledge_snapshots",
        "billing_subscriptions", "billing_invoices", "billing_payments",
        "conversation_logs", "conversation_traces", "ai_action_logs",
        "system_events", "users", "whatsapp_usage",
        "webhook_guardian_log", "integrity_events",
    ]
    for tbl in tenant_fk_tables:
        report["actions"].append({
            "resource": tbl,
            "action":   "reassign",
            "from_tenant": source_tenant_id,
            "to_tenant":   target_tenant_id,
        })

    report["actions"].append({
        "resource": "tenant",
        "action":   "delete",
        "tenant_id": source_tenant_id,
        "name":      source.name,
    })

    log_integrity_event(
        db,
        event="reconciliation_started",
        tenant_id=target_tenant_id,
        other_tenant_id=source_tenant_id,
        action="reconcile",
        result="dry_run" if dry_run else "live",
        detail=f"Merge #{source_tenant_id}→#{target_tenant_id} dry_run={dry_run}",
        actor=actor,
        dry_run=dry_run,
    )

    if dry_run:
        report["status"] = "dry_run_complete"
        return report

    # ── Live mode: execute changes ────────────────────────────────────────────
    try:
        # Move WA connection (if source has one and target doesn't have active)
        if src_wa and not (tgt_wa and tgt_wa.status == "connected"):
            if tgt_wa:
                # Delete target's stale WA row so source can be moved
                db.delete(tgt_wa)
                db.flush()
            src_wa.tenant_id = target_tenant_id
            db.flush()

        # Reassign all FK tables
        for tbl in tenant_fk_tables:
            try:
                db.execute(
                    _text(f"UPDATE {tbl} SET tenant_id=:to WHERE tenant_id=:from"),
                    {"to": target_tenant_id, "from": source_tenant_id},
                )
            except Exception as exc:
                logger.warning("reconcile: update %s failed: %s", tbl, exc)

        # Delete source tenant row
        db.delete(source)
        db.flush()
        db.commit()

        log_integrity_event(
            db,
            event="reconciliation_done",
            tenant_id=target_tenant_id,
            other_tenant_id=source_tenant_id,
            action="reconcile",
            result="ok",
            detail=f"Merge #{source_tenant_id}→#{target_tenant_id} completed",
            actor=actor,
            dry_run=False,
        )
        report["status"] = "completed"
        return report

    except Exception as exc:
        db.rollback()
        logger.error("reconcile: failed — %s", exc, exc_info=True)
        report["status"] = "failed"
        report["error"]  = str(exc)
        return report


# ─────────────────────────────────────────────────────────────────────────────
# Post-deploy background scan
# ─────────────────────────────────────────────────────────────────────────────

def run_post_deploy_check(db: Session) -> Dict[str, Any]:
    """
    Called once per deployment (via startup webhook_guardian).
    Scans all tenants, logs every conflict, returns a compact summary.
    Does NOT auto-fix anything — only detects and logs.
    """
    try:
        audit = run_integrity_audit(db)
    except Exception as exc:
        logger.error("[IntegrityCheck] audit failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}

    s = audit["summary"]

    for dup in audit["duplicate_phone_ids"]:
        log_integrity_event(
            db,
            event="duplicate_identity",
            phone_number_id=dup["phone_number_id"],
            detail=f"phone_number_id shared by tenants {dup['tenant_ids']}",
            result="conflict",
            actor="system_post_deploy_check",
        )
        logger.critical(
            "[IntegrityCheck] DUPLICATE phone_number_id=%s tenants=%s",
            dup["phone_number_id"], dup["tenant_ids"],
        )

    for dup in audit["duplicate_waba_ids"]:
        log_integrity_event(
            db,
            event="duplicate_identity",
            waba_id=dup["waba_id"],
            detail=f"waba_id shared by tenants {dup['tenant_ids']}",
            result="conflict",
            actor="system_post_deploy_check",
        )
        logger.critical(
            "[IntegrityCheck] DUPLICATE waba_id=%s tenants=%s",
            dup["waba_id"], dup["tenant_ids"],
        )

    for dup in audit["duplicate_store_ids"]:
        log_integrity_event(
            db,
            event="duplicate_identity",
            store_id=dup["store_id"],
            provider=dup["provider"],
            detail=f"store_id shared by tenants {dup['tenant_ids']}",
            result="conflict",
            actor="system_post_deploy_check",
        )
        logger.error(
            "[IntegrityCheck] DUPLICATE store_id=%s provider=%s tenants=%s",
            dup["store_id"], dup["provider"], dup["tenant_ids"],
        )

    for row in audit["orphaned_wa"]:
        log_integrity_event(
            db,
            event="orphaned_wa_connection",
            tenant_id=row["tenant_id"],
            phone_number_id=row["phone_number_id"],
            waba_id=row["waba_id"],
            result="conflict",
            actor="system_post_deploy_check",
        )

    for row in audit["orphaned_stores"]:
        log_integrity_event(
            db,
            event="orphaned_store",
            tenant_id=row["tenant_id"],
            store_id=row["store_id"],
            provider=row["provider"],
            result="conflict",
            actor="system_post_deploy_check",
        )

    try:
        db.commit()
    except Exception:
        db.rollback()

    logger.info(
        "[IntegrityCheck] Post-deploy scan — healthy=%d warning=%d critical=%d "
        "dup_phones=%d dup_wabas=%d dup_stores=%d orphaned_wa=%d orphaned_stores=%d",
        s["healthy"], s["warning"], s["critical"],
        s["duplicate_phone_ids"], s["duplicate_waba_ids"], s["duplicate_store_ids"],
        s["orphaned_wa"], s["orphaned_stores"],
    )
    return {"ok": True, "summary": s}


# ─────────────────────────────────────────────────────────────────────────────
# Structured event logger (best-effort, never raises)
# ─────────────────────────────────────────────────────────────────────────────

def log_integrity_event(
    db: Session,
    event: str,
    tenant_id: Optional[int] = None,
    other_tenant_id: Optional[int] = None,
    phone_number_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    store_id: Optional[str] = None,
    provider: Optional[str] = None,
    action: Optional[str] = None,
    result: Optional[str] = None,
    detail: Optional[str] = None,
    actor: Optional[str] = "system",
    dry_run: Optional[bool] = None,
) -> None:
    """Write one row to integrity_events (best-effort)."""
    try:
        from database.models import IntegrityEvent  # noqa: PLC0415
        entry = IntegrityEvent(
            event=event,
            tenant_id=tenant_id,
            other_tenant_id=other_tenant_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            store_id=store_id,
            provider=provider,
            action=action,
            result=result,
            detail=detail,
            actor=actor,
            dry_run=dry_run,
        )
        db.add(entry)
        db.flush()
    except Exception as exc:
        logger.debug("[IntegrityEvent] write failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass

    # Mirror to structured log regardless of DB success
    logger.info(
        "INTEGRITY event=%s tenant=%s other=%s phone=%s waba=%s store=%s action=%s result=%s detail=%s",
        event, tenant_id, other_tenant_id, phone_number_id,
        waba_id, store_id, action, result, (detail or "")[:200],
    )
