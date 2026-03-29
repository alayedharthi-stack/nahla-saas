from datetime import datetime
from typing import List, Optional

from models.models import (
    AppCreate,
    AppInstallCreate,
    AppInstallResponse,
    AppPaymentCreate,
    AppPaymentResponse,
    AppResponse,
    DeveloperCreate,
    DeveloperResponse,
)
from repositories.data_access import (
    create_app_record,
    list_app_records,
    get_app_record,
    create_app_install_record,
    get_app_install_record,
    list_app_installs_for_tenant,
    create_app_payment_record,
    create_developer_record,
    list_developer_records,
)


def register_app(payload: AppCreate) -> AppResponse:
    app_id = create_app_record(payload)
    now = datetime.utcnow()
    return AppResponse(
        id=app_id,
        developer_id=payload.developer_id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        price_sar=payload.price_sar,
        billing_model=payload.billing_model,
        commission_rate=payload.commission_rate,
        permissions=payload.permissions,
        categories=payload.categories,
        icon_url=payload.icon_url,
        metadata=payload.metadata,
        is_published=True,
        created_at=now,
        updated_at=now,
    )


def list_apps() -> List[AppResponse]:
    return [
        AppResponse(
            id=app["id"],
            developer_id=app["developer_id"],
            name=app["name"],
            slug=app["slug"],
            description=app.get("description"),
            price_sar=app["price_sar"],
            billing_model=app["billing_model"],
            commission_rate=app["commission_rate"],
            permissions=app.get("permissions"),
            categories=app.get("categories"),
            icon_url=app.get("icon_url"),
            metadata=app.get("metadata"),
            is_published=app.get("is_published", True),
            created_at=app["created_at"],
            updated_at=app["updated_at"],
        )
        for app in list_app_records()
    ]


def get_app(app_id: int) -> Optional[AppResponse]:
    stored = get_app_record(app_id)
    if not stored:
        return None
    return AppResponse(
        id=stored["id"],
        developer_id=stored["developer_id"],
        name=stored["name"],
        slug=stored["slug"],
        description=stored.get("description"),
        price_sar=stored["price_sar"],
        billing_model=stored["billing_model"],
        commission_rate=stored["commission_rate"],
        permissions=stored.get("permissions"),
        categories=stored.get("categories"),
        icon_url=stored.get("icon_url"),
        metadata=stored.get("metadata"),
        is_published=stored.get("is_published", True),
        created_at=stored["created_at"],
        updated_at=stored["updated_at"],
    )


def install_app(payload: AppInstallCreate) -> AppInstallResponse:
    install_id = create_app_install_record(payload)
    return AppInstallResponse(
        id=install_id,
        tenant_id=payload.tenant_id,
        app_id=payload.app_id,
        permissions=payload.permissions,
        config=payload.config,
        metadata=payload.metadata,
        status="installed",
        enabled=True,
        installed_at=datetime.utcnow(),
    )


def get_app_install(install_id: int) -> Optional[AppInstallResponse]:
    stored = get_app_install_record(install_id)
    if not stored:
        return None
    return AppInstallResponse(
        id=stored["id"],
        tenant_id=stored["tenant_id"],
        app_id=stored["app_id"],
        permissions=stored.get("permissions"),
        config=stored.get("config"),
        metadata=stored.get("metadata"),
        status=stored.get("status", "installed"),
        enabled=stored.get("enabled", True),
        installed_at=stored["installed_at"],
    )


def list_installs_for_tenant(tenant_id: int) -> List[AppInstallResponse]:
    return [
        AppInstallResponse(
            id=install["id"],
            tenant_id=install["tenant_id"],
            app_id=install["app_id"],
            permissions=install.get("permissions"),
            config=install.get("config"),
            metadata=install.get("metadata"),
            status=install.get("status", "installed"),
            enabled=install.get("enabled", True),
            installed_at=install["installed_at"],
        )
        for install in list_app_installs_for_tenant(tenant_id)
    ]


def record_app_payment(payload: AppPaymentCreate) -> AppPaymentResponse:
    payment_id = create_app_payment_record(payload)
    commission_amount = int(payload.amount_sar * payload.commission_rate)
    return AppPaymentResponse(
        id=payment_id,
        tenant_id=payload.tenant_id,
        app_id=payload.app_id,
        developer_id=payload.developer_id,
        amount_sar=payload.amount_sar,
        commission_rate=payload.commission_rate,
        gateway=payload.gateway,
        status=payload.status,
        transaction_reference=payload.transaction_reference,
        metadata=payload.metadata,
        commission_amount_sar=commission_amount,
        paid_at=datetime.utcnow() if payload.status == "paid" else None,
        created_at=datetime.utcnow(),
    )


def create_developer(payload: DeveloperCreate) -> DeveloperResponse:
    developer_id = create_developer_record(payload)
    return DeveloperResponse(
        id=developer_id,
        username=payload.username,
        email=payload.email,
        company_name=payload.company_name,
        website=payload.website,
        metadata=payload.metadata,
        created_at=datetime.utcnow(),
    )


def list_developers() -> List[DeveloperResponse]:
    return [
        DeveloperResponse(
            id=dev["id"],
            username=dev["username"],
            email=dev["email"],
            company_name=dev.get("company_name"),
            website=dev.get("website"),
            metadata=dev.get("metadata"),
            created_at=dev["created_at"],
        )
        for dev in list_developer_records()
    ]
