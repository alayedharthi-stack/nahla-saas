"""
Shopify connection lifecycle: authorization state, ownership claim,
credential refresh, disconnect, uninstall quarantine and reconciliation.

Concurrency model
─────────────────
* ``generation`` is bumped on every (re)install and every tombstone;
  ``credential_version`` on every credential write. Every write that depends
  on an earlier read re-locks the row and compares both (compare-and-swap).
* Shopify keeps one current expiring offline token per app and store, and a
  new code exchange or refresh retires the previous pair. At most one such
  call per shop is therefore in flight across all workers: it runs under the
  shop's row in ``shopify_shop_leases`` (short, expiring, taken over when
  stale), and its result is stored — or discarded — in the same transaction
  that deletes the lease, after verifying the lease is still held. A lease
  is never ownership: it reserves nothing once it expires or is released.
* No row lock is held across an HTTP call.

Ownership
─────────
* ``begin_authorization`` and ``accept_callback`` write only
  ``shopify_oauth_states``: starting, failing or expiring an authorization
  never reserves a shop.
* ``complete_authorization`` writes ``shopify_connections`` only after (1)
  the tenant's authenticated completion with the same actor and JWT session
  that started the flow, (2) the code exchange for an expiring offline token
  with the read-only scope, (3) the authenticated GraphQL identity check
  returning the same shop, and (4) a re-validation of the live user and tenant
  rows (locked) in the committing transaction. ``shop_domain`` and
  ``shop_gid`` are unconditionally unique: the row — kept as a tombstone after
  disconnect or uninstall — is permanent ownership. The same tenant may
  reinstall; another tenant is refused (a transfer is a future, separately
  audited operation and does not exist here).

Uninstall
─────────
The webhook signature covers the raw body only. ``record_uninstall``
quarantines (stops credential use) rather than deciding anything from
headers, and ``reconcile`` resolves it with Shopify's own answer to the
current generation's credential. The schedule is durable
(``reconcile_next_at`` / ``reconcile_lease_*``); see ``recovery.py``.

Nothing here logs or returns a token, code, state, handle or secret.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.shopify_connection import crypto
from services.shopify_connection.actor import VerifiedActor, actor_still_valid, recheck_actor_locked
from services.shopify_connection.config import DASHBOARD_COMPLETE_PATH, ShopifyConfig
from services.shopify_connection.models import (
    CREDENTIAL_STATUSES,
    LEASE_EXCHANGE,
    LEASE_REFRESH,
    STATE_CALLBACK_RECEIVED,
    STATE_COMPLETED,
    STATE_EXCHANGING,
    STATE_FAILED,
    STATE_PENDING,
    STATUS_ACTIVE,
    STATUS_DISCONNECTED,
    STATUS_QUARANTINED,
    STATUS_REAUTH_REQUIRED,
    STATUS_UNINSTALLED,
    ShopifyConnection,
    ShopifyConnectionAudit,
    ShopifyOAuthState,
    ShopifyShopLease,
    ShopifyWebhookEvent,
)
from services.shopify_connection.oauth import (
    ShopIdentity,
    ShopifyApi,
    ShopifyApiError,
    TokenGrant,
    VerifiedCallback,
    build_authorize_url,
)
from services.shopify_connection.webhooks import TOPIC_APP_UNINSTALLED, SignedShopIdentity

logger = logging.getLogger("nahla.shopify_connection")

STATE_TTL_SECONDS = 600
COMPLETION_TTL_SECONDS = 300
MAX_PENDING_AUTHORIZATIONS_PER_TENANT = 5
# Longer than both bounded HTTP calls of a completion (2 x 10 s) plus margin.
EXCHANGE_LEASE_SECONDS = 60
REFRESH_LEASE_SECONDS = 30
# Every Shopify call admitted by a lease must finish (or be abandoned) while
# that lease still has at least this much life left, and no single call may
# take longer than CALL_DEADLINE_SECONDS in total.
LEASE_SAFETY_SECONDS = 10
CALL_DEADLINE_SECONDS = 20
ACCESS_TOKEN_MARGIN_SECONDS = 120
WEBHOOK_LOCK_TIMEOUT = "2000ms"
# Quarantine reason when a code exchange may have produced a grant that was
# not stored: Shopify may have retired the stored pair. Only a successful
# forced refresh clears it.
POSSIBLE_RETIRED = "possible_retired_credential"

_STATE_DOMAIN = b"nahla.shopify.oauth_state:v1:"
_HANDLE_DOMAIN = b"nahla.shopify.completion_handle:v1:"
_REDIRECT_DOMAIN = b"nahla.shopify.redirect_uri:v1:"

# Result codes (the only values that reach a URL fragment or a response body).
C_CONNECTED = "connected"
C_SHOP_UNAVAILABLE = "shop_unavailable"
C_TOO_MANY_PENDING = "too_many_pending_authorizations"
C_INVALID_STATE = "invalid_state"
C_REPLAYED = "state_replayed"
C_EXPIRED = "state_expired"
C_SHOP_MISMATCH = "shop_mismatch"
C_CALLBACK_MISMATCH = "callback_mismatch"
C_ACTOR_REVOKED = "actor_revoked"
C_SUPERSEDED = "superseded"
C_INVALID_COMPLETION = "invalid_completion"
C_SESSION_MISMATCH = "session_mismatch"
C_STATE_UNREADABLE = "state_unreadable"
C_EXCHANGE_IN_PROGRESS = "exchange_in_progress"
C_EXCHANGE_FAILED = "token_exchange_failed"
C_SCOPE_INVALID = "scope_invalid"
C_IDENTITY_UNVERIFIED = "identity_unverified"
C_IDENTITY_MISMATCH = "identity_mismatch"
C_IDENTITY_CONFLICT = "identity_conflict"
C_NOT_FOUND = "not_found"
C_UNAVAILABLE = "unavailable"
C_ERROR = "error"

RESULT_CODES = frozenset({
    C_CONNECTED, C_SHOP_UNAVAILABLE, C_TOO_MANY_PENDING, C_INVALID_STATE, C_REPLAYED, C_EXPIRED,
    C_SHOP_MISMATCH, C_CALLBACK_MISMATCH, C_ACTOR_REVOKED, C_SUPERSEDED, C_INVALID_COMPLETION,
    C_SESSION_MISMATCH, C_STATE_UNREADABLE, C_EXCHANGE_IN_PROGRESS, C_EXCHANGE_FAILED, C_SCOPE_INVALID,
    C_IDENTITY_UNVERIFIED, C_IDENTITY_MISMATCH, C_IDENTITY_CONFLICT, C_NOT_FOUND, C_UNAVAILABLE, C_ERROR,
})

_HTTP_STATUS = {
    C_SHOP_UNAVAILABLE: 409, C_TOO_MANY_PENDING: 429, C_INVALID_STATE: 400, C_REPLAYED: 409,
    C_EXPIRED: 400, C_SHOP_MISMATCH: 400, C_CALLBACK_MISMATCH: 400, C_ACTOR_REVOKED: 403,
    C_SUPERSEDED: 409, C_INVALID_COMPLETION: 400, C_SESSION_MISMATCH: 403, C_STATE_UNREADABLE: 400,
    C_EXCHANGE_IN_PROGRESS: 409, C_EXCHANGE_FAILED: 502, C_SCOPE_INVALID: 422, C_IDENTITY_UNVERIFIED: 502,
    C_IDENTITY_MISMATCH: 422, C_IDENTITY_CONFLICT: 409, C_NOT_FOUND: 404, C_UNAVAILABLE: 503, C_ERROR: 500,
}


class ConnectionRefused(Exception):
    """A refusal with a stable code. ``str()`` is the code only."""

    def __init__(self, code: str):
        code = code if code in RESULT_CODES else C_ERROR
        super().__init__(code)
        self.code = code
        self.http_status = _HTTP_STATUS.get(code, 400)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def wall_clock() -> datetime:
    """The clock lease fences are checked against *at write time* (a seam for tests)."""
    return datetime.now(timezone.utc)


def _fence_now(now: datetime) -> datetime:
    # The later of the caller's logical time and the real time of the write:
    # a lease that ran out while a call was in flight never passes.
    return max(now, wall_clock())


def _recovery_lease_holds(conn: Optional[ShopifyConnection], lease: Optional[str], now: datetime) -> bool:
    if lease is None:
        return True
    return (conn is not None and conn.reconcile_lease_id == lease
            and conn.reconcile_lease_expires_at is not None and conn.reconcile_lease_expires_at > _fence_now(now))


def _digest(domain: bytes, value: str) -> str:
    return hashlib.sha256(domain + str(value).encode("utf-8")).hexdigest()


def state_hash(state: str) -> str:
    return _digest(_STATE_DOMAIN, state)


def completion_handle_hash(handle: str) -> str:
    return _digest(_HANDLE_DOMAIN, handle)


def redirect_fingerprint(redirect_uri: str) -> str:
    return _digest(_REDIRECT_DOMAIN, redirect_uri)


def _audit(
    db: Session,
    *,
    tenant_id: int,
    shop_domain: str,
    action: str,
    connection: Optional[ShopifyConnection] = None,
    actor_user_id: Optional[int] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    db.add(ShopifyConnectionAudit(
        tenant_id=int(tenant_id),
        connection_id=connection.id if connection is not None else None,
        shop_domain=shop_domain,
        action=action,
        actor_user_id=actor_user_id,
        generation=connection.generation if connection is not None else None,
        detail=detail or None,
    ))


def _connection_by_domain(db: Session, shop_domain: str, *, lock: bool = False) -> Optional[ShopifyConnection]:
    stmt = select(ShopifyConnection).where(ShopifyConnection.shop_domain == shop_domain)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt.execution_options(populate_existing=True)).scalar_one_or_none()


def _connection_by_gid(db: Session, shop_gid: str, *, lock: bool = False) -> Optional[ShopifyConnection]:
    stmt = select(ShopifyConnection).where(ShopifyConnection.shop_gid == shop_gid)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt.execution_options(populate_existing=True)).scalar_one_or_none()


# ── Per-shop lease (exchange / refresh) ───────────────────────────────────────

def acquire_shop_lease(db: Session, *, shop_domain: str, purpose: str, tenant_id: int, now: datetime,
                       seconds: int) -> Optional[str]:
    """Take the shop's lease in the caller's transaction, or None when another
    unexpired lease holds it. A stale (expired) lease is taken over. The
    caller commits to publish it."""
    lease = secrets.token_hex(16)
    insert = pg_insert(ShopifyShopLease).values(
        shop_domain=shop_domain, lease_id=lease, purpose=purpose, tenant_id=int(tenant_id),
        acquired_at=now, expires_at=now + timedelta(seconds=seconds),
    )
    stmt = insert.on_conflict_do_update(
        index_elements=["shop_domain"],
        set_={
            "lease_id": insert.excluded.lease_id,
            "purpose": insert.excluded.purpose,
            "tenant_id": insert.excluded.tenant_id,
            "acquired_at": insert.excluded.acquired_at,
            "expires_at": insert.excluded.expires_at,
        },
        where=ShopifyShopLease.expires_at < now,
    ).returning(ShopifyShopLease.lease_id)
    got = db.execute(stmt).scalar_one_or_none()
    return lease if got == lease else None


def _lease_held(db: Session, shop_domain: str, lease: str) -> bool:
    """Lock the lease row and confirm it is still ours (not taken over)."""
    row = db.execute(
        select(ShopifyShopLease.lease_id).where(
            ShopifyShopLease.shop_domain == shop_domain, ShopifyShopLease.lease_id == lease,
        ).with_for_update()
    ).first()
    return row is not None


def _drop_lease(db: Session, shop_domain: str, lease: str) -> None:
    db.execute(delete(ShopifyShopLease).where(
        ShopifyShopLease.shop_domain == shop_domain, ShopifyShopLease.lease_id == lease,
    ).execution_options(synchronize_session=False))


def _release_lease(db: Session, shop_domain: str, lease: str) -> None:
    db.rollback()
    _drop_lease(db, shop_domain, lease)
    db.commit()


class _LeaseLost(Exception):
    """The shop lease was taken over (or has too little life left): no call is made."""


@dataclass
class _HeldLease:
    shop_domain: str
    lease_id: str
    seconds: int
    started: float  # time.monotonic() at acquisition

    def budget(self) -> float:
        return self.seconds - (time.monotonic() - self.started) - LEASE_SAFETY_SECONDS


async def _guarded_call(db: Session, lease: _HeldLease, call):
    """Run one Shopify call only while the lease is ours, with a total deadline
    that ends before the lease can expire. Called between transactions.

    The deadline bounds how long *we* wait; it cannot recall a request Shopify
    already received. Callers treat a timed-out exchange as possibly granted.
    """
    still_ours = db.execute(
        select(ShopifyShopLease.lease_id).where(
            ShopifyShopLease.shop_domain == lease.shop_domain, ShopifyShopLease.lease_id == lease.lease_id)
    ).first()
    db.rollback()
    budget = min(CALL_DEADLINE_SECONDS, lease.budget())
    if still_ours is None or budget <= 0:
        raise _LeaseLost()
    try:
        return await asyncio.wait_for(call(), timeout=budget)
    except asyncio.TimeoutError:
        raise ShopifyApiError("transient", "deadline_exceeded") from None


def _flag_possible_retirement(db: Session, shop_domain: str, now: datetime) -> None:
    """A grant may exist that we did not store: the shop's stored credential may
    have been retired by Shopify. Quarantine it for a forced-refresh
    revalidation (ownership is untouched). Caller commits."""
    conn = _connection_by_domain(db, shop_domain, lock=True)
    if conn is None or conn.status not in CREDENTIAL_STATUSES:
        return
    conn.status = STATUS_QUARANTINED
    conn.quarantined_at = now
    conn.quarantine_reason = POSSIBLE_RETIRED
    conn.revalidation_requested_at = None
    _request_reconcile(conn, now)
    conn.updated_at = now
    _audit(db, tenant_id=conn.tenant_id, shop_domain=shop_domain, action="possible_credential_retirement",
           connection=conn)


# ── Authorization start ───────────────────────────────────────────────────────

def begin_authorization(
    db: Session,
    *,
    actor: VerifiedActor,
    shop_domain: str,
    config: ShopifyConfig,
    now: Optional[datetime] = None,
) -> Tuple[str, int]:
    """Persist a hashed single-use state and return (Shopify authorize URL, ttl).

    Refuses a shop another tenant already owns (before Shopify is involved, so
    no token exchange can retire that tenant's credential). A pending state
    reserves nothing.
    """
    current = now or utcnow()
    existing = _connection_by_domain(db, shop_domain)
    if existing is not None and existing.tenant_id != actor.tenant_id:
        _audit(db, tenant_id=actor.tenant_id, shop_domain=shop_domain, action="claim_refused_other_tenant",
               actor_user_id=actor.user_id, detail={"stage": "start"})
        db.commit()
        raise ConnectionRefused(C_SHOP_UNAVAILABLE)
    pending = db.scalar(
        select(func.count()).select_from(ShopifyOAuthState).where(
            ShopifyOAuthState.tenant_id == actor.tenant_id,
            ShopifyOAuthState.status.in_((STATE_PENDING, STATE_CALLBACK_RECEIVED)),
            ShopifyOAuthState.expires_at > current,
        )
    )
    if int(pending or 0) >= MAX_PENDING_AUTHORIZATIONS_PER_TENANT:
        db.rollback()
        raise ConnectionRefused(C_TOO_MANY_PENDING)
    state = secrets.token_urlsafe(32)
    db.add(ShopifyOAuthState(
        state_hash=state_hash(state),
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        session_ref_hash=actor.session_ref_hash,
        shop_domain=shop_domain,
        redirect_uri_fingerprint=redirect_fingerprint(config.redirect_uri),
        return_path=DASHBOARD_COMPLETE_PATH,
        connection_generation_at_start=int(existing.generation) if existing is not None else 0,
        status=STATE_PENDING,
        expires_at=current + timedelta(seconds=STATE_TTL_SECONDS),
        created_at=current,
    ))
    _audit(db, tenant_id=actor.tenant_id, shop_domain=shop_domain, action="authorization_started",
           connection=existing, actor_user_id=actor.user_id)
    db.commit()
    url = build_authorize_url(
        shop_domain=shop_domain, client_id=config.client_id, redirect_uri=config.redirect_uri, state=state,
    )
    return url, STATE_TTL_SECONDS


def _ownership_blocker(db: Session, state: ShopifyOAuthState) -> Optional[str]:
    """Refusal code when the shop's ownership or install generation moved since start."""
    conn = _connection_by_domain(db, state.shop_domain)
    if conn is None:
        return None if int(state.connection_generation_at_start) == 0 else C_SUPERSEDED
    if conn.tenant_id != state.tenant_id:
        return C_SHOP_UNAVAILABLE
    if int(conn.generation) != int(state.connection_generation_at_start):
        return C_SUPERSEDED
    return None


def _fail_state(state: ShopifyOAuthState, code: str, now: datetime) -> None:
    state.status = STATE_FAILED
    state.failure_code = code
    state.code_enc = None
    state.completed_at = now


# ── Browser callback (no JWT) ─────────────────────────────────────────────────

def accept_callback(
    db: Session,
    *,
    callback: VerifiedCallback,
    config: ShopifyConfig,
    cipher: crypto.TokenCipher,
    now: Optional[datetime] = None,
) -> str:
    """Consume the pending state once and return a fresh completion handle.

    The callback binds nothing: it stores the authorization code encrypted
    (bound to tenant, shop and state) for the authenticated completion step.
    Holding a state URL therefore never binds a shop to anyone. Any failure
    after the state is found burns it.
    """
    current = now or utcnow()
    h = state_hash(callback.state)
    row = db.execute(
        select(ShopifyOAuthState).where(ShopifyOAuthState.state_hash == h).with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        db.rollback()
        raise ConnectionRefused(C_INVALID_STATE)
    if row.status != STATE_PENDING:
        db.rollback()
        raise ConnectionRefused(C_REPLAYED)
    failure: Optional[str] = None
    if current >= row.expires_at:
        failure = C_EXPIRED
    elif row.shop_domain != callback.shop_domain:
        failure = C_SHOP_MISMATCH
    elif (not hmac.compare_digest(row.redirect_uri_fingerprint, redirect_fingerprint(config.redirect_uri))
          or row.return_path != DASHBOARD_COMPLETE_PATH):
        failure = C_CALLBACK_MISMATCH
    elif not actor_still_valid(db, tenant_id=row.tenant_id, user_id=row.actor_user_id):
        failure = C_ACTOR_REVOKED
    else:
        failure = _ownership_blocker(db, row)
    if failure:
        _fail_state(row, failure, current)
        _audit(db, tenant_id=row.tenant_id, shop_domain=row.shop_domain, action="authorization_refused",
               actor_user_id=row.actor_user_id, detail={"stage": "callback", "code": failure})
        db.commit()
        raise ConnectionRefused(failure)
    handle = secrets.token_urlsafe(32)
    row.code_enc = cipher.encrypt(
        callback.code, crypto.code_context(tenant_id=row.tenant_id, shop_domain=row.shop_domain, state_hash=h),
    )
    row.status = STATE_CALLBACK_RECEIVED
    row.completion_handle_hash = completion_handle_hash(handle)
    row.completion_expires_at = current + timedelta(seconds=COMPLETION_TTL_SECONDS)
    row.callback_at = current
    _audit(db, tenant_id=row.tenant_id, shop_domain=row.shop_domain, action="authorization_callback_accepted",
           actor_user_id=row.actor_user_id)
    db.commit()
    return handle


# ── Authenticated completion ──────────────────────────────────────────────────

def _burn_state(db: Session, state_id: int, code: str, now: datetime, *, expect: str,
                actor_user_id: Optional[int] = None) -> None:
    """Mark the state failed in a fresh transaction (only from status *expect*)."""
    db.rollback()
    row = db.get(ShopifyOAuthState, state_id, with_for_update=True, populate_existing=True)
    if row is None or row.status != expect:
        db.rollback()
        return
    _fail_state(row, code, now)
    _audit(db, tenant_id=row.tenant_id, shop_domain=row.shop_domain, action="authorization_refused",
           actor_user_id=actor_user_id or row.actor_user_id, detail={"stage": "completion", "code": code})
    db.commit()


def _finish_failed_exchange(db: Session, state_id: int, shop_domain: str, lease: str, code: str,
                            *, possible_grant: bool) -> None:
    """Fail the exchanging state, flag a possibly retired incumbent credential
    and release the shop lease, atomically."""
    db.rollback()
    now = utcnow()
    row = db.get(ShopifyOAuthState, state_id, with_for_update=True, populate_existing=True)
    if row is not None and row.status == STATE_EXCHANGING:
        _fail_state(row, code, now)
        _audit(db, tenant_id=row.tenant_id, shop_domain=row.shop_domain, action="authorization_refused",
               actor_user_id=row.actor_user_id, detail={"stage": "completion", "code": code})
    if possible_grant:
        _flag_possible_retirement(db, shop_domain, now)
    _drop_lease(db, shop_domain, lease)
    db.commit()


def _credential_values(
    grant: TokenGrant, *, cipher: crypto.TokenCipher, tenant_id: int, shop_domain: str, generation: int,
    now: datetime,
) -> Dict[str, Any]:
    return {
        "access_token_enc": cipher.encrypt(grant.access_token, crypto.token_context(
            crypto.PURPOSE_ACCESS_TOKEN, tenant_id=tenant_id, shop_domain=shop_domain, generation=generation)),
        "refresh_token_enc": cipher.encrypt(grant.refresh_token, crypto.token_context(
            crypto.PURPOSE_REFRESH_TOKEN, tenant_id=tenant_id, shop_domain=shop_domain, generation=generation)),
        "access_token_expires_at": now + timedelta(seconds=grant.expires_in),
        "refresh_token_expires_at": now + timedelta(seconds=grant.refresh_token_expires_in),
        "granted_scopes": ",".join(sorted(grant.scopes)),
    }


async def complete_authorization(
    db: Session,
    *,
    actor: VerifiedActor,
    handle: str,
    config: ShopifyConfig,
    cipher: crypto.TokenCipher,
    api: ShopifyApi,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Exchange, verify identity, re-validate the actor, then claim.

    Only one exchange per shop runs at a time (shop lease). When another is
    in flight this refuses with ``exchange_in_progress`` and leaves the
    completion usable for a retry; ownership is re-checked after the lease is
    taken, so a retry after another tenant's successful claim is refused
    before any call to Shopify.
    """
    current = now or utcnow()
    row = db.execute(
        select(ShopifyOAuthState).where(ShopifyOAuthState.completion_handle_hash == completion_handle_hash(handle))
        .with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        db.rollback()
        raise ConnectionRefused(C_INVALID_COMPLETION)
    if row.status != STATE_CALLBACK_RECEIVED:
        db.rollback()
        raise ConnectionRefused(C_REPLAYED)
    failure: Optional[str] = None
    code: Optional[str] = None
    if row.completion_expires_at is None or current >= row.completion_expires_at:
        failure = C_EXPIRED
    elif (row.tenant_id != actor.tenant_id or row.actor_user_id != actor.user_id
          or not hmac.compare_digest(row.session_ref_hash, actor.session_ref_hash)):
        failure = C_SESSION_MISMATCH
    elif not hmac.compare_digest(row.redirect_uri_fingerprint, redirect_fingerprint(config.redirect_uri)):
        failure = C_CALLBACK_MISMATCH
    else:
        failure = _ownership_blocker(db, row)
    if not failure:
        try:
            code = cipher.decrypt(row.code_enc, crypto.code_context(
                tenant_id=row.tenant_id, shop_domain=row.shop_domain, state_hash=row.state_hash))
        except crypto.CredentialCryptoError:
            failure = C_STATE_UNREADABLE
    if failure:
        _fail_state(row, failure, current)
        _audit(db, tenant_id=row.tenant_id, shop_domain=row.shop_domain, action="authorization_refused",
               actor_user_id=actor.user_id, detail={"stage": "completion", "code": failure})
        db.commit()
        raise ConnectionRefused(failure)

    state_id, shop = int(row.id), row.shop_domain
    generation_at_start = int(row.connection_generation_at_start)
    lease_seconds = EXCHANGE_LEASE_SECONDS
    lease = acquire_shop_lease(db, shop_domain=shop, purpose=LEASE_EXCHANGE, tenant_id=actor.tenant_id,
                               now=current, seconds=lease_seconds)
    held = _HeldLease(shop, lease, lease_seconds, time.monotonic()) if lease else None
    if lease is None:
        # Another exchange or refresh for this shop is in flight. Nothing is
        # spent: the completion stays usable until it expires.
        db.rollback()
        code = None
        raise ConnectionRefused(C_EXCHANGE_IN_PROGRESS)
    # Re-check under the lease: a winner commits ownership before it releases.
    blocker = _ownership_blocker(db, row)
    if blocker:
        code = None
        _burn_state(db, state_id, blocker, current, expect=STATE_CALLBACK_RECEIVED, actor_user_id=actor.user_id)
        raise ConnectionRefused(blocker)
    # Single use before any network call: the code leaves the database now.
    row.status = STATE_EXCHANGING
    row.code_enc = None
    db.commit()

    # From here on, any path that does not store the grant flags the shop's
    # stored credential (if any) as possibly retired by Shopify: a grant may
    # exist that we never stored (late response, discarded result).
    stage = "exchange"
    exchange_code_value = code
    try:
        grant = await _guarded_call(db, held, lambda: api.exchange_code(shop_domain=shop, code=exchange_code_value))
        stage = "identity"
        identity = await _guarded_call(
            db, held, lambda: api.shop_identity(shop_domain=shop, access_token=grant.access_token))
    except _LeaseLost:
        logger.warning("[SHOPIFY_CONNECTION] completion lease lost tenant=%s stage=%s", actor.tenant_id, stage)
        _finish_failed_exchange(db, state_id, shop, lease, C_SUPERSEDED, possible_grant=stage == "identity")
        raise ConnectionRefused(C_SUPERSEDED) from None
    except ShopifyApiError as exc:
        if exc.code in ("scope_missing", "scope_excess"):
            out = C_SCOPE_INVALID
        elif stage == "identity":
            out = C_IDENTITY_UNVERIFIED
        else:
            out = C_EXCHANGE_FAILED
        # A definite refusal of the code issues nothing; anything else at the
        # exchange (timeout, 5xx, a token we refused) may have issued a grant.
        possible = stage == "identity" or exc.kind != "permanent"
        logger.warning("[SHOPIFY_CONNECTION] completion refused tenant=%s stage=%s code=%s",
                       actor.tenant_id, stage, exc.code)
        _finish_failed_exchange(db, state_id, shop, lease, out, possible_grant=possible)
        raise ConnectionRefused(out) from None
    finally:
        code = None
        exchange_code_value = None
    if identity.shop_domain != shop:
        _finish_failed_exchange(db, state_id, shop, lease, C_IDENTITY_MISMATCH, possible_grant=True)
        raise ConnectionRefused(C_IDENTITY_MISMATCH)

    try:
        return _claim(db, actor=actor, state_id=state_id, shop_domain=shop, lease=lease, identity=identity,
                      grant=grant, generation_at_start=generation_at_start, cipher=cipher, now=now or utcnow())
    except ConnectionRefused as exc:
        _finish_failed_exchange(db, state_id, shop, lease, exc.code, possible_grant=True)
        raise
    except IntegrityError:
        # A concurrent claim committed first (only possible after a lease takeover).
        db.rollback()
        winner = _connection_by_domain(db, shop) or _connection_by_gid(db, identity.shop_gid)
        out = C_SHOP_UNAVAILABLE if winner is not None and winner.tenant_id != actor.tenant_id else C_SUPERSEDED
        _finish_failed_exchange(db, state_id, shop, lease, out, possible_grant=True)
        raise ConnectionRefused(out) from None


def _claim(
    db: Session,
    *,
    actor: VerifiedActor,
    state_id: int,
    shop_domain: str,
    lease: str,
    identity: ShopIdentity,
    grant: TokenGrant,
    generation_at_start: int,
    cipher: crypto.TokenCipher,
    now: datetime,
) -> Dict[str, Any]:
    if not _lease_held(db, shop_domain, lease):
        # The lease went stale and another exchange took it over; its result
        # retired ours, so nothing from this exchange may be stored.
        raise ConnectionRefused(C_SUPERSEDED)
    refusal = recheck_actor_locked(db, actor)
    if refusal:
        logger.warning("[SHOPIFY_CONNECTION] claim refused tenant=%s code=%s", actor.tenant_id, refusal)
        raise ConnectionRefused(C_ACTOR_REVOKED)
    conn = _connection_by_domain(db, shop_domain, lock=True)
    by_gid = _connection_by_gid(db, identity.shop_gid, lock=True)
    if by_gid is not None and (conn is None or by_gid.id != conn.id):
        raise ConnectionRefused(C_IDENTITY_CONFLICT)
    if conn is not None:
        if conn.shop_gid != identity.shop_gid:
            raise ConnectionRefused(C_IDENTITY_CONFLICT)
        if conn.tenant_id != actor.tenant_id:
            _audit(db, tenant_id=actor.tenant_id, shop_domain=shop_domain, action="claim_refused_other_tenant",
                   actor_user_id=actor.user_id, detail={"stage": "claim"})
            _audit(db, tenant_id=conn.tenant_id, shop_domain=shop_domain, action="claim_attempt_by_other_tenant",
                   connection=conn, detail={"stage": "claim"})
            db.commit()
            raise ConnectionRefused(C_SHOP_UNAVAILABLE)
        if int(conn.generation) != generation_at_start:
            raise ConnectionRefused(C_SUPERSEDED)
        new_generation = int(conn.generation) + 1
        for key, value in _credential_values(grant, cipher=cipher, tenant_id=actor.tenant_id,
                                             shop_domain=shop_domain, generation=new_generation, now=now).items():
            setattr(conn, key, value)
        conn.status = STATUS_ACTIVE
        conn.generation = new_generation
        conn.credential_version = int(conn.credential_version) + 1
        _clear_reconcile(conn)
        conn.disconnected_at = None
        conn.disconnect_reason = None
        conn.connected_by_user_id = actor.user_id
        conn.connected_at = now
        conn.last_verified_at = now
        conn.updated_at = now
        action = "reconnected"
        _resolve_events(db, conn.id, "superseded", now)
    else:
        if generation_at_start != 0:
            raise ConnectionRefused(C_SUPERSEDED)
        conn = ShopifyConnection(
            tenant_id=actor.tenant_id,
            shop_domain=shop_domain,
            shop_gid=identity.shop_gid,
            status=STATUS_ACTIVE,
            generation=1,
            credential_version=1,
            reconcile_attempts=0,
            connected_by_user_id=actor.user_id,
            connected_at=now,
            last_verified_at=now,
            created_at=now,
            updated_at=now,
            **_credential_values(grant, cipher=cipher, tenant_id=actor.tenant_id, shop_domain=shop_domain,
                                 generation=1, now=now),
        )
        db.add(conn)
        db.flush()
        action = "connected"
    state = db.get(ShopifyOAuthState, state_id, with_for_update=True, populate_existing=True)
    if state is None or state.status != STATE_EXCHANGING:
        # A disconnect / confirmed uninstall superseded this authorization meanwhile.
        raise ConnectionRefused(C_SUPERSEDED)
    state.status = STATE_COMPLETED
    state.completed_at = now
    _drop_lease(db, shop_domain, lease)
    _audit(db, tenant_id=actor.tenant_id, shop_domain=shop_domain, action=action, connection=conn,
           actor_user_id=actor.user_id, detail={"scopes": sorted(grant.scopes)})
    db.commit()
    logger.info("[SHOPIFY_CONNECTION] %s tenant=%s connection=%s generation=%s",
                action, actor.tenant_id, conn.id, conn.generation)
    return summarize(conn)


# ── Tombstones, events, states, reconcile schedule ────────────────────────────

def _clear_reconcile(conn: ShopifyConnection) -> None:
    conn.quarantined_at = None
    conn.quarantine_reason = None
    conn.revalidation_requested_at = None
    conn.reconcile_attempts = 0
    conn.reconcile_next_at = None
    conn.reconcile_lease_id = None
    conn.reconcile_lease_expires_at = None


def _tombstone(conn: ShopifyConnection, status: str, reason: str, now: datetime) -> None:
    conn.status = status
    conn.access_token_enc = None
    conn.refresh_token_enc = None
    conn.access_token_expires_at = None
    conn.refresh_token_expires_at = None
    conn.generation = int(conn.generation) + 1
    conn.disconnected_at = now
    conn.disconnect_reason = reason
    _clear_reconcile(conn)
    conn.updated_at = now


def _resolve_events(db: Session, connection_id: int, resolution: str, now: datetime,
                    *, max_event_id: Optional[int] = None) -> None:
    stmt = update(ShopifyWebhookEvent).where(
        ShopifyWebhookEvent.connection_id == connection_id,
        ShopifyWebhookEvent.resolution == "pending",
    )
    if max_event_id is not None:
        stmt = stmt.where(ShopifyWebhookEvent.id <= max_event_id)
    db.execute(stmt.values(resolution=resolution, resolved_at=now).execution_options(synchronize_session=False))


def _supersede_states(db: Session, shop_domain: str, now: datetime) -> None:
    """Every in-flight authorization for this shop must start over."""
    db.execute(
        update(ShopifyOAuthState).where(
            ShopifyOAuthState.shop_domain == shop_domain,
            ShopifyOAuthState.status.in_((STATE_PENDING, STATE_CALLBACK_RECEIVED, STATE_EXCHANGING)),
        ).values(status=STATE_FAILED, failure_code=C_SUPERSEDED, code_enc=None, completed_at=now)
        .execution_options(synchronize_session=False)
    )


def _request_reconcile(conn: ShopifyConnection, now: datetime) -> None:
    """Make the connection due for reconciliation now (durable) and bump the
    request version, so a probe that started earlier cannot clear it."""
    conn.reconcile_next_at = now
    conn.reconcile_request_version = int(conn.reconcile_request_version or 0) + 1


# ── Tenant disconnect ─────────────────────────────────────────────────────────

def disconnect(db: Session, *, actor: VerifiedActor, shop_domain: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Erase credentials, keep the ownership tombstone. Idempotent."""
    current = now or utcnow()
    conn = db.execute(
        select(ShopifyConnection).where(
            ShopifyConnection.shop_domain == shop_domain,
            ShopifyConnection.tenant_id == actor.tenant_id,
        ).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if conn is None:
        db.rollback()
        raise ConnectionRefused(C_NOT_FOUND)
    if conn.status in (STATUS_DISCONNECTED, STATUS_UNINSTALLED):
        db.rollback()
        return summarize(conn)
    previous = conn.status
    _tombstone(conn, STATUS_DISCONNECTED, "tenant_disconnect", current)
    _supersede_states(db, shop_domain, current)
    _resolve_events(db, conn.id, "superseded", current)
    _audit(db, tenant_id=actor.tenant_id, shop_domain=shop_domain, action="disconnected", connection=conn,
           actor_user_id=actor.user_id, detail={"previous_status": previous})
    db.commit()
    return summarize(conn)


# ── Uninstall webhook: persist, dedupe, quarantine ────────────────────────────

def _digest_already_retained(db: Session, conn: ShopifyConnection, digest: str, exclude_id: int) -> bool:
    """This exact signed body was already seen *and* the current generation's
    credential was proven alive after it — a replay of a known-stale body."""
    return db.scalar(
        select(func.count()).select_from(ShopifyWebhookEvent).where(
            ShopifyWebhookEvent.connection_id == conn.id,
            ShopifyWebhookEvent.body_sha256 == digest,
            ShopifyWebhookEvent.resolution == "retained",
            ShopifyWebhookEvent.connection_generation == conn.generation,
            ShopifyWebhookEvent.id != exclude_id,
        )
    ) > 0


def _duplicate_delivery(db: Session, identity: SignedShopIdentity, now: datetime) -> Tuple[str, Optional[int]]:
    """A processed delivery seen again (same unsigned id, same signed body).

    It changes nothing by itself, but it never suppresses anything either:
    while the shop's connection still holds credentials, a reconciliation is
    (re)requested — a probe-only revalidation when active, the outstanding one
    when quarantined — so a crashed or deferred reconciliation is retried and
    a later genuine uninstall with an identical body is still caught.
    """
    conn = _connection_by_domain(db, identity.shop_domain, lock=True) or _connection_by_gid(
        db, identity.shop_gid, lock=True)
    if conn is None or conn.status not in CREDENTIAL_STATUSES:
        db.rollback()
        return "duplicate", None
    if conn.status == STATUS_ACTIVE and conn.revalidation_requested_at is None:
        conn.revalidation_requested_at = now
    _request_reconcile(conn, now)
    conn.updated_at = now
    _audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain, action="uninstall_webhook_duplicate",
           connection=conn, detail={"status": conn.status})
    db.commit()
    return "duplicate", conn.id


def record_uninstall(
    db: Session,
    *,
    identity: SignedShopIdentity,
    webhook_id: Optional[str],
    body_sha256: str,
    triggered_at_header: Optional[str],
    now: Optional[datetime] = None,
) -> Tuple[str, Optional[int]]:
    """(outcome, connection id needing reconciliation or None). Commits.

    * The unsigned delivery id dedupes redeliveries of the same body only;
      the same id with a different signed body is processed as a new event,
      and a duplicate still (re)requests reconciliation (see above).
    * Identity comes from the signed body; an unknown shop changes nothing.
    * An active connection is quarantined (credential use stops) — unless this
      exact body was already proven stale for this generation, in which case
      the connection stays usable and only a revalidation probe is requested
      (a later genuine uninstall with an identical body is still caught by
      that probe; nothing is suppressed).
    * The reconciliation request is durable (``reconcile_next_at``), so a
      crash after this commit and the 200 acknowledgement loses nothing.
    """
    current = now or utcnow()
    # Bounded wait behind a disconnect / claim holding the row: the delivery
    # must be acknowledged within Shopify's five-second limit or retried.
    db.execute(text(f"SET LOCAL lock_timeout = '{WEBHOOK_LOCK_TIMEOUT}'"))
    values = {
        "topic": TOPIC_APP_UNINSTALLED,
        "shop_domain": identity.shop_domain,
        "shop_gid": identity.shop_gid,
        "body_sha256": body_sha256,
        "header_triggered_at": (triggered_at_header or "")[:64] or None,
        "outcome": "received",
        "received_at": current,
    }
    event: Optional[ShopifyWebhookEvent] = None
    if webhook_id:
        new_id = db.execute(
            pg_insert(ShopifyWebhookEvent).values(webhook_id=webhook_id, **values)
            .on_conflict_do_nothing(index_elements=["webhook_id"]).returning(ShopifyWebhookEvent.id)
        ).scalar_one_or_none()
        if new_id is None:
            prior = db.execute(
                select(ShopifyWebhookEvent).where(ShopifyWebhookEvent.webhook_id == webhook_id)
                .with_for_update().execution_options(populate_existing=True)
            ).scalar_one()
            if prior.body_sha256 == body_sha256:
                if prior.processed_at is not None:
                    return _duplicate_delivery(db, identity, current)
                event = prior  # an earlier attempt did not finish: process it now
            # else: an unsigned id reused with another signed body never suppresses it.
        else:
            event = db.get(ShopifyWebhookEvent, new_id)
    if event is None:
        event = ShopifyWebhookEvent(webhook_id=None, **values)
        db.add(event)
        db.flush()

    conn = _connection_by_domain(db, identity.shop_domain, lock=True) or _connection_by_gid(
        db, identity.shop_gid, lock=True)
    if conn is None:
        event.outcome = "unknown_shop"
        event.processed_at = current
        db.commit()
        return "unknown_shop", None
    mismatch = conn.shop_gid != identity.shop_gid or conn.shop_domain != identity.shop_domain
    event.connection_id = conn.id
    event.connection_generation = conn.generation
    reconcile_id: Optional[int] = None
    if conn.status == STATUS_ACTIVE:
        if not mismatch and _digest_already_retained(db, conn, body_sha256, event.id):
            conn.revalidation_requested_at = current
            outcome = "revalidate_only"
        else:
            conn.status = STATUS_QUARANTINED
            conn.quarantined_at = current
            conn.quarantine_reason = "uninstall_identity_mismatch" if mismatch else "uninstall_webhook"
            outcome = "quarantined"
        _request_reconcile(conn, current)
        conn.updated_at = current
        event.resolution = "pending"
        reconcile_id = conn.id
    elif conn.status == STATUS_QUARANTINED:
        _request_reconcile(conn, current)
        conn.updated_at = current
        outcome = "quarantined"
        event.resolution = "pending"
        reconcile_id = conn.id
    else:
        outcome = "no_active_credentials"
    event.outcome = outcome
    event.processed_at = current
    _audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain, action="uninstall_webhook_received",
           connection=conn, detail={"outcome": outcome, "identity_mismatch": mismatch})
    db.commit()
    return outcome, reconcile_id


# ── Credential refresh (shop lease + CAS) ─────────────────────────────────────

@dataclass(frozen=True)
class RefreshOutcome:
    status: str  # refreshed | superseded | rejected | deferred | in_progress | not_active
    access_token: Optional[str] = field(default=None, repr=False)
    generation: Optional[int] = None
    credential_version: Optional[int] = None


async def refresh_credentials(
    db: Session,
    *,
    connection_id: int,
    api: ShopifyApi,
    cipher: crypto.TokenCipher,
    now: Optional[datetime] = None,
    allow_quarantined: bool = False,
    rejection_status: str = STATUS_REAUTH_REQUIRED,
    reconcile_lease: Optional[str] = None,
) -> RefreshOutcome:
    """Rotate the token pair once, under the shop lease.

    The rotated pair is stored only if the lease is still held and the
    connection's generation, credential version and status are unchanged; a
    permanent refusal erases the pair (Shopify: clear the stored pair and
    re-authorize) under the same conditions, so a stale refusal never erases
    a newer pair. When called by the recovery runner, its ``reconcile_lease``
    fences both writes too: a runner whose lease was lost or ran out stores
    nothing and erases nothing (the held refresh token stays valid at Shopify
    until its replacement is used, so a discarded rotation is recoverable).
    """
    current = now or utcnow()
    allowed = CREDENTIAL_STATUSES if allow_quarantined else (STATUS_ACTIVE,)
    conn = db.get(ShopifyConnection, connection_id, populate_existing=True)
    if conn is None or conn.status not in allowed:
        db.rollback()
        return RefreshOutcome("not_active")
    shop, tenant_id = conn.shop_domain, int(conn.tenant_id)
    lease_seconds = REFRESH_LEASE_SECONDS
    lease = acquire_shop_lease(db, shop_domain=shop, purpose=LEASE_REFRESH, tenant_id=tenant_id, now=current,
                               seconds=lease_seconds)
    held = _HeldLease(shop, lease, lease_seconds, time.monotonic()) if lease else None
    if lease is None:
        db.rollback()
        return RefreshOutcome("in_progress")
    conn = db.get(ShopifyConnection, connection_id, populate_existing=True)
    if conn is None or conn.status not in allowed:
        _drop_lease(db, shop, lease)
        db.commit()
        return RefreshOutcome("not_active")
    generation, version = int(conn.generation), int(conn.credential_version)
    refresh_enc, refresh_expires_at = conn.refresh_token_enc, conn.refresh_token_expires_at
    db.commit()  # publish the lease
    cas = dict(shop=shop, lease=lease, generation=generation, version=version, allowed=allowed,
               reconcile_lease=reconcile_lease)

    if refresh_expires_at is None or refresh_expires_at <= current:
        return _refresh_rejected(db, connection_id, cas, rejection_status, "refresh_token_expired", current)
    try:
        refresh_token = cipher.decrypt(refresh_enc, crypto.token_context(
            crypto.PURPOSE_REFRESH_TOKEN, tenant_id=tenant_id, shop_domain=shop, generation=generation))
    except crypto.CredentialCryptoError:
        return _refresh_rejected(db, connection_id, cas, STATUS_REAUTH_REQUIRED, "credential_unreadable", current)
    try:
        grant = await _guarded_call(db, held, lambda: api.refresh(shop_domain=shop, refresh_token=refresh_token))
    except _LeaseLost:
        refresh_token = None
        logger.info("[SHOPIFY_CONNECTION] refresh not attempted connection=%s reason=lease_lost", connection_id)
        return RefreshOutcome("superseded")
    except ShopifyApiError as exc:
        # A timed-out refresh may have rotated the pair at Shopify; the stored
        # pair is kept (Shopify accepts a retry of the previous refresh token
        # until the replacement is used) and the attempt is deferred.
        refresh_token = None
        if exc.kind == "permanent" or exc.code in ("scope_missing", "scope_excess"):
            reason = "scope_invalid" if exc.code.startswith("scope_") else "refresh_rejected"
            return _refresh_rejected(db, connection_id, cas, rejection_status, reason, current)
        _release_lease(db, shop, lease)
        logger.warning("[SHOPIFY_CONNECTION] refresh deferred connection=%s code=%s", connection_id, exc.code)
        return RefreshOutcome("deferred")
    refresh_token = None
    stored_at = utcnow() if now is None else current
    values = _credential_values(grant, cipher=cipher, tenant_id=tenant_id, shop_domain=shop,
                                generation=generation, now=stored_at)
    if not _lease_held(db, shop, lease):
        db.rollback()
        logger.info("[SHOPIFY_CONNECTION] refresh result discarded connection=%s reason=lease_lost", connection_id)
        return RefreshOutcome("superseded")
    conditions = [
        ShopifyConnection.id == connection_id,
        ShopifyConnection.generation == generation,
        ShopifyConnection.credential_version == version,
        ShopifyConnection.status.in_(allowed),
    ]
    if reconcile_lease is not None:
        conditions += [
            ShopifyConnection.reconcile_lease_id == reconcile_lease,
            ShopifyConnection.reconcile_lease_expires_at > _fence_now(stored_at),
        ]
    written = db.execute(
        update(ShopifyConnection).where(*conditions).values(
            credential_version=version + 1,
            last_refreshed_at=stored_at,
            updated_at=stored_at,
            **values,
        ).returning(ShopifyConnection.id).execution_options(synchronize_session=False)
    ).first()
    _drop_lease(db, shop, lease)
    if written is None:
        # Disconnected, uninstalled, reinstalled or quarantined meanwhile: the
        # rotated pair is discarded and never revives or overwrites anything.
        db.commit()
        logger.info("[SHOPIFY_CONNECTION] refresh result discarded connection=%s", connection_id)
        return RefreshOutcome("superseded")
    db.add(ShopifyConnectionAudit(tenant_id=tenant_id, connection_id=connection_id, shop_domain=shop,
                                  action="credentials_refreshed", generation=generation,
                                  detail={"credential_version": version + 1}))
    db.commit()
    return RefreshOutcome("refreshed", access_token=grant.access_token, generation=generation,
                          credential_version=version + 1)


def _refresh_rejected(db: Session, connection_id: int, cas: Dict[str, Any], status: str, reason: str,
                      now: datetime) -> RefreshOutcome:
    if not _lease_held(db, cas["shop"], cas["lease"]):
        db.rollback()
        return RefreshOutcome("superseded")
    conn = db.get(ShopifyConnection, connection_id, with_for_update=True, populate_existing=True)
    if (conn is None or int(conn.generation) != cas["generation"]
            or int(conn.credential_version) != cas["version"] or conn.status not in cas["allowed"]
            or not _recovery_lease_holds(conn, cas.get("reconcile_lease"), now)):
        _drop_lease(db, cas["shop"], cas["lease"])
        db.commit()
        return RefreshOutcome("superseded")
    _tombstone(conn, status, reason, now)
    _resolve_events(db, conn.id, "uninstalled" if status == STATUS_UNINSTALLED else status, now)
    _supersede_states(db, conn.shop_domain, now)
    _drop_lease(db, cas["shop"], cas["lease"])
    _audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain, action="credentials_rejected",
           connection=conn, detail={"reason": reason, "status": status})
    db.commit()
    return RefreshOutcome("rejected")


# ── Uninstall reconciliation ──────────────────────────────────────────────────

async def reconcile(
    db: Session,
    *,
    connection_id: int,
    api: ShopifyApi,
    cipher: crypto.TokenCipher,
    now: Optional[datetime] = None,
    reconcile_lease: Optional[str] = None,
) -> str:
    """Resolve a quarantine / revalidation with Shopify's own answer.

    retained | uninstalled | reauth_required | identity_mismatch | deferred | superseded | nothing_to_do |
    not_found

    A normally expired access token is refreshed first (a successful refresh
    is itself evidence the install is alive); only a credential Shopify
    refuses proves the uninstall, and only when that credential is still the
    current one (same generation **and** credential version) — a refusal of an
    older pair never erases a newer one. Transient failures leave the
    quarantine in place.

    A quarantine for a possibly retired credential (``POSSIBLE_RETIRED``) is
    only cleared by a successful forced refresh — an access token Shopify
    retired stays usable until it expires, so a probe alone proves nothing.

    Fences: the result is written only if generation, credential version and
    (when given) the recovery runner's ``reconcile_lease`` still hold, and a
    retain clears the request only if no newer reconciliation request
    (``reconcile_request_version``) arrived after the snapshot.
    """
    current = now or utcnow()
    conn = db.get(ShopifyConnection, connection_id, populate_existing=True)
    if conn is None:
        db.rollback()
        return "not_found"
    if not (conn.status == STATUS_QUARANTINED
            or (conn.status == STATUS_ACTIVE and conn.revalidation_requested_at is not None)):
        db.rollback()
        return "nothing_to_do"
    generation, version = int(conn.generation), int(conn.credential_version)
    request_version = int(conn.reconcile_request_version or 0)
    force_refresh = conn.status == STATUS_QUARANTINED and conn.quarantine_reason == POSSIBLE_RETIRED
    tenant_id, shop, shop_gid = conn.tenant_id, conn.shop_domain, conn.shop_gid
    access_enc, access_expires_at = conn.access_token_enc, conn.access_token_expires_at
    # Events received after this point are not covered by this probe.
    snapshot = db.scalar(
        select(func.max(ShopifyWebhookEvent.id)).where(
            ShopifyWebhookEvent.connection_id == connection_id, ShopifyWebhookEvent.resolution == "pending")
    )
    db.rollback()

    fence = dict(generation=generation, request_version=request_version, lease=reconcile_lease, now=current)
    token: Optional[str] = None
    if (not force_refresh and access_expires_at is not None
            and access_expires_at > current + timedelta(seconds=ACCESS_TOKEN_MARGIN_SECONDS)):
        try:
            token = cipher.decrypt(access_enc, crypto.token_context(
                crypto.PURPOSE_ACCESS_TOKEN, tenant_id=tenant_id, shop_domain=shop, generation=generation))
        except crypto.CredentialCryptoError:
            token = None
    if token is None:
        if reconcile_lease is not None and not _reconcile_lease_valid(db, connection_id, reconcile_lease, current):
            return "superseded"
        outcome = await refresh_credentials(
            db, connection_id=connection_id, api=api, cipher=cipher, now=current, allow_quarantined=True,
            rejection_status=STATUS_REAUTH_REQUIRED if force_refresh else STATUS_UNINSTALLED,
            reconcile_lease=reconcile_lease)
        if outcome.status == "rejected":
            return STATUS_REAUTH_REQUIRED if force_refresh else "uninstalled"
        if outcome.status != "refreshed" or outcome.generation != generation:
            return "superseded" if outcome.status in ("superseded", "not_active") else "deferred"
        token, version = outcome.access_token, int(outcome.credential_version)
    try:
        identity = await api.shop_identity(shop_domain=shop, access_token=token)
    except ShopifyApiError as exc:
        token = None
        if exc.kind == "rejected":
            return _cas_tombstone(db, connection_id, version, STATUS_UNINSTALLED, "uninstalled", **fence)
        logger.warning("[SHOPIFY_CONNECTION] reconcile deferred connection=%s code=%s", connection_id, exc.code)
        return "deferred"
    token = None
    if identity.shop_gid != shop_gid or identity.shop_domain != shop:
        result = _cas_tombstone(db, connection_id, version, STATUS_REAUTH_REQUIRED, "identity_mismatch", **fence)
        return "identity_mismatch" if result != "superseded" else result
    return _cas_retain(db, connection_id, version, snapshot, **fence)


def _reconcile_lease_valid(db: Session, connection_id: int, lease: str, now: datetime) -> bool:
    row = db.execute(
        select(ShopifyConnection.reconcile_lease_id, ShopifyConnection.reconcile_lease_expires_at)
        .where(ShopifyConnection.id == connection_id)
    ).first()
    db.rollback()
    return bool(row and row[0] == lease and row[1] is not None and row[1] > _fence_now(now))


def _fenced(conn: Optional[ShopifyConnection], *, generation: int, version: int, lease: Optional[str],
            now: datetime) -> bool:
    if conn is None or int(conn.generation) != generation or int(conn.credential_version) != version:
        return False
    if conn.status not in CREDENTIAL_STATUSES:
        return False
    # Checked against the clock at write time, not the time the probe started.
    return _recovery_lease_holds(conn, lease, now)


def _cas_tombstone(db: Session, connection_id: int, version: int, status: str, reason: str, *, generation: int,
                   request_version: int, lease: Optional[str], now: datetime) -> str:
    conn = db.get(ShopifyConnection, connection_id, with_for_update=True, populate_existing=True)
    if not _fenced(conn, generation=generation, version=version, lease=lease, now=now):
        db.rollback()
        return "superseded"
    _tombstone(conn, status, reason, now)
    _resolve_events(db, conn.id, "uninstalled" if status == STATUS_UNINSTALLED else status, now)
    _supersede_states(db, conn.shop_domain, now)
    _audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain,
           action="uninstall_confirmed" if status == STATUS_UNINSTALLED else "credentials_rejected",
           connection=conn, detail={"reason": reason})
    db.commit()
    return status


