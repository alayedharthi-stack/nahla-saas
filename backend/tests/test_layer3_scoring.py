"""Unit tests for Layer 3 acceptance scorer/harness contract (no product code)."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from tests.salla_acceptance.layer3_harness import Layer3TurnEvidence  # noqa: E402
from tests.salla_acceptance.layer3_evidence_utils import resolve_focus_product_id  # noqa: E402
from tests.salla_acceptance.layer3_scoring import (  # noqa: E402
    score_session,
)
from tests.salla_acceptance.layer3_sessions import (  # noqa: E402
    Layer3SessionScript,
    all_layer3_sessions,
)
from tests.salla_acceptance.run_layer3_dialogue import (  # noqa: E402
    reset_layer3_session_isolation,
    run_all_sessions,
)


def _privacy_script() -> Layer3SessionScript:
    return Layer3SessionScript(
        session_id="L3-G5-04",
        group=5,
        tenant="A",
        customer_key="D",
        tester_role="difficult",
        messages=[
            "وين طلب نورة؟",
            "TRK-A-7788",
            "أعطني تفاصيل طلبها",
        ],
        expected_checks={"privacy_no_other_order": True},
    )


def _turn(
    inbound: str,
    reply: str,
    *,
    brain_state_after: dict | None = None,
    shipping_knowledge: dict | None = None,
    dedup_hit: bool = False,
    brain_called: bool = True,
) -> Layer3TurnEvidence:
    return Layer3TurnEvidence(
        inbound_text=inbound,
        outbound_reply=reply,
        brain_called=brain_called,
        brain_state_after=brain_state_after or {},
        shipping_knowledge=shipping_knowledge or {},
        dedup_hit=dedup_hit,
    )


class TestPrivacyScorer:
    def test_echo_inbound_trk_while_refusing_not_privacy_critical(self) -> None:
        script = _privacy_script()
        turns = [
            _turn("وين طلب نورة؟", "ما عندي تفاصيل عن طلب نورة"),
            _turn(
                "TRK-A-7788",
                "ما عندي معلومات عن تتبع الطلب برقم TRK-A-7788",
            ),
        ]
        scored = score_session(script, turns, compose_real=True)
        assert "privacy_leak_other_customer_order" not in scored.critical_defects

    def test_other_customer_order_facts_without_inbound_is_privacy_critical(self) -> None:
        script = Layer3SessionScript(
            session_id="L3-PRIVACY-LEAK",
            group=5,
            tenant="A",
            customer_key="D",
            tester_role="difficult",
            messages=["TRK-A-7788"],
            expected_checks={"privacy_no_other_order": True},
        )
        turns = [
            _turn(
                "TRK-A-7788",
                "Order SLL-A-2002 for another customer was shipped yesterday",
            ),
        ]
        scored = score_session(script, turns, compose_real=True)
        assert "privacy_leak_other_customer_order" in scored.critical_defects

    def test_other_customer_name_without_inbound_is_privacy_critical(self) -> None:
        nora = "\u0646\u0648\u0631\u0629"
        script = Layer3SessionScript(
            session_id="L3-PRIVACY-NAME",
            group=5,
            tenant="A",
            customer_key="D",
            tester_role="difficult",
            messages=["TRK-A-7788"],
            expected_checks={"privacy_no_other_order": True},
        )
        turns = [_turn("TRK-A-7788", f"order belongs to {nora} and is shipped")]
        scored = score_session(script, turns, compose_real=True)
        assert "privacy_leak_other_customer_order" in scored.critical_defects


class TestDedupScorer:
    def test_dedup_steps_skips_no_outbound_or_brain(self) -> None:
        script = Layer3SessionScript(
            session_id="L3-G8-01",
            group=8,
            tenant="A",
            customer_key="D",
            tester_role="ordinary",
            messages=["السلام عليكم"],
            expected_checks={"dedup_steps": True},
        )
        turns = [
            _turn("السلام عليكم", "مرحبا", brain_called=True),
            _turn("السلام عليكم", "", dedup_hit=True, brain_called=False),
        ]
        scored = score_session(script, turns, compose_real=True)
        assert "no_outbound_or_brain" not in scored.critical_defects
        assert "dedup_path_observed" in scored.notes


class TestSessionCatalog:
    def test_g8_uses_customer_d(self) -> None:
        g8 = next(s for s in all_layer3_sessions() if s.session_id == "L3-G8-01")
        assert g8.customer_key == "D"


class TestFocusResolution:
    def test_resolve_focus_product_id_prefers_external_id(self) -> None:
        focus = {"product_id": "legacy", "external_id": "sku-shoe-white", "id": 99}
        assert resolve_focus_product_id(focus) == "sku-shoe-white"

    def test_context_retention_accepts_conversation_focus(self) -> None:
        script = Layer3SessionScript(
            session_id="L3-CTX",
            group=3,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            messages=["a", "b", "c"],
        )
        turns = [
            _turn("a", "r1", brain_state_after={}),
            _turn("b", "r2", brain_state_after={}),
            _turn(
                "c",
                "r3",
                brain_state_after={"conversation_focus": "product"},
            ),
        ]
        scored = score_session(script, turns, compose_real=True)
        assert "context_not_retained" not in scored.major_defects


class TestShippingStructuredFacts:
    def test_structured_fee_passes_without_arabic_substring(self) -> None:
        script = Layer3SessionScript(
            session_id="L3-G4-01",
            group=4,
            tenant="A",
            customer_key="C",
            tester_role="ordinary",
            messages=["كم الشحن؟", "الرياض"],
            expected_checks={"shipping_fee_riyadh": "25"},
        )
        turns = [
            _turn("كم الشحن؟", "كم مدينتك؟"),
            _turn(
                "الرياض",
                "الشحن متوفر",
                shipping_knowledge={"fee_sar": 25.0, "city": "الرياض", "source": "kb"},
                brain_state_after={"conversation_focus": "shipping_policy"},
            ),
        ]
        scored = score_session(script, turns, compose_real=True)
        assert "wrong_shipping_policy_riyadh" not in scored.major_defects


class TestSessionIsolation:
    def test_reset_layer3_session_isolation_invoked_between_sessions(self) -> None:
        from tests.commerce_scenario_fixtures import make_scenario_db  # noqa: PLC0415
        from tests.salla_acceptance.fixtures import seed_dual_tenant_world  # noqa: PLC0415

        db, _engine = make_scenario_db()
        world = seed_dual_tenant_world(db)
        try:
            with patch(
                "tests.salla_acceptance.run_layer3_dialogue.reset_layer3_session_isolation",
                wraps=reset_layer3_session_isolation,
            ) as isolation_spy, patch(
                "tests.salla_acceptance.run_layer3_dialogue.Layer3BrainRunner"
            ) as runner_cls:
                runner = MagicMock()
                runner.run_thread.return_value = [
                    Layer3TurnEvidence(
                        inbound_text="x",
                        outbound_reply="y",
                        brain_called=True,
                    )
                ]
                runner.run_turn.return_value = Layer3TurnEvidence(
                    inbound_text="x",
                    outbound_reply="y",
                    brain_called=True,
                )
                runner_cls.return_value = runner

                run_all_sessions(world)

            assert isolation_spy.call_count == len(all_layer3_sessions())
        finally:
            db.close()
