"""Secure Meta Graph OAuth transport shared by Cloud API and Coexistence.

Meta contracts (official docs):
- ``GET /debug_token?input_token=…`` with app access token in Authorization header.
  ``input_token`` in the query string is the documented closed exception for this endpoint.
- ``GET /oauth/access_token`` with grant parameters in the query string (never ``access_token``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

import httpx

from core.config import META_APP_ID, META_APP_SECRET, META_GRAPH_API_VERSION
from core.log_redaction import redact_sensitive_log_text

logger = logging.getLogger("nahla.meta_graph_oauth")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"

# Documented closed exception: only ``/debug_token`` may place ``input_token`` in the URL query.
# Ref: https://developers.facebook.com/docs/graph-api/reference/debug_token/
DEBUG_TOKEN_QUERY_EXCEPTION = frozenset({"input_token"})

# Documented GET grant parameters for /oauth/access_token.
OAUTH_ACCESS_TOKEN_QUERY_PARAMS = frozenset({
    "client_id", "client_secret", "code", "redirect_uri", "grant_type", "fb_exchange_token",
})

_SENSITIVE_QUERY_PARAM_NAMES = frozenset(
    {
        "access_token",
        "input_token",
        "fb_exchange_token",
        "client_secret",
        "code",
    }
)


def _app_access_token() -> str:
    return f"{META_APP_ID}|{META_APP_SECRET}"


def sanitize_graph_oauth_exception(exc: BaseException) -> str:
    """Redact tokens and raw Graph identifiers before surfacing/logging."""
    return redact_sensitive_log_text(exc)


def _safe_oauth_log_fields(params: Mapping[str, str]) -> Dict[str, Any]:
    return {
        key: ("present" if params.get(key) else "absent")
        for key in ("code", "client_id", "client_secret", "redirect_uri", "fb_exchange_token", "grant_type")
    }


def assert_no_sensitive_query_params(request: httpx.Request) -> None:
    """Test helper: block undocumented token query params on Graph URLs."""
    path = request.url.path.rstrip("/")
    lowered = {k.lower(): k for k in request.url.params.keys()}
    if path.endswith("/debug_token"):
        allowed = DEBUG_TOKEN_QUERY_EXCEPTION
    elif path.endswith("/oauth/access_token"):
        allowed = OAUTH_ACCESS_TOKEN_QUERY_PARAMS
    else:
        allowed = frozenset()
    leaked = (set(lowered) & _SENSITIVE_QUERY_PARAM_NAMES) - allowed
    assert "access_token" not in lowered, "access_token must not appear in Graph URL query"
    assert not leaked, f"sensitive OAuth params in URL query: {sorted(leaked)}"


async def debug_token(input_token: str, *, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    if not input_token or not META_APP_ID or not META_APP_SECRET:
        return {}
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(
            f"{GRAPH}/debug_token",
            params={"input_token": input_token},
            headers={"Authorization": f"Bearer {_app_access_token()}"},
        )
        data = resp.json()
    except Exception as exc:
        safe = sanitize_graph_oauth_exception(exc)
        logger.warning("[meta_graph_oauth] debug_token network error: %s", safe)
        return {"is_valid": False, "error": {"message": safe}}
    finally:
        if owns_client:
            await client.aclose()
    info = dict(data.get("data") or {}) if isinstance(data, dict) else {}
    logger.info(
        "[meta_graph_oauth] debug_token is_valid=%s type=%s expires_at=%s",
        info.get("is_valid"),
        info.get("type"),
        info.get("expires_at"),
    )
    return info


async def exchange_code_for_token(
    params: Mapping[str, str],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(f"{GRAPH}/oauth/access_token", params=dict(params))
        return resp.json()
    except Exception as exc:
        safe = sanitize_graph_oauth_exception(exc)
        logger.warning("[meta_graph_oauth] code exchange network error: %s", safe)
        return {"error": {"message": safe}}
    finally:
        if owns_client:
            await client.aclose()


async def exchange_for_long_lived_token(short_token: str, *, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    if not META_APP_ID or not META_APP_SECRET or not short_token:
        return {"access_token": short_token, "token_type": "short_lived"}
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=20)
    try:
        resp = await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": short_token,
            },
        )
        data = resp.json()
    except Exception as exc:
        safe = sanitize_graph_oauth_exception(exc)
        logger.warning("[meta_graph_oauth] long-lived exchange network error: %s", safe)
        return {"access_token": short_token, "token_type": "short_lived"}
    finally:
        if owns_client:
            await client.aclose()
    if "error" in data:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        logger.warning(
            "[meta_graph_oauth] long-lived exchange failed: %s",
            sanitize_graph_oauth_exception(err.get("message") or "unknown"),
        )
        return {"access_token": short_token, "token_type": "short_lived"}
    return {
        "access_token": data.get("access_token", short_token),
        "token_type": "long_lived",
        "expires_in": data.get("expires_in", 5183944),
    }


def debug_token_sync(input_token: str, *, client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    if not input_token or not META_APP_ID or not META_APP_SECRET:
        return {}
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20)
    try:
        resp = client.get(
            f"{GRAPH}/debug_token",
            params={"input_token": input_token},
            headers={"Authorization": f"Bearer {_app_access_token()}"},
        )
        data = resp.json()
    except Exception as exc:
        safe = sanitize_graph_oauth_exception(exc)
        logger.warning("[meta_graph_oauth] debug_token sync error: %s", safe)
        return {"is_valid": False, "error": {"message": safe}}
    finally:
        if owns_client:
            client.close()
    return dict(data.get("data") or {}) if isinstance(data, dict) else {}


async def refresh_long_lived_token(plain: str, *, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    if not META_APP_ID or not META_APP_SECRET or not plain:
        return {}
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=20)
    try:
        resp = await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": plain,
            },
        )
        return resp.json()
    except Exception as exc:
        safe = sanitize_graph_oauth_exception(exc)
        logger.warning("[meta_graph_oauth] refresh network error: %s", safe)
        return {"error": {"message": safe}}
    finally:
        if owns_client:
            await client.aclose()


__all__ = [
    "DEBUG_TOKEN_QUERY_EXCEPTION",
    "OAUTH_ACCESS_TOKEN_QUERY_PARAMS",
    "assert_no_sensitive_query_params",
    "debug_token",
    "debug_token_sync",
    "exchange_code_for_token",
    "exchange_for_long_lived_token",
    "refresh_long_lived_token",
    "sanitize_graph_oauth_exception",
]
