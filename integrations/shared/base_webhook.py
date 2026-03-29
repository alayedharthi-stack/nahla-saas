"""
Shared webhook processing primitives.

Each platform's webhook handler imports from here instead of duplicating
the HMAC check, tenant lookup, and SyncLog write.
"""

from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request

from integrations.shared.base_oauth import verify_hmac_signature
from integrations.shared.tenant_resolver import get_tenant_id_for_store
from integrations.shared.base_sync import write_sync_log


async def extract_and_verify(
    request:          Request,
    secret:           str,
    signature_header: str,
    prefix:           str = "sha256=",
) -> bytes:
    """
    Read the request body and verify the HMAC signature.

    Raises HTTP 401 if the signature is invalid.
    Returns the raw body bytes so the caller can parse JSON from them.
    """
    body = await request.body()
    sig  = request.headers.get(signature_header, "")
    if not verify_hmac_signature(body, sig, secret, prefix=prefix):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    return body


def resolve_tenant_or_skip(
    provider: str,
    store_id: str,
) -> Optional[int]:
    """
    Return tenant_id for a store, or None if not found.
    Callers should return {"processed": False, "reason": "unknown_store"} on None.
    """
    return get_tenant_id_for_store(provider, store_id)


def log_webhook_event(
    tenant_id:     int,
    resource_type: str,
    external_id:   str,
    action:        str,
    provider:      str,
) -> None:
    write_sync_log(
        tenant_id     = tenant_id,
        resource_type = resource_type,
        external_id   = external_id,
        status        = action,
        message       = f"{provider} webhook: {resource_type} {action}",
    )
