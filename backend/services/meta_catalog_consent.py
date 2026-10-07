"""
services/meta_catalog_consent.py
────────────────────────────────
Catalog-only Meta consent: signed state, verification of what Meta actually
granted, encrypted persistence, and the binding the catalog push path uses.

Scope requested: ``catalog_management`` and ``business_management`` only. No
WhatsApp permission is requested, no WABA or phone number is discovered,
registered or subscribed, nothing is sent, and no WhatsApp connection is
created or marked connected.

What a stored authorization proves (all read-only Graph calls with the new
user token, ``appsecret_proof`` attached):

  1. ``debug_token``: valid, issued for the configured app, a USER token with
     a user id, not expired, both scopes in ``scopes``; when Meta returns
     ``granular_scopes`` target ids for a scope, the approved id is among them.
  2. ``/me/permissions``: both scopes ``granted``; ``/me`` is the same user.
  3. ``/{catalog}?fields=id,business``: the approved catalog, owned by the
     approved business.
  4. ``/{business}/owned_product_catalogs``: the business lists the catalog.
  5. ``/me/business_users?fields=id,business,role``: the user is a business
     user with role ``ADMIN`` of exactly the approved business. Any error or a
     non-ADMIN role fails closed — readability or mere membership is never
     taken as admin proof, and ``ads_management`` is never requested.

A user access token is not limited to one asset by Meta. Restriction to the
approved catalog is enforced here (``CatalogConsentBinding.token_for``), not
claimed of the token. Write capability itself is only proven by a write.

Never logged or returned: the code, the state, any token, the app secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from core.meta_catalog_consent_config import (
    REQUESTED_SCOPES,
    CatalogApproval,
    approval_for_tenant,
    encryption_key_reason,
    runtime_consent_enabled,
)

logger = logging.getLogger("nahla.meta_catalog_consent")

PURPOSE = "meta_catalog_consent"
STATE_VERSION = 1
STATE_TTL_SECONDS = 600
_STATE_DOMAIN = b"meta_catalog_consent_state:v1:"
_CLOCK_SKEW_SECONDS = 60
TOKEN_SOURCE = "merchant_catalog_consent"
STATUS_ACTIVE = "active"
TABLE_NAME = "meta_catalog_authorizations"
_GRAPH_TIMEOUT = 15.0
_MAX_PAGES = 10
ADMIN_ROLE = "ADMIN"

# Stable result codes (also the only values the callback puts in the URL fragment).
C_CONNECTED = "connected"
C_DENIED = "denied"
C_INVALID_STATE = "invalid_state"
C_EXPIRED = "expired"
C_REPLAYED = "replayed"
C_UNAVAILABLE = "unavailable"
C_NOT_APPROVED = "not_approved"
C_ASSET_CHANGED = "asset_changed"
C_CALLBACK_MISMATCH = "callback_mismatch"
C_APP_MISMATCH = "app_mismatch"
C_MISSING_CODE = "missing_code"
C_TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
C_TOKEN_INVALID = "token_invalid"
C_TOKEN_TYPE_INVALID = "token_type_invalid"
C_TOKEN_IDENTITY = "token_identity_mismatch"
C_TOKEN_EXPIRED = "token_expired"
C_SCOPE_MISSING = "scope_missing"
C_ASSET_NOT_GRANTED = "asset_not_granted"
C_CATALOG_NOT_ACCESSIBLE = "catalog_not_accessible"
C_CATALOG_OWNER_MISMATCH = "catalog_owner_mismatch"
C_CATALOG_NOT_OWNED = "catalog_not_owned_by_business"
C_CATALOG_OWNERSHIP_UNVERIFIED = "catalog_ownership_unverified"
C_BUSINESS_ADMIN_MISSING = "business_admin_missing"
C_BUSINESS_ADMIN_UNVERIFIED = "business_admin_unverified"
C_CATALOG_CLAIMED = "catalog_claimed_by_other_tenant"
C_ENCRYPTION_UNAVAILABLE = "encryption_unavailable"
C_STORAGE_UNAVAILABLE = "storage_unavailable"
C_PERSIST_UNVERIFIED = "persist_unverified"
C_ENTITLEMENT_MISSING = "entitlement_missing"
C_SYNC_SCOPE_EXCLUDED = "sync_scope_excluded"
C_TENANT_MISSING = "tenant_missing"
C_ERROR = "error"

RESULT_CODES = frozenset({
    C_CONNECTED, C_DENIED, C_INVALID_STATE, C_EXPIRED, C_REPLAYED, C_UNAVAILABLE, C_NOT_APPROVED,
    C_ASSET_CHANGED, C_CALLBACK_MISMATCH, C_APP_MISMATCH, C_MISSING_CODE, C_TOKEN_EXCHANGE_FAILED,
    C_TOKEN_INVALID, C_TOKEN_TYPE_INVALID, C_TOKEN_IDENTITY, C_TOKEN_EXPIRED, C_SCOPE_MISSING,
    C_ASSET_NOT_GRANTED, C_CATALOG_NOT_ACCESSIBLE, C_CATALOG_OWNER_MISMATCH, C_CATALOG_NOT_OWNED,
    C_CATALOG_OWNERSHIP_UNVERIFIED, C_BUSINESS_ADMIN_MISSING, C_BUSINESS_ADMIN_UNVERIFIED,
    C_CATALOG_CLAIMED, C_ENCRYPTION_UNAVAILABLE, C_STORAGE_UNAVAILABLE, C_PERSIST_UNVERIFIED,
    C_ENTITLEMENT_MISSING, C_SYNC_SCOPE_EXCLUDED, C_TENANT_MISSING, C_ERROR,
})


class ConsentError(Exception):
    """A refusal with a stable result code. ``str()`` is the code only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code if code in RESULT_CODES else C_ERROR


