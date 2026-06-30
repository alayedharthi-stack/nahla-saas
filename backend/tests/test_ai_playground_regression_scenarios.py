"""
test_ai_playground_regression_scenarios.py
──────────────────────────────────────────
Automated regression scenarios for POST /intelligence/playground/dry-run.

Exercises the HTTP handler (tenant-scoped) without manual merchant testing.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from commerce_scenario_fixtures import (  # noqa: E402
    list_orders,
    make_scenario_db,
    seed_knowledge_section,
    seed_tenant,
)
from core.ai_disabled_gate import REASON_STORE_AI_DISABLED  # noqa: E402
from models import TenantSettings  # noqa: E402
from routers.ai_playground import PlaygroundDryRunBody, playground_dry_run  # noqa: E402
from services.ai_playground_dry_run import OUTBOUND_SESSION_TEXT  # noqa: E402

INTERNAL_KB_MARKERS = (
    "قواعد علينا يجب أن يلتزم بها الذكاء",
    "قاعدة المعرفة الرسمية",
    "تعليمات للنظام",
    "guardrail",
    "system prompt",
)

PHONE_OR_ADDRESS_MARKERS = (
    "رقم الجوال",
    "رقم الهاتف",
    "ما هو رقم",
    "العنوان",
    "google maps",
)


def _run(coro):
    return asyncio.run(coro)


def _format_playground_failure(
    message: str,
    payload: Dict[str, Any],
    *,
    reason: str = "",
) -> str:
    return (
        f"Playground regression failed{': ' + reason if reason else ''}\n"
        f"  message={message!r}\n"
        f"  reply_text={payload.get('reply_text')!r}\n"
        f"  decision_topic={payload.get('decision_topic')!r}\n"
        f"  decision_action={payload.get('decision_action')!r}\n"
        f"  would_send={payload.get('would_send')!r}\n"
        f"  outbound_kind={payload.get('outbound_kind')!r}\n"
        f"  blocked_reason={payload.get('blocked_reason')!r}\n"
        f"  needs_context={payload.get('needs_context')!r}\n"
        f"  needs_better_kb_answer={payload.get('needs_better_kb_answer')!r}\n"
        f"  warnings={payload.get('warnings')!r}\n"
        f"  side_effects={payload.get('side_effects')!r}"
    )


def _assert_playground(
    message: str,
    payload: Dict[str, Any],
    condition: bool,
    *,
    reason: str,
) -> None:
    if not condition:
        pytest.fail(_format_playground_failure(message, payload, reason=reason))


def _assert_no_side_effects(message: str, payload: Dict[str, Any]) -> None:
    effects = dict(payload.get("side_effects") or {})
    _assert_playground(
        message,
        payload,
        effects.get("whatsapp_sent") is False,
        reason="whatsapp_sent must stay false",
    )
    _assert_playground(
        message,
        payload,
        effects.get("order_created") is False,
        reason="order_created must stay false",
    )
    _assert_playground(
        message,
        payload,
        effects.get("customer_updated") is False,
        reason="customer_updated must stay false",
    )
    _assert_playground(
        message,
        payload,
        effects.get("automation_triggered") is False,
        reason="automation_triggered must stay false",
    )


def _assert_no_internal_kb_in_reply(message: str, payload: Dict[str, Any]) -> None:
    reply = str(payload.get("reply_text") or "")
    for marker in INTERNAL_KB_MARKERS:
        _assert_playground(
            message,
            payload,
            marker not in reply,
            reason=f"reply must not leak internal KB marker: {marker!r}",
        )
    _assert_playground(
        message,
        payload,
        not re.search(r"^#+\s", reply, flags=re.MULTILINE),
        reason="reply must not contain markdown KB headings",
    )


def _assert_no_phone_or_address_request(message: str, payload: Dict[str, Any]) -> None:
    reply = str(payload.get("reply_text") or "").lower()
    for marker in PHONE_OR_ADDRESS_MARKERS:
        if marker == "رابط" and ("تتبع" in reply or "trk" in reply):
            continue
        _assert_playground(
            message,
            payload,
            marker.lower() not in reply,
            reason=f"reply must not request phone/address: {marker!r}",
        )


def _call_playground_endpoint(
    db,
    tenant_id: int,
    *,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    request = MagicMock()
    body = PlaygroundDryRunBody(message=message)
    if context is not None:
        body = PlaygroundDryRunBody(message=message, context=context)

    with patch(
        "routers.ai_playground.resolve_tenant_id",
        return_value=tenant_id,
    ), patch(
        "routers.ai_playground.get_or_create_tenant",
        return_value=MagicMock(id=tenant_id),
    ), patch(
        "services.ai_playground_dry_run.has_billing_access",
        return_value=True,
    ):
        return _run(playground_dry_run(request, body, db))


@pytest.fixture
def regression_db():
    db, _ = make_scenario_db()
    return db


class TestPlaygroundRegressionScenarios:
    FAQ_MESSAGE = "هل منتجاتكم أصلية؟"
    AVAIL_MESSAGE = "هل السدر متوفر؟"
    SHIPPING_MESSAGE = "كم مدة التوصيل لمكة؟"
    TRACKING_MESSAGE = "أرسل رقم التتبع"
    DELIVERED_MESSAGE = "وصل الطلب"
    INTERNAL_HEADING = "قواعد علينا يجب أن يلتزم بها الذكاء"

    TRACKING_CONTEXT = {
        "order_status": "shipped",
        "order_reference": "NHL-7788",
        "tracking_number": "TRK123456",
        "shipping_provider": "smsa",
    }

    def test_regression_faq_authenticity(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة، ويمكنك طلب التفاصيل حسب المنتج.",
        )
        orders_before = len(list_orders(db, tenant.id))
        payload = _call_playground_endpoint(db, tenant.id, message=self.FAQ_MESSAGE)

        _assert_playground(
            self.FAQ_MESSAGE,
            payload,
            payload.get("dry_run") is True,
            reason="dry_run flag",
        )
        _assert_playground(
            self.FAQ_MESSAGE,
            payload,
            payload.get("would_send") is True,
            reason="store AI on should preview a send",
        )
        _assert_playground(
            self.FAQ_MESSAGE,
            payload,
            payload.get("outbound_kind") == OUTBOUND_SESSION_TEXT,
            reason="FAQ inbound preview is session text",
        )
        _assert_no_internal_kb_in_reply(self.FAQ_MESSAGE, payload)
        _assert_no_phone_or_address_request(self.FAQ_MESSAGE, payload)
        _assert_no_side_effects(self.FAQ_MESSAGE, payload)
        _assert_playground(
            self.FAQ_MESSAGE,
            payload,
            len(list_orders(db, tenant.id)) == orders_before,
            reason="FAQ must not create orders",
        )

    def test_regression_availability_no_invented_stock(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="quick_update",
            title="توفر السدر",
            body="عسل السدر غير متوفر حالياً — سنعلن عند توفر دفعة جديدة.",
        )
        orders_before = len(list_orders(db, tenant.id))
        payload = _call_playground_endpoint(db, tenant.id, message=self.AVAIL_MESSAGE)

        _assert_no_side_effects(self.AVAIL_MESSAGE, payload)
        _assert_no_phone_or_address_request(self.AVAIL_MESSAGE, payload)
        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            len(list_orders(db, tenant.id)) == orders_before,
            reason="availability inquiry must not create orders",
        )
        reply = str(payload.get("reply_text") or "")
        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            bool(reply),
            reason="availability KB should produce a preview",
        )
        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            "غير متوفر" in reply or "غير" in reply,
            reason="reply should reflect unavailable KB fact",
        )
        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            "متوفر حالياً" not in reply.replace("غير متوفر", ""),
            reason="reply must not invent availability",
        )

    def test_regression_shipping_inquiry_no_order_or_tracking(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        settings = db.query(TenantSettings).filter_by(tenant_id=tenant.id).one()
        store = dict(settings.store_settings or {})
        store["shipping_policy"] = "التوصيل 2-4 أيام داخل السعودية"
        settings.store_settings = store
        db.add(settings)
        db.commit()

        orders_before = len(list_orders(db, tenant.id))
        payload = _call_playground_endpoint(db, tenant.id, message=self.SHIPPING_MESSAGE)

        _assert_no_side_effects(self.SHIPPING_MESSAGE, payload)
        _assert_playground(
            self.SHIPPING_MESSAGE,
            payload,
            len(list_orders(db, tenant.id)) == orders_before,
            reason="shipping inquiry must not create orders",
        )
        _assert_playground(
            self.SHIPPING_MESSAGE,
            payload,
            payload.get("decision_topic") != "tracking_link_follow_up",
            reason="shipping must not route to tracking follow-up",
        )
        _assert_playground(
            self.SHIPPING_MESSAGE,
            payload,
            payload.get("would_send") is True,
            reason="shipping policy preview should be sendable",
        )
        _assert_playground(
            self.SHIPPING_MESSAGE,
            payload,
            payload.get("outbound_kind") == OUTBOUND_SESSION_TEXT,
            reason="shipping preview is session text",
        )

    def test_regression_tracking_without_context_no_invention(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        payload = _call_playground_endpoint(db, tenant.id, message=self.TRACKING_MESSAGE)

        _assert_no_side_effects(self.TRACKING_MESSAGE, payload)
        _assert_playground(
            self.TRACKING_MESSAGE,
            payload,
            payload.get("needs_context") is True
            or payload.get("would_send") is False,
            reason="tracking without context must not pretend to send",
        )
        reply = str(payload.get("reply_text") or "")
        _assert_playground(
            self.TRACKING_MESSAGE,
            payload,
            "TRK" not in reply and "NHL-" not in reply,
            reason="must not invent tracking or order reference",
        )
        _assert_playground(
            self.TRACKING_MESSAGE,
            payload,
            bool(payload.get("warnings")),
            reason="tracking without context should warn",
        )

    def test_regression_tracking_with_context_shows_order_and_carrier(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        payload = _call_playground_endpoint(
            db,
            tenant.id,
            message=self.TRACKING_MESSAGE,
            context=self.TRACKING_CONTEXT,
        )

        _assert_no_side_effects(self.TRACKING_MESSAGE, payload)
        reply = str(payload.get("reply_text") or "")
        _assert_playground(
            self.TRACKING_MESSAGE,
            payload,
            "NHL-7788" in reply,
            reason="tracking preview must include order reference",
        )
        _assert_playground(
            self.TRACKING_MESSAGE,
            payload,
            "TRK123456" in reply,
            reason="tracking preview must include tracking number",
        )
        _assert_playground(
            self.TRACKING_MESSAGE,
            payload,
            re.search(r"smsa", reply, re.IGNORECASE) is not None,
            reason="tracking preview must include carrier",
        )

    def test_regression_delivery_confirmation_no_review_automation(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        orders_before = len(list_orders(db, tenant.id))
        with patch("core.automation_emitters.scan_post_delivery_review_requests") as scan:
            payload = _call_playground_endpoint(db, tenant.id, message=self.DELIVERED_MESSAGE)
            scan.assert_not_called()

        _assert_no_side_effects(self.DELIVERED_MESSAGE, payload)
        _assert_playground(
            self.DELIVERED_MESSAGE,
            payload,
            len(list_orders(db, tenant.id)) == orders_before,
            reason="delivery confirmation must not create orders",
        )

    def test_regression_store_ai_off_blocks_reply(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db, store_ai_enabled=False)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="quick_update",
            title="توفر السدر",
            body="عسل السدر غير متوفر حالياً.",
        )
        payload = _call_playground_endpoint(db, tenant.id, message=self.AVAIL_MESSAGE)

        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            payload.get("would_send") is False,
            reason="store AI off must not send",
        )
        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            payload.get("blocked_reason") == REASON_STORE_AI_DISABLED,
            reason="blocked_reason must be store_ai_disabled",
        )
        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            payload.get("used_llm") is False,
            reason="store AI off must not invoke LLM",
        )
        _assert_playground(
            self.AVAIL_MESSAGE,
            payload,
            not payload.get("reply_text"),
            reason="store AI off must not produce reply_text",
        )
        _assert_no_side_effects(self.AVAIL_MESSAGE, payload)

    def test_regression_internal_kb_not_leaked_to_customer_preview(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="custom",
            title="قاعدة المعرفة الرسمية",
            body=(
                "# قاعدة المعرفة الرسمية — متجر تجريبي\n"
                f"## {self.INTERNAL_HEADING}\n"
                "- لا تخترع أسعارًا."
            ),
        )
        payload = _call_playground_endpoint(db, tenant.id, message=self.FAQ_MESSAGE)

        _assert_no_internal_kb_in_reply(self.FAQ_MESSAGE, payload)
        _assert_playground(
            self.FAQ_MESSAGE,
            payload,
            payload.get("would_send") is False,
            reason="internal-only KB must not preview as customer send",
        )
        _assert_playground(
            self.FAQ_MESSAGE,
            payload,
            payload.get("needs_better_kb_answer") is True,
            reason="internal-only KB should flag needs_better_kb_answer",
        )
        _assert_playground(
            self.FAQ_MESSAGE,
            payload,
            any("صالحة للعرض للعميل" in w for w in (payload.get("warnings") or [])),
            reason="internal-only KB should warn merchant",
        )
        _assert_no_side_effects(self.FAQ_MESSAGE, payload)

    def test_regression_no_real_whatsapp_provider_across_endpoint(self, regression_db) -> None:
        db = regression_db
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة.",
        )

        async def _boom(*_args, **_kwargs):
            raise AssertionError("real WhatsApp provider must not be called in playground regression")

        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=_boom,
        ), patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=_boom,
        ):
            payload = _call_playground_endpoint(db, tenant.id, message=self.FAQ_MESSAGE)

        _assert_no_side_effects(self.FAQ_MESSAGE, payload)
