"""
Layer 3 — Human Dialogue Review harness.

Real ``DefaultComposer._llm_compose`` (no compose stubs). Wraps
``_handle_merchant_message`` with FakeWhatsAppSender capture.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union
from unittest.mock import MagicMock, patch

from commerce_scenario_fixtures import ScenarioWorld
from commerce_scenario_runner import FakeOutboundRecord, FakeWhatsAppSender, run_async
from models import Conversation, MessageEvent, Tenant

from tests.salla_acceptance.fixtures import TenantBundle
from tests.salla_acceptance.layer2_harness import (
    TurnStep,
    _mask_phone,
    _sanitize_args,
    scenario_world_from_bundle,
)
from tests.salla_acceptance.layer3_provider import apply_layer3_process_env


@dataclass
class Layer3ComposeSpy:
    call_count: int = 0
    last_reply_len: int = 0
    last_compose_source: str = ""
    last_model: str = ""
    last_provider: str = ""
    last_latency_ms: float = 0.0

    def record(self, *, reply: str, compose_source: str, model: str, provider: str, latency_ms: float) -> None:
        self.call_count += 1
        self.last_reply_len = len(reply or "")
        self.last_compose_source = compose_source
        self.last_model = model
        self.last_provider = provider
        self.last_latency_ms = latency_ms


COMPOSE_SPY = Layer3ComposeSpy()


@dataclass
class Layer3TurnEvidence:
    label: str = ""
    inbound_text: str = ""
    tenant_id: int = 0
    customer_phone: str = ""
    conversation_id: Optional[int] = None
    brain_called: bool = False
    intent: str = ""
    decision_action: str = ""
    decision_args: Dict[str, Any] = field(default_factory=dict)
    compose_invoked: int = 0
    compose_source: str = ""
    compose_model: str = ""
    compose_provider: str = ""
    latency_ms: float = 0.0
    raw_composed_reply: str = ""
    outbound_reply: str = ""
    catalog_product_ids: List[Any] = field(default_factory=list)
    price_source: str = ""
    knowledge_source: str = ""
    kb_section_ids: List[Any] = field(default_factory=list)
    guards: Dict[str, Any] = field(default_factory=dict)
    handoff_active: bool = False
    outbound_send_count: int = 0
    fake_outbound_bodies: List[str] = field(default_factory=list)
    brain_state_before: Dict[str, Any] = field(default_factory=dict)
    brain_state_after: Dict[str, Any] = field(default_factory=dict)
    dedup_hit: bool = False
    dedup_msg_id: str = ""
    skip_ai: bool = False
    tools_observed: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    outcome: str = "unknown"
    severity: str = "major"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Layer3BrainRunner:
    """Layer 2 webhook runner with real LLM compose (no stub patches)."""

    def __init__(
        self,
        world: ScenarioWorld,
        *,
        phone_id: str = "PH_L3",
        ownership_state: str = "ai_active",
        skip_ai: bool = False,
        disable_memory: bool = False,
        ownership_override: Optional[Callable[[], Any]] = None,
        stub_slot_extractor: bool = True,
    ) -> None:
        apply_layer3_process_env()
        self.world = world
        self.phone_id = phone_id
        self.ownership_state = ownership_state
        self.should_skip_ai = skip_ai
        self.disable_memory = disable_memory
        self.ownership_override = ownership_override
        self.stub_slot_extractor = stub_slot_extractor
        self.fake_sender = FakeWhatsAppSender()
        self.turns: List[Layer3TurnEvidence] = []
        self.errors: List[str] = []
        self._msg_counter = 0
        self._last_brain_result: Dict[str, Any] = {}
        self._brain_called = False
        self._compose_calls_at_turn_start = 0
        self._original_llm_compose = None
        self._original_legacy_compose = None

    def _next_msg_id(self, override: Optional[str] = None) -> str:
        if override:
            return override
        self._msg_counter += 1
        return f"wamid.layer3.{self._msg_counter}.{uuid.uuid4().hex[:8]}"

    def _brain_state_keys(self) -> Dict[str, Any]:
        convo = (
            self.world.db.query(Conversation)
            .filter_by(id=self.world.conversation.id)
            .one()
        )
        brain = dict((convo.extra_metadata or {}).get("brain_state") or {})
        prep = dict(brain.get("order_prep") or {})
        focus = brain.get("current_product_focus") or prep.get("current_product_focus") or {}
        snapshot: Dict[str, Any] = {"order_prep_keys": sorted(prep.keys())[:20]}
        if isinstance(focus, dict):
            snapshot["focus_product_id"] = focus.get("product_id")
            snapshot["focus_product_name"] = str(focus.get("name") or focus.get("title") or "")[:80]
        elif focus:
            snapshot["focus_product_id"] = focus
        return snapshot

    @contextmanager
    def _runtime_patches(self) -> Iterator[None]:
        import sys  # noqa: PLC0415

        import models as _models  # noqa: PLC0415

        sys.modules.setdefault("database.models", _models)

        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415
        from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: PLC0415
            apply_payment_credential_guard as _pcg,
        )
        from modules.ai.brain.postprocess.saudi_dialect_guard import (  # noqa: PLC0415
            apply_saudi_dialect_guard as _sdg,
        )

        runner = self
        self._original_llm_compose = DefaultComposer._llm_compose
        self._original_legacy_compose = DefaultComposer._legacy_llm_compose

        async def _spying_llm_compose(composer_self, ctx, result, *args, **kwargs):
            t0 = time.monotonic()
            text = await runner._original_llm_compose(
                composer_self, ctx, result, *args, **kwargs
            )
            latency = (time.monotonic() - t0) * 1000.0
            data = dict(getattr(result, "data", None) or {})
            COMPOSE_SPY.record(
                reply=str(text or ""),
                compose_source=str(data.get("compose_source") or ""),
                model=str(data.get("model_used") or data.get("llm_provider") or ""),
                provider=str(data.get("llm_provider") or ""),
                latency_ms=latency,
            )
            return text

        async def _spying_legacy_compose(composer_self, ctx, result, *args, **kwargs):
            t0 = time.monotonic()
            text = await runner._original_legacy_compose(
                composer_self, ctx, result, *args, **kwargs
            )
            latency = (time.monotonic() - t0) * 1000.0
            data = dict(getattr(result, "data", None) or {})
            COMPOSE_SPY.record(
                reply=str(text or ""),
                compose_source=str(data.get("compose_source") or "legacy"),
                model=str(data.get("model_used") or ""),
                provider=str(data.get("llm_provider") or ""),
                latency_ms=latency,
            )
            return text

        def _ownership(*_args, **_kwargs):
            if runner.ownership_override is not None:
                return runner.ownership_override()
            return MagicMock(state=runner.ownership_state, takeover_class="")

        def _skip_ai(*_args, **_kwargs):
            if runner.should_skip_ai:
                return True, MagicMock(reason="layer3_test_skip_ai")
            return False, None

        original_process = None
        brain_instance = None
        try:
            from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

            brain_instance = get_brain()
            original_process = brain_instance.process

            async def _capturing_process(*args, **kwargs):
                runner._brain_called = True
                result = await original_process(*args, **kwargs)
                if isinstance(result, dict):
                    runner._last_brain_result = dict(result)
                return result

            brain_instance.process = _capturing_process  # type: ignore[method-assign]
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"brain_capture_setup: {exc}")

        import services.turn_trace as _turn_trace  # noqa: PLC0415

        if not hasattr(_turn_trace, "SOURCE_FALLBACK"):
            _turn_trace.SOURCE_FALLBACK = "fallback"  # type: ignore[attr-defined]

        slot_patch = nullcontext()
        if self.stub_slot_extractor:
            from tests.salla_acceptance.layer2_compose_stubs import (  # noqa: PLC0415
                layer2_stub_extract_slots,
            )

            slot_patch = patch(
                "modules.ai.brain.intent.slot_extractor.extract_slots",
                new=layer2_stub_extract_slots,
            )

        memory_patch = (
            patch("modules.ai.brain.memory.updater.DefaultMemoryUpdater.update")
            if self.disable_memory
            else nullcontext()
        )

        with self.fake_sender.patch(), patch(
            "core.billing.has_billing_access",
            return_value=True,
        ), patch(
            "modules.ai.routing.conversation_mode.resolve_conversation_mode",
            return_value=MagicMock(
                lease=MagicMock(),
                to_log_dict=lambda: {},
            ),
        ), patch(
            "modules.ai.routing.conversation_mode.save_lease",
        ), patch(
            "core.ownership_state.resolve_ownership_state",
            side_effect=_ownership,
        ), patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=MagicMock(released=False, reason=""),
        ), patch(
            "core.ai_pause_guard.should_skip_ai",
            side_effect=_skip_ai,
        ), patch.object(
            DefaultComposer,
            "_llm_compose",
            new=_spying_llm_compose,
        ), patch.object(
            DefaultComposer,
            "_legacy_llm_compose",
            new=_spying_legacy_compose,
        ), patch(
            "modules.ai.brain.postprocess.payment_credential_guard.apply_payment_credential_guard",
            new=_pcg,
        ), patch(
            "modules.ai.brain.postprocess.saudi_dialect_guard.apply_saudi_dialect_guard",
            new=_sdg,
        ), slot_patch, memory_patch:
            try:
                yield
            finally:
                if brain_instance is not None and original_process is not None:
                    brain_instance.process = original_process  # type: ignore[method-assign]

    def _list_outbound_since(self, after_id: int) -> List[MessageEvent]:
        return (
            self.world.db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == self.world.tenant.id,
                MessageEvent.conversation_id == self.world.conversation.id,
                MessageEvent.direction == "outbound",
                MessageEvent.id > after_id,
            )
            .order_by(MessageEvent.id.asc())
            .all()
        )

    def _check_dedup(self, provider_msg_id: str) -> bool:
        from core.inbound_dedup import is_duplicate_inbound  # noqa: PLC0415

        return bool(
            is_duplicate_inbound(
                phone_number_id=self.phone_id,
                msg_id=provider_msg_id,
            )
        )

    async def _run_webhook_turn(
        self,
        *,
        label: str,
        text: str,
        inbound_metadata: Optional[Dict[str, Any]] = None,
        provider_msg_id: Optional[str] = None,
    ) -> Layer3TurnEvidence:
        from routers.whatsapp_webhook import _handle_merchant_message  # noqa: PLC0415

        self._last_brain_result = {}
        self._brain_called = False
        self._compose_calls_at_turn_start = COMPOSE_SPY.call_count
        t_turn = time.monotonic()
        msg_id = self._next_msg_id(provider_msg_id)

        last_event = (
            self.world.db.query(MessageEvent)
            .filter_by(
                tenant_id=self.world.tenant.id,
                conversation_id=self.world.conversation.id,
            )
            .order_by(MessageEvent.id.desc())
            .first()
        )
        after_id = int(getattr(last_event, "id", 0) or 0)
        outbound_before = len(self.fake_sender.sent)
        state_before = self._brain_state_keys()
        dedup_hit = self._check_dedup(msg_id) if msg_id else False

        convo = (
            self.world.db.query(Conversation)
            .filter_by(id=self.world.conversation.id)
            .one()
        )
        handoff_active = bool(
            convo.is_human_handoff or convo.handoff_active or convo.needs_human
        )

        evidence = Layer3TurnEvidence(
            label=label,
            inbound_text=text,
            tenant_id=self.world.tenant.id,
            customer_phone=_mask_phone(self.world.phone),
            conversation_id=self.world.conversation.id,
            dedup_hit=dedup_hit,
            dedup_msg_id=msg_id,
            brain_state_before=state_before,
            handoff_active=handoff_active,
        )

        if dedup_hit:
            evidence.skip_ai = True
            evidence.outcome = "pass"
            evidence.brain_state_after = state_before
            self.turns.append(evidence)
            return evidence

        meta = dict(inbound_metadata or {})
        meta.setdefault("type", meta.get("inbound_normalized_type") or "text")

        try:
            await _handle_merchant_message(
                phone_id=self.phone_id,
                to=self.world.phone,
                text=text,
                tenant_id=self.world.tenant.id,
                db=self.world.db,
                inbound_metadata=meta or None,
                wa_msg_id=msg_id,
                wa_message_ts=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            evidence.errors.append(str(exc))
            self.errors.append(f"{label}: {exc}")

        out_events = self._list_outbound_since(after_id)
        new_fake: List[FakeOutboundRecord] = self.fake_sender.sent[outbound_before:]
        outbound_event = out_events[-1] if out_events else None
        outbound_meta = dict(getattr(outbound_event, "extra_metadata", None) or {})
        reply = ""
        if outbound_event is not None:
            reply = str(outbound_event.body or "")
        elif new_fake:
            reply = str(new_fake[-1].body or "")

        quality = dict(self._last_brain_result.get("quality_observability") or {})
        if not quality and outbound_meta.get("quality_observability"):
            quality = dict(outbound_meta.get("quality_observability") or {})

        evidence.brain_called = self._brain_called
        evidence.intent = str(
            self._last_brain_result.get("intent")
            or quality.get("intent")
            or outbound_meta.get("intent")
            or ""
        )
        evidence.decision_action = str(
            self._last_brain_result.get("decision_action")
            or quality.get("decision_action")
            or outbound_meta.get("decision_action")
            or ""
        )
        evidence.decision_args = _sanitize_args(
            self._last_brain_result.get("decision_args") or {}
        )
        evidence.compose_invoked = COMPOSE_SPY.call_count - self._compose_calls_at_turn_start
        evidence.compose_source = str(
            self._last_brain_result.get("compose_source")
            or outbound_meta.get("compose_source")
            or COMPOSE_SPY.last_compose_source
            or ""
        )
        evidence.compose_model = str(
            self._last_brain_result.get("model_used")
            or COMPOSE_SPY.last_model
            or ""
        )
        evidence.compose_provider = str(
            self._last_brain_result.get("llm_provider")
            or COMPOSE_SPY.last_provider
            or "openai_compatible"
        )
        evidence.latency_ms = round((time.monotonic() - t_turn) * 1000.0, 1)
        evidence.raw_composed_reply = str(self._last_brain_result.get("reply") or "")[:500]
        evidence.outbound_reply = reply
        evidence.catalog_product_ids = list(
            quality.get("catalog_product_ids")
            or self._last_brain_result.get("catalog_product_ids")
            or []
        )
        evidence.price_source = str(
            quality.get("price_source")
            or self._last_brain_result.get("price_source")
            or ""
        )
        evidence.knowledge_source = str(
            quality.get("knowledge_source")
            or self._last_brain_result.get("knowledge_source")
            or ""
        )
        evidence.kb_section_ids = list(
            self._last_brain_result.get("kb_section_ids")
            or outbound_meta.get("kb_section_ids")
            or []
        )
        evidence.guards = {
            "guards_triggered": list(quality.get("guards_triggered") or []),
            "final_turn_violations": list(quality.get("final_turn_violations") or []),
            "chosen_path": str(
                quality.get("chosen_path")
                or self._last_brain_result.get("chosen_path")
                or outbound_meta.get("chosen_path")
                or ""
            ),
        }
        evidence.outbound_send_count = len(new_fake) + len(out_events)
        evidence.fake_outbound_bodies = [str(r.body or "") for r in new_fake if r.body]
        evidence.brain_state_after = self._brain_state_keys()
        evidence.skip_ai = self.should_skip_ai
        evidence.tools_observed = list(
            self._last_brain_result.get("tools_used")
            or quality.get("tools_used")
            or []
        )

        self.turns.append(evidence)
        return evidence

    def run_turn(
        self,
        text: str,
        *,
        label: str = "",
        provider_msg_id: Optional[str] = None,
        inbound_metadata: Optional[Dict[str, Any]] = None,
    ) -> Layer3TurnEvidence:
        with self._runtime_patches():
            return run_async(
                self._run_webhook_turn(
                    label=label or text[:40],
                    text=text,
                    inbound_metadata=inbound_metadata,
                    provider_msg_id=provider_msg_id,
                )
            )

    def run_thread(self, steps: Sequence[TurnStep]) -> List[Layer3TurnEvidence]:
        results: List[Layer3TurnEvidence] = []
        for step in steps:
            if isinstance(step, str):
                results.append(self.run_turn(step))
            else:
                results.append(
                    self.run_turn(
                        str(step.get("text") or ""),
                        label=str(step.get("label") or step.get("text") or "")[:40],
                        provider_msg_id=step.get("provider_msg_id"),
                        inbound_metadata=step.get("inbound_metadata"),
                    )
                )
        return results


__all__ = [
    "COMPOSE_SPY",
    "Layer3BrainRunner",
    "Layer3ComposeSpy",
    "Layer3TurnEvidence",
]
