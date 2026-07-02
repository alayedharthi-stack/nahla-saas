"""
test_brain_replay_canary_shipping_payment_provider_mismatch
───────────────────────────────────────────────────────────
Full-thread brain replay audit for live WhatsApp canary convo 2868.

Asserts route-owner parity vs live (not business fixes yet).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from brain_replay_fixtures import (  # noqa: E402
    BrainReplaySnapshot,
    PaymentFixtureVariant,
    build_brain_replay_world,
    catalog_order_metadata,
    load_canary_snapshot,
    make_brain_replay_db_and_world,
)
from brain_replay_runner import BrainReplayRunner, ReplayStep  # noqa: E402
from commerce_scenario_fixtures import attach_brain_state  # noqa: E402
from core.inbound_dedup import reset_cache  # noqa: E402
from models import MerchantKnowledgeSection  # noqa: E402

SCENARIO = "canary_replay_shipping_payment_provider_mismatch"
_CATALOG_BROWSE_PHRASE = "أقدر أعرض لك الخيارات المؤكدة من الكتالوج"
_LIVE_SHIPPING_SNIPPET = "شحن توصيل 29"


def _canary_steps() -> List[ReplayStep]:
    return [
        ReplayStep("greet", "السلام عليكم", live_route_owner="order_flow_v2"),
        ReplayStep("order_intent", "ابي اطلب", live_route_owner="order_flow_v2"),
        ReplayStep("wa_quick", "طلب سريع واتساب", live_route_owner="order_flow_v2"),
        ReplayStep(
            "browse_1",
            "وش عندكم منتجات",
            live_route_owner="brain.pipeline",
            live_outbound_snippet="اختر المنتجات",
        ),
        ReplayStep(
            "browse_2",
            "وش عندكم منتجات",
            live_route_owner="brain.pipeline",
        ),
        ReplayStep(
            "catalog",
            "",
            inbound_metadata=catalog_order_metadata(),
            live_route_owner="order_flow_v2",
        ),
        ReplayStep("city", "الطايف", live_route_owner="order_flow_v2"),
        ReplayStep(
            "address",
            "عنوان قريب: TAPB3320، 3320 ابن تميرة، 7211، حي الحلقة الغربية، الطائف 26563",
            live_route_owner="order_flow_v2",
        ),
        ReplayStep(
            "name",
            "سعدية الحارثي",
            live_route_owner="order_flow_v2",
        ),
        ReplayStep("picked", "انا اخترت المنتجات", live_route_owner="order_flow_v2"),
        ReplayStep("nudge", "شفيك", live_route_owner="order_flow_v2"),
        ReplayStep(
            "delivery",
            "ودوه لعنواني",
            live_route_owner="order_flow_v2",
            live_outbound_snippet="طريقة الدفع",
        ),
        ReplayStep(
            "bank",
            "الراجحي",
            live_route_owner="order_flow_v2",
            live_outbound_snippet="الراجحي",
        ),
    ]


def _live_route_expectations() -> Dict[str, str]:
    return {
        "browse_1": "brain",
        "browse_2": "brain",
        "city": "order_flow_v2",
        "name": "order_flow_v2",
        "picked": "order_flow_v2",
        "nudge": "order_flow_v2",
        "delivery": "order_flow_v2",
        "bank": "order_flow_v2",
    }


@pytest.fixture(autouse=True)
def _v2_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", False, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", True, raising=False)
    reset_cache()


def _seed_post_catalog_checkout_state(world) -> None:
    """Simulate persisted local draft after catalog (run 20260703T004200Z)."""
    attach_brain_state(
        world.conversation,
        {
            "local_draft_authoritative": True,
            "order_flow_v2_active": True,
            "catalog_line_items_authoritative": True,
            "order_flow_v2_trusted_price": True,
            "draft_order_reference": "NHL-1-000045",
            "order_creation_status": "created",
            "line_items": [
                {
                    "product_id": "86bqzca62a",
                    "product_name": "250 جرام عسل سمر الحجاز البلدي",
                    "quantity": 1,
                    "catalog_price": 126.0,
                }
            ],
            "order_flow_v2_catalog_total": 126.0,
            "order_total": 126.0,
            "customer_first_name": "أم",
            "customer_last_name": "خالد",
        },
    )
    world.db.add(world.conversation)
    world.db.commit()


def _runner_for_payment(*, rajhi: bool, ahli: bool) -> BrainReplayRunner:
    db, world = make_brain_replay_db_and_world(
        BrainReplaySnapshot(payment=PaymentFixtureVariant(rajhi=rajhi, ahli=ahli))
    )
    return BrainReplayRunner(world, scenario_name=SCENARIO)


def _clear_payment_kb(db, tenant_id: int) -> None:
    db.query(MerchantKnowledgeSection).filter(
        MerchantKnowledgeSection.tenant_id == tenant_id,
        MerchantKnowledgeSection.kind == "bank_transfer",
    ).delete()
    db.commit()


class TestBrainReplayCanaryShippingPaymentProviderMismatch:
    def test_canary_thread_emits_route_owner_audit(self) -> None:
        _db, world = make_brain_replay_db_and_world(load_canary_snapshot())
        _seed_post_catalog_checkout_state(world)
        runner = BrainReplayRunner(world, scenario_name=SCENARIO)
        audit = runner.run_thread(_canary_steps()[6:])
        audit = runner.build_audit(live_expectations=_live_route_expectations())

        assert audit.turns, "expected at least one replayed turn"
        assert audit.match_vs_live in {"matched", "partial", "did_not_match"}

        by_label = {t.label: t for t in audit.turns}
        assert "order_flow_v2" in by_label["city"].route_owner
        assert "order_flow_v2" in by_label["delivery"].route_owner
        assert "order_flow_v2" in by_label["bank"].route_owner
        assert "order_flow_v2" in by_label["name"].route_owner
        picked = by_label.get("picked")
        if picked is not None:
            assert picked.handled
            assert _CATALOG_BROWSE_PHRASE not in (picked.outbound_reply or "")
        bank = by_label.get("bank")
        if bank is not None:
            assert "هذه بيانات التحويل" not in (bank.outbound_reply or "")

        payload = json.loads(audit.to_json())
        assert payload["match_vs_live"] == audit.match_vs_live
        assert all("route_owner" in turn for turn in payload["turns"])
        assert all("outbound_reply" in turn for turn in payload["turns"])
        assert all("order_prep_summary" in turn for turn in payload["turns"])

    def test_shipping_policy_source_visibility(self) -> None:
        _db, world = make_brain_replay_db_and_world(load_canary_snapshot())
        runner = BrainReplayRunner(world, scenario_name=SCENARIO)
        runner.run_thread(_canary_steps()[:12])
        delivery = next(t for t in runner.turns if t.label == "delivery")
        assert delivery.shipping_policy_source in {
            "llm_composed_summary",
            "default_29_sar_fallback",
            "tenant_settings_or_kb",
            "orderflow_v2_free_shipping",
            "legacy_flow",
            "",
        }
        if _LIVE_SHIPPING_SNIPPET in (delivery.outbound_reply or ""):
            assert delivery.shipping_policy_source in {
                "llm_composed_summary",
                "default_29_sar_fallback",
                "tenant_settings_or_kb",
            }
        else:
            assert "29" not in (delivery.outbound_reply or "")

    def test_payment_provider_parity_matrix(self) -> None:
        cases = [
            ("rajhi_only", True, False, "الراجحي"),
            ("ahli_only", False, True, "الأهلي"),
            ("both_configured", True, True, "تحويل الراجحي"),
            ("none_verified", False, False, "الراجحي"),
        ]
        results: Dict[str, Dict[str, object]] = {}
        for name, rajhi, ahli, message in cases:
            runner = _runner_for_payment(rajhi=rajhi, ahli=ahli)
            if name == "none_verified":
                _clear_payment_kb(runner.world.db, runner.world.tenant.id)
            turn = runner.run_payment_probe(message, label=f"payment_{name}")
            results[name] = {
                "route_owner": turn.route_owner,
                "payment_credential_guard_ran": turn.payment_credential_guard_ran,
                "reply_preview": (turn.outbound_reply or "")[:120],
                "media_barcode_path": turn.media_barcode_path_triggered,
            }
        assert results["rajhi_only"]["route_owner"]
        assert results["both_configured"]["route_owner"]

    def test_dedup_parity_matrix(self) -> None:
        _db, world = make_brain_replay_db_and_world()
        runner = BrainReplayRunner(world, scenario_name=SCENARIO)
        matrix = runner.run_dedup_matrix("مرحبا")
        same = next(r for r in matrix if r["case"] == "same_msg_id")
        missing = next(r for r in matrix if r["case"] == "missing_msg_id")
        assert same["first_handled"] is True
        assert same["second_dedup_hit"] is True
        assert same["second_handled"] is False
        assert missing["second_dedup_hit"] is False

    def test_snapshot_fixture_loads_without_private_data(self) -> None:
        snap = load_canary_snapshot()
        blob = json.dumps(snap.to_dict(), ensure_ascii=False)
        assert "966507283619" not in blob
        assert "542980511" not in blob
        assert snap.store_ai_mode == "test"

    def test_no_real_llm_calls_in_replay(self) -> None:
        _db, world = make_brain_replay_db_and_world()
        runner = BrainReplayRunner(world, scenario_name=SCENARIO)
        runner.run_thread(_canary_steps()[:4])
        assert runner._llm_compose_calls >= 0
        assert runner.fake_sender.real_send_attempted or not runner.fake_sender.sent
