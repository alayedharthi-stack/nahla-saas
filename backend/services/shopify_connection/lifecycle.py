"""
Shopify connection lifecycle: authorization state, ownership claim,
credential refresh, disconnect, uninstall quarantine and reconciliation.

Every write is guarded by a row lock and/or a compare-and-swap on
``generation`` (bumped on every (re)install and every tombstone),
``credential_version`` (bumped on every credential write) and, for a refresh,
a short ``refresh_lease_id``. Network calls never run while a row lock is
held; their results are only committed when the row is still the one they
were started against, so a concurrent refresh, disconnect, reinstall or
uninstall can never overwrite newer credentials or revive a revoked
connection.

Ownership
─────────
* ``begin_authorization`` and ``accept_callback`` write only
  ``shopify_oauth_states``. Starting, failing or expiring an authorization
  never reserves a shop.
* ``complete_authorization`` writes the ``shopify_connections`` row, and only
  after (1) the tenant's authenticated completion with the same actor and
  JWT session that started the flow, (2) the code exchange for an expiring
  offline token with the read-only scope, and (3) the authenticated GraphQL
  identity check returning the same shop. ``shop_domain`` and ``shop_gid`` are
  unconditionally unique, so the row — kept as a tombstone after disconnect
  or uninstall — is permanent ownership: the same tenant may reinstall;
  another tenant is refused (a transfer is a future, separately audited
  operation and does not exist here).

Uninstall
─────────
The webhook signature covers the raw body only. ``record_uninstall`` therefore
quarantines (stops credential use) rather than deciding anything from
headers, and ``reconcile`` resolves it by asking Shopify with the current
generation's credential: still accepted → retained; rejected → uninstalled
(credentials erased, generation bumped). See ``docs/engineering/shopify-connection-foundation.md``
for the residual ambiguity and why the design fails safe.

Nothing here logs or returns a token, code, state, handle or secret.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.shopify_connection import crypto
from services.shopify_connection.actor import VerifiedActor, actor_still_valid
from services.shopify_connection.config import DASHBOARD_COMPLETE_PATH, ShopifyConfig
from services.shopify_connection.models import (
    CREDENTIAL_STATUSES,
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
REFRESH_LEASE_SECONDS = 30
ACCESS_TOKEN_MARGIN_SECONDS = 120
WEBHOOK_LOCK_TIMEOUT = "2000ms"

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
    C_SESSION_MISMATCH, C_STATE_UNREADABLE, C_EXCHANGE_FAILED, C_SCOPE_INVALID, C_IDENTITY_UNVERIFIED,
    C_IDENTITY_MISMATCH, C_IDENTITY_CONFLICT, C_NOT_FOUND, C_UNAVAILABLE, C_ERROR,
})

_HTTP_STATUS = {
    C_SHOP_UNAVAILABLE: 409, C_TOO_MANY_PENDING: 429, C_INVALID_STATE: 400, C_REPLAYED: 409,
    C_EXPIRED: 400, C_SHOP_MISMATCH: 400, C_CALLBACK_MISMATCH: 400, C_ACTOR_REVOKED: 403,
    C_SUPERSEDED: 409, C_INVALID_COMPLETION: 400, C_SESSION_MISMATCH: 403, C_STATE_UNREADABLE: 400,
    C_EXCHANGE_FAILED: 502, C_SCOPE_INVALID: 422, C_IDENTITY_UNVERIFIED: 502, C_IDENTITY_MISMATCH: 422,
    C_IDENTITY_CONFLICT: 409, C_NOT_FOUND: 404, C_UNAVAILABLE: 503, C_ERROR: 500,
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

def _finish_state(db: Session, state_id: int, status: str, code: Optional[str], now: datetime) -> None:
    db.rollback()
    row = db.get(ShopifyOAuthState, state_id, with_for_update=True, populate_existing=True)
    if row is None or row.status != STATE_EXCHANGING:
        db.rollback()
        return
    row.status = status
    row.failure_code = code
    row.code_enc = None
    row.completed_at = now
    if code:
        _audit(db, tenant_id=row.tenant_id, shop_domain=row.shop_domain, action="authorization_refused",
               actor_user_id=row.actor_user_id, detail={"stage": "completion", "code": code})
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
    """Exchange, verify identity, then claim — all bound to the starting actor and session."""
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
    # Single use before any network call: the code leaves the database now.
    row.status = STATE_EXCHANGING
    row.code_enc = None
    db.commit()

    stage = "exchange"
    try:
        grant = await api.exchange_code(shop_domain=shop, code=code)
        stage = "identity"
        identity = await api.shop_identity(shop_domain=shop, access_token=grant.access_token)
    except ShopifyApiError as exc:
        if exc.code in ("scope_missing", "scope_excess"):
            out = C_SCOPE_INVALID
        elif stage == "identity":
            out = C_IDENTITY_UNVERIFIED
        else:
            out = C_EXCHANGE_FAILED
        logger.warning("[SHOPIFY_CONNECTION] completion refused tenant=%s stage=%s code=%s",
                       actor.tenant_id, stage, exc.code)
        _finish_state(db, state_id, STATE_FAILED, out, utcnow())
        raise ConnectionRefused(out) from None
    finally:
        code = None
    if identity.shop_domain != shop:
        _finish_state(db, state_id, STATE_FAILED, C_IDENTITY_MISMATCH, utcnow())
        raise ConnectionRefused(C_IDENTITY_MISMATCH)

    try:
        return _claim(db, actor=actor, state_id=state_id, shop_domain=shop, identity=identity, grant=grant,
                      generation_at_start=generation_at_start, cipher=cipher, now=now or utcnow())
    except ConnectionRefused as exc:
        _finish_state(db, state_id, STATE_FAILED, exc.code, utcnow())
        raise
    except IntegrityError:
        # A concurrent claim committed first; decide from what it committed.
        db.rollback()
        winner = _connection_by_domain(db, shop) or _connection_by_gid(db, identity.shop_gid)
        out = C_SHOP_UNAVAILABLE if winner is not None and winner.tenant_id != actor.tenant_id else C_SUPERSEDED
        _finish_state(db, state_id, STATE_FAILED, out, utcnow())
        raise ConnectionRefused(out) from None


def _claim(
    db: Session,
    *,
    actor: VerifiedActor,
    state_id: int,
    shop_domain: str,
    identity: ShopIdentity,
    grant: TokenGrant,
    generation_at_start: int,
    cipher: crypto.TokenCipher,
    now: datetime,
) -> Dict[str, Any]:
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
        conn.refresh_lease_id = None
        conn.refresh_lease_expires_at = None
        conn.quarantined_at = None
        conn.quarantine_reason = None
        conn.revalidation_requested_at = None
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
    _audit(db, tenant_id=actor.tenant_id, shop_domain=shop_domain, action=action, connection=conn,
           actor_user_id=actor.user_id, detail={"scopes": sorted(grant.scopes)})
    db.commit()
    logger.info("[SHOPIFY_CONNECTION] %s tenant=%s connection=%s generation=%s",
                action, actor.tenant_id, conn.id, conn.generation)
    return summarize(conn)


# ── Tombstones, events, states ────────────────────────────────────────────────

def _tombstone(conn: ShopifyConnection, status: str, reason: str, now: datetime) -> None:
    conn.status = status
    conn.access_token_enc = None
    conn.refresh_token_enc = None
    conn.access_token_expires_at = None
    conn.refresh_token_expires_at = None
    conn.refresh_lease_id = None
    conn.refresh_lease_expires_at = None
    conn.generation = int(conn.generation) + 1
    conn.disconnected_at = now
    conn.disconnect_reason = reason
    conn.quarantined_at = None
    conn.quarantine_reason = None
    conn.revalidation_requested_at = None
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

    * The delivery id dedupes redeliveries of the same body only; the same id
      with a different signed body is processed as a new event.
    * Identity comes from the signed body; an unknown shop changes nothing.
    * An active connection is quarantined (credential use stops) — unless this
      exact body was already proven stale for this generation, in which case
      the connection stays usable and only a revalidation probe is requested
      (a later genuine uninstall with an identical body is still caught by
      that probe; nothing is suppressed).
    """
    current = now or utcnow()
    # Bounded wait behind a refresh / disconnect holding the row: the delivery
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
                    db.rollback()
                    return "duplicate", None
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
            conn.refresh_lease_id = None
            conn.refresh_lease_expires_at = None
            outcome = "quarantined"
        conn.updated_at = current
        event.resolution = "pending"
        reconcile_id = conn.id
    elif conn.status == STATUS_QUARANTINED:
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


