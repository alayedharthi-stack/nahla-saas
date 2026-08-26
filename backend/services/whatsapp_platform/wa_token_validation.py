"""
Meta access-token validation, classification, and connection health metadata.

Uses Graph ``/debug_token`` — never logs the raw token.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from core.config import META_APP_ID, META_APP_SECRET, META_GRAPH_API_VERSION
from core.log_redaction import redact_graph_id, redact_sensitive_log_text
from services.meta_graph_oauth_client import debug_token as _secure_debug_token, debug_token_sync as _secure_debug_token_sync
from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_360DIALOG, wa_provider

logger = logging.getLogger("nahla.wa_token_validation")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"

# Admin warning thresholds (days before expiry)
_WARN_DAYS_14 = 14
_WARN_DAYS_7 = 7

_TOKEN_STATUSES = frozenset({
    "valid",
    "expiring",
    "expired",
    "invalid",
    "unknown_expiry",
    "missing",
})

_HEALTH_STATUSES = frozenset({
    "healthy",
    "token_expiring_soon",
    "token_expired",
    "permission_revoked",
    "meta_error",
    "unknown",
})


@dataclass
class TokenValidationResult:
    is_valid: bool
    production_ready: bool
    token_status: str
    token_type: str
    token_source_label: str
    expires_at: Optional[datetime]
    data_access_expires_at: Optional[datetime]
    scopes: List[str]
    app_id: Optional[str]
    warnings: List[str]
    error_code: Optional[str]
    error_message: Optional[str]
    debug_info: Dict[str, Any]

    @property
    def health_status(self) -> str:
        if not self.is_valid:
            if self.error_code == "190" or self.token_status == "expired":
                return "token_expired"
            if self.error_code in {"200", "10"}:
                return "permission_revoked"
            return "meta_error"
        if self.token_status == "expiring":
            return "token_expiring_soon"
        if self.token_status in {"invalid", "expired"}:
            return "token_expired"
        if self.production_ready:
            return "healthy"
        return "unknown"


def _utc_from_unix(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    try:
        ts = int(raw)
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def debug_meta_token(token: str) -> Dict[str, Any]:
    if not token or not META_APP_ID or not META_APP_SECRET:
        return {}
    info = await _secure_debug_token(token)
    logger.info(
        "[wa_token_validation] debug_token is_valid=%s type=%s app_id=%s expires_at=%s",
        info.get("is_valid"),
        info.get("type"),
        redact_graph_id(str(info.get("app_id") or "")),
        info.get("expires_at"),
    )
    return info


def classify_debug_info(debug_info: Dict[str, Any]) -> TokenValidationResult:
    warnings: List[str] = []
    is_valid = bool(debug_info.get("is_valid"))
    meta_type = str(debug_info.get("type") or "UNKNOWN").upper()
    expires_at = _utc_from_unix(debug_info.get("expires_at"))
    data_access_expires_at = _utc_from_unix(debug_info.get("data_access_expires_at"))
    scopes = list(debug_info.get("scopes") or [])
    app_id = debug_info.get("app_id")
    err = dict(debug_info.get("error") or {})
    error_code = str(err.get("code")) if err.get("code") is not None else None
    error_message = err.get("message")

    # Effective expiry — prefer token expiry, fall back to data-access expiry
    effective_expiry = expires_at or data_access_expires_at

    token_source_label = "unknown"
    if meta_type in {"SYSTEM_USER", "SYSTEM"}:
        token_source_label = "system_user"
    elif meta_type == "USER":
        token_source_label = "user"
    elif meta_type == "PAGE":
        token_source_label = "page"

    if not is_valid:
        status = "expired" if error_code == "190" else "invalid"
        return TokenValidationResult(
            is_valid=False,
            production_ready=False,
            token_status=status,
            token_type=meta_type,
            token_source_label=token_source_label,
            expires_at=effective_expiry,
            data_access_expires_at=data_access_expires_at,
            scopes=scopes,
            app_id=str(app_id) if app_id else None,
            warnings=warnings,
            error_code=error_code,
            error_message=error_message,
            debug_info=debug_info,
        )

    now = _now_utc()
    if effective_expiry and now >= effective_expiry:
        return TokenValidationResult(
            is_valid=False,
            production_ready=False,
            token_status="expired",
            token_type=meta_type,
            token_source_label=token_source_label,
            expires_at=effective_expiry,
            data_access_expires_at=data_access_expires_at,
            scopes=scopes,
            app_id=str(app_id) if app_id else None,
            warnings=["Token expiry timestamp is in the past."],
            error_code="expired",
            error_message="Token has expired.",
            debug_info=debug_info,
        )

    # Non-expiring / permanent system user (expires_at absent or 0 in debug)
    if not effective_expiry and token_source_label == "system_user":
        return TokenValidationResult(
            is_valid=True,
            production_ready=True,
            token_status="valid",
            token_type=meta_type,
            token_source_label=token_source_label,
            expires_at=None,
            data_access_expires_at=data_access_expires_at,
            scopes=scopes,
            app_id=str(app_id) if app_id else None,
            warnings=[],
            error_code=None,
            error_message=None,
            debug_info=debug_info,
        )

    if not effective_expiry:
        warnings.append(
            "Token has no expiry metadata — verify this is a permanent System User token."
        )
        status = "unknown_expiry"
        production_ready = token_source_label == "system_user"
    else:
        days_left = (effective_expiry - now).days
        if days_left <= _WARN_DAYS_7:
            warnings.append(
                f"Token expires in {days_left} day(s). Use a permanent System User token."
            )
            status = "expiring"
        elif days_left <= _WARN_DAYS_14:
            warnings.append(f"Token expires in {days_left} day(s).")
            status = "expiring"
        else:
            status = "valid"
        # Short-lived user tokens / API-setup temp tokens are not production-ready
        production_ready = (
            token_source_label == "system_user"
            or (days_left > _WARN_DAYS_14 and meta_type not in {"USER"})
        )
        if meta_type == "USER" and effective_expiry:
            warnings.append(
                "User access token detected — prefer a permanent System User token from Meta Business Manager."
            )
            production_ready = False

    if meta_type == "USER" and effective_expiry and (effective_expiry - now) <= timedelta(days=65):
        warnings.append(
            "This may be a 60-day System User token — plan renewal or replace with a permanent token."
        )

    return TokenValidationResult(
        is_valid=True,
        production_ready=production_ready,
        token_status=status,
        token_type=meta_type,
        token_source_label=token_source_label,
        expires_at=effective_expiry,
        data_access_expires_at=data_access_expires_at,
        scopes=scopes,
        app_id=str(app_id) if app_id else None,
        warnings=warnings,
        error_code=None,
        error_message=None,
        debug_info=debug_info,
    )


async def validate_meta_access_token(token: str) -> TokenValidationResult:
    debug_info = await debug_meta_token(token)
    if not debug_info:
        return TokenValidationResult(
            is_valid=False,
            production_ready=False,
            token_status="invalid",
            token_type="UNKNOWN",
            token_source_label="unknown",
            expires_at=None,
            data_access_expires_at=None,
            scopes=[],
            app_id=None,
            warnings=["Could not validate token with Meta."],
            error_code=None,
            error_message="debug_token unavailable",
            debug_info={},
        )
    return classify_debug_info(debug_info)


def validate_meta_access_token_sync(token: str) -> TokenValidationResult:
    """Synchronous validation for commit_connection and admin writes."""
    if not token or not META_APP_ID or not META_APP_SECRET:
        return TokenValidationResult(
            is_valid=False,
            production_ready=False,
            token_status="invalid",
            token_type="UNKNOWN",
            token_source_label="unknown",
            expires_at=None,
            data_access_expires_at=None,
            scopes=[],
            app_id=None,
            warnings=["Meta app credentials not configured."],
            error_code=None,
            error_message="debug_token unavailable",
            debug_info={},
        )
    try:
        debug_info = _secure_debug_token_sync(token)
    except Exception as exc:
        logger.warning("[wa_token_validation] debug_token sync error: %s", redact_sensitive_log_text(exc))
        debug_info = {"is_valid": False, "error": {"message": str(exc)}}
    if not debug_info:
        return TokenValidationResult(
            is_valid=False,
            production_ready=False,
            token_status="invalid",
            token_type="UNKNOWN",
            token_source_label="unknown",
            expires_at=None,
            data_access_expires_at=None,
            scopes=[],
            app_id=None,
            warnings=["Could not validate token with Meta."],
            error_code=None,
            error_message="debug_token unavailable",
            debug_info={},
        )
    return classify_debug_info(debug_info)


def apply_validation_to_connection(
    conn: Any,
    result: TokenValidationResult,
    *,
    token_plaintext_stored: bool = True,
) -> None:
    """Persist validation metadata on the connection row (no token in metadata)."""
    now = _now_utc()
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    meta["token_status"] = result.token_status
    meta["token_health"] = result.health_status
    meta["health_status"] = result.health_status
    meta["health_checked_at"] = now.isoformat()
    meta["token_last_validated_at"] = now.isoformat()
    meta["token_type_meta"] = result.token_type
    meta["token_source_label"] = result.token_source_label
    meta["token_scopes"] = result.scopes
    meta["token_app_id"] = result.app_id
    meta["production_ready"] = result.production_ready
    if result.warnings:
        meta["token_validation_warnings"] = result.warnings
    else:
        meta.pop("token_validation_warnings", None)
    if result.error_message:
        meta["last_meta_validation_error"] = str(result.error_message)[:500]
    else:
        meta.pop("last_meta_validation_error", None)
    meta["oauth_debug"] = {
        "is_valid": result.is_valid,
        "expires_at": int(result.expires_at.timestamp()) if result.expires_at else 0,
        "scopes": result.scopes,
        "type": result.token_type,
        "app_id": result.app_id,
    }
    conn.extra_metadata = meta
    if result.expires_at is not None:
        conn.token_expires_at = result.expires_at.replace(tzinfo=None)
    elif result.token_status == "valid" and result.token_source_label == "system_user":
        conn.token_expires_at = None
    conn.last_verified_at = now.replace(tzinfo=None)


def production_sending_allowed(result: TokenValidationResult) -> bool:
    return bool(result.is_valid and result.production_ready)


def admin_production_block_message(result: TokenValidationResult) -> str:
    base = (
        "This token does not appear production-ready. "
        "Use a permanent System User token or configure renewal before enabling production sending."
    )
    if result.warnings:
        return f"{base} {'; '.join(result.warnings)}"
    return base


async def validate_connection_health(conn: Any) -> TokenValidationResult:
    """Validate stored Meta token and update health fields without disconnecting."""
    if wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        return TokenValidationResult(
            is_valid=bool(getattr(conn, "access_token", None)),
            production_ready=True,
            token_status="valid",
            token_type="D360_API_KEY",
            token_source_label="dialog360",
            expires_at=None,
            data_access_expires_at=None,
            scopes=[],
            app_id=None,
            warnings=[],
            error_code=None,
            error_message=None,
            debug_info={},
        )
    from services.whatsapp_platform.wa_connection_secrets import read_access_token  # noqa: PLC0415

    plain = read_access_token(conn)
    if not plain:
        result = TokenValidationResult(
            is_valid=False,
            production_ready=False,
            token_status="missing",
            token_type="UNKNOWN",
            token_source_label="unknown",
            expires_at=None,
            data_access_expires_at=None,
            scopes=[],
            app_id=None,
            warnings=["No access token stored."],
            error_code=None,
            error_message="missing token",
            debug_info={},
        )
        apply_validation_to_connection(conn, result)
        return result

    result = await validate_meta_access_token(plain)
    apply_validation_to_connection(conn, result)

    # Never silently disconnect — adjust operational flags only
    if not result.is_valid or result.token_status == "expired":
        conn.sending_enabled = False
        conn.last_error = (result.error_message or "Meta access token invalid or expired.")[:500]
    elif result.token_status == "expiring" and not result.production_ready:
        # Keep sending if already enabled but surface health warning
        pass
    elif result.production_ready and conn.status == "connected":
        conn.sending_enabled = True
        if result.token_status == "valid":
            conn.last_error = None

    return result
