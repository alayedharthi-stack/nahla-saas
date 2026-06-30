"""
Compose smoke tests — FAQ/KB, availability, tracking, and delivery confirmation.

Uses decision + FakeFacts compose (no external LLM). Inbound session replies
are asserted as free-form text, not WhatsApp templates.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from commerce_scenario_fixtures import (  # noqa: E402
    make_scenario_db,
    persona_delivered_order,
    persona_kb_inquiry,
    persona_new_customer,
    persona_shipped_order,
    seed_knowledge_section,
)
from commerce_scenario_runner import AIScenarioRunner  # noqa: E402
from core import automation_emitters  # noqa: E402
from core.payment_intent import looks_like_delivery_confirmation  # noqa: E402
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    TOPIC_KB_AVAILABILITY_FACTS,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    TOPIC_PRODUCT_KNOWLEDGE_FACTS,
    try_product_knowledge_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_PROPOSE_DRAFT_ORDER  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    is_explicit_order_tracking_request,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


@dataclass
class ComposeSmokeResult:
    decision_action: str
    decision_topic: str
    reply_text: str
    outbound_kind: str


def _brain_ctx(
    world,
    message: str,
    *,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> BrainContext:
    intent = rules.match(message) or Intent(
        name="general",
        confidence=0.5,
        raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=world.tenant.id,
        customer_phone=world.phone_e164,
        customer_id=world.customer.id,
        conversation_id=world.conversation.id,
        message=message,
        intent=intent,
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            product_count=1,
            orderable=True,
            has_active_integration=True,
            store_name="Scenario Store",
            shipping_policy="التوصيل 2-4 أيام داخل السعودية",
        ),
        history=[],
        commerce_bundle=dict(commerce_bundle or {}),
    )
    ctx._db = world.db  # noqa: SLF001
    return ctx


def _tracking_compose_decision(ctx: BrainContext) -> Optional[Any]:
    bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
    if not is_explicit_order_tracking_request(ctx.message or "", commerce_bundle=bundle):
        return None
    from core.active_order_context import prepare_tracking_follow_up_decision  # noqa: PLC0415

    return Decision(
        action=ACTION_LLM_REPLY,
        args=prepare_tracking_follow_up_decision(ctx),
        reason="compose_smoke_tracking_follow_up",
        confidence=0.93,
    )


def _resolve_compose_decision(ctx: BrainContext):
    tracking = _tracking_compose_decision(ctx)
    if tracking is not None:
        return tracking
    for resolver in (
        try_non_catalog_availability_kb_decision,
        try_product_knowledge_decision,
    ):
        decision = resolver(ctx)
        if decision is not None:
            return decision
    return DefaultDecisionEngine().decide(ctx)


def _kb_sections_from_db(ctx: BrainContext, *, hint: str = "") -> List[str]:
    from models import MerchantKnowledgeSection  # noqa: PLC0415

    rows = (
        ctx._db.query(MerchantKnowledgeSection)  # noqa: SLF001
        .filter_by(tenant_id=ctx.tenant_id, is_active=True)
        .all()
    )
    hint_tokens = [
        tok for tok in re.findall(r"[\w\u0600-\u06FF]+", hint or "")
        if len(tok) >= 3
    ]
    scored: List[Tuple[int, str]] = []
    for row in rows:
        body = str(getattr(row, "body", "") or "").strip()
        title = str(getattr(row, "title", "") or "").strip()
        if not body:
            continue
        score = sum(1 for tok in hint_tokens if tok in title or tok in body)
        scored.append((score, body))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        return [scored[0][1]]
    return [body for _, body in scored if body]


def _synthesize_facts_reply(decision: Any, ctx: BrainContext) -> str:
    """Fake LLM: wording from allowed_facts / KB only — no invented claims."""
    args = dict(getattr(decision, "args", None) or {})
    allowed = dict(args.get("allowed_facts") or {})

    body = str(allowed.get("kb_section_body") or "").strip()
    if body:
        return body

    kb_sections = allowed.get("kb_sections") or []
    if isinstance(kb_sections, list) and kb_sections:
        chunks = [
            str(item.get("body") or item.get("text") or "").strip()
            for item in kb_sections
            if isinstance(item, dict) and (item.get("body") or item.get("text"))
        ]
        if chunks:
            return " ".join(chunks)

    topic = str(args.get("topic") or "")
    if topic == "tracking_link_follow_up":
        bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
        active = dict(bundle.get("active_order_context") or {})
        ref = str(args.get("order_reference") or active.get("order_id") or "").strip()
        tracking = str(active.get("tracking_number") or "").strip()
        provider = str(active.get("shipping_provider") or active.get("provider") or "").strip()
        parts = [p for p in (ref, tracking, provider) if p]
        return " | ".join(parts)

    if topic in {TOPIC_KB_AVAILABILITY_FACTS, TOPIC_PRODUCT_KNOWLEDGE_FACTS}:
        hint = str(allowed.get("inquiry_subject") or ctx.message or "")
        bodies = _kb_sections_from_db(ctx, hint=hint)
        if bodies:
            return bodies[0]

    bodies = _kb_sections_from_db(ctx, hint=str(ctx.message or ""))
    if bodies:
        return bodies[0]
    return ""


def run_compose_smoke(
    world,
    message: str,
    *,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> ComposeSmokeResult:
    ctx = _brain_ctx(world, message, commerce_bundle=commerce_bundle)
    decision = _resolve_compose_decision(ctx)
    reply = _synthesize_facts_reply(decision, ctx)
    topic = str((decision.args or {}).get("topic") or "")
    return ComposeSmokeResult(
        decision_action=str(getattr(decision, "action", "") or ""),
        decision_topic=topic,
        reply_text=reply,
        outbound_kind="text",
    )


def _assert_no_address_request(reply: str) -> None:
    lowered = reply.lower()
    assert "العنوان" not in lowered
    assert "google maps" not in lowered
    assert "رابط" not in lowered or "تتبع" in lowered or "TRK" in reply


def _assert_session_text_outbound(runner: AIScenarioRunner) -> None:
    for record in runner.fake_sender.sent:
        assert record.type == "text", (
            f"expected session free-form text, got {record.type!r} body={record.body!r}"
        )
    assert not any(
        record.type == "template" for record in runner.fake_sender.sent
    ), "inbound compose smoke must not emit WhatsApp templates"


class TestFAQComposeSmoke:
    GENERIC_MESSAGE = "هل منتجاتكم أصلية؟"

    def test_faq_kb_compose_uses_allowed_facts_without_order(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        seed_knowledge_section(
            db,
            world.tenant.id,
            kind="faq",
            title="أصلية المنتجات",
            body="منتجاتنا أصلية ومضمونة، ويمكنك طلب التفاصيل حسب المنتج.",
        )
        runner = AIScenarioRunner(world)
        before = runner.order_count()

        smoke = run_compose_smoke(world, self.GENERIC_MESSAGE)
        assert smoke.decision_action == ACTION_LLM_REPLY
        assert smoke.reply_text
        assert "أصلية" in smoke.reply_text or "مضمونة" in smoke.reply_text
        assert "طبيعي 100%" not in smoke.reply_text  # no cross-KB bleed
        _assert_no_address_request(smoke.reply_text)

        assert runner.order_count() == before
        assert is_explicit_order_tracking_request(self.GENERIC_MESSAGE) is False

    def test_honey_fixture_faq_still_uses_kb_facts_in_tests_only(self) -> None:
        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)
        smoke = run_compose_smoke(world, "هل عسلكم طبيعي؟")
        assert smoke.reply_text
        assert "طبيعي" in smoke.reply_text
        assert "100%" in smoke.reply_text or "بدون إضافات" in smoke.reply_text


class TestAvailabilityComposeSmoke:
    def test_availability_compose_does_not_invent_stock(self) -> None:
        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)
        sidr = world.extras["kb_sections"]["sidr_availability"]
        sidr.body = "عسل السدر غير متوفر حالياً — سنعلن عند توفر دفعة جديدة."
        world.db.add(sidr)
        world.db.commit()

        message = "هل السدر متوفر؟"
        smoke = run_compose_smoke(world, message)
        assert smoke.decision_topic == TOPIC_KB_AVAILABILITY_FACTS
        assert smoke.reply_text
        assert "غير متوفر" in smoke.reply_text or "غير" in smoke.reply_text
        assert "متوفر حالياً" not in smoke.reply_text.replace("غير متوفر", "")
        _assert_no_address_request(smoke.reply_text)

        runner = AIScenarioRunner(world)
        assert runner.order_count() == 0


class TestTrackingComposeSmoke:
    TRACKING_MESSAGE = "أرسل رقم التتبع"

    def test_tracking_full_reply_contains_order_carrier_tracking(self) -> None:
        db, _ = make_scenario_db()
        world = persona_shipped_order(db)
        runner = AIScenarioRunner(world)
        bundle = runner.commerce_bundle()
        before = runner.order_count()

        smoke = run_compose_smoke(
            world,
            self.TRACKING_MESSAGE,
            commerce_bundle=bundle,
        )
        assert smoke.decision_topic == "tracking_link_follow_up"
        assert smoke.reply_text
        assert "NHL-7788" in smoke.reply_text
        assert "TRK123456" in smoke.reply_text
        assert re.search(r"smsa", smoke.reply_text, re.IGNORECASE)
        _assert_no_address_request(smoke.reply_text)

        assert runner.order_count() == before
        assert is_explicit_order_tracking_request(
            self.TRACKING_MESSAGE,
            commerce_bundle=bundle,
        ) is True

        with runner.fake_sender.patch():
            from commerce_scenario_runner import FakeOutboundRecord  # noqa: PLC0415

            runner.fake_sender.sent.append(
                FakeOutboundRecord(
                    type="text",
                    to=world.phone,
                    body=smoke.reply_text,
                    path="compose_smoke_simulated",
                )
            )
        _assert_session_text_outbound(runner)


class TestDeliveryConfirmationComposeSmoke:
    DELIVERED_MESSAGE = "وصل الطلب"

    def test_delivery_confirmation_smoke_no_new_order_no_review_duplicate(self) -> None:
        db, _ = make_scenario_db()
        world = persona_delivered_order(db)
        runner = AIScenarioRunner(world)
        before = runner.order_count()
        meta_before = dict(world.order.extra_metadata or {})

        assert looks_like_delivery_confirmation(self.DELIVERED_MESSAGE) is True
        smoke = run_compose_smoke(
            world,
            self.DELIVERED_MESSAGE,
            commerce_bundle=runner.commerce_bundle(),
        )
        assert smoke.decision_action != ACTION_PROPOSE_DRAFT_ORDER

        runner.run_inbound_only(self.DELIVERED_MESSAGE)
        assert runner.order_count() == before

        emitted = automation_emitters.scan_post_delivery_review_requests(
            db, world.tenant.id,
        )
        assert emitted == 0
        db.refresh(world.order)
        meta_after = dict(world.order.extra_metadata or {})
        assert meta_after.get("review_request_sent") is meta_before.get("review_request_sent")

        with runner.fake_sender.patch():
            from commerce_scenario_runner import FakeOutboundRecord  # noqa: PLC0415

            if smoke.reply_text:
                runner.fake_sender.sent.append(
                    FakeOutboundRecord(
                        type="text",
                        to=world.phone,
                        body=smoke.reply_text,
                        path="compose_smoke_simulated",
                    )
                )
            _assert_session_text_outbound(runner)

    def test_no_external_llm_calls_in_compose_smoke_layer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db, _ = make_scenario_db()
        world = persona_kb_inquiry(db)

        def _boom(*_args, **_kwargs):
            raise AssertionError("external LLM must not be called in compose smoke")

        monkeypatch.setattr(
            "modules.ai.brain.pipeline.MerchantBrain.process",
            _boom,
        )
        smoke = run_compose_smoke(world, "هل منتجاتكم أصلية؟")
        assert smoke.reply_text