# ── Credential refresh (lease + CAS) ──────────────────────────────────────────

@dataclass(frozen=True)
class RefreshOutcome:
    status: str  # refreshed | superseded | rejected | deferred | in_progress | not_active
    access_token: Optional[str] = field(default=None, repr=False)
    generation: Optional[int] = None


async def refresh_credentials(
    db: Session,
    *,
    connection_id: int,
    api: ShopifyApi,
    cipher: crypto.TokenCipher,
    now: Optional[datetime] = None,
    allow_quarantined: bool = False,
    rejection_status: str = STATUS_REAUTH_REQUIRED,
) -> RefreshOutcome:
    """Rotate the token pair once. Only the lease holder may call Shopify, and
    its result is stored only if lease, generation, credential version and
    status are all unchanged. A permanent refusal erases the pair (Shopify:
    clear the stored pair and re-authorize)."""
    current = now or utcnow()
    allowed = CREDENTIAL_STATUSES if allow_quarantined else (STATUS_ACTIVE,)
    lease = secrets.token_hex(16)
    acquired = db.execute(
        update(ShopifyConnection).where(
            ShopifyConnection.id == connection_id,
            ShopifyConnection.status.in_(allowed),
            or_(ShopifyConnection.refresh_lease_expires_at.is_(None),
                ShopifyConnection.refresh_lease_expires_at < current),
        ).values(
            refresh_lease_id=lease,
            refresh_lease_expires_at=current + timedelta(seconds=REFRESH_LEASE_SECONDS),
            updated_at=current,
        ).returning(
            ShopifyConnection.generation, ShopifyConnection.credential_version, ShopifyConnection.refresh_token_enc,
            ShopifyConnection.refresh_token_expires_at, ShopifyConnection.tenant_id, ShopifyConnection.shop_domain,
        ).execution_options(synchronize_session=False)
    ).first()
    db.commit()
    if acquired is None:
        status = db.scalar(select(ShopifyConnection.status).where(ShopifyConnection.id == connection_id))
        return RefreshOutcome("not_active" if status not in allowed else "in_progress")
    generation, version, refresh_enc, refresh_expires_at, tenant_id, shop = acquired
    cas = dict(lease=lease, generation=int(generation), version=int(version), allowed=allowed)

    if refresh_expires_at is None or refresh_expires_at <= current:
        return _refresh_rejected(db, connection_id, cas, rejection_status, "refresh_token_expired", current)
    try:
        refresh_token = cipher.decrypt(refresh_enc, crypto.token_context(
            crypto.PURPOSE_REFRESH_TOKEN, tenant_id=tenant_id, shop_domain=shop, generation=int(generation)))
    except crypto.CredentialCryptoError:
        return _refresh_rejected(db, connection_id, cas, STATUS_REAUTH_REQUIRED, "credential_unreadable", current)
    try:
        grant = await api.refresh(shop_domain=shop, refresh_token=refresh_token)
    except ShopifyApiError as exc:
        refresh_token = None
        if exc.kind == "permanent" or exc.code in ("scope_missing", "scope_excess"):
            reason = "scope_invalid" if exc.code.startswith("scope_") else "refresh_rejected"
            return _refresh_rejected(db, connection_id, cas, rejection_status, reason, current)
        _release_lease(db, connection_id, lease)
        logger.warning("[SHOPIFY_CONNECTION] refresh deferred connection=%s code=%s", connection_id, exc.code)
        return RefreshOutcome("deferred")
    refresh_token = None
    stored_at = utcnow() if now is None else current
    values = _credential_values(grant, cipher=cipher, tenant_id=tenant_id, shop_domain=shop,
                                generation=int(generation), now=stored_at)
    written = db.execute(
        update(ShopifyConnection).where(
            ShopifyConnection.id == connection_id,
            ShopifyConnection.refresh_lease_id == lease,
            ShopifyConnection.generation == int(generation),
            ShopifyConnection.credential_version == int(version),
            ShopifyConnection.status.in_(allowed),
        ).values(
            credential_version=int(version) + 1,
            refresh_lease_id=None,
            refresh_lease_expires_at=None,
            last_refreshed_at=stored_at,
            updated_at=stored_at,
            **values,
        ).returning(ShopifyConnection.id).execution_options(synchronize_session=False)
    ).first()
    if written is None:
        # Disconnected, uninstalled, reinstalled or quarantined meanwhile: the
        # rotated pair is discarded and never revives or overwrites anything.
        db.rollback()
        logger.info("[SHOPIFY_CONNECTION] refresh result discarded connection=%s", connection_id)
        return RefreshOutcome("superseded")
    db.add(ShopifyConnectionAudit(tenant_id=tenant_id, connection_id=connection_id, shop_domain=shop,
                                  action="credentials_refreshed", generation=int(generation),
                                  detail={"credential_version": int(version) + 1}))
    db.commit()
    return RefreshOutcome("refreshed", access_token=grant.access_token, generation=int(generation))


