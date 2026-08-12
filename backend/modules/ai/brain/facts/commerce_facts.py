"""
brain/facts/commerce_facts.py
──────────────────────────────
DefaultFactsLoader — Phase 2 enriched version.

Loads a CommerceFacts snapshot for a single decision turn.

Phase 1 scalars (cheap, always loaded):
  has_products, product_count, has_active_integration, has_coupons,
  store_name, store_url, snapshot_fresh

Phase 2 additions (slightly more work but still < 5ms):
  in_stock_count     — products with in_stock=True
  orderable          — integration active + in_stock > 0
  coupon_eligibility — best active coupon code (first match)
  top_products       — top 5 in-stock products (id, external_id, title, price)
  integration_platform — "salla" | "zid" | "manual" | "unknown"
  within_working_hours — None when no store_hours configured
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from core.catalog import apply_active_catalog_query_filters
from core.store_display import clean_store_name

from ..types import CommerceFacts

logger = logging.getLogger("nahla.brain.facts_loader")


class DefaultFactsLoader:
    """Implements FactsLoader protocol."""

    def load(self, db: Any, tenant_id: int) -> CommerceFacts:
        from models import (  # noqa: PLC0415
            Coupon,
            Integration,
            Product,
            StoreKnowledgeSnapshot,
            TenantSettings,
        )
        from sqlalchemy import func

        facts = CommerceFacts()

        # ── 1. Integration ────────────────────────────────────────────────
        integration = (
            db.query(Integration)
            .filter(Integration.tenant_id == tenant_id)
            .first()
        )
        integration_cfg = (integration.config or {}) if integration else {}
        facts.has_active_integration = bool(
            integration
            and integration.enabled
            and (integration_cfg.get("api_key") or integration_cfg.get("access_token"))
        )
        if integration:
            platform = integration_cfg.get("platform", "")
            if not platform:
                # Infer from integration type
                platform = getattr(integration, "provider", "") or "unknown"
            facts.integration_platform = platform or "unknown"

        # ── 2. Products ───────────────────────────────────────────────────
        product_count = (
            db.query(func.count(Product.id))
            .filter(Product.tenant_id == tenant_id)
            .scalar()
        ) or 0
        facts.product_count = product_count
        facts.has_products  = product_count > 0

        # In-stock count — only count synced products (external_id required).
        # Unsynced products cannot be ordered through Salla, so they must not
        # inflate `orderable` or appear in top_products shown to customers.
        in_stock_count = (
            apply_active_catalog_query_filters(
                db.query(func.count(Product.id)).filter(
                    Product.tenant_id == tenant_id,
                    Product.external_id.isnot(None),
                    Product.external_id != "",
                ),
                Product,
            )
            .scalar()
        ) or 0
        facts.in_stock_count = in_stock_count
        facts.orderable = facts.has_active_integration and in_stock_count > 0

        # Top 5 in-stock products for greeting / discovery (Phase 2).
        # MUST include external_id so any numeric pick from this list can
        # be resolved to a real Salla product. Products without external_id
        # are unsynced and must never appear in customer-facing lists.
        top_rows = (
            apply_active_catalog_query_filters(
                db.query(Product).filter(
                    Product.tenant_id == tenant_id,
                    Product.external_id.isnot(None),
                    Product.external_id != "",
                ),
                Product,
            )
            .order_by(Product.id)
            .limit(5)
            .all()
        )
        facts.top_products = [
            {
                "id":          p.id,
                "external_id": p.external_id,
                "title":       p.title,
                "price":       p.price,
                "sku":         p.sku,
            }
            for p in top_rows
        ]

        # ── 3. Coupons ────────────────────────────────────────────────────
        now = datetime.now(timezone.utc)

        active_coupons = (
            db.query(Coupon)
            .filter(
                Coupon.tenant_id == tenant_id,
                (Coupon.expires_at == None) | (Coupon.expires_at > now),  # noqa: E711
            )
            .limit(5)
            .all()
        )
        facts.has_coupons = bool(active_coupons)

        # Best coupon eligibility (Phase 2): pick the first valid coupon code
        for c in active_coupons:
            code = getattr(c, "code", "") or ""
            if code:
                facts.coupon_eligibility = str(code)
                break

        # ── 4. Store metadata ─────────────────────────────────────────────
        snapshot = (
            db.query(StoreKnowledgeSnapshot)
            .filter(StoreKnowledgeSnapshot.tenant_id == tenant_id)
            .first()
        )
        if snapshot:
            facts.snapshot_fresh = True
            profile = snapshot.store_profile or {}
            shipping = snapshot.shipping_summary or {}
            policy = snapshot.policy_summary or {}

            facts.store_name = clean_store_name(
                profile.get("store_name", "") or "",
            )
            facts.store_url = profile.get("store_url", "") or ""
            # ``maps_url`` mirrors ``store_settings.google_maps_location``
            # via _rebuild_snapshot. Empty string when no maps URL has
            # been configured anywhere — the FAQ template handles
            # the empty case honestly. See May 2026 #36.
            facts.maps_url = profile.get("maps_url", "") or ""
            facts.store_description = profile.get("description", "") or ""
            facts.store_contact_phone = profile.get("contact_phone", "") or ""
            facts.store_contact_email = profile.get("contact_email", "") or ""

            raw_shipping_methods = shipping.get("methods", []) or []
            if isinstance(raw_shipping_methods, list):
                facts.shipping_methods = list(raw_shipping_methods)
            elif raw_shipping_methods:
                facts.shipping_methods = [str(raw_shipping_methods)]
            facts.shipping_notes = shipping.get("notes", "") or ""
            facts.shipping_policy = policy.get("shipping_policy", "") or ""
            facts.support_hours = policy.get("support_hours", "") or ""
            raw_payment_methods = policy.get("payment_methods", []) or []
            if isinstance(raw_payment_methods, list):
                facts.payment_methods = list(raw_payment_methods)
            elif raw_payment_methods:
                facts.payment_methods = [str(raw_payment_methods)]

        # ── 4b. Salla MERCHANT_ENABLED capabilities (Pack B) ──────────────
        # Separate surface from Nahla-native payment flags / dashboard lore.
        # When Salla status is known/empty, it owns storefront method lists.
        # When UNKNOWN/FORBIDDEN, do not invent COD or keep stale dashboard
        # lists as if they were Salla merchant-enabled truth.
        try:
            from core.salla_merchant_capabilities import (  # noqa: PLC0415
                STATUS_EMPTY,
                STATUS_KNOWN,
                load_checkout_profile_for_tenant,
                payment_codes,
                project_merchant_capabilities,
                shipping_company_names,
            )

            checkout_profile = load_checkout_profile_for_tenant(db, tenant_id)
            if checkout_profile:
                projection = project_merchant_capabilities(checkout_profile)
                facts.salla_payments_status = projection.payments_status
                facts.salla_shipping_companies_status = (
                    projection.shipping_companies_status
                )
                if projection.payments_status in (STATUS_KNOWN, STATUS_EMPTY):
                    facts.payment_methods = payment_codes(checkout_profile)
                    facts.payment_methods_source = "salla_merchant_enabled"
                elif facts.integration_platform == "salla":
                    # Fail closed for Salla storefront payment questions.
                    facts.payment_methods = []
                    facts.payment_methods_source = "salla_unknown"
                if projection.shipping_companies_status in (
                    STATUS_KNOWN,
                    STATUS_EMPTY,
                ):
                    facts.shipping_methods = shipping_company_names(
                        checkout_profile,
                    )
                    facts.shipping_methods_source = "salla_merchant_enabled"
                elif facts.integration_platform == "salla":
                    facts.shipping_methods = []
                    facts.shipping_methods_source = "salla_unknown"
                facts.merchant_capabilities = projection.to_dict()
        except Exception:  # noqa: silent-ok — Pack B facts must not break turns
            pass

        # ── 4c. Pack A2 structured merchant profile projection ────────────
        # Overlay namespaced Salla / manual profile with documented precedence.
        # Public phone only — never WhatsApp owner number.
        try:
            from core.merchant_profile import (  # noqa: PLC0415
                apply_resolved_profile_to_commerce_facts,
                resolve_merchant_profile,
            )

            profile = resolve_merchant_profile(db, tenant_id)
            apply_resolved_profile_to_commerce_facts(facts, profile)
        except Exception:  # noqa: silent-ok — profile overlay must not break turns
            pass

        # ── 4d. Pack A3 MERCHANT_POLICY existence + store_story presence ───
        # Existence only (KNOWN_PRESENT / UNKNOWN + doc_ref). Bodies stay on
        # capped MKS retrieval — never flatten prose into always-on facts.
        try:
            from services.merchant_policy_existence import (  # noqa: PLC0415
                build_policy_existence_map,
            )

            policy_map = build_policy_existence_map(db, tenant_id)
            cleaned: Dict[str, Any] = {}
            for kind, payload in (policy_map or {}).items():
                status = str((payload or {}).get("status") or "UNKNOWN")
                if status == "KNOWN_ABSENT":
                    status = "UNKNOWN"
                if status not in {"KNOWN_PRESENT", "UNKNOWN"}:
                    status = "UNKNOWN"
                row: Dict[str, Any] = {"status": status}
                doc_ref = (payload or {}).get("doc_ref")
                if doc_ref and status == "KNOWN_PRESENT":
                    row["doc_ref"] = str(doc_ref)
                cleaned[str(kind)] = row
            facts.merchant_policy = cleaned
        except Exception:  # noqa: silent-ok — policy map must not break turns
            pass
        try:
            from models import MerchantKnowledgeSection  # noqa: PLC0415
            from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

            story_row = (
                apply_ai_visible_kb_query_filters(
                    db.query(MerchantKnowledgeSection)
                )
                .filter(
                    MerchantKnowledgeSection.tenant_id == int(tenant_id),
                    MerchantKnowledgeSection.kind == "store_story",
                )
                .order_by(MerchantKnowledgeSection.updated_at.desc())
                .first()
            )
            if story_row is not None and str(getattr(story_row, "body", "") or "").strip():
                facts.store_story_status = "KNOWN_PRESENT"
                facts.store_story_doc_ref = f"mks:{getattr(story_row, 'id', None)}"
            else:
                facts.store_story_status = "UNKNOWN"
                facts.store_story_doc_ref = ""
        except Exception:  # noqa: silent-ok — story presence must not break turns
            pass

        # ── 5. Working hours + assistant persona (Phase 2) ────────────────
        # Both pulled from the same TenantSettings row so we make a single
        # query. Failures are non-fatal — neither field is critical to a
        # decision turn.
        try:
            settings = (
                db.query(TenantSettings)
                .filter(TenantSettings.tenant_id == tenant_id)
                .first()
            )
            if settings:
                ai_settings = settings.ai_settings or {}
                store_hours = ai_settings.get("store_hours")
                if store_hours:
                    facts.within_working_hours = _check_working_hours(store_hours)
                # Merchant-configured assistant name. Default lives in
                # core.tenant.DEFAULT_AI ("نحلة"); we only override when
                # the merchant explicitly set a non-empty value.
                assistant_name = str(ai_settings.get("assistant_name") or "").strip()
                if assistant_name:
                    facts.assistant_name = assistant_name
                # Fallback maps URL when the snapshot didn't carry it.
                # Tenants on the native Nahla shop (no integration) may
                # not have a snapshot yet, but they DO populate
                # ``store_settings.google_maps_location`` from the
                # dashboard. We bridge that gap here so the maps
                # resolver can still find a URL on those tenants.
                if not facts.maps_url:
                    store_cfg = settings.store_settings or {}
                    maps_url = str(store_cfg.get("google_maps_location") or "").strip()
                    if maps_url:
                        facts.maps_url = maps_url
        except Exception:
            pass   # working hours / persona are optional — never block a turn

        # ── 5b. Unified store URL resolver (compose + safety-net parity) ─
        try:
            from modules.ai.brain.commerce.store_inquiry_compose_guard import (  # noqa: PLC0415
                apply_store_url_to_facts,
            )

            apply_store_url_to_facts(facts, db, tenant_id)
        except Exception:  # noqa: silent-ok — unified store URL probe must not break facts loader
            pass

        # ── 6. KB free-text fallback for maps_url (May 2026 #38) ────────
        # Parity with :func:`modules.ai.postprocess.safety_nets.
        # _lookup_tenant_maps_url`. Pre-fix the LLM-facing facts only
        # had layers 1 (snapshot) + 2 (store_settings); the safety net
        # had a 3rd layer (KB free-text scan in ``branches`` /
        # ``store_story`` / ``custom`` sections). When a merchant's
        # maps URL lives only in a KB section, the LLM template
        # emitted the awkward "أخبرنا بالفرع" fallback while the
        # safety net later injected the actual URL — the customer
        # then saw both, in sequence, contradicting each other. By
        # mirroring the KB layer here, the template renders the
        # canonical "موقعنا 📍\n{url}" reply on the first pass and
        # the safety net stays out of the way (its
        # ``url_already_in_reply`` short-circuit fires). Failures
        # are non-fatal — degrade to whatever the upper layers
        # already populated.
        if not facts.maps_url:
            try:
                from modules.ai.postprocess.safety_nets import (  # noqa: PLC0415
                    _lookup_tenant_maps_url,
                )
                kb_maps_url, _kb_src = _lookup_tenant_maps_url(db, tenant_id)
                if kb_maps_url:
                    facts.maps_url = kb_maps_url
            except Exception:  # noqa: silent-ok — KB scan errors must not break facts loader
                pass

        logger.debug(
            "[FactsLoader] tenant=%s products=%d (in_stock=%d) orderable=%s coupons=%s platform=%s",
            tenant_id,
            facts.product_count,
            facts.in_stock_count,
            facts.orderable,
            facts.has_coupons,
            facts.integration_platform,
        )
        return facts


def _check_working_hours(store_hours: dict) -> bool:
    """
    Returns True if current UTC time falls inside any configured window.

    store_hours format expected in ai_settings:
      {
        "timezone": "Asia/Riyadh",
        "windows": [
          {"day": 0, "open": "09:00", "close": "22:00"},
          ...
        ]
      }
    Day 0 = Monday, 6 = Sunday (ISO weekday - 1).
    """
    try:
        import zoneinfo
        tz_name = store_hours.get("timezone", "Asia/Riyadh")
        tz = zoneinfo.ZoneInfo(tz_name)
        local_now = datetime.now(tz)
        day_idx = local_now.weekday()   # 0=Mon, 6=Sun
        time_str = local_now.strftime("%H:%M")

        for window in store_hours.get("windows", []):
            if window.get("day") == day_idx:
                if window.get("open", "00:00") <= time_str <= window.get("close", "23:59"):
                    return True
        return False
    except Exception:
        return True   # assume open on any error
