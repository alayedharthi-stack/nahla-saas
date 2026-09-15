"""Narrow authentication for the commerce lifecycle operations surface."""
from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth import require_admin, require_not_support_impersonation


LIFECYCLE_OPS_TOKEN_ENV = "NAHLA_LIFECYCLE_OPS_TOKEN"
LIFECYCLE_OPS_TOKEN_HEADER = "X-Nahlah-Lifecycle-Ops-Token"
MIN_LIFECYCLE_OPS_TOKEN_LENGTH = 43

_optional_bearer = HTTPBearer(auto_error=False)


def _configured_ops_token() -> Optional[str]:
    token = os.getenv(LIFECYCLE_OPS_TOKEN_ENV, "").strip()
    if len(token) < MIN_LIFECYCLE_OPS_TOKEN_LENGTH:
        return None
    return token


def require_lifecycle_operator(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
    ops_token: Optional[str] = Header(default=None, alias=LIFECYCLE_OPS_TOKEN_HEADER),
) -> Dict[str, Any]:
    """Accept a real platform admin or the endpoint-scoped M2M ops token."""
    if ops_token is not None:
        configured = _configured_ops_token()
        if configured is None:
            raise HTTPException(status_code=401, detail="Lifecycle operator authentication unavailable")
        if not hmac.compare_digest(ops_token, configured):
            raise HTTPException(status_code=403, detail="Invalid lifecycle operator credentials")
        return {
            "sub": "lifecycle-operations-m2m",
            "role": "lifecycle_operator",
            "auth_method": "lifecycle_ops_token",
        }

    admin = require_admin(request, credentials)
    require_not_support_impersonation(request, credentials)
    return {**admin, "auth_method": "platform_admin_jwt"}


__all__ = [
    "LIFECYCLE_OPS_TOKEN_ENV",
    "LIFECYCLE_OPS_TOKEN_HEADER",
    "MIN_LIFECYCLE_OPS_TOKEN_LENGTH",
    "require_lifecycle_operator",
]
