"""
Unified store URL resolver — single source of truth for online store links.

Used by FAQ / Layer0 / safety-net / link-intent / facts loader paths.
Operational: returns evidence-backed URL or honest none — never hallucinates.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple

logger = logging.getLogger("nahla.brain.store_url_resolver")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_DIA_RE = re.compile(f"[{_DIA}]+")

_STORE_KB_KINDS = (
    "store_story",
    "custom",
    "quick_update",
    "payment_method",
    "branches",
)

_URL_IN_TEXT_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b[a-z0-9][a-z0-9-]*\."
    r"(?:com|net|sa|store|shop|me|io|co|app|site)\b(?:/\S*)?",
    re.IGNORECASE,
)

_MAPS_HOST_HINTS = (
    "google.com/maps",
    "maps.google",
    "maps.app.goo.gl",
    "goo.gl/maps",
    "waze.com",
    "apple.com/maps",
)

_SOCIAL_HOST_HINTS = (
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "snapchat.com",
)

_WHATSAPP_HOST_HINTS = (
    "wa.me",
    "whatsapp.com",
    "api.whatsapp.com",
)

_STORE_CONTEXT_MARKERS = (
    "متجر",
    "موقع",
    "الكتروني",
    "إلكتروني",
    "اونلاين",
    "أونلاين",
    "store",
    "website",
    "shop",
    "طلب",
    "order",
    "شراء",
)

_ONLINE_STORE_INQUIRY_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:عند(?:كم|ك)|هل\s+عند(?:كم|ك)|لديك(?:م|)?)\s+"
    r"(?:متجر|موقع)(?:\s*(?:ال)?(?:إ|ا)?لكتروني|\s*اونلاين|\s*أونلاين)?"
    r"|(?:^|\s)(?:متجر|موقع)(?:\s*(?:ال)?(?:إ|ا)?لكتروني|\s*اونلاين|\s*أونلاين)"
    r"(?:\s*(?:عند(?:كم|ك)|موجود|متاح))?"
    r"|(?:^|\s)(?:رابط|لينك)\s*(?:ال)?(?:متجر|موقع|طلب|شراء)"
    r"|(?:^|\s)(?:ا|أ)?(?:بي|بغ(?:ى|a)?)\s*(?:أ?)?(?:طلب|اطلب)\s+من\s+(?:ال)?(?:موقع|متجر)"
    r"|(?:^|\s)(?:كيف|وش)\s+(?:أ?)?(?:طلب|اطلب)\s+من\s+(?:ال)?(?:موقع|متجر)"
    r"|online\s+store|e[\-\s]?commerce|website\s+link|store\s+link"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class StoreUrlResolution:
    found: bool
    url: str = ""
    source: str = "none"
    reason: str = ""

    def to_log_dict(self) -> dict:
        return {
            "found": self.found,
            "url_len": len(self.url or ""),
            "source": self.source,
            "reason": self.reason,
        }


def _normalise_url(url: str) -> str:
    s = str(url or "").strip().rstrip("/")
    if not s:
        return ""
    low = s.lower()
    if "<" in s or "magicmock" in low or "mock" in low:
        return ""
    if not s.lower().startswith(("http://", "https://")):
        s = "https://" + s.lstrip("/")
    if not re.match(r"^https?://[^\s<]+", s, re.IGNORECASE):
        return ""
    # Merchant storefront SoT must never accept Nahla platform pages
    # (e.g. app.nahlah.ai/register) as a silent store URL substitute.
    try:
        from modules.ai.brain.commerce.storefront_product_url import (  # noqa: PLC0415
            is_platform_non_merchant_url,
        )

        if is_platform_non_merchant_url(s):
            return ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — hygiene import must not block resolver
        pass
    return s


def _normalise_message(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = _DIA_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    t = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_online_store_inquiry(message: str) -> bool:
    """True when the customer asks about the online store / website / order link."""
    norm = _normalise_message(message)
    if not norm:
        return False
    if _ONLINE_STORE_INQUIRY_RE.search(norm):
        return True
    try:
        from modules.ai.brain.commerce.link_intent import (  # noqa: PLC0415
            LinkIntentType,
            resolve_inbound_link_intent,
        )

        return resolve_inbound_link_intent(message or "") == LinkIntentType.WEBSITE_URL
    except Exception:
        return False


def _is_maps_url(url: str) -> bool:
    low = str(url or "").lower()
    return any(h in low for h in _MAPS_HOST_HINTS)


def _is_whatsapp_url(url: str) -> bool:
    low = str(url or "").lower()
    return any(h in low for h in _WHATSAPP_HOST_HINTS)


def _is_social_url(url: str) -> bool:
    low = str(url or "").lower()
    return any(h in low for h in _SOCIAL_HOST_HINTS)


def _store_context_near(text: str, url_start: int, url_end: int, *, window: int = 120) -> bool:
    """True when surrounding text labels the URL as a store/order channel."""
    body = str(text or "")
    if not body:
        return False
    start = max(0, url_start - window)
    end = min(len(body), url_end + window)
    window_text = _normalise_message(body[start:end])
    if any(m in window_text for m in _STORE_CONTEXT_MARKERS):
        return True
    # Line-level label before URL, e.g. "المتجر الإلكتروني: https://..."
    line_start = body.rfind("\n", 0, url_start) + 1
    line_end = body.find("\n", url_end)
    if line_end < 0:
        line_end = len(body)
    line = _normalise_message(body[line_start:line_end])
    return any(m in line for m in _STORE_CONTEXT_MARKERS)


def _extract_store_url_from_text(text: str) -> str:
    body = str(text or "")
    if not body:
        return ""
    for match in _URL_IN_TEXT_RE.finditer(body):
        raw = match.group(0).strip(" \t\n.,،;:)\"]'>")
        if not raw:
            continue
        if _is_maps_url(raw) or _is_whatsapp_url(raw):
            continue
        if _is_social_url(raw) and not _store_context_near(
            body, match.start(), match.end(),
        ):
            continue
        if not _store_context_near(body, match.start(), match.end()):
            continue
        return _normalise_url(raw)
    return ""


def _lookup_kb_store_url(db: Any, tenant_id: int) -> Tuple[str, str]:
    if db is None or not tenant_id:
        return "", "none"
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.kind.in_(_STORE_KB_KINDS),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(30)
            .all()
        )
        for row in rows:
            body = getattr(row, "body", "") or ""
            url = _extract_store_url_from_text(body)
            if url:
                return url, f"kb_free_text:{row.kind}"
    except Exception as exc:  # noqa: silent-ok — KB scan must not block resolver chain
        logger.debug(
            "store_url_resolver.kb_scan_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return "", "none"


def resolve_store_url(db: Any, tenant_id: int) -> StoreUrlResolution:
    """Resolve canonical online store URL for a tenant."""
    if db is None or not tenant_id:
        return StoreUrlResolution(found=False, reason="missing_db_or_tenant")

    tenant_id = int(tenant_id)

    # 1) Synced merchant profile (StoreKnowledgeSnapshot)
    try:
        from core.store_knowledge import StoreKnowledgeLoader  # noqa: PLC0415

        profile = StoreKnowledgeLoader(db, tenant_id).store_profile() or {}
        url = _normalise_url(profile.get("store_url"))
        if url:
            logger.info(
                "[STORE_URL_RESOLVER] tenant_id=%s source=merchant_profile url_len=%d",
                tenant_id,
                len(url),
            )
            return StoreUrlResolution(
                found=True, url=url, source="merchant_profile",
            )
    except Exception as exc:  # noqa: silent-ok — resolver layer must not block next source
        logger.debug(
            "store_url_resolver.snapshot_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    # 2) Structured tenant settings (store tab + WhatsApp CTA button)
    try:
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE,
            DEFAULT_WHATSAPP,
            get_or_create_settings,
            merge_defaults,
        )

        settings = get_or_create_settings(db, tenant_id)
        store_cfg = merge_defaults(settings.store_settings, DEFAULT_STORE)
        url = _normalise_url(store_cfg.get("store_url"))
        if url:
            logger.info(
                "[STORE_URL_RESOLVER] tenant_id=%s source=structured_settings url_len=%d",
                tenant_id,
                len(url),
            )
            return StoreUrlResolution(
                found=True, url=url, source="structured_settings",
            )

        wa_cfg = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
        url = _normalise_url(wa_cfg.get("store_button_url"))
        if url:
            logger.info(
                "[STORE_URL_RESOLVER] tenant_id=%s source=structured_settings:whatsapp_button url_len=%d",
                tenant_id,
                len(url),
            )
            return StoreUrlResolution(
                found=True,
                url=url,
                source="structured_settings",
                reason="whatsapp_button_url",
            )
    except Exception as exc:  # noqa: silent-ok — resolver layer must not block next source
        logger.debug(
            "store_url_resolver.settings_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    # 3) Platform integrations
    try:
        from models import Integration  # noqa: PLC0415

        for provider in ("salla", "zid", "shopify", "woocommerce"):
            integration = (
                db.query(Integration)
                .filter(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == provider,
                )
                .first()
            )
            if not integration:
                continue
            cfg = integration.config or {}
            url = _normalise_url(
                cfg.get("store_url")
                or cfg.get("storefront_url")
                or cfg.get("domain")
                or cfg.get("shop_domain")
            )
            if url:
                logger.info(
                    "[STORE_URL_RESOLVER] tenant_id=%s source=integration:%s url_len=%d",
                    tenant_id,
                    provider,
                    len(url),
                )
                return StoreUrlResolution(
                    found=True,
                    url=url,
                    source="integration",
                    reason=provider,
                )
    except Exception as exc:  # noqa: silent-ok — resolver layer must not block next source
        logger.debug(
            "store_url_resolver.integration_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    # 4) KB free-text fallback
    kb_url, kb_src = _lookup_kb_store_url(db, tenant_id)
    if kb_url:
        logger.info(
            "[STORE_URL_RESOLVER] tenant_id=%s source=%s url_len=%d",
            tenant_id,
            kb_src,
            len(kb_url),
        )
        return StoreUrlResolution(
            found=True,
            url=kb_url,
            source="kb_free_text",
            reason=kb_src,
        )

    logger.info(
        "[STORE_URL_RESOLVER] tenant_id=%s source=none reason=no_source_configured",
        tenant_id,
    )
    return StoreUrlResolution(found=False, source="none", reason="no_source_configured")


def lookup_tenant_store_url(db: Any, tenant_id: int) -> str:
    """Backward-compatible string API used by safety nets and legacy callers."""
    return resolve_store_url(db, tenant_id).url


__all__ = [
    "StoreUrlResolution",
    "is_online_store_inquiry",
    "lookup_tenant_store_url",
    "resolve_store_url",
]
