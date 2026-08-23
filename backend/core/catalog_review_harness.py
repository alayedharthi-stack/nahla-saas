"""App Review harness gates for business_management + catalog_management.

Staging / Test App only. Default-off. Production never honors the flag.
Does not change live META_WA_CONFIG_ID. Does not use Tenant 1.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, FrozenSet, Iterable, Optional, Sequence, Set

logger = logging.getLogger("nahla.catalog_review_harness")

HARNESS_META_KEY = "meta_catalog_review_harness"
BLOCKED_TENANT_ID = 1
ALLOWED_ENVIRONMENTS = frozenset({"development", "staging", "test"})
REQUIRED_SCOPES = frozenset({
    "business_management",
    "catalog_management",
    "whatsapp_business_management",
    "whatsapp_business_messaging",
})
DEFAULT_REQUIRED_BUSINESS_NAME = "Nahlah Review Test"
UI_SETTING_UP = "Setting up WhatsApp catalog"
UI_CONNECTED_SYNCED = "Connected and synced"
ERROR_HARNESS_DISABLED = "harness_disabled"
ERROR_PRODUCTION_BLOCKED = "production_blocked"
ERROR_WRONG_APP_ID = "wrong_app_id"
ERROR_TENANT_1_BLOCKED = "tenant_1_blocked"
ERROR_MISSING_TEST_APP = "missing_test_app_id"
ERROR_REAUTH_REQUIRED = "REAUTH_REQUIRED"
ERROR_NAHLAH_BM_BLOCKED = "nahlah_bm_owner_blocked"
ERROR_BUSINESS_NAME_MISMATCH = "business_name_mismatch"
ERROR_OWNERSHIP_MISMATCH = "ownership_mismatch"
ERROR_BLOCKLIST_UNCONFIGURED = "blocked_business_ids_unconfigured"
ERROR_ENVIRONMENT_BLOCKED = "environment_blocked"

_GRAPH_ID_KEYS = frozenset({
    "whatsapp_business_account_id",
    "phone_number_id",
    "meta_business_account_id",
    "meta_catalog_id",
    "waba_id",
    "catalog_id",
    "business_id",
    "business_owner",
    "waba_owner_business_id",
    "catalog_owner_business_id",
    "linked_catalog_ids",
    "expected_catalog_id",
    "linked_catalogs",
})

LIVE_OAUTH_SCOPES = (
    "business_management",
    "whatsapp_business_management",
    "whatsapp_business_messaging",
)
HARNESS_OAUTH_SCOPES = (
    "business_management",
    "catalog_management",
    "whatsapp_business_management",
    "whatsapp_business_messaging",
)


def _truthy(name: str) -> bool:
    return (os.environ.get(name, "") or "").strip().lower() in {
        "1", "true", "yes", "on", "enabled",
    }


def current_environment() -> str:
    return (os.environ.get("ENVIRONMENT") or "development").strip().lower()


def is_production_environment() -> bool:
    return current_environment() == "production"


def raw_harness_flag() -> bool:
    return _truthy("NAHLA_CATALOG_REVIEW_HARNESS")


def test_app_id() -> str:
    return (os.environ.get("NAHLA_CATALOG_REVIEW_TEST_APP_ID") or "").strip()


def test_app_secret() -> str:
    return (os.environ.get("NAHLA_CATALOG_REVIEW_TEST_APP_SECRET") or "").strip()


def test_config_id() -> str:
    return (os.environ.get("NAHLA_CATALOG_REVIEW_TEST_CONFIG_ID") or "").strip()


def required_business_name() -> str:
    return (
        (os.environ.get("NAHLA_CATALOG_REVIEW_REQUIRED_BUSINESS_NAME") or "").strip()
        or DEFAULT_REQUIRED_BUSINESS_NAME
    )


def blocked_business_ids() -> FrozenSet[str]:
    raw = os.environ.get("NAHLA_CATALOG_REVIEW_BLOCKED_BUSINESS_IDS") or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def blocked_tenant_ids() -> FrozenSet[int]:
    raw = os.environ.get("NAHLA_CATALOG_REVIEW_BLOCKED_TENANT_IDS") or ""
    extra = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            extra.add(int(part))
    extra.add(BLOCKED_TENANT_ID)
    return frozenset(extra)


def is_catalog_review_harness_requested() -> bool:
    """True when the flag is on in a non-production environment.

    Production ignores the flag so an accidental env copy cannot blank
    live Embedded Signup or enable harness mutations.
    """
    if is_production_environment():
        if raw_harness_flag():
            logger.critical(
                "[REVIEW_HARNESS] NAHLA_CATALOG_REVIEW_HARNESS is set in production; ignored",
            )
        return False
    return raw_harness_flag()


def is_catalog_review_harness_enabled() -> bool:
    if not is_catalog_review_harness_requested():
        return False
    if current_environment() not in ALLOWED_ENVIRONMENTS:
        return False
    if not test_app_id() or not test_app_secret() or not test_config_id():
        return False
    return True


def embedded_signup_app_id() -> str:
    from core.config import META_APP_ID  # noqa: PLC0415

    if is_production_environment():
        return META_APP_ID
    if raw_harness_flag():
        if is_catalog_review_harness_enabled():
            return test_app_id()
        return ""
    return META_APP_ID


def embedded_signup_app_secret() -> str:
    from core.config import META_APP_SECRET  # noqa: PLC0415

    if is_production_environment():
        return META_APP_SECRET
    if raw_harness_flag():
        if is_catalog_review_harness_enabled():
            return test_app_secret()
        return ""
    return META_APP_SECRET


def embedded_signup_config_id() -> str:
    from core.config import META_EMBEDDED_SIGNUP_CONFIG_ID  # noqa: PLC0415

    if is_production_environment():
        return META_EMBEDDED_SIGNUP_CONFIG_ID
    if raw_harness_flag():
        if is_catalog_review_harness_enabled():
            return test_config_id()
        return ""
    return META_EMBEDDED_SIGNUP_CONFIG_ID


def embedded_signup_oauth_scopes() -> Sequence[str]:
    if is_catalog_review_harness_enabled():
        return HARNESS_OAUTH_SCOPES
    return LIVE_OAUTH_SCOPES


def evaluate_harness_gate(*, tenant_id: Optional[int] = None) -> Dict[str, Any]:
    """Fail-closed gate used before any Graph mutation."""
    env = current_environment()
    if is_production_environment():
        return {
            "ok": False,
            "error": ERROR_PRODUCTION_BLOCKED if raw_harness_flag() else ERROR_HARNESS_DISABLED,
            "environment": env,
        }
    if not raw_harness_flag():
        return {"ok": False, "error": ERROR_HARNESS_DISABLED, "environment": env}
    if env not in ALLOWED_ENVIRONMENTS:
        return {"ok": False, "error": ERROR_ENVIRONMENT_BLOCKED, "environment": env}
    if not test_app_id() or not test_app_secret() or not test_config_id():
        return {"ok": False, "error": ERROR_MISSING_TEST_APP, "environment": env}
    if tenant_id is not None and int(tenant_id) in blocked_tenant_ids():
        return {
            "ok": False,
            "error": ERROR_TENANT_1_BLOCKED,
            "environment": env,
            "tenant_id": int(tenant_id),
        }
    return {
        "ok": True,
        "error": None,
        "environment": env,
        "app_id": test_app_id(),
        "tenant_id": tenant_id,
    }


def missing_required_scopes(scopes: Iterable[str]) -> Set[str]:
    have = {str(s or "").strip() for s in scopes if str(s or "").strip()}
    return set(REQUIRED_SCOPES) - have


def business_name_matches(actual: Optional[str]) -> bool:
    got = " ".join(str(actual or "").split()).casefold()
    need = " ".join(required_business_name().split()).casefold()
    return bool(got) and got == need


def is_blocked_business_id(business_id: Optional[str]) -> bool:
    bid = str(business_id or "").strip()
    if not bid:
        return True
    return bid in blocked_business_ids()


def harness_state(conn: Any) -> Dict[str, Any]:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    raw = meta.get(HARNESS_META_KEY) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def public_review_harness_status(conn: Any = None) -> Optional[Dict[str, Any]]:
    """Merchant-visible status. No Graph IDs, no tokens, no raw Graph errors."""
    if not is_catalog_review_harness_enabled():
        return None
    state = harness_state(conn) if conn is not None else {}
    error = str(state.get("error_code") or "").strip() or None
    ui_status = str(state.get("ui_status") or "setting_up").strip() or "setting_up"
    if error == ERROR_REAUTH_REQUIRED:
        ui_status = "reauth_required"
    elif error:
        ui_status = "blocked"
    if ui_status == "connected_and_synced":
        label = UI_CONNECTED_SYNCED
    else:
        label = UI_SETTING_UP
    return {
        "active": True,
        "ui_status": ui_status,
        "ui_label": label,
        "hide_graph_ids": True,
        "error_code": error,
    }


def redact_graph_ids(payload: Any) -> Any:
    """Strip BM / WABA / catalog identifiers from merchant JSON."""
    if not is_catalog_review_harness_enabled():
        return payload
    if isinstance(payload, list):
        return [redact_graph_ids(item) for item in payload]
    if not isinstance(payload, dict):
        return payload
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in _GRAPH_ID_KEYS:
            if key in {"linked_catalogs", "linked_catalog_ids"}:
                out[key] = []
            else:
                out[key] = None
            continue
        out[key] = redact_graph_ids(value)
    return out


def strip_secrets(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Drop token-like keys before persist or API. Never log the result's secrets."""
    banned = {
        "token", "access_token", "authorization", "client_secret",
        "app_secret", "input_token", "fb_exchange_token", "code",
    }
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if str(key).lower() in banned:
            continue
        if isinstance(value, dict):
            out[key] = strip_secrets(value)
        else:
            out[key] = value
    return out
