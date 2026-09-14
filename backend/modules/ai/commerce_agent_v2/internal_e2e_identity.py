"""Trusted identity contract for the Commerce V2 internal acceptance channel.

The identifier is deliberately not phone-shaped.  It cannot pass WhatsApp,
SMS, or ordinary E.164 recipient validation even if it is accidentally handed
to an external sender.
"""
from __future__ import annotations

import re
from typing import Literal


INTERNAL_E2E_CHANNEL = "internal_e2e"
INTERNAL_E2E_CONNECTION_ID = "internal_e2e"
INTERNAL_E2E_ALIASES = ("A", "B", "C")
InternalE2EAlias = Literal["A", "B", "C"]
_IDENTITY_RE = re.compile(r"^internal_e2e:t([1-9][0-9]*):customer:([abc])$")


def normalize_internal_e2e_alias(value: object) -> InternalE2EAlias:
    alias = str(value or "").strip().upper()
    if alias not in INTERNAL_E2E_ALIASES:
        raise ValueError("internal_e2e_alias_invalid")
    return alias  # type: ignore[return-value]


def internal_e2e_customer_identity(tenant_id: int, alias: object) -> str:
    resolved_tenant_id = int(tenant_id)
    if resolved_tenant_id <= 0:
        raise ValueError("internal_e2e_tenant_id_invalid")
    resolved_alias = normalize_internal_e2e_alias(alias)
    return f"internal_e2e:t{resolved_tenant_id}:customer:{resolved_alias.lower()}"


def parse_internal_e2e_customer_identity(value: object) -> tuple[int, InternalE2EAlias]:
    match = _IDENTITY_RE.fullmatch(str(value or "").strip())
    if match is None:
        raise ValueError("internal_e2e_identity_invalid")
    return int(match.group(1)), normalize_internal_e2e_alias(match.group(2))


def internal_e2e_metadata(tenant_id: int, alias: object) -> dict[str, object]:
    resolved_alias = normalize_internal_e2e_alias(alias)
    return {
        "channel": INTERNAL_E2E_CHANNEL,
        "synthetic": True,
        "test_only": True,
        "tenant_id": int(tenant_id),
        "synthetic_customer_alias": resolved_alias,
        "identity": internal_e2e_customer_identity(tenant_id, resolved_alias),
        "exclude_from_analytics": True,
        "exclude_from_automation": True,
        "exclude_from_unread": True,
        "external_egress_allowed": False,
    }


def metadata_matches_internal_e2e_identity(
    metadata: object,
    *,
    tenant_id: int,
    alias: object,
) -> bool:
    if not isinstance(metadata, dict):
        return False
    expected = internal_e2e_metadata(tenant_id, alias)
    return all(metadata.get(key) == value for key, value in expected.items())


__all__ = [
    "INTERNAL_E2E_ALIASES",
    "INTERNAL_E2E_CHANNEL",
    "INTERNAL_E2E_CONNECTION_ID",
    "InternalE2EAlias",
    "internal_e2e_customer_identity",
    "internal_e2e_metadata",
    "metadata_matches_internal_e2e_identity",
    "normalize_internal_e2e_alias",
    "parse_internal_e2e_customer_identity",
]
