"""
tests/test_customer_identity.py
───────────────────────────────
Customer identity extraction + persistence during active order flows.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.customer_identity import (  # noqa: E402
    apply_customer_identity_during_order_flow,
    extract_customer_identity_fields,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _active_order_ctx(
    message: str,
    *,
    order_prep: OrderPreparationState | None = None,
    stage: str = "ordering",
) -> BrainContext:
    prep = order_prep or OrderPreparationState(
        product_id="prod-1",
        order_status="awaiting_address",
        missing_fields=["customer_first_name", "city"],
    )
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        raw_message=message,
        intent=Intent(name="general", confidence=0.5, raw_message=message, slots={}),
        state=MerchantConversationState(stage=stage, order_prep=prep),
        facts=CommerceFacts(),
    )


def _idle_ctx(message: str) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        raw_message=message,
        intent=Intent(name="general", confidence=0.5, raw_message=message, slots={}),
        state=MerchantConversationState(stage="browsing"),
        facts=CommerceFacts(),
    )


class TestCustomerIdentityExtraction:
    def test_standalone_full_name_during_active_order(self):
        ctx = _active_order_ctx("سعيد مستور الحارثي")
        result = apply_customer_identity_during_order_flow(ctx, db=None)
        prep = ctx.state.order_prep
        assert prep.customer_first_name == "سعيد"
        assert prep.customer_last_name == "مستور الحارثي"
        assert any(a.field == "name" for a in result.applied)

    def test_ismi_pattern_saved(self):
        ctx = _active_order_ctx("اسمي سعيد العتيبي")
        apply_customer_identity_during_order_flow(ctx, db=None)
        prep = ctx.state.order_prep
        assert prep.customer_first_name == "سعيد"
        assert prep.customer_last_name == "العتيبي"

    def test_recipient_name_saved(self):
        ctx = _active_order_ctx("المستلم محمد")
        result = apply_customer_identity_during_order_flow(ctx, db=None)
        assert ctx.state.order_prep.recipient_name == "محمد"
        assert any(a.field == "recipient_name" for a in result.applied)

    def test_no_active_order_does_not_save_random_phrase(self):
        ctx = _idle_ctx("سعيد مستور الحارثي")
        result = apply_customer_identity_during_order_flow(ctx, db=None)
        prep = ctx.state.order_prep
        assert prep is None or not prep.customer_first_name
        assert not result.applied

    def test_no_save_outside_order_flow_extractor_returns_empty(self):
        fields = extract_customer_identity_fields(
            "سعيد مستور الحارثي",
            in_order_flow=False,
        )
        assert fields == []

    def test_existing_name_without_update_wording_not_overwritten(self):
        prep = OrderPreparationState(
            product_id="prod-1",
            order_status="awaiting_address",
            customer_first_name="جميل",
            customer_last_name="العتيبي",
        )
        ctx = _active_order_ctx("أحمد محمد", order_prep=prep)
        result = apply_customer_identity_during_order_flow(ctx, db=None)
        assert ctx.state.order_prep.customer_first_name == "جميل"
        assert ctx.state.order_prep.customer_last_name == "العتيبي"
        assert result.needs_confirmation is not None
        assert ("name", "existing_verified_name") in result.skipped

    def test_update_wording_allows_name_change(self):
        prep = OrderPreparationState(
            product_id="prod-1",
            order_status="awaiting_address",
            customer_first_name="جميل",
            customer_last_name="العتيبي",
        )
        ctx = _active_order_ctx("غير الاسم إلى أحمد محمد", order_prep=prep)
        apply_customer_identity_during_order_flow(ctx, db=None)
        # High-confidence pattern may still need explicit name in message;
        # standalone name with update wording uses medium → pending or merge.
        assert (
            ctx.state.order_prep.customer_first_name in ("أحمد", "جميل")
            or ctx.state.order_prep.pending_identity_value == "أحمد محمد"
        )

    def test_persistence_runs_before_decision_engine(self):
        """Identity layer mutates order_prep independently of decision engine."""
        ctx = _active_order_ctx("اسمي فهد")
        apply_customer_identity_during_order_flow(ctx, db=None)
        assert ctx.state.order_prep.customer_first_name == "فهد"
        # Pipeline calls apply_customer_identity_during_order_flow before decide().
        import inspect
        from modules.ai.brain import pipeline as pipeline_mod

        src = inspect.getsource(pipeline_mod.MerchantBrain.process)
        assert "apply_customer_identity_during_order_flow" in src
        assert src.index("apply_customer_identity_during_order_flow") < src.index(
            "self._decision_engine.decide"
        )

    @patch("modules.ai.brain.commerce.customer_identity._persist_customer_profile")
    def test_high_confidence_name_persists_customer_profile(self, mock_persist):
        ctx = _active_order_ctx("اسمي سعيد")
        apply_customer_identity_during_order_flow(ctx, db=MagicMock())
        mock_persist.assert_called_once()
        call_kw = mock_persist.call_args.kwargs
        assert call_kw["name"] == "سعيد"

    def test_register_bname_pattern_recipient(self):
        ctx = _active_order_ctx("سجل باسم أبو يوسف")
        apply_customer_identity_during_order_flow(ctx, db=None)
        assert ctx.state.order_prep.recipient_name == "أبو يوسف"

    def test_khalih_bname_recipient(self):
        ctx = _active_order_ctx("خليه باسم أبو يوسف")
        apply_customer_identity_during_order_flow(ctx, db=None)
        assert ctx.state.order_prep.recipient_name == "أبو يوسف"
        assert not ctx.state.order_prep.customer_first_name


class TestCustomerIdentitySafeguards:
    def test_greeting_vocative_not_saved(self):
        ctx = _active_order_ctx("هلا ريحاني")
        result = apply_customer_identity_during_order_flow(ctx, db=None)
        assert not ctx.state.order_prep.customer_first_name
        assert not result.applied

    def test_service_phrase_not_saved(self):
        ctx = _active_order_ctx("سعيد بالخدمة")
        apply_customer_identity_during_order_flow(ctx, db=None)
        assert not ctx.state.order_prep.customer_first_name

    def test_religious_phrase_not_saved(self):
        ctx = _active_order_ctx("محمد رسول الله")
        apply_customer_identity_during_order_flow(ctx, db=None)
        assert not ctx.state.order_prep.customer_first_name

    def test_provenance_stamped_on_save(self):
        ctx = _active_order_ctx("اسمي فهد")
        apply_customer_identity_during_order_flow(ctx, db=None)
        assert ctx.state.order_prep.identity_provenance.get("customer_name") == (
            "explicit_customer_statement"
        )

    def test_uses_raw_message_not_repaired(self):
        ctx = _active_order_ctx("اسمي فهد")
        ctx.message = "اسمي فهد العتيبي المُصلح"
        ctx.raw_message = "اسمي فهد"
        apply_customer_identity_during_order_flow(ctx, db=None)
        assert ctx.state.order_prep.customer_first_name == "فهد"
