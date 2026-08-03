"""P1-B post-compose guard consolidation — shared pipeline ownership tests."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.postprocess.payment_reply_guard import (  # noqa: E402
    REJECTED_EVIDENCE_REPLY_AR,
)
from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: E402
    run_post_compose_truth_guards,
)


@dataclass
class _GuardResult:
    replaced: bool = False
    reply: str = ""
    stripped: bool = False
    requires_grounded_compose: bool = False
    route_action: str = ""


def _convo_stub(conversation_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        id=conversation_id,
        needs_human=False,
        handoff_active=False,
        is_human_handoff=False,
        status="open",
        extra_metadata={},
    )


def _pipeline_kwargs(**overrides):
    base = {
        "db": MagicMock(),
        "tenant_id": 7,
        "to": "966500000001",
        "text": "تم التحويل",
        "reply": "وصل الإيصال وسيتم متابعة الطلب",
        "convo": _convo_stub(),
        "inbound_metadata": {},
        "brain_handoff": False,
        "brain_nc_block": False,
        "brain_nc_category": "",
        "br_action": "",
        "brain_persona_compose_event": None,
        "mode": "primary",
        "primary_already_applied": False,
        "persona_ownership": None,
        "live_provenance_tracker": None,
        "conversation_id": 42,
    }
    base.update(overrides)
    return base


class TestPrimaryPipelineOrderAndEvents:
    @patch(
        "modules.ai.brain.postprocess.staff_escalation_truth_guard.apply_staff_escalation_truth_guard"
    )
    @patch("modules.ai.brain.postprocess.shipment_truth_guard.apply_shipment_truth_guard")
    @patch("modules.ai.brain.postprocess.payment_reply_guard.apply_payment_reply_guard")
    @patch("core.payment_intent.rewrite_generic_reply_for_payment_context")
    @patch("modules.ai.brain.postprocess.service_closer_guard.apply_service_closer_guard")
    @patch("core.order_flow._load_brain_state")
    @patch("core.order_flow._focus_summary")
    def test_primary_records_guard_events_in_order(
        self,
        mock_focus,
        mock_load_state,
        mock_service_closer,
        mock_payment_rewrite,
        mock_payment_guard,
        mock_shipment_guard,
        mock_staff_guard,
    ) -> None:
        mock_load_state.return_value = (None, {"stage": "exploring"})
        mock_focus.return_value = {}
        mock_service_closer.return_value = _GuardResult(stripped=False, reply="original")
        mock_payment_rewrite.return_value = None
        mock_payment_guard.return_value = _GuardResult(replaced=False, reply="original")
        mock_shipment_guard.return_value = _GuardResult(replaced=False, reply="original")
        mock_staff_guard.return_value = _GuardResult(replaced=False, reply="original")

        result = run_post_compose_truth_guards(**_pipeline_kwargs(mode="primary"))

        assert result.primary_applied is True
        guard_names = [event.guard for event in result.events]
        assert guard_names == [
            "service_closer_guard",
            "payment_context_rewrite",
            "payment_reply_guard",
            "shipment_truth_guard",
            "staff_escalation_truth_guard",
        ]
        mock_payment_guard.assert_called_once()
        mock_shipment_guard.assert_called_once()
        mock_staff_guard.assert_called_once()


class TestLastLineSkipsWhenPrimaryApplied:
    @patch("modules.ai.brain.postprocess.payment_reply_guard.apply_payment_reply_guard")
    def test_last_line_skips_payment_guard_when_primary_owner(self, mock_payment_guard) -> None:
        with patch(
            "modules.ai.brain.postprocess.post_compose_guard_pipeline.POST_COMPOSE_SINGLE_OWNER",
            True,
        ):
            result = run_post_compose_truth_guards(
                **_pipeline_kwargs(
                    mode="last_line",
                    primary_already_applied=True,
                )
            )

        mock_payment_guard.assert_not_called()
        payment_event = next(
            event for event in result.events if event.guard == "payment_reply_guard"
        )
        assert payment_event.acted is False
        assert payment_event.reason == "skipped_primary_owner"
        assert payment_event.layer == "last_line"


class TestLastLineRunsGuardsForLegacy:
    @patch("modules.ai.brain.postprocess.payment_reply_guard.apply_payment_reply_guard")
    @patch("core.order_flow._load_brain_state")
    @patch("core.order_flow._focus_summary")
    @patch("modules.ai.brain.postprocess.service_closer_guard.apply_service_closer_guard")
    def test_last_line_without_primary_still_runs_guards(
        self,
        mock_service_closer,
        mock_focus,
        mock_load_state,
        mock_payment_guard,
    ) -> None:
        mock_load_state.return_value = (None, {})
        mock_focus.return_value = {}
        mock_service_closer.return_value = _GuardResult(stripped=False, reply="x")
        mock_payment_guard.return_value = _GuardResult(replaced=False, reply="x")

        with patch(
            "modules.ai.brain.postprocess.shipment_truth_guard.apply_shipment_truth_guard",
            return_value=_GuardResult(replaced=False, reply="x"),
        ), patch(
            "modules.ai.brain.postprocess.staff_escalation_truth_guard.apply_staff_escalation_truth_guard",
            return_value=_GuardResult(replaced=False, reply="x"),
        ), patch("core.payment_intent.rewrite_generic_reply_for_payment_context", return_value=None):
            run_post_compose_truth_guards(
                **_pipeline_kwargs(
                    mode="last_line",
                    primary_already_applied=False,
                )
            )

        mock_payment_guard.assert_called_once()


class TestTelemetryModifiedFlag:
    @patch("modules.ai.brain.postprocess.payment_reply_guard.apply_payment_reply_guard")
    @patch("core.order_flow._load_brain_state")
    @patch("core.order_flow._focus_summary")
    @patch("modules.ai.brain.postprocess.service_closer_guard.apply_service_closer_guard")
    def test_modified_true_when_guard_replaces(
        self,
        mock_service_closer,
        mock_focus,
        mock_load_state,
        mock_payment_guard,
    ) -> None:
        mock_load_state.return_value = (None, {})
        mock_focus.return_value = {}
        mock_service_closer.return_value = _GuardResult(stripped=False, reply="before")
        llm_reply = "وصل الإيصال وسيتم متابعة الطلب"
        mock_payment_guard.return_value = _GuardResult(
            replaced=True,
            reply=REJECTED_EVIDENCE_REPLY_AR,
        )

        with patch(
            "modules.ai.brain.postprocess.shipment_truth_guard.apply_shipment_truth_guard",
            return_value=_GuardResult(replaced=False, reply=llm_reply),
        ), patch(
            "modules.ai.brain.postprocess.staff_escalation_truth_guard.apply_staff_escalation_truth_guard",
            return_value=_GuardResult(replaced=False, reply=llm_reply),
        ), patch("core.payment_intent.rewrite_generic_reply_for_payment_context", return_value=None):
            result = run_post_compose_truth_guards(
                **_pipeline_kwargs(reply=llm_reply, mode="primary")
            )

        payment_event = next(
            event for event in result.events if event.guard == "payment_reply_guard"
        )
        assert payment_event.modified is True
        assert result.reply == REJECTED_EVIDENCE_REPLY_AR


class TestFlagFalseAllowsDualRun:
    @patch("modules.ai.brain.postprocess.payment_reply_guard.apply_payment_reply_guard")
    @patch("core.order_flow._load_brain_state")
    @patch("core.order_flow._focus_summary")
    @patch("modules.ai.brain.postprocess.service_closer_guard.apply_service_closer_guard")
    def test_flag_false_reruns_guards_on_last_line(
        self,
        mock_service_closer,
        mock_focus,
        mock_load_state,
        mock_payment_guard,
    ) -> None:
        mock_load_state.return_value = (None, {})
        mock_focus.return_value = {}
        mock_service_closer.return_value = _GuardResult(stripped=False, reply="x")
        mock_payment_guard.return_value = _GuardResult(replaced=False, reply="x")

        with patch(
            "modules.ai.brain.postprocess.post_compose_guard_pipeline.POST_COMPOSE_SINGLE_OWNER",
            False,
        ), patch(
            "modules.ai.brain.postprocess.shipment_truth_guard.apply_shipment_truth_guard",
            return_value=_GuardResult(replaced=False, reply="x"),
        ), patch(
            "modules.ai.brain.postprocess.staff_escalation_truth_guard.apply_staff_escalation_truth_guard",
            return_value=_GuardResult(replaced=False, reply="x"),
        ), patch("core.payment_intent.rewrite_generic_reply_for_payment_context", return_value=None):
            run_post_compose_truth_guards(
                **_pipeline_kwargs(
                    mode="last_line",
                    primary_already_applied=True,
                )
            )

        mock_payment_guard.assert_called_once()


class TestMerchantBrainTurnPrimaryApplied:
    def test_apply_wrapper_sets_primary_applied(self) -> None:
        from services.merchant_brain_turn import _apply_post_compose_truth_guards

        with patch(
            "modules.ai.brain.postprocess.post_compose_guard_pipeline.run_post_compose_truth_guards"
        ) as mock_run:
            from modules.ai.brain.postprocess.post_compose_guard_pipeline import (
                PostComposeGuardEvent,
                PostComposeGuardResult,
            )

            mock_run.return_value = PostComposeGuardResult(
                reply="guarded",
                events=[
                    PostComposeGuardEvent(
                        guard="payment_reply_guard",
                        acted=True,
                        modified=False,
                        suppressed_send=False,
                        layer="primary",
                    )
                ],
                primary_applied=True,
            )
            reply, primary_applied, events = _apply_post_compose_truth_guards(
                db=MagicMock(),
                tenant_id=1,
                to="966500000099",
                text="hi",
                reply="hello",
                convo=_convo_stub(),
                inbound_metadata={},
                brain_handoff=False,
                brain_nc_block=False,
                brain_nc_category="",
                br_action="",
                brain_persona_compose_event=None,
                persona_ownership=None,
            )

        assert reply == "guarded"
        assert primary_applied is True
        assert events[0]["guard"] == "payment_reply_guard"
