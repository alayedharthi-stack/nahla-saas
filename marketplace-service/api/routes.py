from fastapi import APIRouter, HTTPException
from models.models import (
    AppCreate,
    AppResponse,
    AppInstallCreate,
    AppInstallResponse,
    AppPaymentCreate,
    AppPaymentResponse,
    DeveloperCreate,
    DeveloperResponse,
)
from services.business_logic import (
    register_app,
    list_apps,
    get_app,
    install_app,
    get_app_install,
    list_installs_for_tenant,
    record_app_payment,
    list_developers,
    create_developer,
)

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

@router.post("/apps", response_model=AppResponse)
async def create_app(payload: AppCreate):
    return register_app(payload)

@router.get("/apps", response_model=list[AppResponse])
async def get_apps():
    return list_apps()

@router.get("/apps/{app_id}", response_model=AppResponse)
async def get_app_by_id(app_id: int):
    app = get_app(app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app

@router.post("/installs", response_model=AppInstallResponse)
async def create_app_install(payload: AppInstallCreate):
    return install_app(payload)

@router.get("/tenants/{tenant_id}/installs", response_model=list[AppInstallResponse])
async def get_tenant_installs(tenant_id: int):
    return list_installs_for_tenant(tenant_id)

@router.get("/installs/{install_id}", response_model=AppInstallResponse)
async def get_install(install_id: int):
    install = get_app_install(install_id)
    if not install:
        raise HTTPException(status_code=404, detail="App install not found")
    return install

@router.post("/payments", response_model=AppPaymentResponse)
async def create_app_payment(payload: AppPaymentCreate):
    return record_app_payment(payload)

@router.post("/developers", response_model=DeveloperResponse)
async def create_developer_account(payload: DeveloperCreate):
    return create_developer(payload)

@router.get("/developers", response_model=list[DeveloperResponse])
async def get_developers():
    return list_developers()
