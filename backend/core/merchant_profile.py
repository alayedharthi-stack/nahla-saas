"""
Pack A2 — customer-facing structured merchant profile resolution.

Resolves field-by-field from:
  1) explicit Nahla manual override in TenantSettings.store_settings only
  2) namespaced Salla ``salla_store_info`` (when present)
  3) legacy snapshot fallback when both override and Salla field are absent

Snapshot values are NOT treated as manual overrides (avoids stale copy beating Salla).
Phone NEVER comes from WhatsApp owner number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


PROFILE_FIELDS = (
    "name",
    "description",
    "email",
    "domain",
    "logo_url",
    "social_links",
    "currency",
    "status",
    "phone",
    "location",
    "working_hours",
    "default_branch",
)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict)):
        return bool(value)
    return bool(str(value).strip())


def _s(value: Any) -> str:
    return str(value or "").strip()


def _load_store_settings(db: Any, tenant_id: int) -> Dict[str, Any]:
    if db is None or not tenant_id:
        return {}
    try:
        from models import TenantSettings  # noqa: PLC0415

        row = db.query(TenantSettings).filter_by(tenant_id=int(tenant_id)).first()
        cfg = (row.store_settings or {}) if row else {}
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load_snapshot_profile(db: Any, tenant_id: int) -> Dict[str, Any]:
    if db is None or not tenant_id:
        return {}
    try:
        from core.store_knowledge import StoreKnowledgeLoader  # noqa: PLC0415

        return dict(StoreKnowledgeLoader(db, int(tenant_id)).store_profile() or {})
    except Exception:  # noqa: BLE001
        return {}


def _manual_social(store_cfg: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    mapping = {
        "instagram": "instagram_url",
        "twitter": "twitter_url",
        "snapchat": "snapchat_url",
        "tiktok": "tiktok_url",
    }
    for key, cfg_key in mapping.items():
        url = _s(store_cfg.get(cfg_key))
        if url:
            out[key] = url
    return out


def _pick_field(
    *,
    manual: Any,
    salla: Any,
    fallback: Any = None,
) -> Tuple[Any, str]:
    """Return (value, provenance_source)."""
    if _present(manual):
        return manual, "manual_override"
    if _present(salla):
        return salla, "salla_store_info"
    if _present(fallback):
        return fallback, "legacy_fallback"
    return None, "absent"


@dataclass(frozen=True)
class ResolvedMerchantProfile:
    tenant_id: int
    name: str = ""
    description: str = ""
    email: str = ""
    domain: str = ""
    logo_url: str = ""
    social_links: Dict[str, str] = field(default_factory=dict)
    currency: str = ""
    status: str = ""
    phone: str = ""
    location: Any = None
    working_hours: Any = None
    default_branch: Any = None
    field_sources: Dict[str, str] = field(default_factory=dict)
    salla_present: bool = False
    manual_present: bool = False

    def field_status(self, key: str) -> str:
        return "KNOWN_VALUE" if _present(getattr(self, key, None)) else "UNKNOWN"

    def to_public_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tenant_id": int(self.tenant_id)}
        for key in PROFILE_FIELDS:
            val = getattr(self, key, None)
            if _present(val):
                out[key] = val
            out[f"{key}_status"] = self.field_status(key)
        out["field_sources"] = dict(self.field_sources)
        out["salla_surface_present"] = self.salla_present
        out["manual_surface_present"] = self.manual_present
        return out


def resolve_merchant_profile(db: Any, tenant_id: int) -> ResolvedMerchantProfile:
    """Resolve structured customer-facing merchant profile for one tenant."""
    store_cfg = _load_store_settings(db, tenant_id)
    snap = _load_snapshot_profile(db, tenant_id)
    salla = store_cfg.get("salla_store_info") or snap.get("salla_store_info") or {}
    if not isinstance(salla, dict):
        salla = {}

    # Explicit Nahla overrides live ONLY in TenantSettings.store_settings.
    # Snapshot flat fields are legacy fallback — never override Salla.
    manual_name = _s(store_cfg.get("store_name"))
    manual_desc = _s(store_cfg.get("store_description"))
    manual_email = _s(store_cfg.get("contact_email"))
    manual_domain = _s(store_cfg.get("store_url"))
    manual_logo = _s(store_cfg.get("logo_url") or store_cfg.get("store_logo_url"))
    manual_social = _manual_social(store_cfg)
    manual_phone = _s(
        store_cfg.get("store_phone")
        or store_cfg.get("public_phone")
        or store_cfg.get("contact_phone_public")
    )
    manual_hours = store_cfg.get("working_hours") or store_cfg.get("business_hours")
    manual_location = store_cfg.get("location") or store_cfg.get("store_location")
    manual_branch = store_cfg.get("default_branch")

    salla_name = _s(salla.get("name"))
    salla_desc = _s(salla.get("description") or salla.get("about"))
    salla_email = _s(salla.get("email"))
    salla_domain = _s(salla.get("domain"))
    salla_logo = _s(salla.get("logo_url"))
    salla_social = salla.get("social_links") if isinstance(salla.get("social_links"), dict) else {}
    salla_currency = _s(salla.get("currency"))
    salla_status = _s(salla.get("store_status") or salla.get("status"))
    salla_phone = _s(salla.get("phone") or salla.get("mobile"))
    salla_location = salla.get("location")
    salla_hours = salla.get("working_hours")
    salla_branch = salla.get("default_branch")

    # Snapshot fallbacks (never WhatsApp owner contact_phone).
    snap_name = _s(snap.get("store_name"))
    snap_desc = _s(snap.get("description"))
    snap_email = _s(snap.get("contact_email"))
    snap_domain = _s(snap.get("store_url"))
    snap_logo = _s(snap.get("logo_url"))
    snap_hours = snap.get("working_hours") or snap.get("business_hours")

    sources: Dict[str, str] = {}
    name, sources["name"] = _pick_field(manual=manual_name, salla=salla_name, fallback=snap_name)
    description, sources["description"] = _pick_field(
        manual=manual_desc, salla=salla_desc, fallback=snap_desc,
    )
    email, sources["email"] = _pick_field(manual=manual_email, salla=salla_email, fallback=snap_email)
    domain, sources["domain"] = _pick_field(
        manual=manual_domain, salla=salla_domain, fallback=snap_domain,
    )
    logo_url, sources["logo_url"] = _pick_field(
        manual=manual_logo, salla=salla_logo, fallback=snap_logo,
    )
    social, sources["social_links"] = _pick_field(manual=manual_social, salla=salla_social)
    currency, sources["currency"] = _pick_field(manual=None, salla=salla_currency)
    status, sources["status"] = _pick_field(manual=None, salla=salla_status)
    phone, sources["phone"] = _pick_field(manual=manual_phone, salla=salla_phone)
    location, sources["location"] = _pick_field(
        manual=manual_location, salla=salla_location,
    )
    working_hours, sources["working_hours"] = _pick_field(
        manual=manual_hours, salla=salla_hours, fallback=snap_hours,
    )
    default_branch, sources["default_branch"] = _pick_field(
        manual=manual_branch, salla=salla_branch,
    )

    return ResolvedMerchantProfile(
        tenant_id=int(tenant_id),
        name=_s(name),
        description=_s(description),
        email=_s(email),
        domain=_s(domain),
        logo_url=_s(logo_url),
        social_links=dict(social or {}),
        currency=_s(currency),
        status=_s(status),
        phone=_s(phone),
        location=location,
        working_hours=working_hours,
        default_branch=default_branch,
        field_sources=sources,
        salla_present=bool(salla),
        manual_present=any(
            _present(v)
            for v in (
                manual_name,
                manual_desc,
                manual_email,
                manual_domain,
                manual_logo,
                manual_social,
                manual_phone,
                manual_hours,
                manual_location,
                manual_branch,
            )
        ),
    )


def apply_resolved_profile_to_commerce_facts(facts: Any, profile: ResolvedMerchantProfile) -> None:
    """Overlay CommerceFacts with resolved profile (no WA-owner phone invention)."""
    if not facts or profile is None:
        return
    if profile.name:
        try:
            from core.store_display import clean_store_name  # noqa: PLC0415

            facts.store_name = clean_store_name(profile.name)
        except Exception:  # noqa: BLE001
            facts.store_name = profile.name
    if profile.domain:
        facts.store_url = profile.domain
    if profile.description:
        facts.store_description = profile.description
    if profile.email:
        facts.store_contact_email = profile.email
    # Pack A2: public profile phone only — never WhatsApp owner number.
    facts.store_contact_phone = profile.phone or ""
    setattr(facts, "merchant_profile_social_links", dict(profile.social_links or {}))
    setattr(facts, "merchant_profile_currency", profile.currency)
    setattr(facts, "merchant_profile_status", profile.status)
    setattr(facts, "merchant_profile_field_sources", dict(profile.field_sources or {}))
    setattr(facts, "merchant_profile_phone_status", profile.field_status("phone"))