def _release_lease(db: Session, connection_id: int, lease: str) -> None:
    db.execute(
        update(ShopifyConnection).where(
            ShopifyConnection.id == connection_id, ShopifyConnection.refresh_lease_id == lease,
        ).values(refresh_lease_id=None, refresh_lease_expires_at=None).execution_options(synchronize_session=False)
    )
    db.commit()


def _refresh_rejected(db: Session, connection_id: int, cas: Dict[str, Any], status: str, reason: str,
                      now: datetime) -> RefreshOutcome:
    conn = db.get(ShopifyConnection, connection_id, with_for_update=True, populate_existing=True)
    if (conn is None or conn.refresh_lease_id != cas["lease"] or int(conn.generation) != cas["generation"]
            or int(conn.credential_version) != cas["version"] or conn.status not in cas["allowed"]):
        db.rollback()
        return RefreshOutcome("superseded")
    _tombstone(conn, status, reason, now)
    _resolve_events(db, conn.id, "uninstalled" if status == STATUS_UNINSTALLED else status, now)
    _supersede_states(db, conn.shop_domain, now)
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
) -> str:
    """Resolve a quarantine / revalidation with Shopify's own answer.

    retained | uninstalled | identity_mismatch | deferred | superseded | nothing_to_do | not_found

    A normally expired access token is refreshed first (a valid refresh is
    itself evidence the install is alive); only a credential Shopify refuses
    proves the uninstall. Transient failures leave the quarantine in place.
    """
    current = now or utcnow()
    conn = db.get(ShopifyConnection, connection_id, populate_existing=True)
    if conn is None:
        db.rollback()
        return "not_found"
    if conn.status == STATUS_QUARANTINED:
        pass
    elif conn.status == STATUS_ACTIVE and conn.revalidation_requested_at is not None:
        pass
    else:
        db.rollback()
        return "nothing_to_do"
    generation = int(conn.generation)
    tenant_id, shop, shop_gid = conn.tenant_id, conn.shop_domain, conn.shop_gid
    access_enc, access_expires_at = conn.access_token_enc, conn.access_token_expires_at
    # Events received after this point are not covered by this probe.
    snapshot = db.scalar(
        select(func.max(ShopifyWebhookEvent.id)).where(
            ShopifyWebhookEvent.connection_id == connection_id, ShopifyWebhookEvent.resolution == "pending")
    )
    db.rollback()

    token: Optional[str] = None
    if access_expires_at is not None and access_expires_at > current + timedelta(seconds=ACCESS_TOKEN_MARGIN_SECONDS):
        try:
            token = cipher.decrypt(access_enc, crypto.token_context(
                crypto.PURPOSE_ACCESS_TOKEN, tenant_id=tenant_id, shop_domain=shop, generation=generation))
        except crypto.CredentialCryptoError:
            token = None
    if token is None:
        outcome = await refresh_credentials(db, connection_id=connection_id, api=api, cipher=cipher, now=current,
                                            allow_quarantined=True, rejection_status=STATUS_UNINSTALLED)
        if outcome.status == "rejected":
            return "uninstalled"
        if outcome.status != "refreshed" or outcome.generation != generation:
            return "superseded" if outcome.status in ("superseded", "not_active") else "deferred"
        token = outcome.access_token
    try:
        identity = await api.shop_identity(shop_domain=shop, access_token=token)
    except ShopifyApiError as exc:
        token = None
        if exc.kind == "rejected":
            return _cas_tombstone(db, connection_id, generation, STATUS_UNINSTALLED, "uninstalled", current)
        logger.warning("[SHOPIFY_CONNECTION] reconcile deferred connection=%s code=%s", connection_id, exc.code)
        return "deferred"
    token = None
    if identity.shop_gid != shop_gid or identity.shop_domain != shop:
        result = _cas_tombstone(db, connection_id, generation, STATUS_REAUTH_REQUIRED, "identity_mismatch", current)
        return "identity_mismatch" if result != "superseded" else result
    return _cas_retain(db, connection_id, generation, snapshot, current)


