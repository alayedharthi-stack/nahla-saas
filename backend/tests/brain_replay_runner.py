"""
brain_replay_runner.py
──────────────────────
Full-thread WhatsApp replay through _handle_merchant_message with real
brain.pipeline routing (compose LLM stubbed for CI safety).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union
from unittest.mock import AsyncMock, MagicMock, patch

from brain_replay_compose_stubs import (
    stub_extract_slots,
    stub_legacy_llm_compose,
    stub_llm_compose,
)
from commerce_scenario_fixtures import ScenarioWorld
from commerce_scenario_runner import FakeOutboundRecord, FakeWhatsAppSender, run_async
from models import Conversation, Customer, MessageEvent


@dataclass
class ReplayStep:
    label: str
    text: str = ""
    inbound_metadata: Optional[Dict[str, Any]] = None
    provider_msg_id: Optional[str] = None
    live_route_owner: str = ""
    live_outbound_snippet: str = ""


@dataclass
class TurnAudit:
    label: str
    customer_text: str
    route_owner: str = ""
    handled: bool = False
    skip_brain: bool = False
    decision_action: str = ""
    decision_topic: str = ""
    state_patch_keys: List[str] = field(default_factory=list)
    order_prep_summary: Dict[str, Any] = field(default_factory=dict)
    outbound_kind: str = ""
    outbound_reply: str = ""
    payment_credential_guard_ran: bool = False
    saudi_dialect_guard_ran: bool = False
    shipping_policy_source: str = ""
    media_barcode_path_triggered: bool = False
    dedup_hit: bool = False
    dedup_msg_id: str = ""
    brain_called: bool = False
    errors: List[str] = field(default_factory=list)
    divergence_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrainReplayAudit:
    scenario: str = "brain_replay"
    match_vs_live: str = "unknown"
    turns: List[TurnAudit] = field(default_factory=list)
    divergence_reasons: List[str] = field(default_factory=list)
    payment_variants: Dict[str, Any] = field(default_factory=dict)
    dedup_runs: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "match_vs_live": self.match_vs_live,
            "turns": [t.to_dict() for t in self.turns],
            "divergence_reasons": self.divergence_reasons,
            "payment_variants": self.payment_variants,
            "dedup_runs": self.dedup_runs,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class _GuardTrace:
    payment_credential: bool = False
    saudi_dialect: bool = False

    def reset(self) -> None:
        self.payment_credential = False
        self.saudi_dialect = False


class BrainReplayRunner:
    """Replay inbound turns via the merchant webhook with brain.pipeline enabled."""

    def __init__(
        self,
        world: ScenarioWorld,
        *,
        phone_id: str = "PH_SCENARIO",
        scenario_name: str = "brain_replay",
    ) -> None:
        self.world = world
        self.phone_id = phone_id
        self.scenario_name = scenario_name
        self.fake_sender = FakeWhatsAppSender()
        self.turns: List[TurnAudit] = []
        self.errors: List[str] = []
        self._msg_counter = 0
        self._guard_trace = _GuardTrace()
        self._last_brain_result: Dict[str, Any] = {}
        self._brain_called = False
        self._llm_compose_calls = 0

    def _next_msg_id(self, override: Optional[str] = None) -> str:
        if override:
            return override
        self._msg_counter += 1
        return f"wamid.brain_replay.{self._msg_counter}.{uuid.uuid4().hex[:8]}"

    @contextmanager
    def _runtime_patches(self) -> Iterator[None]:
        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415
        from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: PLC0415
            apply_payment_credential_guard as _pcg,
        )
        from modules.ai.brain.postprocess.saudi_dialect_guard import (  # noqa: PLC0415
            apply_saudi_dialect_guard as _sdg,
        )

        runner = self

        async def _counting_stub_llm(*args, **kwargs):
            runner._llm_compose_calls += 1
            return await stub_llm_compose(*args, **kwargs)

        async def _counting_stub_legacy(*args, **kwargs):
            runner._llm_compose_calls += 1
            return await stub_legacy_llm_compose(*args, **kwargs)

        def _pcg_wrap(*args, **kwargs):
            runner._guard_trace.payment_credential = True
            return _pcg(*args, **kwargs)

        def _sdg_wrap(*args, **kwargs):
            runner._guard_trace.saudi_dialect = True
            return _sdg(*args, **kwargs)

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

        token_ctx = MagicMock(token="tok", source="test")
        import services.turn_trace as _turn_trace  # noqa: PLC0415

        if not hasattr(_turn_trace, "SOURCE_FALLBACK"):
            _turn_trace.SOURCE_FALLBACK = "fallback"  # type: ignore[attr-defined]

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
            return_value=MagicMock(state="ai_active", takeover_class=""),
        ), patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=MagicMock(released=False, reason=""),
        ), patch(
            "core.ai_pause_guard.should_skip_ai",
            return_value=(False, None),
        ), patch.object(
            DefaultComposer,
            "_llm_compose",
            new=_counting_stub_llm,
        ), patch.object(
            DefaultComposer,
            "_legacy_llm_compose",
            new=_counting_stub_legacy,
        ), patch(
            "modules.ai.brain.intent.slot_extractor.extract_slots",
            new=stub_extract_slots,
        ), patch(
            "modules.ai.brain.postprocess.payment_credential_guard.apply_payment_credential_guard",
            new=_pcg_wrap,
        ), patch(
            "modules.ai.brain.postprocess.saudi_dialect_guard.apply_saudi_dialect_guard",
            new=_sdg_wrap,
        ), patch(
            "modules.ai.order_flow_v2.owner.build_line_items_from_payload",
            side_effect=self._line_items_from_catalog_meta,
        ):
            try:
                yield
            finally:
                if brain_instance is not None and original_process is not None:
                    brain_instance.process = original_process  # type: ignore[method-assign]

    def _line_items_from_catalog_meta(self, payload: Any) -> Any:
        from types import SimpleNamespace  # noqa: PLC0415

        from brain_replay_fixtures import GENERIC_PRODUCT_A, GENERIC_PRODUCT_B  # noqa: PLC0415

        return SimpleNamespace(
            line_items=[
                {
                    "product_name": GENERIC_PRODUCT_A["title"],
                    "quantity": 1,
                    "catalog_price": GENERIC_PRODUCT_A["catalog_price"],
                    "price_source": "whatsapp_catalog",
                },
                {
                    "product_name": GENERIC_PRODUCT_B["title"],
                    "quantity": 1,
                    "catalog_price": GENERIC_PRODUCT_B["catalog_price"],
                    "price_source": "whatsapp_catalog",
                },
            ],
            unmatched_count=0,
        )

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

    def _order_prep_summary(self) -> Dict[str, Any]:
        convo = (
            self.world.db.query(Conversation)
            .filter_by(id=self.world.conversation.id)
            .one()
        )
        brain = dict((convo.extra_metadata or {}).get("brain_state") or {})
        prep = dict(brain.get("order_prep") or {})
        keys = (
            "city",
            "customer_first_name",
            "customer_last_name",
            "free_shipping",
            "payment_method",
            "order_flow_v2_catalog_total",
            "delivery_address_status",
            "short_address_code",
            "awaiting_payment_receipt",
        )
        return {k: prep.get(k) for k in keys if k in prep}

    def _infer_route_owner(
        self,
        *,
        outbound_meta: Dict[str, Any],
        brain_called: bool,
        of2_reason: str,
    ) -> str:
        if outbound_meta.get("reply_owner"):
            return str(outbound_meta["reply_owner"])
        if outbound_meta.get("deterministic_path") == "inbound_fragment_guard":
            return "legacy.fragment_guard"
        if outbound_meta.get("fragment_guard_reason"):
            return "legacy.fragment_guard"
        if brain_called:
            action = str(self._last_brain_result.get("decision_action") or "")
            intent = str(self._last_brain_result.get("intent") or "")
            if "payment" in action or "payment" in intent:
                return "brain.pipeline/payment"
            return "brain.pipeline"
        if of2_reason:
            return f"order_flow_v2.{of2_reason}"
        return "unhandled"

    def _infer_outbound_kind(
        self,
        *,
        outbound_event: Optional[MessageEvent],
        fake_records: Sequence[FakeOutboundRecord],
    ) -> str:
        if fake_records:
            last = fake_records[-1]
            return str(last.type or "text")
        if outbound_event is not None:
            meta = dict(outbound_event.extra_metadata or {})
            return str(meta.get("outbound_kind") or meta.get("message_type") or "text")
        return ""

    def _infer_shipping_policy_source(
        self,
        *,
        reply: str,
        route_owner: str,
        order_prep: Dict[str, Any],
    ) -> str:
        text = reply or ""
        if order_prep.get("free_shipping") and any(
            tok in text for tok in ("مجاني", "مجانا", "free")
        ):
            return "orderflow_v2_free_shipping"
        if "29" in text:
            if route_owner.startswith("brain"):
                return "llm_composed_summary"
            if route_owner.startswith("order_flow_v2"):
                return "tenant_settings_or_kb"
            return "default_29_sar_fallback"
        if order_prep.get("shipping_fee") == 29:
            return "default_29_sar_fallback"
        if route_owner.startswith("order_flow_v2") and order_prep.get("free_shipping"):
            return "orderflow_v2_free_shipping"
        if route_owner.startswith("brain") and "شحن" in text:
            return "llm_composed_summary"
        if route_owner.startswith("legacy"):
            return "legacy_flow"
        return ""

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
        live_route_owner: str = "",
        live_outbound_snippet: str = "",
    ) -> TurnAudit:
        from routers.whatsapp_webhook import _handle_merchant_message  # noqa: PLC0415

        self._guard_trace.reset()
        self._last_brain_result = {}
        self._brain_called = False
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

        dedup_hit = self._check_dedup(msg_id) if msg_id else False

        meta = dict(inbound_metadata or {})
        meta.setdefault("type", meta.get("inbound_normalized_type") or "text")

        turn = TurnAudit(
            label=label,
            customer_text=text,
            dedup_hit=dedup_hit,
            dedup_msg_id=msg_id,
        )

        if dedup_hit:
            turn.handled = False
            turn.skip_brain = True
            turn.route_owner = "dedup.suppressed"
            turn.divergence_reason = "duplicate provider_msg_id suppressed turn"
            self.turns.append(turn)
            return turn

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
            turn.errors.append(str(exc))
            self.errors.append(f"{label}: {exc}")

        out_events = self._list_outbound_since(after_id)
        new_fake = self.fake_sender.sent[outbound_before:]
        outbound_event = out_events[-1] if out_events else None
        outbound_meta = dict(getattr(outbound_event, "extra_metadata", None) or {})
        reply = ""
        if outbound_event is not None:
            reply = str(outbound_event.body or "")
        elif new_fake:
            reply = str(new_fake[-1].body or "")

        of2_reason = str(outbound_meta.get("order_flow_v2_reason") or "")
        turn.brain_called = self._brain_called
        turn.skip_brain = not self._brain_called and bool(reply)
        turn.route_owner = self._infer_route_owner(
            outbound_meta=outbound_meta,
            brain_called=self._brain_called,
            of2_reason=of2_reason,
        )
        turn.handled = bool(reply) or self._brain_called
        turn.decision_action = str(self._last_brain_result.get("decision_action") or "")
        args = dict(self._last_brain_result.get("decision_args") or {})
        turn.decision_topic = str(args.get("topic") or "")
        turn.state_patch_keys = sorted(
            k for k in (args.get("state_patch") or {}) if isinstance(k, str)
        )
        turn.order_prep_summary = self._order_prep_summary()
        turn.outbound_kind = self._infer_outbound_kind(
            outbound_event=outbound_event,
            fake_records=new_fake,
        )
        turn.outbound_reply = reply
        turn.payment_credential_guard_ran = self._guard_trace.payment_credential
        turn.saudi_dialect_guard_ran = self._guard_trace.saudi_dialect
        turn.shipping_policy_source = self._infer_shipping_policy_source(
            reply=reply,
            route_owner=turn.route_owner,
            order_prep=turn.order_prep_summary,
        )
        turn.media_barcode_path_triggered = any(
            r.type in {"image", "document"} for r in new_fake
        ) or bool(outbound_meta.get("payment_asset_id"))

        if live_route_owner and live_route_owner not in turn.route_owner:
            turn.divergence_reason = (
                f"route_owner replay={turn.route_owner!r} live_expected~{live_route_owner!r}"
            )
        elif live_outbound_snippet and live_outbound_snippet not in reply:
            turn.divergence_reason = (
                f"outbound_snippet missing live={live_outbound_snippet[:40]!r}"
            )

        self.turns.append(turn)
        return turn

    def run_turn(self, step: ReplayStep) -> TurnAudit:
        with self._runtime_patches():
            return run_async(
                self._run_webhook_turn(
                    label=step.label,
                    text=step.text,
                    inbound_metadata=step.inbound_metadata,
                    provider_msg_id=step.provider_msg_id,
                    live_route_owner=step.live_route_owner,
                    live_outbound_snippet=step.live_outbound_snippet,
                )
            )

    def run_thread(self, steps: Sequence[ReplayStep]) -> BrainReplayAudit:
        for step in steps:
            self.run_turn(step)
        return self.build_audit()

    def run_payment_probe(self, message: str, *, label: str) -> TurnAudit:
        return self.run_turn(
            ReplayStep(
                label=label,
                text=message,
                live_route_owner="brain.pipeline/payment",
            )
        )

    def run_dedup_matrix(self, text: str = "السلام عليكم") -> List[Dict[str, Any]]:
        from core.inbound_dedup import reset_cache  # noqa: PLC0415

        results: List[Dict[str, Any]] = []
        scenarios = [
            ("same_msg_id", "wamid.dedup.fixed", "wamid.dedup.fixed"),
            ("different_msg_id", "wamid.dedup.a", "wamid.dedup.b"),
            ("missing_msg_id", None, None),
        ]
        for name, first_id, second_id in scenarios:
            reset_cache()
            self.turns.clear()
            first = self.run_turn(
                ReplayStep(label=f"dedup_{name}_1", text=text, provider_msg_id=first_id)
            )
            second = self.run_turn(
                ReplayStep(label=f"dedup_{name}_2", text=text, provider_msg_id=second_id)
            )
            results.append(
                {
                    "case": name,
                    "first_handled": first.handled,
                    "second_dedup_hit": second.dedup_hit,
                    "second_handled": second.handled,
                    "second_route_owner": second.route_owner,
                }
            )
        return results

    def build_audit(self, *, live_expectations: Optional[Dict[str, str]] = None) -> BrainReplayAudit:
        audit = BrainReplayAudit(scenario=self.scenario_name, turns=list(self.turns))
        expectations = live_expectations or {}
        mismatches = 0
        matches = 0
        for turn in audit.turns:
            expected = expectations.get(turn.label) or ""
            if not expected:
                continue
            if expected in turn.route_owner or turn.route_owner.startswith(expected):
                matches += 1
            else:
                mismatches += 1
                audit.divergence_reasons.append(
                    turn.divergence_reason
                    or f"{turn.label}: expected route ~{expected!r} got {turn.route_owner!r}"
                )
        if expectations:
            if mismatches == 0:
                audit.match_vs_live = "matched"
            elif matches > 0:
                audit.match_vs_live = "partial"
            else:
                audit.match_vs_live = "did_not_match"
        else:
            diverged = [t for t in audit.turns if t.divergence_reason]
            if not diverged:
                audit.match_vs_live = "matched"
            elif len(diverged) < len(audit.turns):
                audit.match_vs_live = "partial"
            else:
                audit.match_vs_live = "did_not_match"
            audit.divergence_reasons.extend(t.divergence_reason for t in diverged if t.divergence_reason)
        return audit


__all__ = [
    "BrainReplayAudit",
    "BrainReplayRunner",
    "ReplayStep",
    "TurnAudit",
]
