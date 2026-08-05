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
    create_fresh_layer3_world,
    dispose_layer3_world,
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

    def test_context_retention_required_tagged_sessions(self) -> None:
        required = {
            "L3-G1-01",
            "L3-G1-02",
            "L3-G1-03",
            "L3-G1-05",
            "L3-G1-07",
            "L3-G2-01",
            "L3-G2-02",
            "L3-G3-01",
            "L3-G3-02",
            "L3-G6-01",
            "L3-G9-01",
            "L3-G9-02",
            "L3-G10-01",
        }
        untagged = {"L3-G1-04", "L3-G1-06", "L3-G4-03"}
        by_id = {s.session_id: s for s in all_layer3_sessions()}
        for sid in required:
            assert by_id[sid].expected_checks.get("context_retention_required") is True
        for sid in untagged:
            assert not by_id[sid].expected_checks.get("context_retention_required")


class TestFocusResolution:
    def test_resolve_focus_product_id_prefers_external_id(self) -> None:
        focus = {"product_id": "legacy", "external_id": "sku-shoe-white", "id": 99}
        assert resolve_focus_product_id(focus) == "sku-shoe-white"

    def test_context_retention_rejects_conversation_focus_without_id(self) -> None:
        script = Layer3SessionScript(
            session_id="L3-CTX",
            group=3,
            tenant="A",
            customer_key="A",
            tester_role="ordinary",
            messages=["a", "b", "c"],
            expected_checks={"context_retention_required": True},
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
        assert "context_not_retained" in scored.major_defects


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
    def test_each_session_gets_fresh_world_and_db(self, tmp_path) -> None:
        scripts = all_layer3_sessions()[:2]
        world_ids: list[int] = []
        engine_ids: list[int] = []

        def tracking_create():
            world, db, engine = create_fresh_layer3_world()
            world_ids.append(id(world))
            engine_ids.append(id(engine))
            return world, db, engine

        with patch(
            "tests.salla_acceptance.run_layer3_dialogue.all_layer3_sessions",
            return_value=scripts,
        ), patch(
            "tests.salla_acceptance.run_layer3_dialogue.create_fresh_layer3_world",
            side_effect=tracking_create,
        ), patch(
            "tests.salla_acceptance.run_layer3_dialogue.dispose_layer3_world",
            wraps=dispose_layer3_world,
        ) as dispose_spy, patch(
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

            run_all_sessions(sessions_dir=tmp_path)

        assert len(world_ids) == 2
        assert len(set(world_ids)) == 2
        assert len(set(engine_ids)) == 2
        assert dispose_spy.call_count == 2
        assert list(tmp_path.glob("*.json"))

    def test_fresh_world_prevents_inherited_brain_state(self, tmp_path) -> None:
        scripts = [
            Layer3SessionScript(
                session_id="L3-ISO-1",
                group=1,
                tenant="A",
                customer_key="A",
                tester_role="ordinary",
                messages=["first"],
            ),
            Layer3SessionScript(
                session_id="L3-ISO-2",
                group=1,
                tenant="A",
                customer_key="A",
                tester_role="ordinary",
                messages=["second"],
            ),
        ]
        first_turn_states: list[dict] = []

        def run_thread(messages):
            first_turn_states.append(
                Layer3TurnEvidence(
                    inbound_text=messages[0],
                    outbound_reply="ok",
                    brain_called=True,
                    brain_state_before={},
                    brain_state_after={"focus_product_id": "sku-shoe-white"},
                )
            )
            return first_turn_states[-1:]

        with patch(
            "tests.salla_acceptance.run_layer3_dialogue.all_layer3_sessions",
            return_value=scripts,
        ), patch(
            "tests.salla_acceptance.run_layer3_dialogue.Layer3BrainRunner"
        ) as runner_cls:
            runner = MagicMock()
            runner.run_thread.side_effect = run_thread
            runner_cls.return_value = runner

            run_all_sessions(sessions_dir=tmp_path)

        assert len(first_turn_states) == 2
        assert first_turn_states[0].brain_state_before == {}
        assert first_turn_states[1].brain_state_before == {}