def _cas_tombstone(db: Session, connection_id: int, generation: int, status: str, reason: str,
                   now: datetime) -> str:
    conn = db.get(ShopifyConnection, connection_id, with_for_update=True, populate_existing=True)
    if conn is None or int(conn.generation) != generation or conn.status not in CREDENTIAL_STATUSES:
        db.rollback()
        return "superseded"
    _tombstone(conn, status, reason, now)
    _resolve_events(db, conn.id, "uninstalled" if status == STATUS_UNINSTALLED else status, now)
    _supersede_states(db, conn.shop_domain, now)
    _audit(db, tenant_id=conn.tenant_id, shop_domain=conn.shop_domain, action="uninstall_confirmed"
           if status == STATUS_UNINSTALLED else "credentials_rejected", connection=conn, detail={"reason": reason})
    db.commit()
    return status


def _cas_retain(db: Session, connection_id: int, generation: int, snapshot: Optional[int], now: datetime) -> str:
    conn = db.get(ShopifyConnection, connection_id, with_for_update=True, populate_existing=True)
    if conn is None or int(conn.generation) != generation or conn.status not in CREDENTIAL_STATUSES:
        db.rollback()
        return "superseded"
    newer = db.scalar(
        select(func.count()).select_from(ShopifyWebhookEvent).where(
            ShopifyWebhookEvent.connection_id == connection_id,
            ShopifyWebhookEvent.resolution == "pending",
            ShopifyWebhookEvent.id > (snapshot or 0),
        )
    )
    _resolve_events(db, connection_id, "retained", now, max_event_id=snapshot or 0)
    if newer:
        # An uninstall arrived after the probe began; it needs its own probe.
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


