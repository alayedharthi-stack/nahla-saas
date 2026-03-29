"""Zid OAuth — thin wrapper over integrations/shared/base_oauth.py."""

import os
from typing import Any, Dict, Optional

import httpx

from integrations.shared.base_oauth import (
    build_authorization_url as _build_url,
    consume_state,
    generate_state,
    verify_hmac_signature,
)

ZID_CLIENT_ID      = os.getenv("ZID_CLIENT_ID", "")
ZID_CLIENT_SECRET  = os.getenv("ZID_CLIENT_SECRET", "")
ZID_REDIRECT_URI   = os.getenv("ZID_REDIRECT_URI", "https://your-domain.com/integrations/zid/oauth/callback")
ZID_WEBHOOK_SECRET = os.getenv("ZID_WEBHOOK_SECRET", "")

ZID_OAUTH_URL = "https://oauth.zid.sa/oauth/authorize"
ZID_TOKEN_URL = "https://oauth.zid.sa/oauth/token"
ZID_API_BASE  = "https://api.zid.sa/v1"


def build_authorization_url(app_id: str) -> Dict[str, str]:
    return _build_url(
        oauth_base_url = ZID_OAUTH_URL,
        client_id      = ZID_CLIENT_ID,
        redirect_uri   = ZID_REDIRECT_URI,
        app_id         = app_id,
        scope          = "offline_access",
    )


async def exchange_code_for_token(code: str, state: Optional[str] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            ZID_TOKEN_URL,
            json={
                "client_id":     ZID_CLIENT_ID,
                "client_secret": ZID_CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  ZID_REDIRECT_URI,
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        token_data = resp.json()

    store_info = await _fetch_store_info(token_data.get("access_token", ""))
    return {
        "access_token":  token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_in":    token_data.get("expires_in", 3600),
        "store_id":      str(store_info.get("id", "")),
        "store_name":    store_info.get("name", "Zid Store"),
        "store_domain":  store_info.get("domain", ""),
        "store_email":   store_info.get("email", ""),
    }


async def _fetch_store_info(access_token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{ZID_API_BASE}/profile/merchant/",
            headers={
                "Authorization":  f"Bearer {access_token}",
                "X-Manager-Token": access_token,
                "Accept":          "application/json",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("store", data.get("merchant", {}))
    return {}


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    return verify_hmac_signature(payload_bytes, signature_header, ZID_WEBHOOK_SECRET)
