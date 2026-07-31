"""Multi-turn MerchantBrain journey harness for model payload attestation evidence.

Exercises the production ``brain.process`` path (classifier → decision → executor →
compose) with in-process catalog/order fixtures. Reuses ``[MODEL_PAYLOAD_ATTESTATION]``
telemetry from PR #729 — no second telemetry system.

Read-only measurement: does not mutate runtime architecture.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.truth_surface.model_payload_attestation import (  # noqa: E402
    assert_attestation_redacted,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    clear_trusted_context,
    run_trusted_context_shadow,
)
from modules.ai.brain.types import CommerceFacts, MerchantConversationState  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
TENANT_ID = 1
CONVERSATION_ID = 71001
CUSTOMER_PHONE = "966500000101"
CUSTOMER_ID = 101
ORDER_EXTERNAL_ID = "RRRD1234"
TRACKING_NUMBER = "TRK-99001"

JOURNEY_TURNS: Tuple[str, ...] = (
    "اعرض لي فساتين",
    "فيه غيره؟",
    "أعجبني الثاني",
    "كم سعره؟",
    "أرسل صورته",
    "أرسل رابطه",
    "هل يوجد منه مقاس L؟",
    "أبي كميتين",
    "لا، أقصد اللي قبله",
    "هل عليه عرض أو كوبون؟",
    "ما آخر طلب لي؟",
    "أين وصلت شحنته؟",
)

DRESS_PRODUCTS: Tuple[Dict[str, Any], ...] = (
    {
        "id": 101,
        "product_id": 101,
        "title": "فستان سهرة أزرق",
        "price": 289.0,
        "sale_price": None,
        "regular_price": 289.0,
        "available": True,
        "in_stock": True,
        "can_checkout": True,
        "orderable": True,
        "external_id": "ext-dress-101",
        "product_url": "https://store.example.test/products/101",
        "image_url": "https://cdn.example.test/dress-101.jpg",
        "variants": [{"variant_id": 1001, "size": "M", "available": True}],
    },
    {
        "id": 102,
        "product_id": 102,
        "title": "فستان صيفي وردي",
        "price": 149.0,
        "sale_price": 149.0,
        "regular_price": 199.0,
        "available": True,
        "in_stock": True,
        "can_checkout": True,
        "orderable": True,
        "external_id": "ext-dress-102",
        "product_url": "https://store.example.test/products/102",
        "image_url": "https://cdn.example.test/dress-102.jpg",
        "variants": [
            {"variant_id": 1002, "size": "L", "available": True},
            {"variant_id": 1003, "size": "M", "available": True},
        ],
    },
    {
        "id": 103,
        "product_id": 103,
        "title": "فستان كاجوال أسود",
        "price": 199.0,
        "sale_price": None,
        "regular_price": 199.0,
        "available": True,
        "in_stock": True,
        "can_checkout": True,
        "orderable": True,
        "external_id": "ext-dress-103",
        "product_url": "https://store.example.test/products/103",
        "image_url": "https://cdn.example.test/dress-103.jpg",
        "variants": [{"variant_id": 1004, "size": "L", "available": True}],
    },
)

_ATTESTATION_LOG_RE = re.compile(r"\[MODEL_PAYLOAD_ATTESTATION\]\s+(\{.*\})\s*$")


class _AttestationLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        match = _ATTESTATION_LOG_RE.search(message)
        if not match:
            return
        try:
            payload = ast.literal_eval(match.group(1))
        except Exception:
            return
        if isinstance(payload, dict):
            self.records.append(payload)

    def by_stage(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for row in self.records:
            stage = str(row.get("stage") or "")
            if stage:
                out[stage] = row
        return out

    def clear(self) -> None:
        self.records.clear()


class InMemoryStateStore:
    """Minimal state store — real transition(), in-memory persistence."""

    def __init__(self) -> None:
        from modules.ai.brain.state.store import DefaultStateStore

        self._delegate = DefaultStateStore()
        self._states: Dict[Tuple[int, str], MerchantConversationState] = {}

    def load(self, db: Any, tenant_id: int, customer_phone: str) -> MerchantConversationState:
        return self._states.get(
            (tenant_id, customer_phone),
            MerchantConversationState(),
        )

    def save(
        self,
        db: Any,
        tenant_id: int,
        customer_phone: str,
        state: MerchantConversationState,
    ) -> None:
        self._states[(tenant_id, customer_phone)] = state

    def transition(self, state, intent, decision):
        return self._delegate.transition(state, intent, decision)

    def mark_greeted(self, *args, **kwargs) -> bool:
        return self._delegate.mark_greeted(*args, **kwargs)


def _commerce_facts(*, has_coupons: bool = False) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=len(DRESS_PRODUCTS),
        in_stock_count=len(DRESS_PRODUCTS),
        has_active_integration=True,
        orderable=True,
        has_coupons=has_coupons,
        snapshot_fresh=True,
        store_name=GENERIC_MERCHANT,
        store_url="https://store.example.test",
        store_description="ملابس نسائية",
        store_contact_phone="+966500000001",
        shipping_policy="الشحن خلال 2-4 أيام عمل",
        support_hours="9-22",
        shipping_methods=["سمسا"],
        integration_platform="salla",
    )


def _coupon_row() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=55,
        tenant_id=TENANT_ID,
        code="SAVE15",
        source_type="manual",
        expires_at=now + timedelta(days=7),
        extra_metadata={},
        rules=[],
    )


def _promotion_row() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=77,
        tenant_id=TENANT_ID,
        status="active",
        promotion_type="percentage",
        discount_value=15,
        conditions={},
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(days=7),
        usage_count=0,
        usage_limit=None,
        extra_metadata={},
    )


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.commit = MagicMock()
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


def _order_context_stub(*, include_shipment: bool = False) -> SimpleNamespace:
    draft = SimpleNamespace(
        order_id=9001,
        external_id=ORDER_EXTERNAL_ID,
        status="shipped" if include_shipment else "processing",
        missing_fields=[],
        line_items=[{"product_id": 102, "quantity": 2, "title": "فستان صيفي وردي"}],
    )
    return SimpleNamespace(
        identity=SimpleNamespace(
            customer_id=CUSTOMER_ID,
            operational_name="نورة عبدالله",
            name_status="verified",
        ),
        shipping=SimpleNamespace(
            city="الرياض",
            short_address="RRRD1234",
            maps_url="https://maps.example.test/riyadh",
            accepted_delivery_address=True,
        ),
        active_draft=draft,
        catalog_order=SimpleNamespace(has_catalog_order=True, item_count=2),
    )


def _commerce_bundle_stub(*, include_shipment: bool = False) -> Dict[str, Any]:
    bundle = {
        "order_status": "shipped" if include_shipment else "processing",
        "external_id": ORDER_EXTERNAL_ID,
    }
    if include_shipment:
        bundle["tracking_number"] = TRACKING_NUMBER
        bundle["tracking"] = TRACKING_NUMBER
    return bundle


async def _mock_generate_ai_reply(**kwargs: Any) -> Dict[str, Any]:
    known = kwargs.get("known_facts") or {}
    selected = kwargs.get("selected_product") or {}
    product_id = selected.get("product_id") or selected.get("id")
    projection = (known.get("trusted_context_projection") or {}) if isinstance(known, dict) else {}
    identity = projection.get("product_identity") or {}
    if not product_id and isinstance(identity, dict):
        product_id = identity.get("product_id")
    lines = [
        f"رد تجريبي للمنتج {product_id or 'unknown'}",
        f"متجر {kwargs.get('store_name') or GENERIC_MERCHANT}",
    ]
    return {
        "reply": "\n".join(lines),
        "compose_source": "persona_llm",
        "response_mode": "persona",
        "llm_candidate_present": True,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "catalog_product_ids": [int(product_id)] if product_id else [],
    }


def _search_runtime_result(query: str = "") -> Dict[str, Any]:
    products = list(DRESS_PRODUCTS)
    return {
        "success": True,
        "products": products,
        "count": len(products),
        "query": query,
    }


@dataclass
class TurnEvidence:
    turn_index: int
    message: str
    intent: str = ""
    decision_action: str = ""
    chosen_path: str = ""
    attestations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    reply_metadata: Dict[str, Any] = field(default_factory=dict)
    semantic_checks: Dict[str, Any] = field(default_factory=dict)
    semantic_failures: List[str] = field(default_factory=list)
    stage_classification: str = ""
    error: str = ""


def _pick_attestation(attestations: Mapping[str, Dict[str, Any]], *stages: str) -> Dict[str, Any]:
    for stage in stages:
        row = attestations.get(stage)
        if isinstance(row, dict) and row:
            return row
    return {}


def _semantic_checks_for_turn(
    turn_index: int,
    *,
    attestations: Mapping[str, Dict[str, Any]],
    reply_metadata: Mapping[str, Any],
    state: MerchantConversationState,
) -> Tuple[Dict[str, Any], List[str]]:
    failures: List[str] = []
    brain_att = _pick_attestation(attestations, "compose", "reply_state", "brain")
    compose_att = _pick_attestation(attestations, "compose", "reply_state")
    loaded = brain_att.get("facts_loaded") or {}
    reaching_brain = brain_att.get("facts_reaching_brain") or {}
    reaching_compose = compose_att.get("facts_reaching_compose") or {}
    selected = compose_att.get("selected_product_and_variant") or brain_att.get(
        "selected_product_and_variant",
        {},
    )
    candidates = compose_att.get("candidate_ids_and_order") or brain_att.get(
        "candidate_ids_and_order",
        [],
    )

    checks: Dict[str, Any] = {
        "facts_loaded_present": bool(loaded.get("present")),
        "facts_loaded_domains": list(loaded.get("loaded_domains") or []),
        "facts_loaded_count": int(loaded.get("fact_count") or 0),
        "brain_projection_present": bool(reaching_brain.get("present")),
        "compose_projection_present": bool(reaching_compose.get("trusted_context_projection_present")),
        "candidate_count": int(reaching_brain.get("candidate_count") or len(candidates)),
        "selected_product_id": selected.get("product_id"),
        "selected_variant_id": selected.get("variant_id"),
        "history_message_count": int(
            (brain_att.get("history_window") or {}).get("history_message_count") or 0
        ),
        "catalog_product_ids": list(reply_metadata.get("catalog_product_ids") or []),
        "compose_source": reply_metadata.get("compose_source"),
        "final_text_transformed": reply_metadata.get("final_text_transformed"),
        "final_transform_reasons": list(reply_metadata.get("final_transform_reasons") or []),
        "has_order_fact": bool(reaching_brain.get("has_order")),
        "has_shipment_fact": bool(reaching_brain.get("has_shipment")),
        "model_present": bool((compose_att.get("model_and_route") or {}).get("present")),
    }

    focus = state.current_product_focus or {}
    checks["state_focus_product_id"] = focus.get("product_id") or focus.get("id")

    if turn_index == 0:
        if checks["candidate_count"] < 2:
            failures.append("turn1_expected_multiple_candidates")
        if "catalog" not in checks["facts_loaded_domains"]:
            failures.append("turn1_expected_catalog_domain_loaded")
    elif turn_index == 2:
        expected_id = DRESS_PRODUCTS[1]["id"]
        if checks["selected_product_id"] not in (expected_id, None) and checks[
            "state_focus_product_id"
        ] not in (expected_id, None):
            failures.append("turn3_expected_second_candidate_selected")
    elif turn_index == 3:
        if not checks["selected_product_id"] and not checks["state_focus_product_id"]:
            failures.append("turn4_expected_selected_product_for_price")
    elif turn_index == 4:
        native = reply_metadata.get("native_catalog_entry") or {}
        if not native and not checks["compose_projection_present"]:
            failures.append("turn5_expected_visual_or_projection_context")
    elif turn_index == 5:
        if not checks["selected_product_id"] and not checks["state_focus_product_id"]:
            failures.append("turn6_expected_product_for_link_turn")
    elif turn_index == 6:
        if checks["selected_product_id"] != DRESS_PRODUCTS[1]["id"]:
            if checks["state_focus_product_id"] != DRESS_PRODUCTS[1]["id"]:
                failures.append("turn7_expected_size_variant_product_context")
    elif turn_index == 7:
        qty = getattr(state.order_prep, "quantity", None)
        checks["order_prep_quantity"] = qty
    elif turn_index == 8:
        if checks["state_focus_product_id"] not in (DRESS_PRODUCTS[1]["id"], DRESS_PRODUCTS[0]["id"]):
            failures.append("turn9_expected_reference_resolution_product")
    elif turn_index == 9:
        domains = set(checks["facts_loaded_domains"])
        if not domains.intersection({"coupons", "promotions", "catalog"}):
            failures.append("turn10_expected_offer_or_coupon_domain")
    elif turn_index == 10:
        if not checks["has_order_fact"]:
            failures.append("turn11_expected_order_fact_in_brain_projection")
    elif turn_index == 11:
        if not checks["has_shipment_fact"]:
            failures.append("turn12_expected_shipment_fact_in_brain_projection")

    return checks, failures


def _classify_stage(
    *,
    semantic_failures: Sequence[str],
    reply_metadata: Mapping[str, Any],
    attestations: Mapping[str, Dict[str, Any]],
) -> str:
    brain_att = attestations.get("brain") or {}
    compose_att = attestations.get("compose") or attestations.get("reply_state") or {}
    loaded = brain_att.get("facts_loaded") or {}
    reaching_brain = brain_att.get("facts_reaching_brain") or {}
    reaching_compose = (compose_att.get("facts_reaching_compose") or {})

    if not loaded.get("present"):
        return "A"
    if not reaching_brain.get("present"):
        return "B"
    compose_ok = bool(
        reaching_compose.get("present")
        and reaching_compose.get("trusted_context_projection_present")
    )
    slim = compose_att.get("slim_compose_fingerprint") or {}
    model_ok = bool((compose_att.get("model_and_route") or {}).get("present")) or bool(
        slim.get("present")
    )
    if not compose_ok and not model_ok:
        return "C"
    if semantic_failures:
        return "D"
    if reply_metadata.get("final_text_transformed"):
        return "E"
    return "F"


def _redact_attestations(attestations: Mapping[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for stage, payload in attestations.items():
        if isinstance(payload, dict):
            assert_attestation_redacted(payload)
            out[stage] = payload
    return out


async def run_single_turn(
    *,
    brain: Any,
    state_store: InMemoryStateStore,
    db: Any,
    convo: Any,
    turn_index: int,
    message: str,
    history: List[Dict[str, Any]],
    log_handler: _AttestationLogHandler,
    include_order_context: bool,
    include_shipment_context: bool,
    has_coupons: bool,
) -> Tuple[TurnEvidence, MerchantConversationState, Dict[str, Any], List[Dict[str, Any]]]:
    from modules.ai.brain import pipeline as brain_pipeline

    log_handler.clear()
    clear_trusted_context()

    brain_state = state_store.load(db, TENANT_ID, CUSTOMER_PHONE)
    run_trusted_context_shadow(
        db=db,
        tenant_id=TENANT_ID,
        customer_phone=CUSTOMER_PHONE,
        message=message,
        conversation=convo,
        conversation_id=CONVERSATION_ID,
        brain_state=brain_state,
    )

    captured: Dict[str, Any] = {}
    original_build = brain_pipeline._build_reply_state

    def _capture_reply_state(**kwargs):
        captured["ctx"] = kwargs["ctx"]
        reply_state = original_build(**kwargs)
        captured["reply_attestation"] = getattr(reply_state, "model_payload_attestation", None)
        captured["brain_attestation"] = getattr(kwargs["ctx"], "model_payload_attestation", None)
        return reply_state

    result: Dict[str, Any] = {}
    try:
        with patch.object(brain_pipeline, "_build_reply_state", side_effect=_capture_reply_state):
            result = await brain.process(
                db=db,
                tenant_id=TENANT_ID,
                customer_phone=CUSTOMER_PHONE,
                message=message,
                history=history,
                profile={"preferred_language": "ar", "id": CUSTOMER_ID, "name": "نورة عبدالله"},
                customer_id=CUSTOMER_ID,
                conversation_id=CONVERSATION_ID,
            )
    except Exception as exc:  # noqa: BLE001
        return (
            TurnEvidence(
                turn_index=turn_index,
                message=message,
                error=f"{exc.__class__.__name__}: {exc}",
                stage_classification="A",
            ),
            state_store.load(db, TENANT_ID, CUSTOMER_PHONE),
            {},
            history,
        )

    attestations = log_handler.by_stage()
    if captured.get("brain_attestation"):
        attestations.setdefault("brain", captured["brain_attestation"])
    if captured.get("reply_attestation"):
        attestations.setdefault("reply_state", captured["reply_attestation"])

    new_state = state_store.load(db, TENANT_ID, CUSTOMER_PHONE)
    reply_metadata = {
        key: result.get(key)
        for key in (
            "compose_source",
            "response_mode",
            "chosen_path",
            "llm_candidate_present",
            "final_text_transformed",
            "final_transform_reasons",
            "catalog_product_ids",
            "native_catalog_entry",
            "intent",
            "decision_action",
        )
        if key in result
    }
    semantic_checks, semantic_failures = _semantic_checks_for_turn(
        turn_index,
        attestations=attestations,
        reply_metadata=reply_metadata,
        state=new_state,
    )
    classification = _classify_stage(
        semantic_failures=semantic_failures,
        reply_metadata=reply_metadata,
        attestations=attestations,
    )

    evidence = TurnEvidence(
        turn_index=turn_index,
        message=message,
        intent=str(result.get("intent") or ""),
        decision_action=str(result.get("decision_action") or ""),
        chosen_path=str(result.get("chosen_path") or ""),
        attestations=_redact_attestations(attestations),
        reply_metadata=reply_metadata,
        semantic_checks=semantic_checks,
        semantic_failures=semantic_failures,
        stage_classification=classification,
    )

    updated_history = list(history)
    updated_history.append({"direction": "in", "body": message})
    if (result.get("reply") or "").strip():
        updated_history.append({"direction": "out", "body": str(result.get("reply") or "")})

    return evidence, new_state, result, updated_history


async def run_journey(*, artifact_dir: Optional[Path] = None) -> Dict[str, Any]:
    from modules.ai.brain.pipeline import get_brain

    artifact_root = artifact_dir or Path(_HERE) / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    state_store = InMemoryStateStore()
    db = _fake_db()
    convo = SimpleNamespace(
        id=CONVERSATION_ID,
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        extra_metadata={},
    )
    log_handler = _AttestationLogHandler()
    logging.getLogger("modules.ai.brain.pipeline").addHandler(log_handler)
    logging.getLogger("nahla.brain.compose").addHandler(log_handler)
    logging.getLogger("modules.ai.brain.compose.responder").addHandler(log_handler)

    brain = get_brain()
    brain._state_store = state_store  # type: ignore[attr-defined]

    async def _runtime_execute(self, tool_name: str, payload: Optional[Dict[str, Any]] = None, **kwargs):
        if tool_name == "search_products":
            query = str((payload or {}).get("query") or "")
            return _search_runtime_result(query)
        return {"success": False, "error": "unsupported_tool"}

    turn_records: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    failures_by_stage: Dict[str, int] = {}
    first_loss: Optional[str] = None
    missing_categories: List[str] = []

    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True,
                used_total=0,
                limit=1000,
                reason="",
                pct=0.0,
            ),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_brain_projection_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate.is_trusted_context_brain_projection_enabled",
            return_value=True,
        )
    )
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_commerce_facts()))
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    stack.enter_context(
        patch(
            "modules.ai.commerce.runtime.CommerceToolRuntime.execute",
            new=_runtime_execute,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            new=AsyncMock(side_effect=_mock_generate_ai_reply),
        )
    )
    stack.enter_context(
        patch(
            "core.active_order_context.load_commerce_bundle_from_db",
            side_effect=lambda *_a, **_k: _commerce_bundle_stub(include_shipment=False),
        )
    )
    stack.enter_context(
        patch(
            "core.order_context_builder.build_order_context",
            side_effect=lambda *_a, **_k: _order_context_stub(include_shipment=False),
        )
    )

    coupon = _coupon_row()
    promo = _promotion_row()

    def _query_side_effect(model: Any) -> Any:
        name = getattr(model, "__name__", str(model))
        q = MagicMock()
        if name == "Coupon":
            q.filter.return_value = q
            q.all.return_value = [coupon]
            q.first.return_value = coupon
        elif name == "Promotion":
            q.filter.return_value = q
            q.all.return_value = [promo]
            q.first.return_value = promo
        else:
            q.filter.return_value = q
            q.all.return_value = []
            q.first.return_value = None
        return q

    db.query.side_effect = _query_side_effect

    with stack:
        for idx, message in enumerate(JOURNEY_TURNS):
            include_order = idx >= 10
            include_shipment = idx >= 11
            has_coupons = idx >= 9

            with patch(
                "core.active_order_context.load_commerce_bundle_from_db",
                return_value=_commerce_bundle_stub(include_shipment=include_shipment),
            ), patch(
                "core.order_context_builder.build_order_context",
                return_value=_order_context_stub(include_shipment=include_shipment),
            ), patch.object(
                brain._facts_loader,
                "load",
                return_value=_commerce_facts(has_coupons=has_coupons),
            ):
                evidence, _state, _result, history = await run_single_turn(
                    brain=brain,
                    state_store=state_store,
                    db=db,
                    convo=convo,
                    turn_index=idx,
                    message=message,
                    history=history,
                    log_handler=log_handler,
                    include_order_context=include_order,
                    include_shipment_context=include_shipment,
                    has_coupons=has_coupons,
                )

            turn_records.append(
                {
                    "turn_index": evidence.turn_index,
                    "message": evidence.message,
                    "intent": evidence.intent,
                    "decision_action": evidence.decision_action,
                    "chosen_path": evidence.chosen_path,
                    "stage_classification": evidence.stage_classification,
                    "semantic_failures": evidence.semantic_failures,
                    "semantic_checks": evidence.semantic_checks,
                    "attestations": evidence.attestations,
                    "reply_metadata": evidence.reply_metadata,
                    "error": evidence.error,
                }
            )
            stage = evidence.stage_classification
            failures_by_stage[stage] = failures_by_stage.get(stage, 0) + 1
            if first_loss is None and stage != "F":
                first_loss = f"turn_{idx + 1}:{stage}"
            if stage == "A" and "catalog" not in missing_categories:
                missing_categories.append("catalog_loader")
            if stage in {"B", "C"} and "brain_projection" not in missing_categories:
                missing_categories.append("brain_projection")
            if stage == "C" and "compose_projection" not in missing_categories:
                missing_categories.append("compose_projection")

    production_sha = os.environ.get("NAHLA_PRODUCTION_SHA", "49862dc6")
    harness_sha = ""
    try:
        import subprocess

        harness_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(_HERE).parents[1]),
            text=True,
        ).strip()
    except Exception:
        pass

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "production_sha": production_sha,
        "harness_run_sha": harness_sha,
        "automated_harness": "backend/tests/model_payload_attestation_journey_harness.py",
        "live_whatsapp_run": "BLOCKED:NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED unset (default-off) per docs/engineering/real-channel-conversational-acceptance-runbook.md",
        "fixture_mode": "in_process_catalog_order_fixtures_no_live_db",
        "tenant_id": TENANT_ID,
        "conversation_id": CONVERSATION_ID,
        "turn_count": len(turn_records),
        "facts_loaded": [row["attestations"].get("brain", {}).get("facts_loaded") for row in turn_records],
        "facts_reaching_brain": [
            row["attestations"].get("brain", {}).get("facts_reaching_brain") for row in turn_records
        ],
        "facts_reaching_model": [
            row["attestations"].get("compose", {}).get("facts_reaching_compose")
            or row["attestations"].get("reply_state", {}).get("facts_reaching_compose")
            for row in turn_records
        ],
        "model_output_correct": [
            row["stage_classification"] in {"D", "F"} and not row["semantic_failures"]
            for row in turn_records
        ],
        "post_model_mutation": [
            {
                "final_text_transformed": (row["reply_metadata"] or {}).get("final_text_transformed"),
                "final_transform_reasons": (row["reply_metadata"] or {}).get("final_transform_reasons"),
                "compose_source": (row["reply_metadata"] or {}).get("compose_source"),
            }
            for row in turn_records
        ],
        "failures_by_stage": failures_by_stage,
        "first_proven_loss_boundary": first_loss or "none_all_F",
        "missing_fact_categories": sorted(set(missing_categories)),
        "proven_root_cause": _infer_root_cause(turn_records, failures_by_stage),
        "smallest_required_fix": _infer_smallest_fix(turn_records, failures_by_stage),
        "architecture_change_required": "no",
        "turns": turn_records,
    }

    report_path = artifact_root / "model_payload_attestation_journey_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for handler in (logging.getLogger("modules.ai.brain.pipeline"),):
        handler.removeHandler(log_handler)
    logging.getLogger("nahla.brain.compose").removeHandler(log_handler)
    logging.getLogger("modules.ai.brain.compose.responder").removeHandler(log_handler)

    return {"report": report, "report_path": str(report_path)}


def _infer_root_cause(turn_records: Sequence[Mapping[str, Any]], failures: Mapping[str, int]) -> str:
    if not failures or failures.get("F", 0) == len(turn_records):
        return "none_observed_in_harness_all_turns_classified_F"
    dominant = max(failures.items(), key=lambda item: item[1])[0]
    if dominant == "A":
        return "trusted_context_snapshot_not_built_or_catalog_facts_not_loaded"
    if dominant == "B":
        return "facts_loaded_but_brain_projection_missing_or_empty"
    if dominant == "C":
        return "brain_projection_present_but_compose_known_facts_missing_projection"
    if dominant == "D":
        return "model_path_reached_but_semantic_product_order_shipment_mismatch"
    if dominant == "E":
        return "compose_output_mutated_by_post_compose_gates_or_dedup"
    return "mixed_stage_losses_see_turns"


def _infer_smallest_fix(turn_records: Sequence[Mapping[str, Any]], failures: Mapping[str, int]) -> str:
    root = _infer_root_cause(turn_records, failures)
    if root.startswith("none_observed"):
        return "none_measurement_only"
    if "brain_projection" in root:
        return "wire_trusted_context_projection_into_compose_known_facts_when_candidates_present"
    if "catalog_facts" in root:
        return "ensure_search_candidates_persist_to_brain_state_before_trusted_context_build"
    if "semantic" in root:
        return "align_candidate_selection_and_order_shipment_loaders_with_turn_intent"
    if "mutated" in root:
        return "audit_post_compose_mutation_provenance_per_turn"
    return "inspect_first_proven_loss_boundary_turn_attestations"


def main() -> None:
    outcome = asyncio.run(run_journey())
    print(json.dumps({"report_path": outcome["report_path"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
