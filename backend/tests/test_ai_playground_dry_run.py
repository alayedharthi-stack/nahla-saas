"""
test_ai_playground_dry_run.py
─────────────────────────────
AI Playground dry-run service + endpoint tests.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from commerce_scenario_fixtures import (  # noqa: E402
    make_scenario_db,
    seed_knowledge_section,
    seed_tenant,
)
from models import TenantSettings  # noqa: E402
from core.ai_disabled_gate import REASON_STORE_AI_DISABLED  # noqa: E402
from routers.ai_playground import PlaygroundDryRunBody, playground_dry_run  # noqa: E402
from services.ai_playground_dry_run import (  # noqa: E402
    BLOCKED_BILLING_DENIED,
    OUTBOUND_SESSION_TEXT,
    run_playground_dry_run,
)


def _run(coro):
    return asyncio.run(coro)


def _run_playground(db, tenant_id: int, **kwargs):
    """Run dry-run with billing access enabled unless explicitly testing billing gate."""
    with patch(
        "services.ai_playground_dry_run.has_billing_access",
        return_value=True,
    ):
        return run_playground_dry_run(db, tenant_id=tenant_id, **kwargs)


class TestPlaygroundDryRunService:
    FAQ_MESSAGE = "هل منتجاتكم أصلية؟"
    AVAIL_MESSAGE = "هل السدر متوفر؟"
    SHIPPING_MESSAGE = "كم مدة التوصيل لمكة؟"
    TRACKING_MESSAGE = "أرسل رقم التتبع"
    DELIVERED_MESSAGE = "وصل الطلب"

    def test_faq_dry_run_grounded_no_side_effects(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة، ويمكنك طلب التفاصيل حسب المنتج.",
        )
        result = _run_playground(
            db,
            tenant.id,
            message=self.FAQ_MESSAGE,
        )
        assert result.dry_run is True
        assert result.would_send is True
        assert result.outbound_kind == OUTBOUND_SESSION_TEXT
        assert result.reply_text
        assert "أصلية" in result.reply_text or "مضمونة" in result.reply_text
        assert result.used_llm is False
        assert result.blocked_reason is None
        assert result.side_effects["whatsapp_sent"] is False
        assert result.side_effects["order_created"] is False
        assert result.side_effects["customer_updated"] is False
        assert result.side_effects["automation_triggered"] is False

    def test_availability_dry_run_does_not_invent_stock(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="quick_update",
            title="توفر السدر",
            body="عسل السدر غير متوفر حالياً — سنعلن عند توفر دفعة جديدة.",
        )
        result = _run_playground(
            db,
            tenant.id,
            message=self.AVAIL_MESSAGE,
        )
        assert result.would_send is True
        assert result.reply_text
        assert "غير متوفر" in result.reply_text or "غير" in result.reply_text
        assert "متوفر حالياً" not in result.reply_text.replace("غير متوفر", "")

    def test_store_ai_off_blocked_no_compose(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, store_ai_enabled=False)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة.",
        )
        result = _run_playground(
            db,
            tenant.id,
            message=self.FAQ_MESSAGE,
        )
        assert result.would_send is False
        assert result.blocked_reason == REASON_STORE_AI_DISABLED
        assert result.used_llm is False
        assert result.reply_text is None
        assert all(not v for v in result.side_effects.values())

    def test_billing_denied_blocked(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        with patch(
            "services.ai_playground_dry_run.has_billing_access",
            return_value=False,
        ):
            result = run_playground_dry_run(
                db,
                tenant_id=tenant.id,
                message=self.FAQ_MESSAGE,
            )
        assert result.would_send is False
        assert result.blocked_reason == BLOCKED_BILLING_DENIED
        assert result.reply_text is None

    def test_shipping_inquiry_session_text_no_order(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        settings = db.query(TenantSettings).filter_by(tenant_id=tenant.id).one()
        store = dict(settings.store_settings or {})
        store["shipping_policy"] = "التوصيل 2-4 أيام داخل السعودية"
        settings.store_settings = store
        db.add(settings)
        db.commit()

        result = _run_playground(
            db,
            tenant.id,
            message=self.SHIPPING_MESSAGE,
        )
        assert result.would_send is True
        assert result.outbound_kind == OUTBOUND_SESSION_TEXT
        assert result.reply_text
        assert result.side_effects["order_created"] is False

    def test_tracking_without_context_needs_context_no_invention(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        result = _run_playground(
            db,
            tenant.id,
            message=self.TRACKING_MESSAGE,
        )
        assert result.needs_context is True
        assert result.would_send is False
        assert result.reply_text is None
        assert "TRK" not in (result.reply_text or "")
        assert result.decision_topic == "tracking_link_follow_up"

    def test_tracking_with_context_contains_order_tracking_carrier(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        result = _run_playground(
            db,
            tenant.id,
            message=self.TRACKING_MESSAGE,
            context={
                "order_status": "shipped",
                "order_reference": "NHL-7788",
                "tracking_number": "TRK123456",
                "shipping_provider": "smsa",
            },
        )
        assert result.needs_context is False
        assert result.would_send is True
        assert result.reply_text
        assert "NHL-7788" in result.reply_text
        assert "TRK123456" in result.reply_text
        assert re.search(r"smsa", result.reply_text, re.IGNORECASE)

    def test_delivery_confirmation_no_review_automation(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        with patch("core.automation_emitters.scan_post_delivery_review_requests") as scan:
            result = _run_playground(
                db,
                tenant.id,
                message=self.DELIVERED_MESSAGE,
            )
            scan.assert_not_called()
        assert result.side_effects["order_created"] is False
        assert result.side_effects["automation_triggered"] is False

    def test_no_external_llm_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة.",
        )

        def _boom(*_args, **_kwargs):
            raise AssertionError("MerchantBrain.process must not run in playground")

        monkeypatch.setattr(
            "modules.ai.brain.pipeline.MerchantBrain.process",
            _boom,
        )
        result = _run_playground(
            db,
            tenant.id,
            message=self.FAQ_MESSAGE,
        )
        assert result.reply_text

    def test_no_real_whatsapp_provider_called(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة.",
        )

        async def _boom(*_args, **_kwargs):
            raise AssertionError("real WhatsApp provider must not be called")

        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=_boom,
        ), patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=_boom,
        ):
            result = run_playground_dry_run(
                db,
                tenant_id=tenant.id,
                message=self.FAQ_MESSAGE,
            )
        assert result.side_effects["whatsapp_sent"] is False

    def test_tenant_kb_isolation(self) -> None:
        db, _ = make_scenario_db()
        tenant_a = seed_tenant(db, name="Store A")
        tenant_b = seed_tenant(db, name="Store B")
        seed_knowledge_section(
            db,
            tenant_a.id,
            kind="faq",
            title="أصلية A",
            body="منتجات متجر A أصلية فقط.",
        )
        seed_knowledge_section(
            db,
            tenant_b.id,
            kind="faq",
            title="أصلية B",
            body="منتجات متجر B مضمونة فقط.",
        )
        result_a = _run_playground(
            db,
            tenant_a.id,
            message=self.FAQ_MESSAGE,
        )
        result_b = _run_playground(
            db,
            tenant_b.id,
            message=self.FAQ_MESSAGE,
        )
        assert result_a.reply_text
        assert result_b.reply_text
        assert "متجر A" in result_a.reply_text
        assert "متجر B" in result_b.reply_text
        assert "متجر B" not in result_a.reply_text
        assert "متجر A" not in result_b.reply_text


class TestPlaygroundDryRunEndpoint:
    def test_endpoint_returns_tenant_scoped_preview(self) -> None:
        db, _ = make_scenario_db()
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة.",
        )
        request = MagicMock()
        body = PlaygroundDryRunBody(message="هل منتجاتكم أصلية؟")

        with patch(
            "routers.ai_playground.resolve_tenant_id",
            return_value=tenant.id,
        ), patch(
            "routers.ai_playground.get_or_create_tenant",
            return_value=tenant,
        ), patch(
            "services.ai_playground_dry_run.has_billing_access",
            return_value=True,
        ):
            payload = _run(playground_dry_run(request, body, db))

        assert payload["dry_run"] is True
        assert payload["would_send"] is True
        assert payload["outbound_kind"] == OUTBOUND_SESSION_TEXT
        assert payload["reply_text"]

    def test_endpoint_rejects_other_tenant_via_resolver(self) -> None:
        db, _ = make_scenario_db()
        tenant_a = seed_tenant(db, name="A")
        tenant_b = seed_tenant(db, name="B")
        seed_knowledge_section(
            db,
            tenant_a.id,
            kind="faq",
            title="A only",
            body="حقيقة خاصة بمتجر A.",
        )
        request = MagicMock()
        body = PlaygroundDryRunBody(message="هل منتجاتكم أصلية؟")

        with patch(
            "routers.ai_playground.resolve_tenant_id",
            return_value=tenant_b.id,
        ), patch(
            "routers.ai_playground.get_or_create_tenant",
            return_value=tenant_b,
        ), patch(
            "services.ai_playground_dry_run.has_billing_access",
            return_value=True,
        ):
            payload = _run(playground_dry_run(request, body, db))

        assert "متجر A" not in (payload.get("reply_text") or "")
