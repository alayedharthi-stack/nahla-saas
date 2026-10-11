"""
Provider-owned shop cleanup scaffolding — blocked until a policy is approved.

Two pieces, neither reachable from a route or a scheduler:

``delete_shop_mirror``
    Deletes the catalog mirror, staged records and import runs of exactly one
    ``(connection_id, tenant_id)`` — every statement is filtered by both, in
    foreign-key-safe order. It never touches ``products`` /
    ``product_variants`` (shared hub, Salla), ``tenants``, ``users``,
    ``integrations``, ``shopify_connections`` (the ownership tombstone), the
    connection audit, uninstall events, OAuth states, leases or privacy
    receipts, and never another connection or tenant. The caller holds the
    connection row lock and commits. The PostgreSQL proofs exercise it on
    disposable fixtures only.

``execute_shop_redaction``
    Operator-only. Refuses unless ``NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED``
    is on **and** an ``ApprovedErasurePolicy`` whose scope is exactly the
    supported ``{catalog_mirror}`` is passed (any extra scope refuses). This slice ships no approved policy
    (``approved_policy()`` returns ``None``): the owner, legal and business
    decisions listed in ``docs/engineering/shopify-privacy-webhooks.md`` come
    first, and a policy is a reviewed code change. Under the policy it
    re-verifies everything under the connection lock (lock order connection →
    request): the request is ``blocked_policy`` with no newer delivery since
    its evaluation, the connection is still Shopify-confirmed ``uninstalled``
    at the evaluated generation with the exact signed identity, and no
    authorization, credential call or import is in flight; and the evidence
    that made the proposal safe is re-checked fresh in the same transaction:
    the request's audit version is still the current one, the audited schema
    still equals its pinned snapshot and the legacy Shopify footprint is still
    empty. Any refusal rolls back: no row is deleted and the request stays
    open in ``blocked_policy``. A reinstall that
    commits first is therefore refused; one that starts later waits for this
    transaction and builds a new generation. Completion records the deleted
    counts and the policy's retained categories, and states explicitly that
    only the approved mirror scope was executed, that legal fulfilment is not
    asserted and that retained obligations stay pending owner policy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, FrozenSet, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.shopify_connection.models import STATUS_UNINSTALLED, ShopifyConnection
from services.shopify_privacy import audit
from services.shopify_privacy.config import AUDIT_VERSION, destructive_enabled
from services.shopify_privacy.models import (
    STATE_BLOCKED_POLICY,
    STATE_COMPLETED,
    TOPIC_SHOP_REDACT,
    ShopifyPrivacyRequest,
)
from services.shopify_privacy.store import event, utcnow

logger = logging.getLogger("nahla.shopify_privacy")

SCOPE_CATALOG_MIRROR = "catalog_mirror"
# The only scope this helper can execute. A policy naming anything else is
# refused as a whole: nothing outside the mirror is ever labelled deleted.
SUPPORTED_SCOPES = frozenset({SCOPE_CATALOG_MIRROR})
_OPERATOR_REF_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{0,63}")

C_DESTRUCTIVE_DISABLED = "destructive_disabled"
C_POLICY_NOT_APPROVED = "policy_not_approved"
C_POLICY_SCOPE_UNSUPPORTED = "policy_scope_unsupported"
C_OPERATOR_REF_INVALID = "operator_ref_invalid"
C_NOT_FOUND = "not_found"
C_NOT_BLOCKED_POLICY = "not_blocked_policy"
C_REDELIVERED = "redelivered_since_evaluation"
C_CONNECTION_CHANGED = "connection_changed"
C_IN_FLIGHT = "in_flight_lifecycle"
C_AUDIT_VERSION_CHANGED = "audit_version_changed"
C_AUDIT_SCHEMA_CHANGED = "audit_schema_changed"
C_LEGACY_FOOTPRINT = "legacy_footprint_present"


class ExecutionRefused(Exception):
    """A refusal with a fixed code; nothing was changed."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ApprovedErasurePolicy:
    """An owner-approved erasure policy (a reviewed code change, never input)."""

    version: str
    approved_by: str
    scope: FrozenSet[str]
    retained: Mapping[str, str] = field(default_factory=dict)


# No erasure policy has been approved. Keep it None until the owner decisions
# in docs/engineering/shopify-privacy-webhooks.md are made and reviewed.
APPROVED_POLICY: Optional[ApprovedErasurePolicy] = None


def approved_policy() -> Optional[ApprovedErasurePolicy]:
    return APPROVED_POLICY


_RUNS_OF = "SELECT r.id FROM shopify_catalog_import_runs r WHERE r.connection_id = :c AND r.tenant_id = :t"


def delete_shop_mirror(db: Session, *, connection_id: int, tenant_id: int) -> Dict[str, int]:
    """Delete one (connection, tenant)'s provider-owned mirror rows; counts per
    table. The caller holds the connection lock and commits."""
    p = {"c": int(connection_id), "t": int(tenant_id)}
    deleted: Dict[str, int] = {}
    for table in audit.MIRROR_DELETE_ORDER:
        if table == "shopify_catalog_staged_records":
            sql = f"DELETE FROM {table} WHERE run_id IN ({_RUNS_OF})"
        else:
            sql = f"DELETE FROM {table} WHERE connection_id = :c AND tenant_id = :t"
        deleted[table] = int(db.execute(text(sql), p).rowcount or 0)
    return deleted


