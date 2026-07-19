"""
core/tenant.py
──────────────
Tenant resolution and settings helpers shared across all routers.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from models import Tenant, TenantSettings  # noqa: E402


# ── Default configuration values ──────────────────────────────────────────────
# These are merged over stored JSONB values so new keys always have a value.

DEFAULT_WHATSAPP: Dict[str, Any] = {
    "business_display_name":    "",
    "phone_number":             "",
    "phone_number_id":          "",
    "access_token":             "",
    "verify_token":             "",
    "webhook_url":              "https://api.nahlah.ai/webhook/whatsapp",
    "store_button_label":       "زيارة المتجر",
    "store_button_url":         "",
    "owner_contact_label":      "تواصل مع المالك",
    "owner_whatsapp_number":    "",
    "auto_reply_enabled":       True,
    "transfer_to_owner_enabled": True,
    # draft_approval — save as DRAFT, merchant reviews before Meta submission
    # auto_submit    — generate and submit to Meta immediately
    "template_submission_mode": "draft_approval",
}

STORE_AI_MODE_OFF = "off"
STORE_AI_MODE_TEST = "test"
STORE_AI_MODE_ON = "on"
VALID_STORE_AI_MODES = frozenset({STORE_AI_MODE_OFF, STORE_AI_MODE_TEST, STORE_AI_MODE_ON})

DEFAULT_AI: Dict[str, Any] = {
    # Store-wide AI master switch. When False, no automated AI outbound for
    # any customer. Independent of per-conversation ai_paused flags.
    # Legacy boolean — kept in sync with store_ai_mode on PATCH /settings/ai.
    "store_ai_enabled":  True,
    # off | test | on — test mode replies only to ai_test_allowed_numbers.
    "store_ai_mode":     STORE_AI_MODE_ON,
    "ai_test_allowed_numbers": [],
    "assistant_name":    "نحلة",
    # ARCH-KB-001: neutral store-context stub — no professional role or
    # mandatory first-turn self-introduction. Merchants tune voice via
    # reply_tone / G7 behavioral sections, not a role essay.
    "assistant_role":    (
        "تساعدين عملاء المتجر بأسلوب طبيعي وودّي — اختصاراً وصدقاً، "
        "بدون ضغط بيعي وبدون فرض تعريف أو دور مهني في التحية."
    ),
    "reply_tone":        "friendly",
    "reply_length":      "medium",
    "default_language":  "arabic",
    "owner_instructions": (
        "- ردودك لا تتجاوز 3-4 أسطر في الغالب — اختصري دائماً.\n"
        "- إذا احتاج الموضوع تفصيلاً، لخّصيه في جملتين ثم اسألي: «تبي أعرفك أكثر؟»\n"
        "- لا قوائم طويلة ولا شرح موسوعي.\n"
        "- الإيموجي باعتدال 😊.\n"
        "- أسلوب محادثة طبيعي — كأنك صديقة تتكلم، مش تكتب تقرير.\n"
        "- إذا تحدث العميل بالإنجليزية، ردّي بالإنجليزية بنفس الأسلوب.\n"
        "- لا تعدي بما ليس في يدك.\n"
        "- لا تبالغي في وصف المنتجات أكثر من الحقيقة."
    ),
    "coupon_rules": (
        "- اقترحي خصماً فقط عند تردد العميل في الشراء أو طلبه الخصم صراحةً.\n"
        "- لا تذكري نسبة الخصم مسبقاً — قولي فقط «عندي مفاجأة لك 🎁» ثم أرسلي الكوبون.\n"
        "- اقترحي الخصم مرة واحدة فقط في كل محادثة.\n"
        "- لا خصم على المنتجات المستثناة التي يحددها المالك."
    ),
    "escalation_rules": (
        "- حوّلي المحادثة لفريق الدعم عند: شكاوى جدية، طلبات جملة كبيرة، "
        "أسئلة خارج نطاق معلوماتك.\n"
        "- أبلغي العميل بلطف قبل التحويل: «سأوصلك بفريق الدعم ليساعدك بشكل أفضل».\n"
        "- لا تتعهدي بوعود خارج صلاحياتك."
    ),
    "allowed_discount_levels":  "10",
    "recommendations_enabled":  True,
    # ── Knowledge Base (free-form merchant-supplied store knowledge) ──────
    # Plain text dump that the merchant fills in via "نحلة الذكية → قاعدة
    # المعرفة" page. Architecturally it is an *additional* layer on top of
    # owner_instructions: owner_instructions controls how the assistant
    # *behaves*, while manual_knowledge_base feeds the assistant *facts*
    # about the store (products, FAQ, shipping notes, warranty, payment, …).
    # The runtime overlay tags it as a non-authoritative source and
    # explicitly defers to Salla data for prices / inventory / variants
    # whenever Salla is connected — see modules/ai/prompts/tenant_overlay.py.
    "manual_knowledge_base":     "",
    # ── Merchant-configurable policy rules (Phase 11) ─────────────────────
    # coupon_cap_hours: block a second coupon to same customer within N hours
    "coupon_cap_hours":          24,
    # auto_escalate_after_n: transfer to human after N consecutive GENERAL turns
    "auto_escalate_after_n":    3,
    # max_order_value: refuse orders above this amount (0 or null = unlimited)
    "max_order_value":           0,
    # context_verbosity: "full" (default) or "compact" (A/B test smaller context)
    "context_verbosity":        "full",
    # FactBoundPersonaComposer — Phase 2 social surfaces (test mode only).
    "persona_composer_enabled": False,
    "persona_composer_enforce_test_mode": True,
    "persona_composer_surfaces": [
        "social_greeting",
        "social_checkin",
        "thanks",
        "dua",
        "payment_media_intro",
    ],
    # Deny-all default — acceptance tenants must opt in via stored ai_settings.
    "persona_composer_allowlist_tenants": [],
}

DEFAULT_STORE: Dict[str, Any] = {
    "store_name":           "",
    "store_name_source":    "",
    "store_name_ar":        "",
    "store_name_en":        "",
    "store_name_ar_source": "",
    "store_name_en_source": "",
    "store_logo_url":       "",
    "store_url":            "",
    "platform_type":        "salla",
    "salla_client_id":      "",
    "salla_client_secret":  "",
    "salla_access_token":   "",
    "zid_client_id":        "",
    "zid_client_secret":    "",
    "shopify_shop_domain":  "",
    "shopify_access_token": "",
    "shipping_provider":    "",
    "google_maps_location": "",
    # Merchant toggles for AI purchase-channel facts (JSON — no migration).
    "sales_channels": {
        "online_store": {"enabled": True},
        "whatsapp_quick_order": {"enabled": True},
        "showroom_visit": {"enabled": True},
    },
    "instagram_url":        "",
    "twitter_url":          "",
    "snapchat_url":         "",
    "tiktok_url":           "",
}

DEFAULT_NOTIFICATIONS: Dict[str, Any] = {
    "whatsapp_alerts":       True,
    "email_alerts":          True,
    "system_alerts":         True,
    "failed_webhook_alerts": True,
    "low_balance_alerts":    True,
}


def merge_defaults(stored: Optional[Dict], defaults: Dict) -> Dict:
    """Merge stored values over defaults so new keys always have a fallback."""
    result = dict(defaults)
    if stored:
        result.update(stored)
    return result


# Fields whose empty-string values should fall back to DEFAULT_AI suggestions
_AI_TEXT_FIELDS = frozenset({
    "assistant_role", "owner_instructions", "coupon_rules", "escalation_rules",
})


def resolve_store_ai_mode(ai: Optional[Dict[str, Any]]) -> str:
    """Resolve effective store AI mode with backward compatibility."""
    stored = dict(ai or {})
    if "store_ai_mode" in stored:
        mode = str(stored.get("store_ai_mode") or "").strip().lower()
        if mode in VALID_STORE_AI_MODES:
            return mode
    if stored.get("store_ai_enabled") is False:
        return STORE_AI_MODE_OFF
    return STORE_AI_MODE_ON


def sync_store_ai_enabled_from_mode(mode: str) -> bool:
    """Legacy boolean mirror for banners and older callers."""
    return resolve_store_ai_mode({"store_ai_mode": mode}) == STORE_AI_MODE_ON


def merge_ai_defaults(stored: Optional[Dict]) -> Dict:
    """
    Like merge_defaults but for AI settings:
    - Missing keys get the DEFAULT_AI value.
    - Empty-string text instruction fields also get the DEFAULT_AI value,
      so new and existing tenants see the rich defaults until they customise.
    - Non-empty values (including non-instruction fields) are always respected.
    """
    result = dict(DEFAULT_AI)
    if stored:
        for k, v in stored.items():
            if k in _AI_TEXT_FIELDS and v == "":
                pass  # keep rich default
            else:
                result[k] = v
    if not stored or "store_ai_mode" not in stored:
        if result.get("store_ai_enabled") is False:
            result["store_ai_mode"] = STORE_AI_MODE_OFF
        else:
            result["store_ai_mode"] = STORE_AI_MODE_ON
    return result


_tenant_logger = logging.getLogger("nahla.tenant")


def resolve_tenant_id(request: Request) -> int:
    """
    Resolve tenant_id for the current request.

    Priority (authoritative → restricted fallback)
    ------------------------------------
    1. JWT payload claim   — set by jwt_enforcement_middleware; always present on
                             authenticated routes after the middleware fix.
    2. request.state       — set by multi_tenant_middleware from X-Tenant-ID header
                             (dev/testing only).
    3. Explicit failure     — no tenant scope means request must be rejected.

    Silent fallback to tenant 1 is forbidden because it compromises tenant
    isolation. Every call that reaches the final failure path indicates an
    auth or middleware bug and must fail closed.
    """
    # Path 1 — JWT claim (expected path for all authenticated requests)
    jwt_payload = getattr(request.state, "jwt_payload", None)
    if jwt_payload:
        tid = jwt_payload.get("tenant_id")
        if tid is not None:
            return int(tid)
        # JWT present but missing tenant_id — should have been caught by middleware
        _tenant_logger.critical(
            "[tenant] JWT has no tenant_id! path=%s sub=%s role=%s — rejecting request",
            request.url.path, jwt_payload.get("sub"), jwt_payload.get("role"),
        )
        raise HTTPException(
            status_code=401,
            detail="Token missing tenant scope — please log in again",
        )

    # Path 2 — header/state (dev testing only)
    # NOTE: catch TypeError as well — `int(None)` raises TypeError, not
    # ValueError, and `request.state.tenant_id` is explicitly set to `None`
    # by middleware when no header is present. Without this catch the
    # request would 500 instead of returning a 401, leaking a server error
    # for what is really an auth-scope failure.
    try:
        tid = int(request.state.tenant_id)
        if tid > 0:
            _tenant_logger.warning(
                "[tenant] resolve_tenant_id used X-Tenant-ID header fallback — path=%s tid=%s "
                "(only acceptable in dev; JWT middleware should set this in production)",
                request.url.path, tid,
            )
            return tid
    except (ValueError, AttributeError, TypeError):
        pass

    # Path 3 — explicit failure (SHOULD NEVER REACH HERE)
    _tenant_logger.critical(
        "[tenant] resolve_tenant_id has no tenant scope! path=%s — rejecting request. "
        "Check JWT middleware / impersonation / header propagation.",
        request.url.path,
    )
    raise HTTPException(
        status_code=401,
        detail="Tenant scope required for this request",
    )


def get_or_create_tenant(db: Session, tenant_id: int) -> Tenant:
    """
    Fetch an existing tenant.

    Placeholder auto-creation is disabled by default because it can hide
    tenant-resolution bugs and silently route data into unintended tenants.
    It may be re-enabled only in tightly controlled development setups by
    setting `NAHLA_ALLOW_RUNTIME_TENANT_AUTO_CREATE=1`.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        if os.getenv("NAHLA_ALLOW_RUNTIME_TENANT_AUTO_CREATE", "").strip() == "1":
            _tenant_logger.warning(
                "[tenant] Runtime auto-create enabled for tenant_id=%s — development only",
                tenant_id,
            )
            tenant = Tenant(
                id=tenant_id,
                name=f"متجر رقم {tenant_id}",
                domain=f"store-{tenant_id}.nahla.sa",
                is_active=True,
                created_at=datetime.now(timezone.utc),
            )
            db.add(tenant)
            db.flush()
        else:
            _tenant_logger.error(
                "[tenant] Tenant %s not found — refusing implicit creation",
                tenant_id,
            )
            raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def get_or_create_settings(db: Session, tenant_id: int) -> TenantSettings:
    """Fetch existing TenantSettings or create with defaults."""
    get_or_create_tenant(db, tenant_id)
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    if not settings:
        settings = TenantSettings(
            tenant_id=tenant_id,
            show_nahla_branding=True,
            branding_text="🐝 Powered by Nahla",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(settings)
        db.flush()
    return settings
