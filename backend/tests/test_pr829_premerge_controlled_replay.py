"""PR #829 pre-merge closure: controlled replay + leftover config SoT.

CI-safe: compose LLM is stubbed (existing BrainReplayRunner). Asserts
semantic ownership, Brain entry, and platform execution boundary — not
exact Arabic prose. No production deploy.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from brain_replay_fixtures import (  # noqa: E402
    BrainReplaySnapshot,
    make_brain_replay_db_and_world,
)
from brain_replay_runner import BrainReplayRunner, ReplayStep, TurnAudit  # noqa: E402
from commerce_scenario_fixtures import attach_brain_state, build_order_prep  # noqa: E402
from core.inbound_dedup import reset_cache  # noqa: E402
from core.outbound_dedup import clear_outbound_dedup  # noqa: E402
from core.tenant import DEFAULT_STORE, STORE_AI_MODE_ON, STORE_AI_MODE_TEST  # noqa: E402
from models import Integration, TenantSettings, WhatsAppConnection  # noqa: E402
from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: E402
    reset_fragment_cache_for_tests,
)

_SOCIAL = "شوف اشوي بروح عند اهلي شويه واجي"
_FOLLOWUP = "وش صار"
_PAYMENT = "ابي ادفع"
_TRANSFER = "كيف احول المبلغ"
_T33_PHONE = "966500000033"
_T1_PHONE = "966500000001"
_LONG_SAME_BODY = (
    "شوف اشوي بروح عند اهلي شويه واجي وبعدين اكمل الطلب بهدوء من غير عجلة"
)

_HIJACK_MARKERS = (
    "branch_trigger",
    "showroom",
    "collect_next_field",
)


def _checkout_prep() -> Dict[str, Any]:
    return build_order_prep(
        customer_first_name="",
        customer_last_name="",
        line_items=[
            {
                "product_name": "قميص قطني أزرق",
                "quantity": 1,
                "catalog_price": 89.0,
            }
        ],
        order_flow_v2_active=True,
        missing_fields=["city", "customer_name"],
        pending_delivery_location={
            "source": "whatsapp_location_pin",
            "google_maps_url": "https://maps.app.goo.gl/example",
            "delivery_address_status": "accepted",
        },
    )


def _rebind_phone(world, phone: str) -> None:
    digits = "".join(ch for ch in phone if ch.isdigit())
    e164 = "+" + digits
    world.phone = digits
    world.phone_e164 = e164
    world.customer.phone = e164
    world.customer.normalized_phone = digits
    ts = world.db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
    wa = dict(ts.whatsapp_settings or {})
    wa["phone_number"] = e164
    ts.whatsapp_settings = wa
    ai = dict(ts.ai_settings or {})
    allowed = list(ai.get("ai_test_allowed_numbers") or [])
    allowed.extend([e164, digits])
    ai["ai_test_allowed_numbers"] = list(dict.fromkeys(allowed))
    ts.ai_settings = ai
    conn = (
        world.db.query(WhatsAppConnection)
        .filter_by(tenant_id=world.tenant.id)
        .first()
    )
    if conn is not None:
        conn.phone_number = e164
        world.db.add(conn)
    world.db.add(world.customer)
    world.db.add(ts)
    world.db.commit()


def _apply_checkout(world) -> None:
    attach_brain_state(world.conversation, _checkout_prep())
    world.db.add(world.conversation)
    world.db.commit()


def _canonical_steps() -> List[ReplayStep]:
    return [
        ReplayStep("social", _SOCIAL, inbound_metadata={"type": "text", "normalized_type": "text"}),
        ReplayStep("followup", _FOLLOWUP, inbound_metadata={"type": "text", "normalized_type": "text"}),
        ReplayStep("payment", _PAYMENT, inbound_metadata={"type": "text", "normalized_type": "text"}),
        ReplayStep("transfer_followup", _TRANSFER, inbound_metadata={"type": "text", "normalized_type": "text"}),
    ]


def _reset_replay_caches() -> None:
    reset_cache()
    reset_fragment_cache_for_tests()
    clear_outbound_dedup()


def _force_ofv2_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "modules.ai.order_flow_v2.owner.operational_tuple",
        lambda *_a, **_k: (True, False, "global_enabled"),
    )
    monkeypatch.setattr(
        "core.wa_usage.check_limit",
        lambda *_a, **_k: type(
            "AllowResult",
            (),
            {
                "allowed": True,
                "used_total": 0,
                "limit": -1,
                "reason": "ok",
                "pct": 0.0,
            },
        )(),
    )


def _assert_brain_owns(turn: TurnAudit, *, label: str) -> None:
    assert turn.errors == [], f"{label} errors={turn.errors}"
    assert turn.structural_or_unstructured == "unstructured", label
    assert turn.brain_called is True, f"{label} brain_ran=false owner={turn.route_owner}"
    assert turn.skip_brain is False, label
    assert turn.route_owner.startswith("brain"), (
        f"{label} owner={turn.route_owner} ofv2={turn.ofv2_reason}"
    )
    assert not any(m in turn.route_owner for m in _HIJACK_MARKERS), label
    assert turn.ofv2_reason not in {"collect_next_field", "branch_trigger"}, label
    # Compose LLM is stubbed in this harness. Some Brain actions compose
    # without DefaultComposer._llm_compose; ownership still requires Brain.
    if turn.llm_called:
        assert turn.llm_stub_output or turn.outbound_reply


def _t33_world():
    db, world = make_brain_replay_db_and_world(
        BrainReplaySnapshot(
            tenant_name="متجر آل عايد للعسل البلدي",
            store_ai_mode=STORE_AI_MODE_TEST,
        )
    )
    ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
    ai = dict(ts.ai_settings or {})
    ai.update(
        {
            "store_ai_mode": STORE_AI_MODE_TEST,
            "store_ai_enabled": True,
            "persona_composer_enabled": True,
            "persona_composer_allowlist_tenants": [33],
            "ai_test_allowed_numbers": [world.phone_e164, world.phone],
        }
    )
    ts.ai_settings = ai
    store = dict(ts.store_settings or {})
    store.update(
        {
            "platform_type": "salla",
            "salla_access_token": "",
            "salla_client_id": "",
        }
    )
    ts.store_settings = store
    db.add(ts)
    _rebind_phone(world, _T33_PHONE)
    _apply_checkout(world)
    return db, world


def _t1_world():
    db, world = make_brain_replay_db_and_world(
        BrainReplaySnapshot(
            tenant_name="متجر تجريبي عام",
            store_ai_mode=STORE_AI_MODE_ON,
        )
    )
    ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
    ai = dict(ts.ai_settings or {})
    ai.update(
        {
            "store_ai_mode": STORE_AI_MODE_ON,
            "store_ai_enabled": True,
            "persona_composer_enabled": False,
            "persona_composer_allowlist_tenants": [33],
        }
    )
    ts.ai_settings = ai
    store = dict(ts.store_settings or {})
    store.update({"platform_type": "salla", "salla_access_token": "salla-live-token"})
    ts.store_settings = store
    db.add(
        Integration(
            provider="salla",
            external_store_id="t1-control-store",
            tenant_id=world.tenant.id,
            enabled=True,
            config={"platform": "salla", "access_token": "salla-live-token"},
        )
    )
    db.add(ts)
    _rebind_phone(world, _T1_PHONE)
    _apply_checkout(world)
    return db, world


class TestPr829ControlledReplay:
    def test_tenant33_equivalent_canonical_families(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_replay_caches()
        _force_ofv2_live(monkeypatch)
        _db, world = _t33_world()
        runner = BrainReplayRunner(world, scenario_name="pr829_t33")
        customer_before = str(world.customer.name or "")
        turns = [runner.run_turn(step) for step in _canonical_steps()]
        by_label = {t.label: t for t in turns}

        social = by_label["social"]
        _assert_brain_owns(social, label="social")
        assert social.llm_called is True
        assert social.llm_stub_output
        assert "فرع" not in (social.outbound_reply or "")

        followup = by_label["followup"]
        _assert_brain_owns(followup, label="followup")
        assert followup.llm_called is True
        world.db.refresh(world.customer)
        world.db.refresh(world.conversation)
        prep = dict(
            ((world.conversation.extra_metadata or {}).get("brain_state") or {}).get(
                "order_prep"
            )
            or {}
        )
        assert str(world.customer.name or "") == customer_before
        assert str(prep.get("customer_first_name") or "") not in {"وش", "صار", "وش صار"}
        assert str(prep.get("customer_last_name") or "") not in {"وش", "صار", "وش صار"}

        payment = by_label["payment"]
        _assert_brain_owns(payment, label="payment")
        assert payment.brain_called is True

        transfer = by_label["transfer_followup"]
        _assert_brain_owns(transfer, label="transfer_followup")
        assert transfer.ofv2_reason != "collect_next_field"

        from core.tenant_config_hygiene import apply_tenant_settings_hygiene  # noqa: PLC0415

        ts = world.db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
        apply_tenant_settings_hygiene(world.db, ts)
        world.db.commit()
        world.db.refresh(ts)
        assert "persona_composer_allowlist_tenants" not in (ts.ai_settings or {})
        assert (ts.store_settings or {}).get("platform_type") == "custom"
        assert (ts.ai_settings or {}).get("persona_composer_enabled") is True
        assert (ts.ai_settings or {}).get("store_ai_mode") == STORE_AI_MODE_TEST

    def test_tenant1_control_same_semantic_core(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_replay_caches()
        _force_ofv2_live(monkeypatch)
        _db, world = _t1_world()
        runner = BrainReplayRunner(world, scenario_name="pr829_t1")
        turns = [runner.run_turn(step) for step in _canonical_steps()]
        for turn in turns:
            _assert_brain_owns(turn, label=f"t1:{turn.label}")

    def test_same_intelligence_core_across_tenants(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_replay_caches()
        _force_ofv2_live(monkeypatch)
        _db33, w33 = _t33_world()
        _db1, w1 = _t1_world()
        r33 = BrainReplayRunner(w33, scenario_name="pr829_t33_parity")
        r1 = BrainReplayRunner(w1, scenario_name="pr829_t1_parity")
        t33 = [r33.run_turn(step) for step in _canonical_steps()[:2]]
        _reset_replay_caches()
        t1 = [r1.run_turn(step) for step in _canonical_steps()[:2]]
        for a, b in zip(t33, t1):
            assert a.brain_called is True and b.brain_called is True
            assert a.structural_or_unstructured == b.structural_or_unstructured == "unstructured"
            assert a.route_owner.startswith("brain") and b.route_owner.startswith("brain")
            assert a.skip_brain is False and b.skip_brain is False

    def test_distinct_inbound_same_body_does_not_collide(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_replay_caches()
        _force_ofv2_live(monkeypatch)
        _db, world = _t33_world()
        runner = BrainReplayRunner(world, scenario_name="pr829_dedup")
        matrix = runner.run_dedup_matrix(_LONG_SAME_BODY)
        by_case = {row["case"]: row for row in matrix}
        assert by_case["same_msg_id"]["second_dedup_hit"] is True
        assert by_case["different_msg_id"]["second_dedup_hit"] is False
        assert by_case["different_msg_id"]["second_route_owner"] != "dedup.suppressed"

    def test_same_inbound_id_replay_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_replay_caches()
        _force_ofv2_live(monkeypatch)
        _db, world = _t33_world()
        runner = BrainReplayRunner(world, scenario_name="pr829_idempotent")
        first = runner.run_turn(
            ReplayStep(label="r1", text=_PAYMENT, provider_msg_id="wamid.pay.SAME")
        )
        second = runner.run_turn(
            ReplayStep(label="r2", text=_PAYMENT, provider_msg_id="wamid.pay.SAME")
        )
        assert first.brain_called is True
        assert second.dedup_hit is True
        assert second.brain_called is False


class TestPr829LegacyConfigClosure:
    def test_default_platform_type_is_not_salla(self) -> None:
        assert DEFAULT_STORE["platform_type"] != "salla"

    def test_platform_type_salla_without_integration_is_disconnected(self) -> None:
        from core.commerce_platform import (  # noqa: PLC0415
            platform_type_alone_is_not_connection,
            resolve_connected_commerce_platform,
        )
        from routers.knowledge import _platform_signal_for_tenant  # noqa: PLC0415

        db, world = make_brain_replay_db_and_world(
            BrainReplaySnapshot(tenant_name="متجر تجريبي عام")
        )
        ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
        ts.store_settings = {
            "platform_type": "salla",
            "salla_access_token": "",
        }
        db.add(ts)
        db.commit()
        assert platform_type_alone_is_not_connection(ts.store_settings) is True
        assert resolve_connected_commerce_platform(db, world.tenant.id) is None
        signal = _platform_signal_for_tenant(db, world.tenant.id)
        assert signal.connected is False
        assert signal.platform is None

    def test_integration_row_is_source_of_truth(self) -> None:
        from core.commerce_platform import resolve_connected_commerce_platform  # noqa: PLC0415
        from routers.knowledge import _platform_signal_for_tenant  # noqa: PLC0415

        db, world = make_brain_replay_db_and_world(
            BrainReplaySnapshot(tenant_name="متجر تجريبي عام")
        )
        ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
        ts.store_settings = {"platform_type": "custom", "salla_access_token": ""}
        db.add(
            Integration(
                provider="salla",
                external_store_id="so-t-store",
                tenant_id=world.tenant.id,
                enabled=True,
                config={"platform": "salla"},
            )
        )
        db.add(ts)
        db.commit()
        assert resolve_connected_commerce_platform(db, world.tenant.id) == "salla"
        signal = _platform_signal_for_tenant(db, world.tenant.id)
        assert signal.connected is True
        assert signal.platform == "salla"