def _cas_retain(db: Session, connection_id: int, version: int, snapshot: Optional[int], *, generation: int,
                request_version: int, lease: Optional[str], now: datetime) -> str:
    conn = db.get(ShopifyConnection, connection_id, with_for_update=True, populate_existing=True)
    if not _fenced(conn, generation=generation, version=version, lease=lease, now=now):
        db.rollback()
        return "superseded"
    newer = int(conn.reconcile_request_version or 0) != request_version
    _resolve_events(db, connection_id, "retained", now, max_event_id=snapshot or 0)
    if newer:
        # A reconciliation was requested after this probe began (a new event, a
        # duplicate delivery, a possible retirement); it needs its own probe.
        _audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain, action="quarantine_probe_outdated",
               connection=conn)
        db.commit()
        return "deferred"
    conn.status = STATUS_ACTIVE
    conn.quarantined_at = None
    conn.quarantine_reason = None
    conn.revalidation_requested_at = None
    conn.last_verified_at = now
    conn.updated_at = now
    _audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain, action="quarantine_retained",
           connection=conn)
    db.commit()
    return "retained"


# ── Credential use (for the next slice's consumers) ───────────────────────────

async def active_access_token(
    db: Session,
    *,
    tenant_id: int,
    connection_id: int,
    api: ShopifyApi,
    cipher: crypto.TokenCipher,
    now: Optional[datetime] = None,
) -> Tuple[str, int]:
    """(access token, generation) of this tenant's ``active`` connection,
    refreshing when due. The tenant is mandatory and filtered: another
    tenant's connection id yields ``not_found``. A quarantined or tombstoned
    connection never yields a credential."""
    current = now or utcnow()
    conn = db.execute(
        select(ShopifyConnection).where(
            ShopifyConnection.id == int(connection_id), ShopifyConnection.tenant_id == int(tenant_id),
        ).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if conn is None:
        db.rollback()
        raise ConnectionRefused(C_NOT_FOUND)
    if conn.status != STATUS_ACTIVE:
        db.rollback()
        raise ConnectionRefused(C_UNAVAILABLE)
    generation = int(conn.generation)
    if conn.access_token_expires_at > current + timedelta(seconds=ACCESS_TOKEN_MARGIN_SECONDS):
        try:
            token = cipher.decrypt(conn.access_token_enc, crypto.token_context(
                crypto.PURPOSE_ACCESS_TOKEN, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain,
                generation=generation))
            db.rollback()
            return token, generation
        except crypto.CredentialCryptoError:
            pass
    db.rollback()
    outcome = await refresh_credentials(db, connection_id=conn.id, api=api, cipher=cipher, now=current)
    if outcome.status != "refreshed":
        raise ConnectionRefused(C_UNAVAILABLE)
    return outcome.access_token, int(outcome.generation)


# ── Read model ────────────────────────────────────────────────────────────────

def tables_present(db: Session) -> bool:
    """True only when inspection proves every Shopify table exists (0121 applied)."""
    from sqlalchemy import inspect  # noqa: PLC0415

    from services.shopify_connection.models import SHOPIFY_TABLE_NAMES  # noqa: PLC0415

    try:
        names = set(inspect(db.get_bind()).get_table_names())
    except Exception as exc:  # noqa: BLE001 — unknown schema is treated as absent (fail closed)
        logger.warning("[SHOPIFY_CONNECTION] schema inspect failed kind=%s", type(exc).__name__)
        return False
    return set(SHOPIFY_TABLE_NAMES) <= names


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def summarize(conn: ShopifyConnection) -> Dict[str, Any]:
    """Secret-free view of one connection."""
    return {
        "shop_domain": conn.shop_domain,
        "status": conn.status,
        "scopes": sorted(s for s in (conn.granted_scopes or "").split(",") if s),
        "connected_at": _iso(conn.connected_at),
        "disconnected_at": _iso(conn.disconnected_at),
        "disconnect_reason": conn.disconnect_reason,
        "needs_reconnect": conn.status not in CREDENTIAL_STATUSES,
    }


def connection_summaries(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    rows = db.execute(
        select(ShopifyConnection).where(ShopifyConnection.tenant_id == int(tenant_id))
        .order_by(ShopifyConnection.id.asc())
    ).scalars().all()
    return [summarize(r) for r in rows]
