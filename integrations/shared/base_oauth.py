"""
Shared OAuth utilities for all platform integrations (Salla, Zid, …).

Each platform subclasses BasePlatformOAuth and provides its own
endpoint URLs and token-exchange details.  The state nonce management
and signature verification logic is identical across all platforms.
"""

import hashlib
import hmac
import secrets
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# In-memory state store for PKCE-style state nonces.
# Replace with Redis in multi-instance / production deployments.
# ---------------------------------------------------------------------------
_pending_states: Dict[str, str] = {}


def generate_state(app_id: str) -> str:
    """Generate a cryptographically random state nonce and cache it."""
    state = secrets.token_urlsafe(32)
    _pending_states[state] = app_id
    return state


def consume_state(state: str) -> Optional[str]:
    """
    Pop and return the app_id for a state nonce.
    Returns None if the state is unknown or already consumed.
    """
    return _pending_states.pop(state, None)


def verify_hmac_signature(
    payload_bytes: bytes,
    signature_header: str,
    secret: str,
    prefix: str = "sha256=",
) -> bool:
    """
    Validate an HMAC-SHA256 webhook signature.

    Args:
        payload_bytes:    Raw request body bytes.
        signature_header: The value of the platform's signature header
                          (e.g. X-Salla-Signature, X-Zid-Signature).
        secret:           The webhook secret configured in the platform dashboard.
        prefix:           Signature prefix to strip before comparing.

    Returns True if valid, True if no secret is configured (dev mode).
    """
    if not secret:
        return True  # Skip verification if secret not configured (dev/test)

    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    received = signature_header
    if prefix and received.startswith(prefix):
        received = received[len(prefix):]

    return hmac.compare_digest(expected, received)


def build_authorization_url(
    oauth_base_url: str,
    client_id: str,
    redirect_uri: str,
    app_id: str,
    scope: str = "offline_access",
    extra_params: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Build an OAuth authorization URL with a fresh state nonce.

    Returns a dict with 'authorization_url' and 'state' keys.
    """
    state = generate_state(app_id)
    params = {
        "client_id":     client_id or app_id,
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         scope,
        "state":         state,
    }
    if extra_params:
        params.update(extra_params)

    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return {"authorization_url": f"{oauth_base_url}?{qs}", "state": state}
