"""
Evaluation worker for durable privacy requests (dormant by default).

Nothing here erases anything, calls Shopify or answers anyone. A due request
is evaluated against our own evidence and moves to one of the open outcome
states; only the operator-only, policy-gated executor (``cleanup.py``) can
complete a request, and no scheduler or route calls it.

Concurrency
  * One row per claim: ``FOR UPDATE SKIP LOCKED`` and a short
    ``lease_id`` / ``lease_expires_at``; a crashed worker's lease expires and
    the row becomes due again.
  * The evaluation reads without locks, then writes in one transaction that
    takes the connection row first (``FOR UPDATE`` when a revalidation probe
    is requested, else ``FOR SHARE``) and the request row second — the
    foundation and the catalog mirror lock the connection first too, and the
    intake locks only the request, so no cycle exists.
  * Write-time fences: the lease must still be ours against the write-time
    clock; the request's ``delivery_count`` must equal the one evaluated (a
    newer delivery of the same body, possibly after a reinstall, sends the
    request back to the queue instead of being closed away by stale work);
    the connection's lifecycle fingerprint (status, generation, credential
    and reconcile versions, revalidation flag) must equal the one evaluated.

Decisions (never from an unsigned header, delivery id or timestamp)
  * identity conflict / changed → ``manual_review``;
  * audited-schema drift, any legacy Shopify footprint or an unavailable
    legacy check → ``manual_review`` (never a broad "no data" outcome);
  * unknown shop identity (no ownership row for the signed domain + GID) →
    ``manual_review`` / ``unknown_shop_identity`` for every topic: the
    obligation stays open with its deadline and no "no data" outcome is
    recorded; once a connection owns the exact signed identity, the
    reconsideration pass makes the request due again (attribution only ever
    to that one ownership row — nothing is rebound);
  * matched customer request otherwise → ``assessed``: no structured Shopify
    customer record exists in the audited product-only schema — catalog
    free text, generic-customer mapping and legal fulfilment stay open;
  * ``shop/redact`` on a matched shop:
      - active / quarantined at the generation observed by the latest
        delivery → one foundation revalidation probe covering that delivery,
        then ``awaiting_evidence``; a probe that retained the credential →
        ``manual_review`` / ``active_installation_retained`` (erasing the
        current installation is unsafe; the request stays open with its
        deadline — it is not "inapplicable" or fulfilled);
      - the latest delivery predates the current installation (a reinstall,
        or a first install after a delivery received for an unknown shop) →
        ``manual_review`` / ``installed_after_last_delivery``;
      - Shopify-confirmed ``uninstalled`` (the tombstone bumps the
        generation once, so the latest delivery must have been observed at
        that generation or the one just before it), nothing in flight →
        ``blocked_policy`` with a counts-only proposal;
      - disconnected / re-authorization required → ``manual_review``.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session

from services.shopify_connection import lifecycle
from services.shopify_connection.models import (
    STATUS_ACTIVE,
    STATUS_QUARANTINED,
    STATUS_UNINSTALLED,
    ShopifyConnection,
)
from services.shopify_privacy import audit
from services.shopify_privacy.config import (
    AUDIT_VERSION,
    WORKER_BACKOFF_BASE_SECONDS,
    WORKER_BACKOFF_CAP_SECONDS,
    WORKER_BATCH_LIMIT,
    WORKER_LEASE_SECONDS,
    WORKER_MAX_EVIDENCE_ATTEMPTS,
    WORKER_TICK_SECONDS,
    worker_enabled,
)
from services.shopify_privacy.models import (
    CUSTOMER_TOPICS,
    DUE_STATES,
    IDENTITY_CONFLICT,
    IDENTITY_MATCHED,
    IDENTITY_UNKNOWN,
    STATE_ASSESSED,
    STATE_AWAITING_EVIDENCE,
    STATE_BLOCKED_POLICY,
    STATE_MANUAL_REVIEW,
    STATE_RECEIVED,
    ShopifyPrivacyRequest,
)
from services.shopify_privacy.store import Identity, event, resolve_identity, snapshot, tables_present, utcnow

logger = logging.getLogger("nahla.shopify_privacy")

# Fixed reason codes.
R_IDENTITY_CONFLICT = "identity_conflict"
R_IDENTITY_CHANGED = "identity_changed"
R_LEGACY_PRESENT = "legacy_shopify_data_present"
R_LEGACY_UNAVAILABLE = "legacy_audit_unavailable"
R_UNKNOWN_SHOP = "unknown_shop_identity"
R_IDENTITY_RESOLVABLE = "identity_resolvable"
# Bounded claim: the audited product-only Shopify schema holds no structured
# customer record (see audit.AUDITED_SCOPE for what stays unassessed).
R_NO_CUSTOMER_RESOURCES = "no_structured_customer_records_in_audited_scope"
R_AWAITING_UNINSTALL = "awaiting_uninstall_evidence"
R_ACTIVE_RETAINED = "active_installation_retained"
R_REINSTALLED = "installed_after_last_delivery"
R_NO_CONFIRMED_UNINSTALL = "no_confirmed_uninstall"
R_EVIDENCE_TIMEOUT = "uninstall_evidence_timeout"
R_IN_FLIGHT_TIMEOUT = "in_flight_timeout"
R_POLICY_NOT_APPROVED = "destructive_policy_not_approved"

OUTCOME_LEASE_LOST = "lease_lost"
OUTCOME_REQUEUED = "requeued"


def wall_clock() -> datetime:
    """The clock lease fences are checked against at write time (a seam for tests)."""
    return utcnow()


def _fence_now(now: datetime) -> datetime:
    return max(now, wall_clock())


def backoff_seconds(attempts: int) -> int:
    # The exponent is clamped before it is computed (as the foundation's recovery does).
    exponent = min(max(0, int(attempts) - 1), 16)
    return int(min(WORKER_BACKOFF_CAP_SECONDS, WORKER_BACKOFF_BASE_SECONDS * (2 ** exponent)))


@dataclass
class Decision:
    state: str
    reason: str
    proposal: Optional[Dict[str, Any]] = field(default=None, repr=False)
    request_probe: bool = False
    eligible_generation: Optional[int] = None


def _manual(reason: str, proposal: Optional[Dict[str, Any]] = None) -> Decision:
    return Decision(STATE_MANUAL_REVIEW, reason, proposal)


def _awaiting(reason: str, attempts: int, timeout_reason: str, *, request_probe: bool = False) -> Decision:
    if not request_probe and attempts >= WORKER_MAX_EVIDENCE_ATTEMPTS:
        return _manual(timeout_reason)
    return Decision(STATE_AWAITING_EVIDENCE, reason, request_probe=request_probe)


def _legacy_reason(footprint: Dict[str, Any]) -> str:
    return R_LEGACY_UNAVAILABLE if footprint.get("unavailable") else R_LEGACY_PRESENT


def decide(db: Session, request: ShopifyPrivacyRequest, identity: Identity, now: datetime) -> Decision:
    """The next state of a due request from current evidence (read-only)."""
    if identity.state == IDENTITY_CONFLICT:
        return _manual(R_IDENTITY_CONFLICT)
    if request.connection_id is not None and identity.connection_id != request.connection_id:
        return _manual(R_IDENTITY_CHANGED)
    drift = audit.schema_drift(db)
    if drift:
        return _manual(drift)
    footprint = audit.legacy_footprint(db, tenant_id=identity.tenant_id, shop_domain=request.shop_domain,
                                       shop_id=int(request.shop_gid.rsplit("/", 1)[1]))
    blocked = audit.footprint_blocks(footprint)
    if identity.state == IDENTITY_UNKNOWN:
        # No ownership row for the exact signed identity: no "no data"
        # outcome; counts only, for the reviewer.
        rows = audit.shop_rows(db, shop_domain=request.shop_domain, shop_gid=request.shop_gid)
        return _manual(R_UNKNOWN_SHOP, audit.shop_assessment(footprint=footprint, rows=rows))
    if request.topic in CUSTOMER_TOPICS:
        proposal = audit.customer_assessment(topic=request.topic, footprint=footprint, matched=True)
        if blocked:
            return _manual(_legacy_reason(footprint), proposal)
        return Decision(STATE_ASSESSED, R_NO_CUSTOMER_RESOURCES, proposal)

    conn = identity.connection
    observed = request.last_delivery_generation
    attempts = int(request.attempts)
    if conn.status == STATUS_UNINSTALLED:
        # The confirmed-uninstall tombstone bumped the generation once: the
        # latest delivery belongs to this installation only if it was observed
        # at the tombstone's generation or the active one just before it.
        if observed is None or observed < conn.generation - 1:
            return _manual(R_REINSTALLED)
        flight = audit.in_flight_lifecycle(db, connection_id=conn.id, shop_domain=conn.shop_domain, now=now)
        if flight:
            return _awaiting(flight, attempts, R_IN_FLIGHT_TIMEOUT)
        proposal = audit.shop_redaction_inventory(db, connection_id=conn.id, tenant_id=conn.tenant_id,
                                                  shop_domain=conn.shop_domain, shop_gid=conn.shop_gid,
                                                  footprint=footprint)
        if blocked:
            return _manual(_legacy_reason(footprint), proposal)
        return Decision(STATE_BLOCKED_POLICY, R_POLICY_NOT_APPROVED, proposal, eligible_generation=conn.generation)
    if conn.status in (STATUS_ACTIVE, STATUS_QUARANTINED):
        if observed is None or observed < conn.generation:
            return _manual(R_REINSTALLED)
        if request.probe_delivery_count is None or int(request.probe_delivery_count) < int(request.delivery_count):
            # Every newer delivery needs its own probe: an earlier retained
            # probe never covers a later delivery.
            return _awaiting(R_AWAITING_UNINSTALL, attempts, R_EVIDENCE_TIMEOUT, request_probe=True)
        if (conn.status == STATUS_ACTIVE and not conn.revalidation_pending
                and conn.reconcile_request_version >= int(request.probe_reconcile_version)):
            # The foundation clears a revalidation only through a retained
            # probe that covered every request up to the current version.
            return _manual(R_ACTIVE_RETAINED)
        return _awaiting(R_AWAITING_UNINSTALL, attempts, R_EVIDENCE_TIMEOUT)
    return _manual(R_NO_CONFIRMED_UNINSTALL)


# ── Reconsideration of unknown identities ─────────────────────────────────────

def reopen_resolvable_unknowns(db: Session, *, now: datetime, limit: int = WORKER_BATCH_LIMIT) -> int:
    """Make due again the requests held for an unknown shop identity whose
    signed domain or GID is now owned by a connection row. The worker then
    re-resolves the exact identity (matched, or conflict → review); nothing
    is attributed or rebound here. Commits; returns the number reopened."""
    ids = db.execute(text(
        "SELECT r.id FROM shopify_privacy_requests r"
        " WHERE r.state = :state AND r.reason = :reason AND EXISTS ("
        "  SELECT 1 FROM shopify_connections c WHERE c.shop_domain = r.shop_domain OR c.shop_gid = r.shop_gid)"
        " ORDER BY r.id LIMIT :limit FOR UPDATE OF r SKIP LOCKED"),
        {"state": STATE_MANUAL_REVIEW, "reason": R_UNKNOWN_SHOP, "limit": int(limit)}).scalars().all()
    for request_id in ids:
        request = db.get(ShopifyPrivacyRequest, request_id)
        request.state = STATE_RECEIVED
        request.reason = R_IDENTITY_RESOLVABLE
        request.next_attempt_at = now
        request.updated_at = now
        event(db, request, "reconsidered")
    db.commit()
    return len(ids)


# ── Claim / apply ─────────────────────────────────────────────────────────────

def claim_next(db: Session, *, now: datetime) -> Optional[Tuple[int, str]]:
    """Lease the next due request (one row) and commit; None when none is due."""
    request_id = db.execute(
        select(ShopifyPrivacyRequest.id).where(
            ShopifyPrivacyRequest.state.in_(DUE_STATES),
            ShopifyPrivacyRequest.next_attempt_at <= now,
            or_(ShopifyPrivacyRequest.lease_expires_at.is_(None), ShopifyPrivacyRequest.lease_expires_at <= now),
        ).order_by(ShopifyPrivacyRequest.next_attempt_at.asc(), ShopifyPrivacyRequest.id.asc())
        .limit(1).with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if request_id is None:
        db.rollback()
        return None
    lease = secrets.token_hex(16)
    db.execute(update(ShopifyPrivacyRequest).where(ShopifyPrivacyRequest.id == request_id).values(
        lease_id=lease, lease_expires_at=now + timedelta(seconds=WORKER_LEASE_SECONDS),
        attempts=ShopifyPrivacyRequest.attempts + 1, updated_at=now,
    ).execution_options(synchronize_session=False))
    db.commit()
    return int(request_id), lease


def _lease_holds(request: Optional[ShopifyPrivacyRequest], lease: str, now: datetime) -> bool:
    return (request is not None and request.state in DUE_STATES and request.lease_id == lease
            and request.lease_expires_at is not None and request.lease_expires_at > _fence_now(now))


def _request_revalidation(db: Session, conn: ShopifyConnection, now: datetime) -> int:
    """Ask the foundation for one revalidation probe of the current credential,
    exactly as a duplicate uninstall delivery does (durable reconcile request,
    version bump; ownership untouched). Caller holds the row lock and commits."""
    if conn.status == STATUS_ACTIVE and conn.revalidation_requested_at is None:
        conn.revalidation_requested_at = now
    lifecycle._request_reconcile(conn, now)  # noqa: SLF001 — the foundation's own durable schedule
    conn.updated_at = now
    lifecycle._audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain,  # noqa: SLF001
                     action="privacy_shop_redact_probe_requested", connection=conn,
                     detail={"status": conn.status})
    return int(conn.reconcile_request_version)


def _requeue(db: Session, request: ShopifyPrivacyRequest, now: datetime, cause: str) -> str:
    request.lease_id = None
    request.lease_expires_at = None
    request.next_attempt_at = now
    request.updated_at = now
    event(db, request, "requeued", detail={"cause": cause})
    db.commit()
    return OUTCOME_REQUEUED


def apply_decision(db: Session, *, request_id: int, lease: str, evaluated_count: int, identity: Identity,
                   decision: Decision, now: datetime) -> str:
    conn: Optional[ShopifyConnection] = None
    if identity.state == IDENTITY_MATCHED:
        stmt = select(ShopifyConnection).where(ShopifyConnection.id == identity.connection_id)
        stmt = stmt.with_for_update() if decision.request_probe else stmt.with_for_update(read=True)
        conn = db.execute(stmt.execution_options(populate_existing=True)).scalar_one_or_none()
    request = db.execute(
        select(ShopifyPrivacyRequest).where(ShopifyPrivacyRequest.id == request_id)
        .with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not _lease_holds(request, lease, now):
        db.rollback()
        return OUTCOME_LEASE_LOST
    if int(request.delivery_count) != evaluated_count:
        return _requeue(db, request, now, "redelivered_during_evaluation")
    # The identity evaluated must still be the identity now (a connect flow
    # may have committed an ownership row meanwhile, or a conflict appeared).
    current_identity = resolve_identity(db, request.shop_domain, request.shop_gid)
    if (current_identity.state != identity.state
            or current_identity.connection_id != identity.connection_id):
        return _requeue(db, request, now, "identity_changed_during_evaluation")
    if identity.state == IDENTITY_MATCHED and (
            conn is None or snapshot(conn).fingerprint() != identity.connection.fingerprint()):
        return _requeue(db, request, now, "evidence_changed_during_evaluation")
    if identity.state == IDENTITY_MATCHED and request.connection_id is None:
        request.identity_state = IDENTITY_MATCHED
        request.tenant_id = identity.tenant_id
        request.connection_id = identity.connection_id
    if decision.request_probe:
        request.probe_reconcile_version = _request_revalidation(db, conn, now)
        request.probe_requested_at = now
        request.probe_delivery_count = int(request.delivery_count)
    request.state = decision.state
    request.reason = decision.reason
    request.lease_id = None
    request.lease_expires_at = None
    request.next_attempt_at = (now + timedelta(seconds=backoff_seconds(request.attempts))
                               if decision.state in DUE_STATES else None)
    if decision.proposal is not None:
        request.proposed_action = decision.proposal
        request.audit_version = AUDIT_VERSION
        request.assessed_at = now
    if decision.state == STATE_BLOCKED_POLICY:
        request.eligible_generation = decision.eligible_generation
        request.evaluated_delivery_count = int(request.delivery_count)
    else:
        request.eligible_generation = None
        request.evaluated_delivery_count = None
    request.updated_at = now
    event(db, request, "evaluated", detail={"probe_requested": decision.request_probe})
    db.commit()
    return decision.state


def process(db: Session, *, request_id: int, lease: str, now: Optional[datetime] = None) -> str:
    """Evaluate one leased request and write the outcome under the fences."""
    current = now or utcnow()
    request = db.get(ShopifyPrivacyRequest, request_id, populate_existing=True)
    if not _lease_holds(request, lease, current):
        db.rollback()
        return OUTCOME_LEASE_LOST
    evaluated_count = int(request.delivery_count)
    identity = resolve_identity(db, request.shop_domain, request.shop_gid)
    decision = decide(db, request, identity, current)
    db.rollback()  # no lock is held between the evaluation and the fenced write
    return apply_decision(db, request_id=request_id, lease=lease, evaluated_count=evaluated_count,
                          identity=identity, decision=decision, now=current)


# ── Runner ────────────────────────────────────────────────────────────────────

def run_tick(
    *,
    session_factory: Optional[Callable[[], Session]] = None,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
    limit: int = WORKER_BATCH_LIMIT,
) -> Dict[str, Any]:
    """One pass. Does nothing (no database access) unless the worker flag is on."""
    if not worker_enabled(env):
        return {"skipped": "disabled"}
    if session_factory is None:
        from core.database import SessionLocal as session_factory  # noqa: PLC0415,N813
    db = session_factory()
    counts: Dict[str, Any] = {"claimed": 0}
    try:
        if not tables_present(db):
            return {"skipped": "storage_unavailable"}
        db.rollback()
        counts["reconsidered"] = reopen_resolvable_unknowns(db, now=now or utcnow(), limit=limit)
        for _ in range(int(limit)):
            claimed = claim_next(db, now=now or utcnow())
            if claimed is None:
                break
            counts["claimed"] += 1
            request_id, lease = claimed
            try:
                outcome = process(db, request_id=request_id, lease=lease, now=now)
            except Exception as exc:  # noqa: BLE001 — the lease expires and the row is retried
                db.rollback()
                logger.error("[SHOPIFY_PRIVACY] evaluation failed request=%s kind=%s", request_id,
                             type(exc).__name__)
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts
    finally:
        db.close()


async def run_worker_scheduler() -> None:
    """Periodic pass. Each tick re-checks the flag first."""
    logger.info("[SHOPIFY_PRIVACY] evaluation worker started — every %ss", WORKER_TICK_SECONDS)
    while True:
        try:
            summary = await asyncio.to_thread(run_tick)
            if summary.get("claimed"):
                logger.info("[SHOPIFY_PRIVACY] evaluation %s",
                            {k: v for k, v in summary.items() if isinstance(v, int)})
        except Exception as exc:  # noqa: BLE001
            logger.error("[SHOPIFY_PRIVACY] evaluation tick failed kind=%s", type(exc).__name__)
        await asyncio.sleep(WORKER_TICK_SECONDS)
