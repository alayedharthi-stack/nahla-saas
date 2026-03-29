"""Zid tenant manager — delegates to shared tenant_resolver."""

from typing import Any, Dict, Optional

from integrations.shared.tenant_resolver import (
    get_integration_config,
    upsert_tenant_and_integration,
)

PROVIDER = "zid"


def create_or_update_tenant(store_data: Dict[str, Any]) -> Dict[str, Any]:
    return upsert_tenant_and_integration(PROVIDER, store_data)


def get_integration_by_store_id(store_id: str) -> Optional[Dict[str, Any]]:
    return get_integration_config(PROVIDER, store_id)


def create_tenant_for_store(store_payload: Dict[str, Any]) -> Dict[str, Any]:
    return create_or_update_tenant(store_payload)
