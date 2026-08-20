"""
services/salla_store_identity.py
────────────────────────────────
Normalize Salla store identity and resolve Integration rows across
canonical store_id and known alias IDs (merchant account id, legacy config).

AD-SALLA-ID-1 — Canonical store owns tenant.
``integrations.external_store_id`` is the only primary ownership key.
``config.store_id`` is a narrow legacy canonical bridge when
``external_store_id`` is empty. Merchant/account aliases
(``salla_merchant_id_alt``, ``config.merchant_id``) may correlate identity
but MUST NOT prove store ownership.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

SallaIdentitySource = Literal["canonical_store_id", "merchant_account_only"]

logger = logging.getLogger("nahla.salla_store_identity")

SALLA_STORE_INFO_URL = "https://api.salla.dev/admin/v2/store/info"


class SallaStoreIdentityConflictError(RuntimeError):
    """Multiple tenants match the same Salla store identity — fail closed."""

    def __init__(
        self,
        lookup_id: str,
        tenant_ids: List[int],
        matched_via: str,
    ) -> None:
        self.lookup_id = lookup_id
        self.tenant_ids = tenant_ids
        self.matched_via = matched_via
        super().__init__(
            f"Salla store identity conflict for {lookup_id}: tenants {tenant_ids} "
            f"(matched_via={matched_via})",
        )


@dataclass
class SallaTenantGuardResult:
    """Result of verifying a JWT tenant against a Salla store owner."""

    ok: bool
    owner_tenant_id: Optional[int] = None
    integration_id: Optional[int] = None
    matched_via: str = ""
    reason: str = ""


def _log_salla_tenant_guard(
    *,
    context: str,
    jwt_tenant_id: int,
    store_id: str,
    result: SallaTenantGuardResult,
) -> None:
    status = "pass" if result.ok else "fail"
    logger.info(
        "[SallaTenantGuard] %s %s tenant_id=%s store_id=%s integration_id=%s "
        "matched_via=%s reason=%s",
        status,
        context or "verify",
        jwt_tenant_id,
        store_id or "-",
        result.integration_id,
        result.matched_via or "-",
        result.reason or "-",
    )


def verify_jwt_tenant_owns_salla_store(
    db: Session,
    *,
    jwt_tenant_id: int,
    store_id: str,
    context: str = "",
    allow_alias_match: bool = False,
) -> SallaTenantGuardResult:
    """Fail closed unless ``jwt_tenant_id`` owns ``store_id`` via integrations.

    Embedded/session/launch paths pass ``allow_alias_match=False`` so
    ``salla_merchant_id_alt`` / ``config.merchant_id`` cannot prove ownership
  when only a merchant account id is known.

  Returns ``SallaTenantGuardResult`` with ``reason`` one of:
  ``ok``, ``store_id_required``, ``invalid_tenant``, ``store_not_registered``,
  ``merchant_identity_not_canonical``, ``store_tenant_mismatch``.
    """
    sid = _str_id(store_id)
    if not sid:
        result = SallaTenantGuardResult(ok=False, reason="store_id_required")
        _log_salla_tenant_guard(
            context=context, jwt_tenant_id=jwt_tenant_id, store_id=sid, result=result,
        )
        return result

    if jwt_tenant_id <= 0:
        result = SallaTenantGuardResult(ok=False, reason="invalid_tenant")
        _log_salla_tenant_guard(
            context=context, jwt_tenant_id=jwt_tenant_id, store_id=sid, result=result,
        )
        return result

    owner_tenant_id, integration, matched_via = _resolve_owner_via_integration_lookup(
        db,
        sid,
        include_disabled=True,
        allow_alias_match=allow_alias_match,
    )

    if owner_tenant_id is None:
        _alias_row, alias_via = find_salla_integration_by_identity(
            db, sid, include_disabled=True, allow_alias_match=True,
        )
        if _alias_row is not None and alias_via not in ("", "external_store_id"):
            result = SallaTenantGuardResult(
                ok=False,
                owner_tenant_id=_alias_row.tenant_id,
                integration_id=_alias_row.id,
                matched_via=alias_via,
                reason="merchant_identity_not_canonical",
            )
            logger.warning(
                "[SallaTenantGuard] FAIL merchant_identity_not_canonical | "
                "context=%s jwt_tenant_id=%s store_id=%s alias_matched_via=%s "
                "alias_owner_tenant=%s has_canonical_store_id=false",
                context or "verify",
                jwt_tenant_id,
                sid,
                alias_via,
                _alias_row.tenant_id,
            )
            _log_salla_tenant_guard(
                context=context, jwt_tenant_id=jwt_tenant_id, store_id=sid, result=result,
            )
            return result

        result = SallaTenantGuardResult(ok=False, reason="store_not_registered")
        _log_salla_tenant_guard(
            context=context, jwt_tenant_id=jwt_tenant_id, store_id=sid, result=result,
        )
        return result

    if owner_tenant_id != jwt_tenant_id:
        result = SallaTenantGuardResult(
            ok=False,
            owner_tenant_id=owner_tenant_id,
            integration_id=integration.id if integration else None,
            matched_via=matched_via,
            reason="store_tenant_mismatch",
        )
        _log_salla_tenant_guard(
            context=context, jwt_tenant_id=jwt_tenant_id, store_id=sid, result=result,
        )
        return result

    result = SallaTenantGuardResult(
        ok=True,
        owner_tenant_id=owner_tenant_id,
        integration_id=integration.id if integration else None,
        matched_via=matched_via,
        reason="ok",
    )
    _log_salla_tenant_guard(
        context=context, jwt_tenant_id=jwt_tenant_id, store_id=sid, result=result,
    )
    return result


def _resolve_owner_via_integration_lookup(
    db: Session,
    lookup_id: str,
    *,
    include_disabled: bool,
    allow_alias_match: bool,
) -> Tuple[Optional[int], Optional[Any], str]:
    row, matched_via = find_salla_integration_by_identity(
        db,
        lookup_id,
        include_disabled=include_disabled,
        allow_alias_match=allow_alias_match,
    )
    if row is None:
        return None, None, ""
    return row.tenant_id, row, matched_via


def reject_merchant_account_only_alias_routing(
    db: Session,
    *,
    merchant_account_id: str,
    context: str = "token_login",
) -> Optional[SallaTenantGuardResult]:
    """Block embedded routing when only merchant id is known but an alias row exists."""
    mid = _str_id(merchant_account_id)
    if not mid:
        return None

    strict_owner, strict_integ, strict_via = _resolve_owner_via_integration_lookup(
        db, mid, include_disabled=True, allow_alias_match=False,
    )
    if strict_owner is not None:
        return None

    alias_row, alias_via = find_salla_integration_by_identity(
        db, mid, include_disabled=True, allow_alias_match=True,
    )
    if alias_row is None or alias_via in ("", "external_store_id"):
        return None

    result = SallaTenantGuardResult(
        ok=False,
        owner_tenant_id=alias_row.tenant_id,
        integration_id=alias_row.id,
        matched_via=alias_via,
        reason="merchant_identity_not_canonical",
    )
    logger.warning(
        "[SallaLogin] merchant_account_only rejected for tenant routing | "
        "context=%s merchant_account_id=%s has_store_id=false "
        "alias_matched_via=%s alias_owner_tenant=%s",
        context,
        mid,
        alias_via,
        alias_row.tenant_id,
    )
    _log_salla_tenant_guard(
        context=context,
        jwt_tenant_id=0,
        store_id=mid,
        result=result,
    )
    return result


SALLA_STORE_LINK_REQUIRED_CODE = "salla_store_link_required"
SALLA_OAUTH_SYNC_NEXT_ACTION = "oauth_sync"
SALLA_EMBEDDED_OAUTH_START_PATH = "/api/salla/oauth/start?embedded_reconcile=1"


def build_merchant_identity_not_canonical_detail(
    *,
    identity_source: str = "merchant_account_only",
    merchant_account_id: str = "",
    has_canonical_store_id: bool = False,
) -> dict:
    """Structured 403 payload for embedded merchant-only identity gaps."""
    payload: dict = {
        "detail": "merchant_identity_not_canonical",
        "code": SALLA_STORE_LINK_REQUIRED_CODE,
        "identity_source": identity_source,
        "has_canonical_store_id": has_canonical_store_id,
        "next_action": SALLA_OAUTH_SYNC_NEXT_ACTION,
        "oauth_start_path": SALLA_EMBEDDED_OAUTH_START_PATH,
    }
    mid = _str_id(merchant_account_id)
    if mid:
        payload["merchant_account_id"] = mid
    return payload


@dataclass
class SallaStoreIdentity:
    """Typed Salla identifiers for one logical store.

    ``store_id`` / ``canonical_store_id`` is the Salla store identity
    (store/info ``data.id`` or introspect ``store.id``).
    ``merchant_account_id`` and ``authorizing_user_id`` are correlation
    only and never prove tenant ownership.
    """

    store_id: str
    merchant_account_id: str = ""
    authorizing_user_id: str = ""
    store_name: str = ""
    owner_email: str = ""
    resolved_via: str = ""
    alias_ids: List[str] = field(default_factory=list)

    @property
    def canonical_store_id(self) -> str:
        return _str_id(self.store_id)

    @property
    def identity_source(self) -> SallaIdentitySource | Literal[""]:
        if self.canonical_store_id:
            return "canonical_store_id"
        if self.merchant_account_id or self.resolved_via == "merchant_account_only":
            return "merchant_account_only"
        return ""

    @property
    def all_lookup_ids(self) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for raw in (self.store_id, self.merchant_account_id, *self.alias_ids):
            s = str(raw or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out


def _str_id(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s if s and s.lower() not in ("none", "null") else ""


def _merchant_account_from_payload(payload: Dict[str, Any]) -> str:
    merchant = payload.get("merchant") or {}
    if isinstance(merchant, dict):
        mid = _str_id(merchant.get("id"))
        if mid:
            return mid
    return _str_id(payload.get("merchant_id"))


def extract_store_id_from_introspect(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(canonical_store_id, merchant_account_id)`` from introspect data.

    Prefers stable store identity over merchant account id.
    """
    if not isinstance(payload, dict):
        return "", ""

    merchant_account_id = _merchant_account_from_payload(payload)

    store = payload.get("store")
    if isinstance(store, dict):
        store_id = _str_id(store.get("id") or store.get("store_id"))
        if store_id:
            return store_id, merchant_account_id

    store_id = _str_id(payload.get("store_id"))
    if store_id:
        return store_id, merchant_account_id

    return "", merchant_account_id


