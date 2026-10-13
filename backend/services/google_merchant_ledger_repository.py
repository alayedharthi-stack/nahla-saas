"""Dormant PostgreSQL ledger adapter; callers own transactions and all I/O.

No connection provisioning, transport, clock, credential access or registration.
Binding/source-role evidence and credential epochs are activation blockers:
connection.version is an observation, NOT a durable credential epoch. This
module must not be wired to a sender until those contracts are implemented.
Plans and responses are trusted caller observations, never Google verification.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import text

from services.google_merchant_lifecycle_v1 import (
    ACTION_INSERT, ACTION_PATCH, ACTION_DELETE, LifecyclePlan, PlannedAction,
    OwnedOfferEvidence, PendingWrite, OfferScope, _integrity_reason,
)
from services.google_merchant_products_v1 import GoogleMerchantTarget
from services.google_merchant_protocol_v1 import (
    MerchantApiBinding, MerchantRequest, TransportResponse, canonical_json,
    request_from_snapshot, get_product_request, payload_fingerprint,
    parse_product_readback, readback_identity_state, READBACK_IDENTITY_MATCHES,
    attributes_match, classify_response, validate_binding, freshness_issue,
    RESP_RATE_LIMITED, RESP_PERMANENT_AUTH, RESP_PERMANENT_PERMISSION,
    RESP_PERMANENT_VALIDATION,
)

CONNECTIONS = "google_merchant_connections"
LEDGER = "google_merchant_offer_ledger"
_ACTIONS = {ACTION_INSERT: "insert", ACTION_PATCH: "patch", ACTION_DELETE: "delete"}
_OP_FIELDS = ("op_id", "op_action", "op_snapshot", "op_snapshot_hash", "op_idempotency_key", "op_generation")


class LedgerConflict(ValueError):
    """Scope, generation, evidence or idempotency conflict; no implicit takeover."""


@dataclass(frozen=True)
class OperationRef:
    tenant_id: int
    connection_id: int
    ledger_id: int
    op_id: str | None
    attempt_id: str | None
    lease_fence: int
    version: int


@dataclass(frozen=True)
class Dispatch:
    ref: OperationRef
    request: MerchantRequest
    connection_version: int  # advisory only; activation still blocked


def _ref(row) -> OperationRef:
    return OperationRef(row["tenant_id"], row["connection_id"], row["id"],
                        row["op_id"], row["attempt_id"], row["lease_fence"], row["version"])


def _now(conn) -> int:
    return int(conn.execute(text("SELECT floor(extract(epoch FROM clock_timestamp()))")).scalar_one())


def _transaction(conn):
    # Locks must survive until the caller commits/rolls back. SQLAlchemy may
    # expose a transaction even when the underlying psycopg driver autocommits.
    if not conn.in_transaction() or conn.connection.driver_connection.autocommit:
        raise LedgerConflict("caller_transaction_required")


def _connection(conn, tenant_id, connection_id, *, exclusive=False):
    _transaction(conn)
    row = conn.execute(text(f"SELECT id, tenant_id, account_id, data_source_id, content_language, feed_label, status, version FROM {CONNECTIONS} WHERE id=:id AND tenant_id=:tenant "
                            + ("FOR UPDATE" if exclusive else "FOR SHARE")),
                       {"id": connection_id, "tenant": tenant_id}).mappings().first()
    if row is None:
        raise LedgerConflict("connection_scope_missing")
    return row


def _locked(conn, ref):
    _transaction(conn)
    row = conn.execute(text(f"SELECT * FROM {LEDGER} WHERE id=:id AND tenant_id=:tenant "
                            "AND connection_id=:cid FOR UPDATE"),
                       {"id": ref.ledger_id, "tenant": ref.tenant_id, "cid": ref.connection_id}).mappings().first()
    if row is None or (row["op_id"], row["attempt_id"], row["lease_fence"], row["version"]) != (
            ref.op_id, ref.attempt_id, ref.lease_fence, ref.version):
        return None
    return row


def _update(conn, row, values):
    # Values are constructed internally; column names never come from a caller.
    params = dict(values, rid=row["id"], old_version=row["version"], tenant=row["tenant_id"], cid=row["connection_id"])
    assignments = ", ".join(f"{key}=:{key}" for key in values)
    return conn.execute(text(f"UPDATE {LEDGER} SET {assignments}, version=version+1, updated_at=clock_timestamp() "
                             "WHERE id=:rid AND tenant_id=:tenant AND connection_id=:cid AND version=:old_version "
                             "RETURNING *"), params).mappings().first()


def _scope(row):
    return OfferScope(row["tenant_id"], row["account_id"], row["data_source_id"],
                      row["content_language"], row["feed_label"], row["offer_id"])


def _binding(row):
    # Only for rebuilding a previously validated snapshot/read request. Does
    # not establish API source-role evidence, OAuth permission or freshness.
    return MerchantApiBinding(row["tenant_id"], row["account_id"], row["data_source_id"],
                              "API", row["content_language"], row["feed_label"], 0)


def _snapshot_action(row):
    frozen = json.loads(row["op_snapshot"])
    request = request_from_snapshot(canonical_json(frozen["request"]))
    action = PlannedAction(frozen["offer_id"], frozen["action"], (), request,
                           frozen["desired"], tuple(frozen["removed"]), row["op_snapshot"])
    if _integrity_reason(_binding(row), action):
        raise LedgerConflict("persisted_snapshot_invalid")
    return action


def idempotency_key_for(scope: OfferScope, generation: int) -> str:
    """Key for one exact identity/generation slot, not a global caller token.

    History is protected by the retained per-offer generation high-water mark.
    A key cannot be transferred to another identity or a later generation. The
    schema retains only the latest applied key, not an arbitrary key history.
    """
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise LedgerConflict("generation_invalid")
    identity = ["nahla.google-merchant.ledger.v1", scope.tenant_id, scope.account_id,
                scope.data_source_id, scope.content_language, scope.feed_label,
                scope.offer_id, generation]
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def admit(conn, connection_id: int, plan: LifecyclePlan, action: PlannedAction, *,
          op_id: str, idempotency_key: str, generation: int, max_age_s: int) -> OperationRef:
    """Admit one planner action; serialize account identity/casefold checks.

    Same unresolved operation/key/snapshot/generation is idempotent.
    Keys must identify the exact scope/generation slot. Completed generations
    remain reserved by the per-offer high-water mark; arbitrary global token
    history is not retained. A fresh generation requires its own derived key.
    A different operation never overwrites pending or uncertain work. No commit.
    """
    if (plan.binding is None or plan.binding_reasons or action not in plan.executable
            or plan.tenant_id != plan.binding.tenant_id):
        raise LedgerConflict("plan_not_executable")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise LedgerConflict("generation_invalid")
    if isinstance(max_age_s, bool) or not isinstance(max_age_s, int) or max_age_s < 1:
        raise LedgerConflict("freshness_policy_invalid")
    row = _connection(conn, plan.tenant_id, connection_id, exclusive=True)
    binding = plan.binding
    if row["status"] != "bound" or (row["account_id"], row["data_source_id"], row["content_language"], row["feed_label"]) != (
            binding.account_id, binding.data_source_id, binding.content_language, binding.feed_label):
        raise LedgerConflict("binding_scope_mismatch")
    now = _now(conn)
    target = GoogleMerchantTarget(plan.tenant_id, binding.content_language, binding.feed_label)
    if validate_binding(binding, target, now=now, max_age_s=max_age_s) or freshness_issue(plan.planned_at, now, max_age_s):
        raise LedgerConflict("plan_or_binding_not_fresh")
    if _integrity_reason(binding, action):
        raise LedgerConflict("action_integrity_failed")
    scope = OfferScope(plan.tenant_id, binding.account_id, binding.data_source_id,
                       binding.content_language, binding.feed_label, action.offer_id)
    if idempotency_key != idempotency_key_for(scope, generation):
        raise LedgerConflict("idempotency_scope_generation_mismatch")
    candidates = conn.execute(text(f"SELECT * FROM {LEDGER} WHERE account_id=:account "
                                   "AND content_language=:language AND feed_label=:label FOR UPDATE"),
                              {"account": binding.account_id, "language": binding.content_language,
                               "label": binding.feed_label}).mappings().all()
    matches = [r for r in candidates if r["offer_id"].casefold() == action.offer_id.casefold()]
    old = matches[0] if matches else None
    values = {"op_id": op_id, "op_action": _ACTIONS[action.action], "op_snapshot": action.snapshot,
              "op_snapshot_hash": hashlib.sha256(action.snapshot.encode("utf-8")).hexdigest(),
              "op_idempotency_key": idempotency_key, "op_generation": generation}
    if old is not None:
        if old["offer_id"] != action.offer_id or old["connection_id"] != connection_id or old["data_source_id"] != binding.data_source_id:
            raise LedgerConflict("identity_collision")
        if all(old[key] == values[key] for key in _OP_FIELDS):
            return _ref(old)
        if (old["op_state"] == "idle" and old["applied_idempotency_key"] == idempotency_key
                and old["applied_generation"] == generation):
            # Resolved operations have cleared snapshots; don't claim a new op
            # id or changed payload is the original admission.
            raise LedgerConflict("completed_key_retained")
        if old["op_state"] != "idle" or generation <= old["admitted_generation"]:
            raise LedgerConflict("unresolved_or_stale_generation")
        values.update(op_state="pending", admitted_generation=generation, attempts=0, readback_attempts=0,
                      dispatched_at=None, attempt_id=None,
                      ownership="claiming" if action.action == ACTION_INSERT else "owned")
        new = _update(conn, old, values)
    else:
        if action.action != ACTION_INSERT:
            raise LedgerConflict("ownership_evidence_missing")
        values.update(tenant_id=plan.tenant_id, connection_id=connection_id, account_id=binding.account_id,
                      data_source_id=binding.data_source_id, content_language=binding.content_language,
                      feed_label=binding.feed_label, offer_id=action.offer_id, ownership="claiming",
                      op_state="pending", admitted_generation=generation)
        columns = ", ".join(values)
        params = ", ".join(":" + key for key in values)
        new = conn.execute(text(f"INSERT INTO {LEDGER} ({columns}) VALUES ({params}) RETURNING *"), values).mappings().one()
    return _ref(new)


def claim(conn, ref: OperationRef, *, owner: str, attempt_id: str, lease_seconds: int) -> Dispatch | None:
    """Claim exactly one pending version, never take over an expired dispatch."""
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 300:
        raise LedgerConflict("lease_duration_invalid")
    connection = _connection(conn, ref.tenant_id, ref.connection_id)
    if connection["status"] != "bound":
        return None
    row = _locked(conn, ref)
    if row is None or row["op_state"] != "pending":
        return None
    action = _snapshot_action(row)
    new = conn.execute(text(f"UPDATE {LEDGER} SET op_state='dispatching', lease_owner=:owner, "
                            "attempt_id=:attempt, lease_expires_at=clock_timestamp()+make_interval(secs=>:seconds), "
                            "lease_fence=lease_fence+1, attempts=attempts+1, version=version+1, updated_at=clock_timestamp() "
                            "WHERE id=:id AND version=:version AND (next_attempt_at IS NULL OR next_attempt_at<=clock_timestamp()) "
                            "RETURNING *"), {"owner": owner, "attempt": attempt_id, "seconds": lease_seconds,
                                             "id": row["id"], "version": row["version"]}).mappings().first()
    return None if new is None else Dispatch(_ref(new), action.request, connection["version"])


def record_outcome(conn, ref: OperationRef, response: TransportResponse, *, retry_delay_s: int) -> OperationRef | None:
    """2xx is not confirmation. Expired attempts cannot authorize any resend."""
    if isinstance(retry_delay_s, bool) or not isinstance(retry_delay_s, int) or not 1 <= retry_delay_s <= 86400:
        raise LedgerConflict("retry_delay_invalid")
    row = _locked(conn, ref)
    if row is None or row["op_state"] != "dispatching":
        return None
    # A transport error makes write delivery uncertain even if a partial or
    # contradictory response also carries an HTTP status. It cannot grant
    # resend permission or definitively close an ownership claim.
    effective = TransportResponse(None, error=response.error) if response.error is not None else response
    classification = classify_response(effective, write=True, now=_now(conn))
    kind = classification.kind
    # Check the real database clock with the row locked, not transaction now().
    live = conn.execute(text("SELECT :expiry > clock_timestamp()"), {"expiry": row["lease_expires_at"]}).scalar_one()
    values = {"op_state": "readback_required", "lease_owner": None, "lease_expires_at": None,
              "last_outcome": kind, "last_error_code": None, "next_attempt_at": None}
    if live and kind == RESP_RATE_LIMITED:
        delay = max(retry_delay_s, classification.retry_after_s or 0)
        due = conn.execute(text("SELECT clock_timestamp()+make_interval(secs=>:seconds)"), {"seconds": delay}).scalar_one()
        values.update(op_state="pending", next_attempt_at=due)
    elif live and kind in (RESP_PERMANENT_AUTH, RESP_PERMANENT_PERMISSION, RESP_PERMANENT_VALIDATION):
        values.update(op_state="rejected", last_error_code=kind)
    new = _update(conn, row, values)
    return _ref(new)


def reconcile_expired(conn, ref: OperationRef) -> OperationRef | None:
    """Crash recovery is read-back only; it never grants another send."""
    row = _locked(conn, ref)
    if row is None or row["op_state"] != "dispatching":
        return None
    expired = conn.execute(text("SELECT :expiry <= clock_timestamp()"), {"expiry": row["lease_expires_at"]}).scalar_one()
    if not expired:
        return None
    new = _update(conn, row, {"op_state": "readback_required", "lease_owner": None, "lease_expires_at": None})
    return _ref(new)


def record_readback(conn, ref: OperationRef, request: MerchantRequest, response: TransportResponse) -> OperationRef | None:
    """Caller-observed products.get only; never confirm from a list page or 2xx write."""
    row = _locked(conn, ref)
    if row is None or row["op_state"] not in ("readback_required", "unresolved"):
        return None
    action = _snapshot_action(row)
    binding = _binding(row)
    if request != get_product_request(binding, row["offer_id"]):
        raise LedgerConflict("readback_request_scope_mismatch")
    parsed = parse_product_readback(response.body)
    valid_status = isinstance(response.status, int) and not isinstance(response.status, bool) and response.error is None
    confirmed = (valid_status and action.action == ACTION_DELETE and response.status == 404)
    if valid_status and action.action != ACTION_DELETE and response.status == 200:
        confirmed = (not parsed.malformed and readback_identity_state(parsed, binding, row["offer_id"]) == READBACK_IDENTITY_MATCHES
                     and attributes_match(action.desired_attributes or {}, parsed.product_attributes, action.removed_attributes))
    values = {"op_state": "unresolved", "readback_attempts": row["readback_attempts"] + 1}
    if confirmed:
        attributes = {} if action.action == ACTION_DELETE else action.desired_attributes
        values.update({key: None for key in _OP_FIELDS})
        values.update(op_state="idle", ownership="released" if action.action == ACTION_DELETE else "owned",
                      applied_attributes=canonical_json(attributes), applied_fingerprint=payload_fingerprint(attributes),
                      applied_generation=row["op_generation"], applied_idempotency_key=row["op_idempotency_key"],
                      publication_state="unknown" if action.action == ACTION_DELETE else parsed.publication_state,
                      observed_at=conn.execute(text("SELECT clock_timestamp()")).scalar_one())
        # JSONB needs an explicit cast with text() parameters.
        params = dict(values, rid=row["id"], old_version=row["version"])
        assigns = ", ".join(f"{k}=" + (f"CAST(:{k} AS jsonb)" if k == "applied_attributes" else f":{k}") for k in values)
        new = conn.execute(text(f"UPDATE {LEDGER} SET {assigns}, version=version+1, updated_at=clock_timestamp() "
                                "WHERE id=:rid AND version=:old_version RETURNING *"), params).mappings().one()
    else:
        new = _update(conn, row, values)
    return _ref(new)


def close_rejected(conn, ref: OperationRef) -> OperationRef | None:
    """Retain definitive rejection evidence while releasing an unconfirmed claim."""
    row = _locked(conn, ref)
    if row is None or row["op_state"] != "rejected":
        return None
    values = {key: None for key in _OP_FIELDS}
    values.update(op_state="idle", ownership="released" if row["ownership"] == "claiming" else row["ownership"])
    return _ref(_update(conn, row, values))


def operation_ref(conn, *, tenant_id: int, connection_id: int, ledger_id: int) -> OperationRef | None:
    """Reload scoped durable work after restart; never use it for an old response."""
    _connection(conn, tenant_id, connection_id)
    row = conn.execute(text(f"SELECT * FROM {LEDGER} WHERE tenant_id=:tenant AND connection_id=:cid AND id=:id"),
                       {"tenant": tenant_id, "cid": connection_id, "id": ledger_id}).mappings().first()
    return None if row is None else _ref(row)


def planner_records(conn, *, tenant_id: int, connection_id: int):
    """Return detached ownership/pending records, scoped even after disable."""
    _connection(conn, tenant_id, connection_id)
    rows = conn.execute(text(f"SELECT * FROM {LEDGER} WHERE tenant_id=:tenant AND connection_id=:cid ORDER BY id"),
                        {"tenant": tenant_id, "cid": connection_id}).mappings().all()
    owned, pending = [], []
    for row in rows:
        if row["ownership"] == "owned" and row["observed_at"] is not None:
            owned.append(OwnedOfferEvidence(_scope(row), row["applied_attributes"], int(row["observed_at"].timestamp())))
        if row["op_state"] != "idle":
            stamp = row["dispatched_at"] or row["created_at"]
            pending.append(PendingWrite(_scope(row), int(stamp.timestamp())))
    return tuple(owned), tuple(pending)
