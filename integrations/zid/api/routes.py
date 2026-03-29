import sys
import os

_ZID_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ZID_ROOT not in sys.path:
    sys.path.insert(0, _ZID_ROOT)

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from api.schemas import (
    AppInstallPayload,
    InstallResponse,
    OAuthAuthorizeResponse,
    OAuthCallbackPayload,
    SyncResponse,
    TenantResponse,
    WebhookResponse,
)
from auth.oauth import (
    build_authorization_url,
    consume_state,
    exchange_code_for_token,
    verify_webhook_signature,
)
from services.tenant_manager import create_or_update_tenant, get_integration_by_store_id
from sync.sync_manager import (
    sync_all,
    sync_customers_for_store,
    sync_orders_for_store,
    sync_products_for_store,
)
from webhooks.handlers import (
    handle_customer_webhook,
    handle_order_webhook,
    handle_product_webhook,
)

router = APIRouter(prefix="/integrations/zid", tags=["zid"])


@router.get("/oauth/authorize", response_model=OAuthAuthorizeResponse)
async def authorize(app_id: str):
    return build_authorization_url(app_id)


@router.post("/oauth/callback", response_model=TenantResponse)
async def oauth_callback(payload: OAuthCallbackPayload, background_tasks: BackgroundTasks):
    if payload.state and consume_state(payload.state) is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    try:
        token_data = await exchange_code_for_token(payload.code, payload.state)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc}")

    tenant = create_or_update_tenant(token_data)

    background_tasks.add_task(
        sync_all,
        token_data["store_id"],
        token_data["access_token"],
        tenant["id"],
    )

    return TenantResponse(
        tenant_id=tenant["id"],
        store_id=token_data["store_id"],
        provider="zid",
        status="connected",
    )


@router.post("/install", response_model=InstallResponse)
async def install_app(payload: AppInstallPayload, background_tasks: BackgroundTasks):
    try:
        token_data = await exchange_code_for_token(payload.oauth_code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc}")

    token_data["store_id"] = token_data.get("store_id") or payload.store_id
    token_data["store_name"] = token_data.get("store_name") or payload.store_name

    tenant = create_or_update_tenant(token_data)

    background_tasks.add_task(
        sync_all,
        token_data["store_id"],
        token_data["access_token"],
        tenant["id"],
    )

    return InstallResponse(tenant_id=tenant["id"], installed=True)


# ── Webhooks ──────────────────────────────────────────────────────────────────

async def _verify_zid_signature(request: Request) -> bytes:
    body = await request.body()
    # Zid sends the signature in X-Zid-Signature
    signature = request.headers.get("X-Zid-Signature", "")
    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    return body


@router.post("/webhooks/products", response_model=WebhookResponse)
async def products_webhook(request: Request):
    await _verify_zid_signature(request)
    payload = await request.json()
    result = handle_product_webhook(payload)
    if not result.get("processed"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Unprocessable product webhook"))
    return WebhookResponse(**result)


@router.post("/webhooks/orders", response_model=WebhookResponse)
async def orders_webhook(request: Request):
    await _verify_zid_signature(request)
    payload = await request.json()
    result = handle_order_webhook(payload)
    if not result.get("processed"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Unprocessable order webhook"))
    return WebhookResponse(**result)


@router.post("/webhooks/customers", response_model=WebhookResponse)
async def customers_webhook(request: Request):
    await _verify_zid_signature(request)
    payload = await request.json()
    result = handle_customer_webhook(payload)
    if not result.get("processed"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Unprocessable customer webhook"))
    return WebhookResponse(**result)


# ── Manual sync ───────────────────────────────────────────────────────────────

def _get_integration_or_404(store_id: str):
    integration = get_integration_by_store_id(store_id)
    if not integration:
        raise HTTPException(status_code=404, detail="Store not found or not installed")
    return integration


@router.post("/sync/products", response_model=SyncResponse)
async def sync_products(store_id: str):
    integration = _get_integration_or_404(store_id)
    result = await sync_products_for_store(store_id, integration["access_token"], integration["tenant_id"])
    return SyncResponse(
        success=result.get("success", False),
        synced_records=result.get("synced_records", 0),
        details=result.get("details"),
    )


@router.post("/sync/orders", response_model=SyncResponse)
async def sync_orders(store_id: str):
    integration = _get_integration_or_404(store_id)
    result = await sync_orders_for_store(store_id, integration["access_token"], integration["tenant_id"])
    return SyncResponse(
        success=result.get("success", False),
        synced_records=result.get("synced_records", 0),
        details=result.get("details"),
    )


@router.post("/sync/customers", response_model=SyncResponse)
async def sync_customers(store_id: str):
    integration = _get_integration_or_404(store_id)
    result = await sync_customers_for_store(store_id, integration["access_token"], integration["tenant_id"])
    return SyncResponse(
        success=result.get("success", False),
        synced_records=result.get("synced_records", 0),
        details=result.get("details"),
    )