# ── Signed state ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConsentState:
    tenant_id: int
    nonce: str = field(repr=False)
    issued_at: int
    expires_at: int
    redirect_uri: str
    catalog_id: str
    business_id: str
    app_id: str


def _state_key() -> bytes:
    from core.config import JWT_SECRET  # noqa: PLC0415

    secret = str(JWT_SECRET or "").encode("utf-8")
    if not secret:
        raise ConsentError(C_UNAVAILABLE)
    return secret


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text_value: str) -> bytes:
    return base64.urlsafe_b64decode(text_value + "=" * (-len(text_value) % 4))


def _state_mac(body: bytes) -> bytes:
    # Domain-separated from the WhatsApp embedded state (plain HMAC over the
    # body), so neither flow's state verifies in the other.
    return hmac.new(_state_key(), _STATE_DOMAIN + body, hashlib.sha256).digest()


def sign_state(
    *,
    tenant_id: int,
    nonce: str,
    redirect_uri: str,
    approval: CatalogApproval,
    app_id: str,
    issued_at: Optional[int] = None,
) -> str:
    iat = int(issued_at if issued_at is not None else time.time())
    body = json.dumps(
        {
            "v": STATE_VERSION,
            "p": PURPOSE,
            "t": int(tenant_id),
            "n": nonce,
            "iat": iat,
            "exp": iat + STATE_TTL_SECONDS,
            "ru": redirect_uri,
            "c": approval.catalog_id,
            "b": approval.business_id,
            "a": str(app_id),
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{_b64(body)}.{_b64(_state_mac(body))}"


def verify_state(state: str, *, now: Optional[int] = None) -> ConsentState:
    """Signature, purpose, version, field types and expiry. Raises ConsentError."""
    current = int(now if now is not None else time.time())
    try:
        body_b64, sig_b64 = str(state or "").split(".", 1)
        body = _unb64(body_b64)
        sig = _unb64(sig_b64)
    except Exception:  # noqa: BLE001
        raise ConsentError(C_INVALID_STATE) from None
    if not hmac.compare_digest(sig, _state_mac(body)):
        raise ConsentError(C_INVALID_STATE)
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != STATE_VERSION or payload.get("p") != PURPOSE:
            raise ValueError("purpose")
        parsed = ConsentState(
            tenant_id=int(payload["t"]),
            nonce=str(payload["n"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            redirect_uri=str(payload["ru"]),
            catalog_id=str(payload["c"]),
            business_id=str(payload["b"]),
            app_id=str(payload["a"]),
        )
    except Exception:  # noqa: BLE001
        raise ConsentError(C_INVALID_STATE) from None
    if not all([parsed.nonce, parsed.redirect_uri, parsed.catalog_id, parsed.business_id, parsed.app_id]):
        raise ConsentError(C_INVALID_STATE)
    if parsed.tenant_id <= 0 or parsed.expires_at - parsed.issued_at != STATE_TTL_SECONDS:
        raise ConsentError(C_INVALID_STATE)
    if parsed.issued_at > current + _CLOCK_SKEW_SECONDS:
        raise ConsentError(C_INVALID_STATE)
    if current > parsed.expires_at:
        raise ConsentError(C_EXPIRED)
    return parsed


def build_authorize_url(*, state: str, redirect_uri: str, app_id: str, config_id: str, graph_version: str) -> str:
    from urllib.parse import urlencode  # noqa: PLC0415

    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "config_id": config_id,
        "scope": ",".join(REQUESTED_SCOPES),
        "state": state,
    }
    return f"https://www.facebook.com/{graph_version}/dialog/oauth?{urlencode(params)}"


# ── Graph (read-only) ─────────────────────────────────────────────────────────

def _graph_version() -> str:
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415

    return str(META_GRAPH_API_VERSION or "v20.0")


def _appsecret_proof(token: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


async def _graph_get(path: str, params: Dict[str, Any], *, token: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    """GET graph.facebook.com/<version>/<path>. Returns (http_status, json body or {}).

    Never raises for HTTP errors; transport errors raise ConsentError. The
    user token travels in the Authorization header, never in the URL.
    """
    url = f"https://graph.facebook.com/{_graph_version()}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=_GRAPH_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError:
        logger.warning("[META_CATALOG_CONSENT] graph transport failure path=%s", path.split("?")[0][:64])
        raise ConsentError(C_ERROR) from None
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    return int(resp.status_code), body if isinstance(body, dict) else {}


def _ok(status: int, body: Dict[str, Any]) -> bool:
    return 200 <= status < 300 and "error" not in body


async def _collect(
    path: str,
    params: Dict[str, Any],
    *,
    token: str,
    app_secret: str,
) -> Optional[List[Dict[str, Any]]]:
    """All ``data`` rows of an edge (cursor paging, bounded). None on any error."""
    rows: List[Dict[str, Any]] = []
    query = dict(params)
    query["appsecret_proof"] = _appsecret_proof(token, app_secret)
    for _ in range(_MAX_PAGES):
        status, body = await _graph_get(path, query, token=token)
        if not _ok(status, body) or not isinstance(body.get("data"), list):
            return None
        rows.extend(r for r in body["data"] if isinstance(r, dict))
        paging = body.get("paging") if isinstance(body.get("paging"), dict) else {}
        after = ((paging.get("cursors") or {}) if isinstance(paging.get("cursors"), dict) else {}).get("after")
        if not paging.get("next") or not after:
            return rows
        query["after"] = after
    return None  # more pages than the bound: not proven


@dataclass(frozen=True)
class VerifiedConsent:
    access_token: str = field(repr=False)
    meta_user_id: str
    scopes: Tuple[str, ...]
    token_expires_at: Optional[datetime]
    data_access_expires_at: Optional[datetime]


def _epoch(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


async def exchange_code(*, code: str, redirect_uri: str, app_id: str, app_secret: str) -> Tuple[str, Optional[int]]:
    status, body = await _graph_get(
        "oauth/access_token",
        {"client_id": app_id, "client_secret": app_secret, "redirect_uri": redirect_uri, "code": code},
    )
    token = str(body.get("access_token") or "") if _ok(status, body) else ""
    if not token:
        raise ConsentError(C_TOKEN_EXCHANGE_FAILED)
    long_status, long_body = await _graph_get(
        "oauth/access_token",
        {"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
         "fb_exchange_token": token},
    )
    long_token = str(long_body.get("access_token") or "") if _ok(long_status, long_body) else ""
    if long_token:
        return long_token, _epoch(long_body.get("expires_in"))
    return token, _epoch(body.get("expires_in"))


def _granular_ok(granular: Iterable[Any], scope: str, required_id: str) -> bool:
    """When Meta lists target ids for *scope*, the approved id must be one of them."""
    for entry in granular or []:
        if not isinstance(entry, dict) or entry.get("scope") != scope:
            continue
        targets = entry.get("target_ids")
        if isinstance(targets, list) and targets:
            return str(required_id) in {str(t) for t in targets}
    return True


async def verify_access(
    *,
    token: str,
    approval: CatalogApproval,
    app_id: str,
    app_secret: str,
    now: Optional[datetime] = None,
) -> VerifiedConsent:
    """Everything listed in the module docstring, in order. Raises ConsentError."""
    current = now or datetime.now(timezone.utc)
    stamp = int(current.timestamp())

    status, body = await _graph_get("debug_token", {"input_token": token, "access_token": f"{app_id}|{app_secret}"})
    data = body.get("data") if _ok(status, body) and isinstance(body.get("data"), dict) else None
    if not data or data.get("is_valid") is not True:
        raise ConsentError(C_TOKEN_INVALID)
    if str(data.get("app_id") or "") != str(app_id):
        raise ConsentError(C_APP_MISMATCH)
    if str(data.get("type") or "").upper() != "USER":
        raise ConsentError(C_TOKEN_TYPE_INVALID)
    user_id = str(data.get("user_id") or "")
    if not user_id:
        raise ConsentError(C_TOKEN_IDENTITY)
    expires = _epoch(data.get("expires_at"))
    if expires is None or (expires != 0 and expires <= stamp + _CLOCK_SKEW_SECONDS):
        raise ConsentError(C_TOKEN_EXPIRED)
    data_access = _epoch(data.get("data_access_expires_at"))
    if data_access is not None and data_access != 0 and data_access <= stamp + _CLOCK_SKEW_SECONDS:
        raise ConsentError(C_TOKEN_EXPIRED)
    scopes = {str(s) for s in (data.get("scopes") or []) if isinstance(s, str)}
    if not set(REQUESTED_SCOPES) <= scopes:
        raise ConsentError(C_SCOPE_MISSING)
    granular = data.get("granular_scopes") if isinstance(data.get("granular_scopes"), list) else []
    if not _granular_ok(granular, "catalog_management", approval.catalog_id):
        raise ConsentError(C_ASSET_NOT_GRANTED)
    if not _granular_ok(granular, "business_management", approval.business_id):
        raise ConsentError(C_ASSET_NOT_GRANTED)

    proof = {"appsecret_proof": _appsecret_proof(token, app_secret)}

    status, me = await _graph_get("me", {"fields": "id", **proof}, token=token)
    if not _ok(status, me) or str(me.get("id") or "") != user_id:
        raise ConsentError(C_TOKEN_IDENTITY)

    permissions = await _collect("me/permissions", {}, token=token, app_secret=app_secret)
    if permissions is None:
        raise ConsentError(C_SCOPE_MISSING)
    granted = {str(p.get("permission")) for p in permissions if str(p.get("status")) == "granted"}
    if not set(REQUESTED_SCOPES) <= granted:
        raise ConsentError(C_SCOPE_MISSING)

    status, catalog = await _graph_get(approval.catalog_id, {"fields": "id,business{id}", **proof}, token=token)
    if not _ok(status, catalog):
        raise ConsentError(C_CATALOG_NOT_ACCESSIBLE)
    owner = catalog.get("business") if isinstance(catalog.get("business"), dict) else {}
    if str(catalog.get("id") or "") != approval.catalog_id or str(owner.get("id") or "") != approval.business_id:
        raise ConsentError(C_CATALOG_OWNER_MISMATCH)

    owned = await _collect(
        f"{approval.business_id}/owned_product_catalogs", {"fields": "id", "limit": 100},
        token=token, app_secret=app_secret,
    )
    if owned is None:
        raise ConsentError(C_CATALOG_OWNERSHIP_UNVERIFIED)
    if approval.catalog_id not in {str(r.get("id") or "") for r in owned}:
        raise ConsentError(C_CATALOG_NOT_OWNED)

    business_users = await _collect(
        "me/business_users", {"fields": "id,business{id},role", "limit": 100},
        token=token, app_secret=app_secret,
    )
    if business_users is None:
        raise ConsentError(C_BUSINESS_ADMIN_UNVERIFIED)
    is_admin = any(
        isinstance(row.get("business"), dict)
        and str(row["business"].get("id") or "") == approval.business_id
        and str(row.get("role") or "") == ADMIN_ROLE
        for row in business_users
    )
    if not is_admin:
        raise ConsentError(C_BUSINESS_ADMIN_MISSING)

    return VerifiedConsent(
        access_token=token,
        meta_user_id=user_id,
        scopes=tuple(sorted(scopes)),
        token_expires_at=None if expires == 0 else datetime.fromtimestamp(expires, tz=timezone.utc),
        data_access_expires_at=(
            None if not data_access else datetime.fromtimestamp(data_access, tz=timezone.utc)
        ),
    )


# ── Persistence ───────────────────────────────────────────────────────────────

_TABLE_SEEN: Dict[Tuple[int, str], float] = {}
_NEGATIVE_TTL = 60.0


def authorization_table_exists(db: Any) -> bool:
    """Whether the table exists on this bind (positive answers cached per engine)."""
    from sqlalchemy import inspect  # noqa: PLC0415

    get_bind = getattr(db, "get_bind", None)
    if get_bind is None:
        # Not a SQLAlchemy session (a test double or a non-DB caller): there is
        # no consent table to consult. Consent entry points treat False as
        # storage_unavailable, so this never enables the consent path.
        return False
    try:
        bind = get_bind()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — unbound session: no consent schema to consult
        return False
    engine = getattr(bind, "engine", bind)
    key = (id(engine), str(getattr(engine, "url", "")))
    seen = _TABLE_SEEN.get(key)
    if seen == float("inf"):
        return True
    if seen is not None and time.monotonic() - seen < _NEGATIVE_TTL:
        return False
    try:
        present = TABLE_NAME in inspect(bind).get_table_names()
    except Exception:  # noqa: BLE001
        logger.warning("[META_CATALOG_CONSENT] schema inspect failed")
        present = False
    _TABLE_SEEN[key] = float("inf") if present else time.monotonic()
    return present


def _encrypt(token: str) -> str:
    from core.wa_token_crypto import decrypt_access_token, encrypt_access_token  # noqa: PLC0415

    if encryption_key_reason() is not None:
        raise ConsentError(C_ENCRYPTION_UNAVAILABLE)
    try:
        stored = encrypt_access_token(token)
        if not stored.startswith("enc1:") or decrypt_access_token(stored) != token:
            raise ValueError("round_trip")
    except ConsentError:
        raise
    except Exception:  # noqa: BLE001 — the message could carry key material; never echoed
        raise ConsentError(C_ENCRYPTION_UNAVAILABLE) from None
    return stored


def _decrypt(stored: str) -> Optional[str]:
    from core.wa_token_crypto import decrypt_access_token  # noqa: PLC0415

    if encryption_key_reason() is not None or not str(stored or "").startswith("enc1:"):
        return None
    try:
        return decrypt_access_token(stored) or None
    except Exception:  # noqa: silent-ok — undecryptable token is reported as inactive (fail closed), never logged
        return None


def persist_authorization(
    db: Any,
    *,
    approval: CatalogApproval,
    verified: VerifiedConsent,
    app_id: str,
    now: Optional[datetime] = None,
) -> None:
    """Upsert the tenant's authorization, commit, then prove it reads back.

    Takes the shared per-catalog claim lock and refuses a catalog another
    tenant holds (WhatsApp binding or consent). Raises ConsentError.
    """
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    from models import MetaCatalogAuthorization  # noqa: PLC0415
    from services.meta_catalog_claim import CatalogClaimError, guard_catalog_claim  # noqa: PLC0415

    stamp = now or datetime.now(timezone.utc)
    if not authorization_table_exists(db):
        raise ConsentError(C_STORAGE_UNAVAILABLE)
    stored = _encrypt(verified.access_token)
    try:
        guard_catalog_claim(db, approval.tenant_id, approval.catalog_id)
        row = (
            db.query(MetaCatalogAuthorization)
            .filter(MetaCatalogAuthorization.tenant_id == approval.tenant_id)
            .with_for_update()
            .first()
        )
        if row is None:
            row = MetaCatalogAuthorization(tenant_id=approval.tenant_id, created_at=stamp)
            db.add(row)
        row.catalog_id = approval.catalog_id
        row.business_id = approval.business_id
        row.meta_app_id = str(app_id)
        row.meta_user_id = verified.meta_user_id
        row.access_token_enc = stored
        row.granted_scopes = list(verified.scopes)
        row.token_expires_at = verified.token_expires_at
        row.data_access_expires_at = verified.data_access_expires_at
        row.status = STATUS_ACTIVE
        row.verified_at = stamp
        row.updated_at = stamp
        db.flush()
        db.commit()
    except CatalogClaimError:
        db.rollback()
        raise ConsentError(C_CATALOG_CLAIMED) from None
    except IntegrityError:
        db.rollback()
        raise ConsentError(C_CATALOG_CLAIMED) from None
    except ConsentError:
        db.rollback()
        raise
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.error("[META_CATALOG_CONSENT] persist failed tenant=%s", approval.tenant_id)
        raise ConsentError(C_STORAGE_UNAVAILABLE) from None

    db.expire_all()
    check = (
        db.query(MetaCatalogAuthorization)
        .filter(MetaCatalogAuthorization.tenant_id == approval.tenant_id)
        .first()
    )
    if (
        check is None
        or check.status != STATUS_ACTIVE
        or check.catalog_id != approval.catalog_id
        or check.business_id != approval.business_id
        or _decrypt(check.access_token_enc) != verified.access_token
    ):
        raise ConsentError(C_PERSIST_UNVERIFIED)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _row_inactive_reason(row: Any, now: datetime) -> Optional[str]:
    """Why a stored authorization must not be used; None when usable.

    Evaluated before the token is ever decrypted or sent anywhere.
    """
    from core.meta_catalog_consent_config import meta_app_credentials  # noqa: PLC0415

    if str(row.status or "") != STATUS_ACTIVE:
        return "authorization_inactive"
    if not runtime_consent_enabled():
        return "consent_disabled"
    current_app_id, _secret = meta_app_credentials()
    if not current_app_id or str(row.meta_app_id or "") != current_app_id:
        # A consent issued for another (e.g. previous) Meta app is never reused.
        return "app_changed"
    approval = approval_for_tenant(int(row.tenant_id))
    if approval is None or approval.catalog_id != row.catalog_id or approval.business_id != row.business_id:
        return "approval_changed"
    for value in (_aware(row.token_expires_at), _aware(row.data_access_expires_at)):
        if value is not None and value <= now:
            return "authorization_expired"
    return None


def authorization_summary(db: Any, tenant_id: int, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Secret-free state for the dashboard."""
    from models import MetaCatalogAuthorization  # noqa: PLC0415

    if not authorization_table_exists(db):
        return {"state": "none"}
    row = (
        db.query(MetaCatalogAuthorization)
        .filter(MetaCatalogAuthorization.tenant_id == int(tenant_id))
        .first()
    )
    if row is None:
        return {"state": "none"}
    current = now or datetime.now(timezone.utc)
    reason = _row_inactive_reason(row, current)
    if reason is None and _decrypt(row.access_token_enc) is None:
        reason = "token_unreadable"

    def _iso(value: Optional[datetime]) -> Optional[str]:
        v = _aware(value)
        return v.isoformat() if v else None

    return {
        "state": "active" if reason is None else ("expired" if reason == "authorization_expired" else "inactive"),
        "inactive_reason": reason,
        "catalog_id": row.catalog_id,
        "business_id": row.business_id,
        "granted_scopes": [s for s in (row.granted_scopes or []) if isinstance(s, str)],
        "verified_at": _iso(row.verified_at),
        "token_expires_at": _iso(row.token_expires_at),
    }


# ── Binding used by the catalog push path ────────────────────────────────────

class CatalogConsentInactive(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class CatalogConsentBinding:
    """Stands in for the connection in catalog push code; consent token only."""

    tenant_id: int
    meta_catalog_id: str
    business_id: str
    catalog_enabled: bool
    access_token: str = field(repr=False)
    provider: str = "meta_catalog_consent"
    connection_type: str = "catalog_consent"
    is_catalog_consent_binding: bool = True

    def token_for(self, catalog_id: str) -> str:
        if str(catalog_id or "").strip() != self.meta_catalog_id:
            raise CatalogConsentInactive("catalog_not_authorized")
        return self.access_token


def is_consent_binding(conn: Any) -> bool:
    return isinstance(conn, CatalogConsentBinding)


def _authorization_row(db: Any, tenant_id: int) -> Any:
    from models import MetaCatalogAuthorization  # noqa: PLC0415

    if not authorization_table_exists(db):
        return None
    return (
        db.query(MetaCatalogAuthorization)
        .filter(MetaCatalogAuthorization.tenant_id == int(tenant_id))
        .first()
    )


def consent_row_exists(db: Any, tenant_id: int) -> bool:
    """Whether this tenant has any stored consent. Touches no WhatsApp table."""
    return _authorization_row(db, tenant_id) is not None


def consent_governs_catalog(db: Any, tenant_id: int, conn: Any) -> bool:
    """True when a stored consent (active or not) owns this tenant's catalog work.

    Same coverage rule as ``load_catalog_consent_binding``; used to keep the
    catalog-only path free of WhatsApp/WABA probes even when the consent is
    currently unusable.
    """
    row = _authorization_row(db, tenant_id)
    if row is None:
        return False
    conn_catalog = str(getattr(conn, "meta_catalog_id", "") or "").strip() if conn is not None else ""
    return not conn_catalog or conn_catalog == row.catalog_id


def _tenant_gate_reason(db: Any, tenant_id: int) -> Optional[str]:
    """Entitlement and sync scope, re-checked before every consent-backed operation."""
    from core.plan_entitlements import get_entitlements  # noqa: PLC0415
    from services.whatsapp_catalog_sync_scope import tenant_in_sync_scope  # noqa: PLC0415

    try:
        if not get_entitlements(db, int(tenant_id), strict_lookup=True).has_feature("meta_catalog_sync"):
            return "entitlement_missing"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unreadable entitlement fails closed as missing
        return "entitlement_missing"
    if not tenant_in_sync_scope(int(tenant_id)):
        return "sync_scope_excluded"
    return None


def load_catalog_consent_binding(db: Any, tenant_id: int, conn: Any) -> Optional[CatalogConsentBinding]:
    """The consent binding for this tenant's catalog work, or None to keep the old path.

    None when no authorization row exists (every tenant until consent is
    granted) or when the tenant's WhatsApp connection is bound to a different
    catalog. When the row covers the catalog in use but is inactive, expired,
    disabled, no longer approved or unreadable, raises CatalogConsentInactive:
    the approved catalog is never served by a WhatsApp or platform token.
    """
    row = _authorization_row(db, tenant_id)
    if row is None:
        return None
    conn_catalog = str(getattr(conn, "meta_catalog_id", "") or "").strip() if conn is not None else ""
    if conn_catalog and conn_catalog != row.catalog_id:
        return None
    reason = _row_inactive_reason(row, datetime.now(timezone.utc)) or _tenant_gate_reason(db, tenant_id)
    if reason:
        raise CatalogConsentInactive(reason)
    token = _decrypt(row.access_token_enc)
    if not token:
        raise CatalogConsentInactive("token_unreadable")
    enabled = bool(getattr(conn, "catalog_enabled", False)) if conn_catalog else True
    return CatalogConsentBinding(
        tenant_id=int(tenant_id),
        meta_catalog_id=row.catalog_id,
        business_id=row.business_id,
        catalog_enabled=enabled,
        access_token=token,
    )


__all__ = [
    "C_CONNECTED",
    "CatalogConsentBinding",
    "CatalogConsentInactive",
    "ConsentError",
    "ConsentState",
    "PURPOSE",
    "RESULT_CODES",
    "STATE_TTL_SECONDS",
    "TOKEN_SOURCE",
    "VerifiedConsent",
    "authorization_summary",
    "authorization_table_exists",
    "consent_governs_catalog",
    "consent_row_exists",
    "build_authorize_url",
    "exchange_code",
    "is_consent_binding",
    "load_catalog_consent_binding",
    "persist_authorization",
    "sign_state",
    "verify_access",
    "verify_state",
]
