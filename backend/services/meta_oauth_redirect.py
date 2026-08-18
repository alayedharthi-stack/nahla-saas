"""Canonical Meta OAuth redirect_uri ownership for WhatsApp Embedded Signup.

Two Meta-facing flows share one config owner and must never reconstruct
redirect_uri from Host, Origin, Referer, or forwarded headers:

  * Server-side dialog (``/oauth/start`` → ``/oauth/callback``):
    bind ``canonical_meta_redirect_uri()`` into the signed OAuth state and
    reuse that exact string on Graph ``/oauth/access_token``.
  * FB.login JS SDK / Coexistence (``POST /exchange``):
    Meta issues the code via the SDK popup (XD arbiter). Graph exchange
    must omit ``redirect_uri`` — injecting ``login_success.html`` or the
    dashboard URL is an identity mismatch.

Never accept a browser-supplied redirect URI for token exchange.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from core.config import META_APP_ID, META_APP_SECRET, META_REDIRECT_URI

JS_SDK_LOGIN_SUCCESS_URI = "https://www.facebook.com/connect/login_success.html"


def canonical_meta_redirect_uri(*_ignored: Any, **_ignored_kw: Any) -> str:
    """Opaque exact OAuth redirect URI for the server-side dialog.

    Extra positional/keyword arguments (Request, Host, X-Forwarded-*) are
    accepted and ignored so call sites cannot accidentally reconstruct.
    Whitespace is stripped from the env value only; no slash/scheme/host
    normalization.
    """
    return (META_REDIRECT_URI or "").strip()


def js_sdk_token_exchange_redirect_uri() -> None:
    """FB.login Embedded Signup codes are not bound to a Nahlah redirect_uri."""
    return None


def graph_oauth_token_params(
    *,
    code: str,
    redirect_uri: Optional[str],
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> Dict[str, str]:
    """Build Graph ``/oauth/access_token`` params.

    ``redirect_uri`` is included only when an exact bound value is provided.
    Empty string is treated as omit (never substitute a second owner).
    """
    params: Dict[str, str] = {
        "client_id": client_id if client_id is not None else META_APP_ID,
        "client_secret": client_secret if client_secret is not None else META_APP_SECRET,
        "code": code,
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    return params


def token_exchange_log_fields(params: Mapping[str, str]) -> Dict[str, Any]:
    """Safe metadata for logs — never includes code, secret, or tokens."""
    redirect = params.get("redirect_uri")
    return {
        "redirect_uri_present": "redirect_uri" in params,
        "redirect_uri": redirect,
        "has_code": bool(params.get("code")),
        "has_client_id": bool(params.get("client_id")),
        "has_client_secret": bool(params.get("client_secret")),
    }