async def reconcile_in_new_session(connection_id: int, *, config: ShopifyConfig) -> str:
    """Background entry used after a webhook ack (its own session; never raises)."""
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        api = ShopifyApi(client_id=config.client_id, client_secret=config.client_secret,
                         api_version=config.api_version)
        return await reconcile(db, connection_id=connection_id, api=api,
                               cipher=crypto.TokenCipher(config.encryption_key))
    except Exception as exc:  # noqa: BLE001 — quarantine stays in place (fail safe)
        logger.error("[SHOPIFY_CONNECTION] reconcile failed connection=%s kind=%s",
                     connection_id, type(exc).__name__)
        try:
            db.rollback()
        except Exception:  # noqa: silent-ok — best-effort rollback; quarantine already fails safe
            pass
        return "deferred"
    finally:
        db.close()


# ── Credential use (for the next slice's consumers) ───────────────────────────

async def active_access_token(
    db: Session,
    *,
    connection_id: int,
    api: ShopifyApi,
    cipher: crypto.TokenCipher,
    now: Optional[datetime] = None,
) -> Tuple[str, int]:
    """(access token, generation) of an ``active`` connection, refreshing when due.
    A quarantined or tombstoned connection never yields a credential."""
    current = now or utcnow()
    conn = db.get(ShopifyConnection, connection_id, populate_existing=True)
    if conn is None or conn.status != STATUS_ACTIVE:
        db.rollback()
        raise ConnectionRefused(C_NOT_FOUND if conn is None else C_UNAVAILABLE)
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
    outcome = await refresh_credentials(db, connection_id=connection_id, api=api, cipher=cipher, now=current)
    if outcome.status != "refreshed":
        raise ConnectionRefused(C_UNAVAILABLE)
    return outcome.access_token, int(outcome.generation)


# ── Read model ────────────────────────────────────────────────────────────────

def tables_present(db: Session) -> bool:
    """True only when inspection proves all four Shopify tables exist (0121 applied)."""
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
