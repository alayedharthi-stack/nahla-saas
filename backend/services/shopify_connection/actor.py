"""
The tenant actor behind a Shopify connection mutation, revalidated against
the database on every call.

Policy (start, complete, disconnect):

  * the JWT must be a merchant session — support-impersonation tokens
    (``role == "support_impersonation"`` or ``impersonation`` set) and
    platform-staff roles are refused. ``core.auth.require_merchant_scope``
    admits impersonation; these mutations deliberately do not;
  * the ``users`` row named by ``user_id`` must exist, be active, belong to the
    JWT's tenant and still carry the permitted role (a demoted, deactivated or
    re-assigned user loses access immediately, not at token expiry);
  * the tenant row must exist and be active;
  * the session reference is the JWT ``jti``; only its domain-separated hash
    is stored with an OAuth state, and completion requires the same ``jti``.

Revocation note (inherited, not new): ``core.auth.decode_token`` and this
module consult ``core.token_revocation.is_jti_revoked``, a Redis denylist with
an in-process fallback that swallows lookup errors. It is a best-effort
check — while Redis is unreachable a revocation recorded on another worker is
not seen. The guarantees added here that do **not** depend on it are the
same-``jti`` binding (a new login after logout has a new ``jti``) and the DB
revalidation of the user and tenant.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

CONNECTION_ROLES = frozenset({"merchant"})
_SESSION_DOMAIN = b"nahla.shopify.session:v1:"


class ActorRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedActor:
    tenant_id: int
    user_id: int
    session_ref_hash: str = field(repr=False)
    jti: str = field(default="", repr=False)


def session_ref_hash(jti: str) -> str:
    return hashlib.sha256(_SESSION_DOMAIN + str(jti).encode("utf-8")).hexdigest()


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ActorRejected("session_claims_invalid")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ActorRejected("session_claims_invalid") from None
    if number <= 0:
        raise ActorRejected("session_claims_invalid")
    return number


def check_claims(payload: Mapping[str, Any]) -> tuple:
    """(tenant_id, user_id, role, jti) from a merchant JWT; refuses support/staff sessions."""
    from core.auth import PLATFORM_ADMIN_ROLES  # noqa: PLC0415

    role = str(payload.get("role") or "").strip()
    if payload.get("impersonation") or role == "support_impersonation":
        raise ActorRejected("support_session_refused")
    if role in PLATFORM_ADMIN_ROLES:
        raise ActorRejected("platform_session_refused")
    if role not in CONNECTION_ROLES:
        raise ActorRejected("role_not_permitted")
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti.strip():
        raise ActorRejected("session_unverifiable")
    return _positive_int(payload.get("tenant_id")), _positive_int(payload.get("user_id")), role, jti


def _user_and_tenant_valid(db: Any, *, tenant_id: int, user_id: int, role: str = "", lock: bool = False) -> str:
    """'' when valid, else a refusal code. ``lock`` takes FOR SHARE row locks on
    the user and tenant rows so a concurrent demotion, deactivation or
    re-assignment waits for the caller's commit (or is seen before it)."""
    from models import Tenant, User  # noqa: PLC0415

    user_q = db.query(User).filter(User.id == int(user_id))
    if lock:
        user_q = user_q.with_for_update(read=True)
    user = user_q.populate_existing().first()
    if user is None or not bool(user.is_active):
        return "actor_inactive"
    if int(user.tenant_id) != int(tenant_id):
        return "actor_tenant_mismatch"
    db_role = str(user.role or "").strip()
    if db_role not in CONNECTION_ROLES or (role and db_role != role):
        return "role_not_permitted"
    tenant_q = db.query(Tenant).filter(Tenant.id == int(tenant_id))
    if lock:
        tenant_q = tenant_q.with_for_update(read=True)
    tenant = tenant_q.populate_existing().first()
    if tenant is None or tenant.is_active is False:
        return "tenant_inactive"
    return ""


def revalidate_actor(db: Any, payload: Mapping[str, Any]) -> VerifiedActor:
    """Claims, then the live user/tenant rows, then the (best-effort) denylist."""
    from core.token_revocation import is_jti_revoked  # noqa: PLC0415

    tenant_id, user_id, role, jti = check_claims(payload)
    refusal = _user_and_tenant_valid(db, tenant_id=tenant_id, user_id=user_id, role=role)
    if refusal:
        raise ActorRejected(refusal)
    if is_jti_revoked(jti):
        raise ActorRejected("session_revoked")
    return VerifiedActor(tenant_id=tenant_id, user_id=user_id, session_ref_hash=session_ref_hash(jti), jti=jti)


def recheck_actor_locked(db: Any, actor: VerifiedActor) -> str:
    """Re-validate the actor inside the transaction that commits ownership.

    Locks the user and tenant rows (FOR SHARE) and repeats the role / active /
    tenant checks, then repeats the inherited best-effort denylist lookup.
    '' when the actor may still commit, else a refusal code.
    """
    from core.token_revocation import is_jti_revoked  # noqa: PLC0415

    refusal = _user_and_tenant_valid(db, tenant_id=actor.tenant_id, user_id=actor.user_id, lock=True)
    if refusal:
        return refusal
    if not actor.jti or is_jti_revoked(actor.jti):
        return "session_revoked"
    return ""


def actor_still_valid(db: Any, *, tenant_id: int, user_id: int) -> bool:
    """Used at the browser callback, which carries no JWT: the initiator is still eligible."""
    return _user_and_tenant_valid(db, tenant_id=tenant_id, user_id=user_id) == ""
