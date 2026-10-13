"""Dormant, owner-only install-first provisioning. No email-based account linking.

OAuth online proof and offline credentials are separate. Browser transactions
are opaque DB rows, never Nahlah JWTs. The existing per-shop lease serializes
all credential acquisition with connection refresh/reinstall.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from models import Tenant, User, UserTotp
from services.shopify_connection import lifecycle as lc
from services.shopify_connection.crypto import TokenCipher
from services.shopify_connection.install_admission import admitted
from services.shopify_connection.install_models import ShopifyInstallFlow as Flow, ShopifyOwnerIdentity as Owner
from services.shopify_connection.models import ShopifyConnection, STATUS_ACTIVE
from services.shopify_connection.oauth import OwnerGrantRejected, numeric_shop_id
from services.shopify_connection.shop_domain import canonical_shop_domain

BROWSER_RE = re.compile(r"[a-f0-9]{64}")
SESSION_SECONDS = 300
FLOW_SECONDS = 900
MAX_BROWSER_STARTS = 5
MAX_SHOP_STARTS = 10
MAX_GLOBAL_STARTS_PER_MINUTE = 100
MAX_RETAINED_FLOWS = 1000
MAX_SHOP_RETAINED_FLOWS = 100
ADMISSION_LOCK = (0x4E41484C, 0x53484946)


class InstallRejected(Exception):
    def __init__(self, code="install_refused"):
        super().__init__(code)
        self.code = code


def digest(kind, value):
    return hashlib.sha256(f"nahla.shopify.install:v1:{kind}:{value}".encode("ascii")).hexdigest()


@dataclass(frozen=True)
class _OnlineContext:
    flow_id: str
    shop: str

    def associated_data(self):
        return f"nahla.shopify.install.online:v1|{self.flow_id}|{self.shop}".encode("ascii")


def _authorize(config, flow, state):
    params = {"client_id": config.client_id, "scope": "read_products", "redirect_uri": config.redirect_uri, "state": state}
    if flow.phase == "online":
        params["grant_options[]"] = "per-user"
    return f"https://{flow.shop_domain}/admin/oauth/authorize?{urlencode(params)}"


def _new_state(flow):
    state = secrets.token_urlsafe(32)
    flow.state_hash = digest("state", state)
    return state


def sweep_expired(db, *, now=None, limit=100, commit=True):
    """Bounded cleanup, also callable by an operator. Never creates tables.

    Uncertain offline exchanges quarantine incumbent credentials rather than
    deleting ownership. Expired authorization cannot be resumed.
    """
    now = now or lc.utcnow()
    retention = now - timedelta(seconds=FLOW_SECONDS)
    rows = db.execute(select(Flow).where(Flow.expires_at <= now,
                      or_(Flow.phase != "failed", Flow.created_at <= retention))
                      .order_by(Flow.expires_at).limit(limit).with_for_update(skip_locked=True)).scalars().all()
    for row in rows:
        if row.phase in {"exchanging_online", "exchanging_offline"}:
            lc._flag_possible_retirement(db, row.shop_domain, now)
        row.phase = "failed"
        row.online_token_enc = None
        row.handoff_hash = None
        # Keep admission history for the 15-minute quota, never a live session.
        if row.created_at <= retention:
            db.delete(row)
    db.flush()
    if commit:
        db.commit()
    return len(rows)


def validate_admission(*, shop, browser, pilot_code, now=None):
    if canonical_shop_domain(shop) != shop or not isinstance(browser, str) or not BROWSER_RE.fullmatch(browser):
        raise InstallRejected()
    if not admitted(shop, pilot_code, now=now):
        raise InstallRejected("install_admission_required")


def begin(db, *, shop, browser, config, pilot_code=None):
    now = lc.utcnow()
    validate_admission(shop=shop, browser=browser, pilot_code=pilot_code, now=now)
    # A single short, nonblocking transaction makes quotas work across workers.
    # No address headers (including X-Real-IP) are trusted or stored here.
    if not db.scalar(select(func.pg_try_advisory_xact_lock(*ADMISSION_LOCK))):
        db.rollback()
        raise InstallRejected("install_busy")
    db.execute(select(func.set_config("lock_timeout", "250ms", True)))
    sweep_expired(db, now=now, commit=False)
    recent = Flow.created_at > now - timedelta(seconds=FLOW_SECONDS)
    def count(*conditions):
        return db.scalar(select(func.count()).select_from(Flow).where(*conditions))
    browser_hash = digest("browser", browser)
    if (count() >= MAX_RETAINED_FLOWS
            or count(Flow.shop_domain == shop) >= MAX_SHOP_RETAINED_FLOWS
            or count(Flow.created_at > now - timedelta(seconds=60)) >= MAX_GLOBAL_STARTS_PER_MINUTE
            or count(recent, Flow.shop_domain == shop) >= MAX_SHOP_STARTS
            or count(recent, Flow.browser_hash == browser_hash) >= MAX_BROWSER_STARTS):
        db.commit()  # Preserve bounded expiry cleanup even when admission is refused.
        raise InstallRejected("install_rate_limited")
    row = Flow(id=secrets.token_hex(24), browser_hash=browser_hash, shop_domain=shop,
               redirect_hash=digest("redirect", config.redirect_uri), phase="online", generation=0,
               created_at=now, expires_at=now + timedelta(seconds=FLOW_SECONDS))
    state = _new_state(row)
    db.add(row)
    db.commit()
    return _authorize(config, row, state)


def _internal_subject(shop_gid, owner_id):
    return f"shopify-{numeric_shop_id(shop_gid)}-{owner_id}-{secrets.token_hex(16)}@identity.invalid"


def _new_principal(db, shop, shop_gid, owner_id, now):
    # Canonical commerce identity lives in ShopifyConnection/Owner, not in the
    # legacy unique tenant name/domain columns used by other providers.
    from core.trial_lifecycle import init_new_tenant_trial_state
    for attempt in range(3):
        name = shop if attempt == 0 else f"{shop} ({secrets.token_hex(16)})"
        try:
            with db.begin_nested():
                tenant = Tenant(name=name, domain=None, is_active=True, created_at=now)
                init_new_tenant_trial_state(tenant)
                db.add(tenant)
                db.flush()
                subject = _internal_subject(shop_gid, owner_id)
                user = User(username=subject, email=subject, password_hash=None, role="merchant",
                            is_active=True, email_verified=False, tenant_id=tenant.id, created_at=now)
                db.add(user)
                db.flush()
            return tenant, user
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if (getattr(exc.orig, "pgcode", None) != "23505"
                    or constraint not in {"tenants_name_key", "users_username_key", "users_email_key"}):
                raise
    raise InstallRejected("identity_conflict")


def _current(db, flow, *, lock=False):
    conn = lc._connection_by_domain(db, flow.shop_domain, lock=lock)
    if conn is None:
        if flow.shop_gid and lc._connection_by_gid(db, flow.shop_gid, lock=lock):
            raise InstallRejected("identity_conflict")
        return None, None, None
    owner = db.get(Owner, conn.id, with_for_update=lock, populate_existing=True)
    if owner is None or (conn.shop_gid, owner.shop_gid, owner.shopify_user_id) != (flow.shop_gid, flow.shop_gid, flow.owner_id):
        raise InstallRejected("identity_conflict")
    user = db.get(User, owner.user_id, with_for_update=lock, populate_existing=True)
    tenant = db.get(Tenant, owner.tenant_id, with_for_update=lock, populate_existing=True)
    if (owner.tenant_id != conn.tenant_id or not user or user.tenant_id != conn.tenant_id
            or user.role != "merchant" or user.is_active is not True or not tenant or tenant.is_active is not True):
        raise InstallRejected("identity_inactive")
    return conn, owner, user


def _ready(flow, conn, now):
    handle = secrets.token_urlsafe(32)
    flow.phase = "ready"
    flow.connection_id = conn.id
    flow.generation = conn.generation
    flow.handoff_hash = digest("handoff", handle)
    flow.expires_at = min(flow.expires_at, flow.owner_expires_at, now + timedelta(seconds=90))
    return handle


def _fail(db, flow_id, shop, lease=None, *, uncertain=False):
    db.rollback()
    row = db.get(Flow, flow_id, with_for_update=True, populate_existing=True)
    if row and row.phase not in {"redeemed", "failed"}:
        row.phase, row.online_token_enc, row.handoff_hash = "failed", None, None
    if uncertain:
        lc._flag_possible_retirement(db, shop, lc.utcnow())
    if lease:
        lc._drop_lease(db, shop, lease)
    db.commit()


async def callback(db, *, verified, browser, config, api):
    now = lc.utcnow()
    flow = db.execute(select(Flow).where(Flow.state_hash == digest("state", verified.state))
                      .with_for_update()).scalar_one_or_none()
    if (not flow or flow.phase not in {"online", "offline"} or flow.expires_at <= now
            or flow.shop_domain != verified.shop_domain
            or flow.redirect_hash != digest("redirect", config.redirect_uri)):
        db.rollback()
        raise InstallRejected()
    if (not isinstance(browser, str) or not BROWSER_RE.fullmatch(browser)
            or not secrets.compare_digest(flow.browser_hash, digest("browser", browser))):
        # Only a verified callback matching THIS still-pending flow can burn it.
        # Never touch its connection, leases, or an exchange already in flight.
        flow.phase, flow.online_token_enc, flow.handoff_hash = "failed", None, None
        db.commit()
        raise InstallRejected()
    phase, flow_id, shop = flow.phase, flow.id, flow.shop_domain
    proof_started_at = now
    lease_started = time.monotonic()
    lease = lc.acquire_shop_lease(db, shop_domain=shop, purpose="exchange", tenant_id=0, now=now, seconds=60)
    if not lease:
        db.rollback()
        raise InstallRejected("install_busy")
    # Another first-install flow may have completed while this browser was at Shopify.
    if phase == "offline":
        try:
            conn, _, _ = _current(db, flow, lock=True)
            if flow.owner_expires_at is None or flow.owner_expires_at <= now:
                raise InstallRejected()
            if conn and conn.status == STATUS_ACTIVE and flow.generation == 0:
                handle = _ready(flow, conn, now)
                lc._drop_lease(db, shop, lease)
                db.commit()
                return "ready", handle
            if (conn.generation if conn else 0) != flow.generation:
                raise InstallRejected("install_superseded")
        except InstallRejected:
            _fail(db, flow_id, shop, lease)
            raise
    flow.phase = "exchanging_" + phase
    db.commit()  # Burn the code's durable state before any network call.
    held = lc._HeldLease(shop, lease, 60, lease_started)
    cipher = TokenCipher(config.encryption_key)
    uncertain = False
    try:
        if phase == "online":
            async def owner_exchange():
                nonlocal uncertain
                uncertain = True  # Only after lease preflight permits dispatch.
                try:
                    result = await api.exchange_owner_code(shop_domain=shop, code=verified.code)
                except OwnerGrantRejected:
                    uncertain = False  # Definitive rejection / verified online response.
                    raise
                uncertain = False
                return result
            grant = await lc._guarded_call(db, held, owner_exchange)
        else:
            # Recheck the retained user-scoped token before acquiring background authority.
            flow = db.get(Flow, flow_id)
            online = cipher.decrypt(flow.online_token_enc, _OnlineContext(flow_id, shop))
            expected_gid = flow.shop_gid
            identity = await lc._guarded_call(db, held, lambda: api.shop_identity(shop_domain=shop, access_token=online))
            if identity.shop_gid != expected_gid or identity.shop_domain != shop:
                raise InstallRejected()
            async def offline_exchange():
                nonlocal uncertain
                uncertain = True
                return await api.exchange_code(shop_domain=shop, code=verified.code)
            grant = await lc._guarded_call(db, held, offline_exchange)
        identity = await lc._guarded_call(db, held, lambda: api.shop_identity(shop_domain=shop, access_token=grant.access_token))
        now = lc.utcnow()
        # Same lock order as callback: transaction first, shop lease next.
        flow = db.get(Flow, flow_id, with_for_update=True, populate_existing=True)
        if (not flow or flow.phase != "exchanging_" + phase or flow.expires_at <= now
                or held.budget() <= 0 or not lc._lease_held(db, shop, lease)
                or identity.shop_domain != shop):
            raise InstallRejected("install_superseded")
        if phase == "online":
            flow.shop_gid, flow.owner_id = identity.shop_gid, grant.user_id
            # account_owner was proven by the online grant, not by later shop
            # identity probes. Bound ALL resulting authority to that proof.
            flow.owner_expires_at = proof_started_at + timedelta(seconds=min(SESSION_SECONDS, max(1, grant.expires_in - 60)))
            flow.expires_at = min(flow.expires_at, flow.owner_expires_at)
            flow.online_token_enc = cipher.encrypt(grant.access_token, _OnlineContext(flow_id, shop))
            conn, _, _ = _current(db, flow, lock=True)
            if conn and conn.status == STATUS_ACTIVE:
                result = ("ready", _ready(flow, conn, now))
            else:
                flow.generation = conn.generation if conn else 0
                flow.phase = "offline"
                result = ("authorize", _authorize(config, flow, _new_state(flow)))
        else:
            if flow.shop_gid != identity.shop_gid or flow.owner_expires_at <= now:
                raise InstallRejected()
            conn, _, user = _current(db, flow, lock=True)
            if (conn.generation if conn else 0) != flow.generation:
                raise InstallRejected("install_superseded")
            if conn is None:
                # Subject is internal, non-deliverable and unique. Shopify email is never a lookup key.
                tenant, user = _new_principal(db, shop, flow.shop_gid, flow.owner_id, now)
                conn = ShopifyConnection(tenant_id=tenant.id, shop_domain=shop, shop_gid=flow.shop_gid,
                                         generation=1, credential_version=1, reconcile_attempts=0,
                                         created_at=now, connected_by_user_id=user.id)
                db.add(conn)
            else:
                conn.generation += 1
                conn.credential_version += 1
            for key, value in lc._credential_values(grant, cipher=cipher, tenant_id=conn.tenant_id,
                                                    shop_domain=shop, generation=conn.generation, now=now).items():
                setattr(conn, key, value)
            conn.status = STATUS_ACTIVE
            conn.connected_at = conn.last_verified_at = conn.updated_at = now
            conn.disconnected_at = conn.disconnect_reason = None
            lc._clear_reconcile(conn)
            db.flush()
            if flow.generation == 0:
                db.add(Owner(connection_id=conn.id, shop_gid=flow.shop_gid, shopify_user_id=flow.owner_id,
                             tenant_id=conn.tenant_id, user_id=user.id, created_at=now))
            lc._resolve_events(db, conn.id, "superseded", now)
            lc._audit(db, tenant_id=conn.tenant_id, shop_domain=shop, action="install_first_connected",
                      connection=conn, actor_user_id=user.id)
            result = ("ready", _ready(flow, conn, now))
        lc._drop_lease(db, shop, lease)
        db.commit()  # Tenant, user, identity, encrypted tokens and completion are atomic.
        return result
    except Exception:
        _fail(db, flow_id, shop, lease, uncertain=uncertain)
        raise InstallRejected() from None


def _mfa_binding(row):
    if not row or row.confirmed_at is None:
        return None
    return hashlib.sha256(bytes(row.secret_enc) + row.confirmed_at.isoformat().encode()).hexdigest()


def _check_totp(db, user, code, now):
    # Fail closed on lookup/decryption errors; do not reuse auth.py's best-effort MFA lookup.
    row = db.get(UserTotp, user.id, with_for_update=True, populate_existing=True)
    if not row or row.confirmed_at is None:
        return
    if not code:
        raise InstallRejected("totp_required")
    if row.locked_until and row.locked_until > now:
        raise InstallRejected("totp_locked")
    import pyotp
    from core.totp_crypto import decrypt_secret
    otp = pyotp.TOTP(decrypt_secret(row.secret_enc))
    matched = next((now + timedelta(seconds=offset * 30) for offset in (-1, 0, 1)
                    if re.fullmatch(r"[0-9]{6}", code) and secrets.compare_digest(otp.at(now + timedelta(seconds=offset * 30)), code)), None)
    if matched is None or (row.last_used_at and int(matched.timestamp()) // 30 <= int(row.last_used_at.timestamp()) // 30):
        row.failed_attempts = (row.failed_attempts or 0) + 1
        if row.failed_attempts >= 5:
            row.locked_until = now + timedelta(minutes=15)
        db.commit()
        raise InstallRejected("totp_invalid")
    row.failed_attempts, row.locked_until, row.last_used_at = 0, None, matched
    return _mfa_binding(row)


async def redeem(db, *, handle, browser, config, api, totp=None):
    if not BROWSER_RE.fullmatch(browser) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", handle):
        raise InstallRejected()
    now = lc.utcnow()
    flow = db.execute(select(Flow).where(Flow.handoff_hash == digest("handoff", handle))
                      .with_for_update()).scalar_one_or_none()
    if (not flow or flow.phase != "ready" or flow.expires_at <= now
            or not secrets.compare_digest(flow.browser_hash, digest("browser", browser))):
        db.rollback()
        raise InstallRejected()
    conn, owner, user = _current(db, flow, lock=True)
    if not conn or conn.status != STATUS_ACTIVE or conn.generation != flow.generation:
        raise InstallRejected()
    mfa_binding = _check_totp(db, user, totp, now)
    flow_id, shop, expected_gid = flow.id, flow.shop_domain, flow.shop_gid
    online = TokenCipher(config.encryption_key).decrypt(flow.online_token_enc, _OnlineContext(flow_id, shop))
    flow.phase = "redeeming"
    db.commit()
    try:
        import asyncio
        identity = await asyncio.wait_for(api.shop_identity(shop_domain=shop, access_token=online), timeout=15)
        now = lc.utcnow()
        flow = db.get(Flow, flow_id, with_for_update=True, populate_existing=True)
        conn, owner, user = _current(db, flow, lock=True)
        if _mfa_binding(db.get(UserTotp, user.id, with_for_update=True, populate_existing=True)) != mfa_binding:
            raise InstallRejected("totp_changed")
        if (flow.phase != "redeeming" or flow.expires_at <= now or flow.owner_expires_at <= now
                or identity.shop_gid != expected_gid or identity.shop_domain != shop
                or not conn or conn.status != STATUS_ACTIVE or conn.generation != flow.generation):
            raise InstallRejected()
        flow.phase, flow.online_token_enc, flow.handoff_hash = "redeemed", None, None
        flow.expires_at = min(flow.owner_expires_at, now + timedelta(seconds=SESSION_SECONDS))
        from jose import jwt
        from core.config import JWT_SECRET, JWT_ALGORITHM
        payload = {"sub": user.email, "role": "merchant", "tenant_id": user.tenant_id, "user_id": user.id,
                   "auth_source": "shopify", "shopify_session": flow.id, "shopify_shop": shop, "jti": secrets.token_urlsafe(24),
                   "iat": int(now.timestamp()), "exp": int(flow.expires_at.timestamp())}
        db.commit()
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        return {"access_token": token, "role": "merchant", "tenant_id": user.tenant_id, "user_id": user.id,
                "email": user.email, "shop": shop, "expires_in": max(0, int((flow.expires_at - now).total_seconds()))}
    except Exception:
        _fail(db, flow_id, shop)
        raise InstallRejected() from None


def validate_session(payload):
    """DB-validate every Shopify session; unavailable storage/config fails closed.

    No automatic rolling refresh. Shopify logout/owner changes without a
    webhook have a maximum five-minute Nahlah session lifetime.
    """
    from services.shopify_connection.install_config import enabled
    if not enabled() or payload.get("role") != "merchant" or payload.get("impersonation"):
        return False
    try:
        from core.database import SessionLocal
        with SessionLocal() as db:
            flow = db.get(Flow, payload.get("shopify_session"))
            if not flow or flow.phase != "redeemed" or flow.expires_at <= lc.utcnow():
                return False
            conn, owner, user = _current(db, flow)
            return bool(conn and conn.status == STATUS_ACTIVE and conn.generation == flow.generation
                        and payload.get("user_id") == user.id and payload.get("tenant_id") == user.tenant_id
                        and payload.get("sub") == user.email and payload.get("exp") <= int(flow.expires_at.timestamp()))
    except Exception:
        return False
