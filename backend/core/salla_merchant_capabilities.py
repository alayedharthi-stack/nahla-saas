"""
Salla merchant-enabled shipping + payment capabilities (Pack B).

Source of Truth distinctions (do not conflate):
  PLATFORM_SUPPORTED  — what Salla platform can offer in general (never customer truth)
  MERCHANT_ENABLED    — configured/enabled for this merchant in Salla
  ELIGIBLE_NOW        — available for this cart/city/customer (zone/on-demand)
  ORDER_ACTUAL        — carrier/method actually used on an order (shipment evidence)

Storage: Integration.config['checkout_profile'] (tenant-scoped via Integration.tenant_id).
Nahla-native WhatsApp payment flags (merchant_payment_methods) are a SEPARATE surface.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.salla_merchant_capabilities")

SCHEMA_VERSION = 1
SURFACE_SALLA_STOREFRONT = "salla_storefront"
SURFACE_NAHLA_CHECKOUT = "nahla_checkout"

STATUS_KNOWN = "known"
STATUS_EMPTY = "empty"
STATUS_UNKNOWN = "unknown"
STATUS_FORBIDDEN = "forbidden"

KIND_MERCHANT_ENABLED = "merchant_enabled"
KIND_ELIGIBLE_NOW = "eligible_now"
KIND_ORDER_ACTUAL = "order_actual"

PAYMENT_ENDPOINT = "/payment/methods"
SHIPPING_COMPANIES_ENDPOINT = "/shipping/companies/"
SHIPPING_ZONES_ENDPOINT = "/shipping/zones"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def normalize_payment_method_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize one Salla payment method row into a compact capability fact."""
    if isinstance(raw, str):
        code = raw.strip()
        if not code:
            return None
        return {"code": code, "label": code, "id": None, "enabled": True}
    if not isinstance(raw, dict):
        return None
    code = str(
        raw.get("slug")
        or raw.get("code")
        or raw.get("type")
        or raw.get("name")
        or ""
    ).strip()
    if not code:
        return None
    label = str(raw.get("name") or raw.get("label") or code).strip() or code
    method_id = raw.get("id")
    return {
        "code": code,
        "label": label,
        "id": method_id,
        "enabled": True,
    }


