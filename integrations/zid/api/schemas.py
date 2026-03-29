from pydantic import BaseModel
from typing import Any, Dict, Optional


class OAuthAuthorizeResponse(BaseModel):
    authorization_url: str
    state: str


class OAuthCallbackPayload(BaseModel):
    code: str
    state: Optional[str] = None


class AppInstallPayload(BaseModel):
    store_id: str
    store_name: str
    oauth_code: str
    tenant_metadata: Optional[Dict[str, Any]] = None


class SyncResponse(BaseModel):
    success: bool
    synced_records: int
    details: Optional[Dict[str, Any]] = None


class TenantResponse(BaseModel):
    tenant_id: int
    store_id: str
    provider: str
    status: str


class InstallResponse(BaseModel):
    tenant_id: int
    installed: bool
    sync: Optional[Dict[str, Any]] = None


class WebhookResponse(BaseModel):
    processed: bool
    action: Optional[str] = None
    external_id: Optional[str] = None
    reason: Optional[str] = None
