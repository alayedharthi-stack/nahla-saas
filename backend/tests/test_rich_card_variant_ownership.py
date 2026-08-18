"""Rich product card × variant prompt ownership — complementary, not exclusive."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from services.catalog_product_orchestrator import (  # noqa: E402
    ProductCardSendAction,
    REASON_TENANT_MISMATCH,
    REASON_VARIANT_CHOICE_REQUIRED,
    evaluate_product_card_send,
    should_attempt_catalog_send,
)


_PUBLISHED = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _conn(**kw):
    defaults = dict(
        status="connected",
        sending_enabled=True,
        phone_number_id="1234567890",
        catalog_enabled=True,
        meta_catalog_id="CAT-1",
        provider="meta",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _attachment(**kw) -> Dict[str, Any]:
    base = dict(
        kind="product_card",
        id=28,
        title="قميص قطني أزرق",
        external_id="1921568272",
        file_url="https://cdn.example/shirt.jpg",
        product_url="https://shop.example/p/shirt",
        in_stock=True,
        confidence="fts",
        needs_variant_choice=False,
        variants=[],
    )
    base.update(kw)
    return base


class TestOrchestratorVariantOwnership:
    def test_single_product_no_variants_attempts_catalog_send(self) -> None:
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(),
            product_row=SimpleNamespace(
                id=28,
                tenant_id=1,
                external_id="1921568272",
                in_stock=True,
                meta_catalog_published_at=_PUBLISHED,
            ),
            positive_commerce_intent=True,
        )
        assert d.action == ProductCardSendAction.SEND_CATALOG
        assert should_attempt_catalog_send(d) is True

    def test_single_product_with_variants_blocks_meta_catalog_only(self, monkeypatch) -> None:
        monkeypatch.setenv("CATALOG_VARIANT_SEND", "true")
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(
                needs_variant_choice=True,
                variants=[
                    {"id": 1, "label": "S", "in_stock": True},
                    {"id": 2, "label": "M", "in_stock": True},
                ],
            ),
            positive_commerce_intent=True,
        )
        assert d.action == ProductCardSendAction.VARIANT_PROMPT
        assert d.reason == REASON_VARIANT_CHOICE_REQUIRED
        assert should_attempt_catalog_send(d) is False

    def test_picked_variant_allows_catalog_send(self, monkeypatch) -> None:
        monkeypatch.setenv("CATALOG_VARIANT_SEND", "true")
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(
                needs_variant_choice=True,
                picked_variant_retailer_id="var-rid-40",
                variants=[{"id": 2, "label": "M", "in_stock": True}],
            ),
            product_row=SimpleNamespace(
                id=28,
                tenant_id=1,
                external_id="1921568272",
                in_stock=True,
                meta_catalog_published_at=_PUBLISHED,
            ),
            positive_commerce_intent=True,
        )
        assert d.action == ProductCardSendAction.SEND_CATALOG
        assert should_attempt_catalog_send(d) is True

    def test_non_product_attachment_is_not_catalog_send(self) -> None:
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment={"kind": "image", "id": 1},
            positive_commerce_intent=True,
        )
        assert should_attempt_catalog_send(d) is False


class TestDeferredVariantPromptHelper:
    def test_sends_prompt_and_patches_state_without_suppressing_card(self) -> None:
        from routers import whatsapp_webhook as wh

        sent: List[str] = []
        patches: List[dict] = []

        async def _fake_send(**kwargs):
            sent.append(str(kwargs.get("text") or ""))
            return True

        with patch.object(wh, "_send_whatsapp_message", new=AsyncMock(side_effect=_fake_send)):
            with patch("core.order_flow.apply_state_patch", side_effect=lambda *a, **k: patches.append(k) or True):
                audit: Dict[str, Any] = {"catalog_card_sent_count": 0}
                ok = asyncio.run(
                    wh._maybe_send_variant_prompt_after_product_card(
                        db=MagicMock(),
                        tenant_id=1,
                        phone_id="pid",
                        to="966555000001",
                        attachment=_attachment(
                            needs_variant_choice=True,
                            variants=[
                                {"id": 1, "label": "S", "in_stock": True, "is_default": False},
                                {"id": 2, "label": "M", "in_stock": True, "is_default": False},
                            ],
                        ),
                        delivery_audit=audit,
                    )
                )
        assert ok is True
        assert sent and "قميص قطني أزرق" in sent[0]
        assert audit.get("variant_prompt_sent_count") == 1
        assert audit.get("catalog_card_sent_count") == 0
        assert patches and patches[0]["state_patch"]["awaiting_variant_choice"] is True
        assert patches[0]["state_patch"]["pending_variant_product_id"] == "28"

    def test_skips_when_no_variant_choice_needed(self) -> None:
        from routers import whatsapp_webhook as wh

        with patch.object(wh, "_send_whatsapp_message", new=AsyncMock()) as send_mock:
            ok = asyncio.run(
                wh._maybe_send_variant_prompt_after_product_card(
                    db=MagicMock(),
                    tenant_id=1,
                    phone_id="pid",
                    to="966555000001",
                    attachment=_attachment(needs_variant_choice=False),
                    delivery_audit={},
                )
            )
        assert ok is False
        send_mock.assert_not_called()

    def test_skips_when_variant_already_picked(self) -> None:
        from routers import whatsapp_webhook as wh

        with patch.object(wh, "_send_whatsapp_message", new=AsyncMock()) as send_mock:
            ok = asyncio.run(
                wh._maybe_send_variant_prompt_after_product_card(
                    db=MagicMock(),
                    tenant_id=1,
                    phone_id="pid",
                    to="966555000001",
                    attachment=_attachment(
                        needs_variant_choice=True,
                        picked_variant_retailer_id="var-1",
                    ),
                    delivery_audit={},
                )
            )
        assert ok is False
        send_mock.assert_not_called()

    def test_no_image_still_allows_variant_prompt(self) -> None:
        from routers import whatsapp_webhook as wh

        with patch.object(wh, "_send_whatsapp_message", new=AsyncMock(return_value=True)) as send_mock:
            with patch("core.order_flow.apply_state_patch", return_value=True):
                ok = asyncio.run(
                    wh._maybe_send_variant_prompt_after_product_card(
                        db=MagicMock(),
                        tenant_id=1,
                        phone_id="pid",
                        to="966555000001",
                        attachment=_attachment(
                            file_url="",
                            needs_variant_choice=True,
                            variants=[{"id": 1, "label": "40", "in_stock": True}],
                        ),
                        delivery_audit={},
                    )
                )
        assert ok is True
        send_mock.assert_awaited()

    def test_no_product_url_does_not_invent_url_in_variant_prompt(self) -> None:
        from routers import whatsapp_webhook as wh

        sent: List[str] = []

        async def _fake_send(**kwargs):
            sent.append(str(kwargs.get("text") or ""))
            return True

        with patch.object(wh, "_send_whatsapp_message", new=AsyncMock(side_effect=_fake_send)):
            with patch("core.order_flow.apply_state_patch", return_value=True):
                asyncio.run(
                    wh._maybe_send_variant_prompt_after_product_card(
                        db=MagicMock(),
                        tenant_id=1,
                        phone_id="pid",
                        to="966555000001",
                        attachment=_attachment(
                            product_url="",
                            needs_variant_choice=True,
                            variants=[{"id": 1, "label": "40", "in_stock": True}],
                        ),
                        delivery_audit={},
                    )
                )
        assert sent
        assert "http" not in sent[0].lower()
        assert "shop.example" not in sent[0]

    def test_try_send_catalog_does_not_claim_card_on_variant_prompt(self, monkeypatch) -> None:
        """VARIANT_PROMPT must return False so legacy rich card can send."""
        from routers import whatsapp_webhook as wh

        monkeypatch.setenv("CATALOG_VARIANT_SEND", "true")
        att = _attachment(
            needs_variant_choice=True,
            variants=[{"id": 1, "in_stock": True}],
        )
        with patch.object(wh, "_send_whatsapp_message", new=AsyncMock()) as send_mock:
            handled = asyncio.run(
                wh._try_send_catalog_product(
                    db=None,
                    connection=_conn(),
                    tenant_id=1,
                    phone_id="pid",
                    to="966555000001",
                    attachment=att,
                    positive_commerce_intent=True,
                )
            )
        assert handled is False
        send_mock.assert_not_called()


class TestCheckoutSafetyPreserved:
    def test_awaiting_variant_routes_to_same_parent_pick(self) -> None:
        from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.intent import rules
        from modules.ai.brain.types import (
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
            OrderPreparationState,
        )

        msg = "2"
        intent = rules.match(msg) or Intent(name="general", confidence=0.5, raw_message=msg)
        state = MerchantConversationState(
            order_prep=OrderPreparationState(
                awaiting_variant_choice=True,
                pending_variant_product_id="28",
            ),
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966555000001",
            message=msg,
            intent=intent,
            state=state,
            facts=CommerceFacts(has_products=True, product_count=1, in_stock_count=1),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("pending_variant_product_id") == "28"
        assert decision.args.get("variant_pick", {}).get("index_one_based") == 2


class TestTelemetryTruth:
    def test_delivery_audit_tracks_variant_prompt_separately(self) -> None:
        from modules.observability.delivery_mode import (
            compute_final_delivery_mode,
            new_delivery_audit,
        )

        audit = new_delivery_audit()
        audit["text_sent"] = True
        audit["legacy_media_sent_count"] = 1
        audit["cta_url_sent_count"] = 1
        audit["variant_prompt_sent_count"] = 1
        assert audit["catalog_card_sent_count"] == 0
        mode = compute_final_delivery_mode(audit)
        assert mode == "image_cta"

    def test_unified_card_counts_as_single_image_cta_payload(self) -> None:
        from modules.observability.delivery_mode import (
            compute_final_delivery_mode,
            new_delivery_audit,
        )

        audit = new_delivery_audit()
        audit["text_sent"] = True
        audit["unified_product_card_attempted_count"] = 1
        audit["unified_product_card_sent_count"] = 1
        audit["variant_prompt_sent_count"] = 1
        assert compute_final_delivery_mode(audit) == "image_cta"
        assert audit["legacy_media_sent_count"] == 0
        assert audit["cta_url_sent_count"] == 0


class TestTenantIsolation:
    def test_orchestrator_rejects_cross_tenant_row(self) -> None:
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(needs_variant_choice=False),
            product_row=SimpleNamespace(
                id=28, tenant_id=99, external_id="1921568272", in_stock=True,
            ),
            positive_commerce_intent=True,
        )
        assert d.reason == REASON_TENANT_MISMATCH
        assert should_attempt_catalog_send(d) is False