def connection_lock_statement(connection_id: int, tenant_id: int):
    """``SELECT … FOR NO KEY UPDATE`` of the one connection row
    (SQLAlchemy: ``key_share=True`` with ``read=False``; ``read=True`` would be
    the much weaker FOR KEY SHARE). It conflicts with a reinstall's claim and
    an uninstall (FOR UPDATE) and with the catalog fence (FOR SHARE), so none
    of them interleaves with the deletion, but it does not block inserts whose
    foreign key references the row (an authorization start's audit row takes
    FOR KEY SHARE). Ownership keys are never changed here."""
    return (select(ShopifyConnection)
            .where(ShopifyConnection.id == int(connection_id), ShopifyConnection.tenant_id == int(tenant_id))
            .with_for_update(key_share=True))


def _refuse(db: Session, code: str) -> ExecutionRefused:
    db.rollback()
    logger.warning("[SHOPIFY_PRIVACY] execution refused code=%s", code)
    return ExecutionRefused(code)


def execute_shop_redaction(
    db: Session,
    *,
    request_id: int,
    policy: Optional[ApprovedErasurePolicy],
    operator_ref: str,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Erase the approved provider-owned scope of one blocked shop redaction.
    Returns the deleted counts; raises ExecutionRefused otherwise."""
    current = now or utcnow()
    if not destructive_enabled(env):
        raise _refuse(db, C_DESTRUCTIVE_DISABLED)
    if not isinstance(policy, ApprovedErasurePolicy) or not policy.version or not policy.approved_by:
        raise _refuse(db, C_POLICY_NOT_APPROVED)
    if frozenset(policy.scope) != SUPPORTED_SCOPES:
        raise _refuse(db, C_POLICY_SCOPE_UNSUPPORTED)
    if not isinstance(operator_ref, str) or not _OPERATOR_REF_RE.fullmatch(operator_ref):
        raise _refuse(db, C_OPERATOR_REF_INVALID)
    head = db.get(ShopifyPrivacyRequest, int(request_id), populate_existing=True)
    if head is None or head.topic != TOPIC_SHOP_REDACT or head.connection_id is None:
        raise _refuse(db, C_NOT_FOUND)
    connection_id, tenant_id = int(head.connection_id), int(head.tenant_id)
    db.rollback()
    # Lock order: connection → request (as everywhere).
    conn = db.execute(
        connection_lock_statement(connection_id, tenant_id).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    request = db.execute(
        select(ShopifyPrivacyRequest).where(ShopifyPrivacyRequest.id == int(request_id))
        .with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if request is None or request.state != STATE_BLOCKED_POLICY:
        raise _refuse(db, C_NOT_BLOCKED_POLICY)
    if int(request.delivery_count) != int(request.evaluated_delivery_count or 0):
        raise _refuse(db, C_REDELIVERED)
    if (conn is None or conn.status != STATUS_UNINSTALLED or int(conn.generation) != int(request.eligible_generation)
            or conn.shop_domain != request.shop_domain or conn.shop_gid != request.shop_gid
            or int(request.connection_id) != connection_id or int(request.tenant_id) != tenant_id):
        raise _refuse(db, C_CONNECTION_CHANGED)
    if audit.in_flight_lifecycle(db, connection_id=connection_id, shop_domain=conn.shop_domain, now=current):
        raise _refuse(db, C_IN_FLIGHT)
    # Fresh evidence, not the proposal's: an assessment is only as current as
    # the audit version, schema and legacy footprint it was made against.
    if request.audit_version != AUDIT_VERSION:
        raise _refuse(db, C_AUDIT_VERSION_CHANGED)
    if audit.schema_drift(db):
        raise _refuse(db, C_AUDIT_SCHEMA_CHANGED)
    footprint = audit.legacy_footprint(db, tenant_id=tenant_id, shop_domain=conn.shop_domain,
                                       shop_id=int(conn.shop_gid.rsplit("/", 1)[1]))
    if audit.footprint_blocks(footprint):
        raise _refuse(db, C_LEGACY_FOOTPRINT)
    deleted = delete_shop_mirror(db, connection_id=connection_id, tenant_id=tenant_id)
    request.state = STATE_COMPLETED
    request.reason = None
    request.completed_at = current
    request.policy_version = policy.version
    request.completion_evidence = {
        "audit_version": AUDIT_VERSION,
        "executed_scope": "catalog_mirror_only",
        "deleted": deleted,
        "legal_fulfillment": "not_asserted",
        "retained_obligations": "pending_owner_policy",
        "retained_by_policy": dict(policy.retained),
        "operator_ref": operator_ref,
        "connection_generation": int(conn.generation),
    }
    request.updated_at = current
    event(db, request, "executed", detail={"deleted": deleted, "policy_version": policy.version})
    db.commit()
    logger.info("[SHOPIFY_PRIVACY] shop redaction executed request=%s tenant=%s", request_id, tenant_id)
    return deleted