def normalize_shipping_company_entry(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    company_id = raw.get("id")
    if company_id in (None, ""):
        return None
    name = str(
        raw.get("name") or raw.get("courier_name") or ""
    ).strip()
    slug = raw.get("slug")
    activation_type = raw.get("activation_type")
    # Official List Shipping Companies returns active companies for the store.
    # Legacy payloads may include is_active; default True when absent.
    if "is_active" in raw:
        active = bool(raw.get("is_active"))
    elif "active" in raw:
        active = bool(raw.get("active"))
    else:
        active = True
    return {
        "id": company_id,
        "name": name,
        "slug": slug,
        "activation_type": activation_type,
        "active": active,
        "type": raw.get("type") or raw.get("delivery_type") or "shipping",
        "enabled": active,
    }


def normalize_zone_summary(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    zone_id = raw.get("id")
    if zone_id in (None, ""):
        return None
    return {
        "id": zone_id,
        "name": str(raw.get("name") or "").strip(),
    }


def resource_block(
    *,
    status: str,
    endpoint: str,
    scope: str,
    items: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "status": status,
        "endpoint": endpoint,
        "scope": scope,
        "fetched_at": _utc_now_iso(),
        "error": error,
        "items": list(items or []),
    }
    if extra:
        block.update(extra)
    return block


def payment_codes(profile: Optional[Dict[str, Any]]) -> List[str]:
    """Return MERCHANT_ENABLED payment codes only when status is known/empty."""
    profile = _as_dict(profile)
    payments = _as_dict(profile.get("payments"))
    status = str(payments.get("status") or "").strip().lower()
    if status not in (STATUS_KNOWN, STATUS_EMPTY):
        # Legacy profiles may only have payment_methods list without status.
        # Treat non-empty legacy list as known; empty/missing as unknown.
        legacy = profile.get("payment_methods")
        if isinstance(legacy, list) and legacy and "payments" not in profile:
            return [str(x).strip() for x in legacy if str(x).strip()]
        return []
    items = payments.get("items") or []
    codes: List[str] = []
    for item in items:
        if isinstance(item, dict):
            code = str(item.get("code") or "").strip()
            if code and item.get("enabled", True):
                codes.append(code)
        elif isinstance(item, str) and item.strip():
            codes.append(item.strip())
    if codes:
        return codes
    # Fall back to flattened payment_methods when status known.
    flat = profile.get("payment_methods") or []
    if isinstance(flat, list):
        return [str(x).strip() for x in flat if str(x).strip()]
    return []


def shipping_company_names(profile: Optional[Dict[str, Any]]) -> List[str]:
    profile = _as_dict(profile)
    shipping = _as_dict(profile.get("shipping"))
    companies = _as_dict(shipping.get("companies"))
    status = str(companies.get("status") or "").strip().lower()
    items = companies.get("items")
    if not isinstance(items, list):
        items = profile.get("shipping_companies") or []
        if items and "shipping" not in profile:
            status = STATUS_KNOWN
    if status not in (STATUS_KNOWN, STATUS_EMPTY):
        return []
    names: List[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("enabled", item.get("active", True)) is False:
            continue
        name = str(item.get("name") or item.get("slug") or "").strip()
        if name:
            names.append(name)
    return names


def payments_status(profile: Optional[Dict[str, Any]]) -> str:
    profile = _as_dict(profile)
    payments = _as_dict(profile.get("payments"))
    status = str(payments.get("status") or "").strip().lower()
    if status:
        return status
    if "payments" not in profile and isinstance(profile.get("payment_methods"), list):
        return STATUS_KNOWN if profile.get("payment_methods") else STATUS_UNKNOWN
    return STATUS_UNKNOWN


def shipping_companies_status(profile: Optional[Dict[str, Any]]) -> str:
    profile = _as_dict(profile)
    shipping = _as_dict(profile.get("shipping"))
    companies = _as_dict(shipping.get("companies"))
    status = str(companies.get("status") or "").strip().lower()
    if status:
        return status
    if "shipping" not in profile and isinstance(profile.get("shipping_companies"), list):
        return STATUS_KNOWN if profile.get("shipping_companies") else STATUS_UNKNOWN
    return STATUS_UNKNOWN


@dataclass(frozen=True)
class MerchantCapabilitiesProjection:
    """Compact Trusted Context / Brain projection (no raw API blobs)."""

    surface: str = SURFACE_SALLA_STOREFRONT
    source: str = "salla"
    kind: str = KIND_MERCHANT_ENABLED
    fetched_at: str = ""
    payments_status: str = STATUS_UNKNOWN
    payment_methods: List[Dict[str, Any]] = field(default_factory=list)
    shipping_companies_status: str = STATUS_UNKNOWN
    shipping_companies: List[Dict[str, Any]] = field(default_factory=list)
    shipping_zones_status: str = STATUS_UNKNOWN
    shipping_zones: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "surface": self.surface,
            "source": self.source,
            "kind": self.kind,
            "freshness": {"fetched_at": self.fetched_at},
            "payments": {
                "status": self.payments_status,
                "methods": list(self.payment_methods),
            },
            "shipping": {
                "companies_status": self.shipping_companies_status,
                "companies": list(self.shipping_companies),
                "zones_status": self.shipping_zones_status,
                # Thin zone summaries only — never fees/duration here.
                "zones": list(self.shipping_zones),
            },
        }


def project_merchant_capabilities(
    profile: Optional[Dict[str, Any]],
) -> MerchantCapabilitiesProjection:
    profile = _as_dict(profile)
    payments = _as_dict(profile.get("payments"))
    shipping = _as_dict(profile.get("shipping"))
    companies = _as_dict(shipping.get("companies"))
    zones = _as_dict(shipping.get("zones"))

    pay_status = payments_status(profile)
    company_status = shipping_companies_status(profile)
    zone_status = str(zones.get("status") or "").strip().lower() or (
        STATUS_KNOWN
        if ("shipping" not in profile and profile.get("shipping_zones"))
        else STATUS_UNKNOWN
    )

    pay_items: List[Dict[str, Any]] = []
    if pay_status in (STATUS_KNOWN, STATUS_EMPTY):
        raw_items = payments.get("items")
        if isinstance(raw_items, list) and raw_items:
            for raw in raw_items:
                norm = normalize_payment_method_entry(raw)
                if norm:
                    pay_items.append(norm)
        else:
            for code in payment_codes(profile):
                pay_items.append({
                    "code": code,
                    "label": code,
                    "id": None,
                    "enabled": True,
                })

    company_items: List[Dict[str, Any]] = []
    if company_status in (STATUS_KNOWN, STATUS_EMPTY):
        raw_companies = companies.get("items")
        if not isinstance(raw_companies, list):
            raw_companies = profile.get("shipping_companies") or []
        for raw in raw_companies:
            norm = normalize_shipping_company_entry(raw)
            if norm and norm.get("enabled", True):
                company_items.append({
                    "id": norm.get("id"),
                    "name": norm.get("name") or "",
                    "slug": norm.get("slug"),
                    "enabled": True,
                })

    zone_items: List[Dict[str, Any]] = []
    if zone_status in (STATUS_KNOWN, STATUS_EMPTY):
        raw_zones = zones.get("items")
        if not isinstance(raw_zones, list):
            raw_zones = profile.get("shipping_zones") or []
        for raw in raw_zones:
            if isinstance(raw, dict) and raw.get("id") is not None:
                zone_items.append({
                    "id": raw.get("id"),
                    "name": str(raw.get("name") or "").strip(),
                })

    fetched_at = str(
        payments.get("fetched_at")
        or companies.get("fetched_at")
        or profile.get("last_synced_at")
        or ""
    )

    return MerchantCapabilitiesProjection(
        fetched_at=fetched_at,
        payments_status=pay_status,
        payment_methods=pay_items,
        shipping_companies_status=company_status,
        shipping_companies=company_items,
        shipping_zones_status=zone_status,
        shipping_zones=zone_items,
    )


def load_checkout_profile_for_tenant(db: Any, tenant_id: int) -> Optional[Dict[str, Any]]:
    """Load tenant-scoped Salla checkout_profile; never cross-tenant."""
    if db is None or not tenant_id:
        return None
    try:
        from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415

        intg = pick_active_salla_integration(db, tenant_id)
        if not intg:
            return None
        cfg = intg.config if isinstance(intg.config, dict) else {}
        profile = cfg.get("checkout_profile")
        return dict(profile) if isinstance(profile, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SallaCapabilities] load checkout_profile failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None


def merge_checkout_profile_into_config(
    existing_config: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """Atomic subtree merge — do not clobber unrelated Integration.config keys."""
    new_config = dict(existing_config or {})
    new_config["checkout_profile"] = dict(profile or {})
    return new_config


def classify_http_capability_error(exc: BaseException) -> str:
    """Map fetch failure to capability status without inventing methods."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in (401, 403):
        return STATUS_FORBIDDEN
    return STATUS_UNKNOWN


def zone_matches_city(zone: Dict[str, Any], city: str) -> bool:
    """Best-effort city match against thin zone name (not eligibility proof alone)."""
    needle = (city or "").strip().lower()
    if not needle:
        return False
    name = str(zone.get("name") or "").strip().lower()
    return bool(name) and (needle in name or name in needle)


def find_zone_ids_for_city(
    profile: Optional[Dict[str, Any]],
    city: str,
) -> List[Any]:
    """Return candidate zone ids from cached thin zone list for on-demand detail fetch."""
    projection = project_merchant_capabilities(profile)
    if projection.shipping_zones_status not in (STATUS_KNOWN, STATUS_EMPTY):
        return []
    return [
        z.get("id")
        for z in projection.shipping_zones
        if zone_matches_city(z, city) and z.get("id") is not None
    ]


def assert_no_fabricated_cod(profile: Dict[str, Any]) -> None:
    """Test helper: unknown/forbidden payments must not materialize as COD-enabled."""
    status = payments_status(profile)
    if status in (STATUS_UNKNOWN, STATUS_FORBIDDEN):
        assert payment_codes(profile) == []
        payments = _as_dict(profile.get("payments"))
        assert not any(
            str((item or {}).get("code") if isinstance(item, dict) else item)
            .strip()
            .lower()
            == "cod"
            for item in (payments.get("items") or [])
        )
