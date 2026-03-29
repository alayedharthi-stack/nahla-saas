from datetime import datetime
from typing import Any, Dict, List, Optional

from models.models import AppCreate, AppInstallCreate, AppPaymentCreate, DeveloperCreate

_APP_STORE: List[Dict[str, Any]] = []
_APP_INSTALL_STORE: List[Dict[str, Any]] = []
_APP_PAYMENT_STORE: List[Dict[str, Any]] = []
_DEVELOPER_STORE: List[Dict[str, Any]] = []


def create_app_record(payload: AppCreate) -> int:
    app_id = len(_APP_STORE) + 1
    now = datetime.utcnow()
    _APP_STORE.append({
        "id": app_id,
        "developer_id": payload.developer_id,
        "name": payload.name,
        "slug": payload.slug,
        "description": payload.description,
        "price_sar": payload.price_sar,
        "billing_model": payload.billing_model,
        "commission_rate": payload.commission_rate,
        "permissions": payload.permissions,
        "categories": payload.categories,
        "icon_url": payload.icon_url,
        "metadata": payload.metadata,
        "is_published": True,
        "created_at": now,
        "updated_at": now,
    })
    return app_id


def list_app_records() -> List[Dict[str, Any]]:
    return list(_APP_STORE)


def get_app_record(app_id: int) -> Optional[Dict[str, Any]]:
    return next((app for app in _APP_STORE if app["id"] == app_id), None)


def create_app_install_record(payload: AppInstallCreate) -> int:
    install_id = len(_APP_INSTALL_STORE) + 1
    now = datetime.utcnow()
    _APP_INSTALL_STORE.append({
        "id": install_id,
        "tenant_id": payload.tenant_id,
        "app_id": payload.app_id,
        "permissions": payload.permissions,
        "config": payload.config,
        "metadata": payload.metadata,
        "status": "installed",
        "enabled": True,
        "installed_at": now,
    })
    return install_id


def get_app_install_record(install_id: int) -> Optional[Dict[str, Any]]:
    return next((install for install in _APP_INSTALL_STORE if install["id"] == install_id), None)


def list_app_installs_for_tenant(tenant_id: int) -> List[Dict[str, Any]]:
    return [install for install in _APP_INSTALL_STORE if install["tenant_id"] == tenant_id]


def create_app_payment_record(payload: AppPaymentCreate) -> int:
    payment_id = len(_APP_PAYMENT_STORE) + 1
    now = datetime.utcnow()
    commission_amount = int(payload.amount_sar * payload.commission_rate)
    _APP_PAYMENT_STORE.append({
        "id": payment_id,
        "tenant_id": payload.tenant_id,
        "app_id": payload.app_id,
        "developer_id": payload.developer_id,
        "amount_sar": payload.amount_sar,
        "commission_rate": payload.commission_rate,
        "commission_amount_sar": commission_amount,
        "gateway": payload.gateway,
        "status": payload.status,
        "transaction_reference": payload.transaction_reference,
        "metadata": payload.metadata,
        "created_at": now,
        "paid_at": now if payload.status == "paid" else None,
    })
    return payment_id


def create_developer_record(payload: DeveloperCreate) -> int:
    developer_id = len(_DEVELOPER_STORE) + 1
    now = datetime.utcnow()
    _DEVELOPER_STORE.append({
        "id": developer_id,
        "username": payload.username,
        "email": payload.email,
        "company_name": payload.company_name,
        "website": payload.website,
        "metadata": payload.metadata,
        "created_at": now,
    })
    return developer_id


def list_developer_records() -> List[Dict[str, Any]]:
    return list(_DEVELOPER_STORE)