async def fetch_store_identity_from_api(
    access_token: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[str, str]:
    """Call Salla ``/admin/v2/store/info``; return ``(store_id, store_name)``."""
    token = (access_token or "").strip()
    if not token:
        return "", ""

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        resp = await client.get(
            SALLA_STORE_INFO_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "[SallaIdentity] store/info HTTP %s",
                resp.status_code,
            )
            return "", ""
        data = (resp.json() or {}).get("data", {}) or {}
        identity = store_identity_from_store_info(data)
        return identity.canonical_store_id, identity.store_name
    except Exception as exc:
        logger.warning("[SallaIdentity] store/info failed: %s", exc)
        return "", ""
    finally:
        if owns_client and client is not None:
            await client.aclose()


async def resolve_salla_store_identity(
    introspect_payload: Dict[str, Any],
    access_token: str = "",
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> SallaStoreIdentity:
    """Resolve canonical store id from introspect payload and optional store/info."""
    payload = introspect_payload if isinstance(introspect_payload, dict) else {}
    store_id, merchant_account_id = extract_store_id_from_introspect(payload)
    resolved_via = "introspect_store" if store_id else ""

    store_name = ""
    owner_email = ""

    store_obj = payload.get("store")
    if isinstance(store_obj, dict):
        store_name = str(store_obj.get("name") or store_obj.get("store_name") or "").strip()
    if not store_name:
        store_name = str(payload.get("store_name") or "").strip()

    merchant = payload.get("merchant") or {}
    if isinstance(merchant, dict):
        if not store_name:
            store_name = str(merchant.get("name") or "").strip()
        owner_email = (
            str(merchant.get("email") or merchant.get("mobile") or "").strip().lower()
        )
    if not owner_email:
        owner_email = str(payload.get("email") or "").strip().lower()

    if not store_id and access_token:
        api_store_id, api_store_name = await fetch_store_identity_from_api(
            access_token, client=client,
        )
        if api_store_id:
            store_id = api_store_id
            resolved_via = "store_info"
        if api_store_name and not store_name:
            store_name = api_store_name

    alias_ids: List[str] = []
    if merchant_account_id and merchant_account_id != store_id:
        alias_ids.append(merchant_account_id)

    return SallaStoreIdentity(
        store_id=store_id,
        merchant_account_id=merchant_account_id,
        store_name=store_name,
        owner_email=owner_email,
        resolved_via=resolved_via or ("merchant_account_only" if merchant_account_id else ""),
        alias_ids=alias_ids,
    )


def store_identity_from_store_info(data: Dict[str, Any]) -> SallaStoreIdentity:
    """Typed identity from GET ``/admin/v2/store/info`` ``data``.

    Salla documents ``data.id`` as Store ID. Nested ``merchant.id`` and
    ``owner.id`` are correlation only and are never used as ``store_id``.
    """
    if not isinstance(data, dict):
        return SallaStoreIdentity(store_id="", resolved_via="store_info")

    store_id = _str_id(data.get("id") or data.get("store_id"))
    store_name = str(data.get("name") or data.get("store_name") or "").strip()

    merchant_account_id = ""
    merchant = data.get("merchant")
    if isinstance(merchant, dict):
        merchant_account_id = _str_id(merchant.get("id"))
    else:
        merchant_account_id = _str_id(merchant)

    authorizing_user_id = ""
    owner = data.get("owner")
    if isinstance(owner, dict):
        authorizing_user_id = _str_id(owner.get("id"))

    alias_ids: List[str] = []
    if merchant_account_id and merchant_account_id != store_id:
        alias_ids.append(merchant_account_id)

    resolved_via = "store_info"
    if not store_id and merchant_account_id:
        resolved_via = "merchant_account_only"

    return SallaStoreIdentity(
        store_id=store_id,
        merchant_account_id=merchant_account_id,
        authorizing_user_id=authorizing_user_id,
        store_name=store_name,
        resolved_via=resolved_via,
        alias_ids=alias_ids,
    )


def normalize_salla_ids_from_event_data(data: Dict[str, Any]) -> SallaStoreIdentity:
    """Normalize merchant/store ids from webhook or OAuth event payloads."""
    if not isinstance(data, dict):
        return SallaStoreIdentity(store_id="")

    store = data.get("store")
    store_id = ""
    store_name = ""
    if isinstance(store, dict):
        store_id = _str_id(store.get("id") or store.get("store_id"))
        store_name = str(store.get("name") or "").strip()

    merchant_account_id = _str_id(data.get("merchant_id"))
    if not merchant_account_id and isinstance(data.get("merchant"), dict):
        merchant_account_id = _str_id(data["merchant"].get("id"))

    if not store_id:
        store_id = _str_id(data.get("store_id"))

    if not store_id and merchant_account_id:
        store_id = merchant_account_id
        merchant_account_id = ""

    alias_ids: List[str] = []
    if merchant_account_id and merchant_account_id != store_id:
        alias_ids.append(merchant_account_id)

    return SallaStoreIdentity(
        store_id=store_id,
        merchant_account_id=merchant_account_id,
        store_name=store_name or str(data.get("name") or "").strip(),
        alias_ids=alias_ids,
        resolved_via="webhook_payload",
    )


def _integration_config_matches_id(cfg: dict, lookup_id: str) -> bool:
    if not lookup_id:
        return False
    for key in ("store_id", "salla_merchant_id_alt", "merchant_id"):
        if _str_id(cfg.get(key)) == lookup_id:
            return True
    return False


def find_salla_integration_by_identity(
    db: Session,
    lookup_id: str,
    *,
    include_disabled: bool = True,
    allow_alias_match: bool = False,
    allow_merchant_alias: bool = True,
) -> Tuple[Optional[Any], str]:
    """Find a Salla integration by canonical or alias id.

    When ``allow_alias_match`` is False, only ``integrations.external_store_id``
    may match.

    When ``allow_alias_match`` is True:
      * ``config.store_id`` is a narrow legacy canonical-metadata bridge and
        matches only when ``external_store_id`` is empty
      * ``salla_merchant_id_alt`` / ``config.merchant_id`` match only if
        ``allow_merchant_alias`` is True (default, correlation / webhooks).
        Ownership callers must pass ``allow_merchant_alias=False``.

    Returns ``(integration, matched_via)`` where ``matched_via`` is one of:
    ``external_store_id``, ``config.store_id``, ``config.salla_merchant_id_alt``,
    ``config.merchant_id``, or ``""`` when not found.

    Raises ``SallaStoreIdentityConflictError`` when the same identity maps to
    more than one tenant (fail closed — never pick an arbitrary first row).
    """
    from models import Integration  # noqa: PLC0415

    sid = _str_id(lookup_id)
    if not sid:
        return None, ""

    q = db.query(Integration).filter(Integration.provider == "salla")
    if not include_disabled:
        q = q.filter(Integration.enabled == True)  # noqa: E712

    canonical_rows = list(q.filter(Integration.external_store_id == sid).all())
    canonical_tenants = sorted({row.tenant_id for row in canonical_rows})
    if len(canonical_tenants) > 1:
        logger.error(
            "[SallaIdentity] ownership conflict | lookup_id=%s matched_via=external_store_id "
            "tenant_ids=%s",
            sid,
            canonical_tenants,
        )
        raise SallaStoreIdentityConflictError(sid, canonical_tenants, "external_store_id")
    if len(canonical_rows) == 1:
        return canonical_rows[0], "external_store_id"

    if not allow_alias_match:
        return None, ""

    alias_matches: List[Tuple[Any, str]] = []
    for row in q.all():
        cfg = row.config or {}
        if (
            _str_id(cfg.get("store_id")) == sid
            and not _str_id(row.external_store_id)
        ):
            alias_matches.append((row, "config.store_id"))
        elif allow_merchant_alias and _str_id(cfg.get("salla_merchant_id_alt")) == sid:
            alias_matches.append((row, "config.salla_merchant_id_alt"))
        elif allow_merchant_alias and _str_id(cfg.get("merchant_id")) == sid:
            alias_matches.append((row, "config.merchant_id"))

    alias_tenants = sorted({row.tenant_id for row, _ in alias_matches})
    if len(alias_tenants) > 1:
        matched_via = alias_matches[0][1] if alias_matches else "config.store_id"
        logger.error(
            "[SallaIdentity] ownership conflict | lookup_id=%s matched_via=%s tenant_ids=%s",
            sid,
            matched_via,
            alias_tenants,
        )
        raise SallaStoreIdentityConflictError(sid, alias_tenants, matched_via)

    if alias_matches:
        return alias_matches[0][0], alias_matches[0][1]

    return None, ""


def find_salla_integration_for_identity(
    db: Session,
    identity: SallaStoreIdentity,
    *,
    include_disabled: bool = True,
    allow_alias_match: bool = False,
) -> Tuple[Optional[Any], str]:
    """Try every known id on ``identity`` until an integration matches."""
    if identity.store_id:
        return find_salla_integration_by_identity(
            db,
            identity.store_id,
            include_disabled=include_disabled,
            allow_alias_match=allow_alias_match,
        )

    if identity.merchant_account_id:
        return find_salla_integration_by_identity(
            db,
            identity.merchant_account_id,
            include_disabled=include_disabled,
            allow_alias_match=False,
        )

    for lookup_id in identity.all_lookup_ids:
        row, matched_via = find_salla_integration_by_identity(
            db,
            lookup_id,
            include_disabled=include_disabled,
            allow_alias_match=allow_alias_match,
        )
        if row is not None:
            return row, matched_via
    return None, ""


def promote_integration_canonical_store(
    db: Session,
    integration: Any,
    identity: SallaStoreIdentity,
) -> None:
    """Repair integration row to canonical store id and persist alias ids."""
    canonical = _str_id(identity.store_id)
    if not canonical:
        return

    cfg = dict(integration.config or {})
    if identity.merchant_account_id and identity.merchant_account_id != canonical:
        cfg["salla_merchant_id_alt"] = identity.merchant_account_id
        cfg["merchant_id"] = identity.merchant_account_id
    for alt in identity.alias_ids:
        alt_s = _str_id(alt)
        if alt_s and alt_s != canonical:
            cfg.setdefault("salla_merchant_id_alt", alt_s)

    cfg["store_id"] = canonical
    if identity.store_name:
        cfg["store_name"] = identity.store_name
    if identity.owner_email:
        cfg.setdefault("salla_owner_email", identity.owner_email)

    if integration.external_store_id != canonical:
        logger.info(
            "[SallaIdentity] promote canonical store_id | integration_id=%s "
            "old_ext=%s new_ext=%s tenant=%s",
            integration.id,
            integration.external_store_id,
            canonical,
            integration.tenant_id,
        )
        integration.external_store_id = canonical

    integration.config = cfg
    flag_modified(integration, "config")


def resolve_canonical_store_owner(
    db: Session,
    identity: SallaStoreIdentity | str,
    *,
    include_disabled: bool = True,
) -> Tuple[Optional[int], Optional[Any], str]:
    """Return canonical owner ``(tenant_id, integration, matched_via)``.

    Ownership may be proven only by:
      1. ``integrations.external_store_id`` (primary)
      2. ``config.store_id`` when ``external_store_id`` is empty (legacy
         canonical-metadata bridge)

    Merchant/account aliases never match. Lookup id must be a canonical
    store identity — callers must not pass merchant-account-only values.
    """
    if isinstance(identity, SallaStoreIdentity):
        sid = identity.canonical_store_id
    else:
        sid = _str_id(identity)
    if not sid:
        return None, None, ""

    from models import Integration  # noqa: PLC0415

    q = db.query(Integration).filter(Integration.provider == "salla")
    if not include_disabled:
        q = q.filter(Integration.enabled == True)  # noqa: E712

    canonical_rows = list(q.filter(Integration.external_store_id == sid).all())
    canonical_tenants = sorted({row.tenant_id for row in canonical_rows})
    if len(canonical_tenants) > 1:
        logger.error(
            "[SallaIdentity] canonical ownership conflict | lookup_id=%s "
            "matched_via=external_store_id tenant_ids=%s",
            sid,
            canonical_tenants,
        )
        raise SallaStoreIdentityConflictError(sid, canonical_tenants, "external_store_id")
    if len(canonical_rows) == 1:
        return canonical_rows[0].tenant_id, canonical_rows[0], "external_store_id"

    bridge_rows: List[Any] = []
    for row in q.all():
        if _str_id(row.external_store_id):
            continue
        cfg_store = _str_id((row.config or {}).get("store_id"))
        if cfg_store == sid:
            bridge_rows.append(row)

    bridge_tenants = sorted({row.tenant_id for row in bridge_rows})
    if len(bridge_tenants) > 1:
        logger.error(
            "[SallaIdentity] canonical ownership conflict | lookup_id=%s "
            "matched_via=config.store_id tenant_ids=%s",
            sid,
            bridge_tenants,
        )
        raise SallaStoreIdentityConflictError(sid, bridge_tenants, "config.store_id")
    if len(bridge_rows) == 1:
        return bridge_rows[0].tenant_id, bridge_rows[0], "config.store_id"

    return None, None, ""


def resolve_tenant_for_salla_store(
    db: Session,
    identity: SallaStoreIdentity,
    *,
    include_disabled: bool = True,
    allow_alias_match: bool = False,
) -> Tuple[Optional[int], Optional[Any], str]:
    """Return ``(tenant_id, integration, matched_via)`` for a Salla identity."""
    integration, matched_via = find_salla_integration_for_identity(
        db,
        identity,
        include_disabled=include_disabled,
        allow_alias_match=allow_alias_match,
    )
    if integration is None:
        return None, None, ""
    return integration.tenant_id, integration, matched_via


def assert_oauth_tenant_matches_store_owner(
    db: Session,
    *,
    session_tenant_id: int,
    store_id: str = "",
    identity: Optional[SallaStoreIdentity] = None,
) -> Tuple[bool, Optional[int], str]:
    """Guard Sync/Legacy OAuth reconnect from claiming the wrong tenant.

    AD-SALLA-ID-1: only canonical store ownership can return
    ``store_owned_by_other_tenant``. Merchant/account aliases do not prove
    ownership. Merchant-account-only identity cannot persist a store.

    Returns ``(ok, owner_tenant_id, reason)``.
    """
    typed = identity if identity is not None else SallaStoreIdentity(store_id=store_id)

    if typed.identity_source == "merchant_account_only" or (
        not typed.canonical_store_id and typed.merchant_account_id
    ):
        result = SallaTenantGuardResult(
            ok=False,
            reason="merchant_identity_not_canonical",
            matched_via="merchant_account_only",
        )
        _log_salla_tenant_guard(
            context="oauth_reconnect",
            jwt_tenant_id=session_tenant_id,
            store_id=typed.merchant_account_id,
            result=result,
        )
        return False, None, "merchant_identity_not_canonical"

    sid = typed.canonical_store_id
    if not sid or session_tenant_id <= 0:
        return True, None, ""

    try:
        owner_tenant_id, integration, matched_via = resolve_canonical_store_owner(
            db, typed, include_disabled=True,
        )
    except SallaStoreIdentityConflictError as conflict:
        result = SallaTenantGuardResult(
            ok=False,
            reason="store_identity_conflict",
            matched_via=conflict.matched_via,
        )
        _log_salla_tenant_guard(
            context="oauth_reconnect",
            jwt_tenant_id=session_tenant_id,
            store_id=sid,
            result=result,
        )
        return False, None, "store_identity_conflict"

    if owner_tenant_id is None:
        logger.info(
            "[SallaTenantGuard] oauth_reconnect unowned canonical store | "
            "session_tenant=%s store_id=%s (merchant aliases ignored)",
            session_tenant_id,
            sid,
        )
        return True, None, ""

    if owner_tenant_id != session_tenant_id:
        result = SallaTenantGuardResult(
            ok=False,
            owner_tenant_id=owner_tenant_id,
            integration_id=integration.id if integration else None,
            matched_via=matched_via,
            reason="store_owned_by_other_tenant",
        )
        _log_salla_tenant_guard(
            context="oauth_reconnect",
            jwt_tenant_id=session_tenant_id,
            store_id=sid,
            result=result,
        )
        return False, owner_tenant_id, "store_owned_by_other_tenant"

    result = SallaTenantGuardResult(
        ok=True,
        owner_tenant_id=owner_tenant_id,
        integration_id=integration.id if integration else None,
        matched_via=matched_via,
        reason="ok",
    )
    _log_salla_tenant_guard(
        context="oauth_reconnect",
        jwt_tenant_id=session_tenant_id,
        store_id=sid,
        result=result,
    )
    return True, owner_tenant_id, ""


def reconcile_salla_oauth_tenant_id(
    db: Session,
    *,
    session_tenant_id: int,
    store_id: str,
) -> Tuple[int, str]:
    """
    Bind an OAuth reconnect to the documented store owner before claim/sync.

    When Salla drops or replaces the OAuth ``state`` param, the callback must
    not create a ghost tenant or sync orders under a fresh tenant_id if an
    integration row already documents *canonical* ownership elsewhere
    (``external_store_id`` or empty-external ``config.store_id``).
    Merchant/account aliases do not reconcile the session tenant.

    Returns ``(effective_tenant_id, reconciliation_reason)``.
    """
    sid = _str_id(store_id)
    if not sid:
        return session_tenant_id or 0, "no_store_id"

    try:
        owner_tid, integration, matched_via = resolve_canonical_store_owner(
            db,
            SallaStoreIdentity(store_id=sid),
            include_disabled=True,
        )
    except SallaStoreIdentityConflictError as conflict:
        logger.error(
            "[SallaIdentity] oauth reconcile blocked by ownership conflict | "
            "store_id=%s tenant_ids=%s matched_via=%s session_tenant=%s",
            conflict.lookup_id,
            conflict.tenant_ids,
            conflict.matched_via,
            session_tenant_id,
        )
        return session_tenant_id or 0, "store_identity_conflict"

    if owner_tid is not None:
        if session_tenant_id and session_tenant_id != owner_tid:
            _log_salla_tenant_guard(
                context="oauth_reconcile_to_owner",
                jwt_tenant_id=session_tenant_id,
                store_id=sid,
                result=SallaTenantGuardResult(
                    ok=True,
                    owner_tenant_id=owner_tid,
                    integration_id=integration.id if integration else None,
                    matched_via=matched_via,
                    reason="reconciled_session_to_store_owner",
                ),
            )
        return owner_tid, "store_owner_resolved"

    if session_tenant_id > 0:
        return session_tenant_id, "session_tenant_preserved"

    return 0, "no_owner_found"
