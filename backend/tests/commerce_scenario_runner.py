"""
commerce_scenario_runner.py
────────────────────────────
Test harness for simulating WhatsApp inbound commerce flows without
real outbound sends.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union
from unittest.mock import AsyncMock, MagicMock, patch

from commerce_scenario_fixtures import (
    ScenarioWorld,
    attach_brain_state,
    build_order_prep,
    list_inbound_messages,
    list_orders,
)
from models import Conversation, Customer, MessageEvent, Order


def run_async(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class TextInbound:
    text: str


@dataclass(frozen=True)
class LocationInbound:
    lat: float
    lng: float
    name: str = ""
    address: str = ""


@dataclass(frozen=True)
class CatalogSelectionInbound:
    product_items: List[Dict[str, Any]]
    catalog_id: str = "CAT-1"


InboundEvent = Union[TextInbound, LocationInbound, CatalogSelectionInbound]


@dataclass
class FakeOutboundRecord:
    type: str
    to: str = ""
    body: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    path: str = ""

    @classmethod
    def from_provider_json(cls, payload: Dict[str, Any], *, path: str = "") -> "FakeOutboundRecord":
        msg_type = str(payload.get("type") or "text")
        to = str(payload.get("to") or "")
        body = ""
        if msg_type == "text":
            body = str((payload.get("text") or {}).get("body") or "")
        elif msg_type == "interactive":
            interactive = payload.get("interactive") or {}
            if interactive.get("type") == "catalog_message":
                msg_type = "catalog"
            body = str(interactive)
        elif msg_type == "template":
            body = str((payload.get("template") or {}).get("name") or "")
        return cls(type=msg_type, to=to, body=body, payload=dict(payload), path=path)


@dataclass
class ScenarioRunResult:
    inbound_messages: List[MessageEvent] = field(default_factory=list)
    fake_outbounds: List[FakeOutboundRecord] = field(default_factory=list)
    orders: List[Order] = field(default_factory=list)
    customer: Optional[Customer] = None
    conversation: Optional[Conversation] = None
    llm_calls: int = 0
    decision_trace: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    no_real_whatsapp_send: bool = True

    @property
    def fake_outbound_count(self) -> int:
        return len(self.fake_outbounds)


class FakeWhatsAppSender:
    """Records outbound payloads instead of calling Meta/360dialog."""

    def __init__(self) -> None:
        self.sent: List[FakeOutboundRecord] = []
        self.real_send_attempted = False

    async def _capture_post(self, *_args, **kwargs):
        self.real_send_attempted = True
        payload = dict(kwargs.get("json") or {})
        self.sent.append(
            FakeOutboundRecord.from_provider_json(payload, path="provider_post_with_context")
        )
        return {"messages": [{"id": f"wamid.fake.{len(self.sent)}"}]}

    async def _capture_send(self, *args, **kwargs):
        self.real_send_attempted = True
        payload = dict(kwargs.get("payload") or kwargs.get("json") or {})
        if not payload and len(args) >= 2 and isinstance(args[1], dict):
            payload = dict(args[1])
        self.sent.append(
            FakeOutboundRecord.from_provider_json(payload, path="provider_send_message")
        )
        return {"messages": [{"id": f"wamid.fake.{len(self.sent)}"}]}, MagicMock()

    @contextmanager
    def patch(self) -> Iterator["FakeWhatsAppSender"]:
        token_ctx = MagicMock(token="tok", source="test")
        with patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=self._capture_post,
        ), patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=self._capture_send,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=token_ctx),
        ), patch(
            "services.whatsapp_platform.service.wa_provider",
            return_value="360dialog",
        ), patch(
            "observability.rate_limiter.check_rate_limit",
            return_value=True,
        ), patch(
            "core.outbound_dedup.check_outbound_send",
            return_value=None,
        ), patch(
            "core.wa_usage.check_limit",
            return_value=MagicMock(allowed=True, used_total=0, limit=1000, reason="", pct=0),
        ):
            yield self


class AIScenarioRunner:
    """Simulate inbound turns and collect DB + fake outbound state."""

    def __init__(
        self,
        world: ScenarioWorld,
        *,
        phone_id: str = "PH_SCENARIO",
        use_webhook: bool = False,
    ) -> None:
        self.world = world
        self.phone_id = phone_id
        self.use_webhook = use_webhook
        self.fake_sender = FakeWhatsAppSender()
        self.llm_calls = 0
        self.decision_trace: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self._msg_counter = 0

    def _next_msg_id(self) -> str:
        self._msg_counter += 1
        return f"wamid.scenario.{self._msg_counter}.{uuid.uuid4().hex[:8]}"

    def refresh(self) -> ScenarioRunResult:
        db = self.world.db
        tenant_id = self.world.tenant.id
        convo_id = self.world.conversation.id
        customer = db.query(Customer).filter_by(id=self.world.customer.id).one()
        conversation = db.query(Conversation).filter_by(id=convo_id).one()
        return ScenarioRunResult(
            inbound_messages=list_inbound_messages(db, tenant_id, convo_id),
            fake_outbounds=list(self.fake_sender.sent),
            orders=list_orders(db, tenant_id),
            customer=customer,
            conversation=conversation,
            llm_calls=self.llm_calls,
            decision_trace=list(self.decision_trace),
            errors=list(self.errors),
            no_real_whatsapp_send=not self.fake_sender.real_send_attempted
            or all(isinstance(r, FakeOutboundRecord) for r in self.fake_sender.sent),
        )

    def run_deterministic_text(self, text: str) -> ScenarioRunResult:
        """Apply deterministic commerce helpers for stable unit scenarios."""
        text = (text or "").strip()
        if not text:
            return self.refresh()

        from core.customer_name_extractor import extract_high_confidence_name  # noqa: PLC0415
        from core.wa_address_ingestion import (  # noqa: PLC0415
            build_short_address_patch,
            build_whatsapp_location_patch,
        )

        db = self.world.db
        convo = db.query(Conversation).filter_by(id=self.world.conversation.id).one()
        meta = dict(convo.extra_metadata or {})
        brain = dict(meta.get("brain_state") or {})
        prep = dict(brain.get("order_prep") or build_order_prep())

        name_hit = extract_high_confidence_name(text)
        if name_hit:
            parts = name_hit.value.split(maxsplit=1)
            prep["customer_first_name"] = parts[0]
            prep["customer_last_name"] = parts[1] if len(parts) > 1 else ""
            self.world.customer.name = name_hit.value
            db.add(self.world.customer)

        if text.upper().startswith("RAGB") or "العنوان" in text:
            prep.update(build_short_address_patch(text))

        if text in {"الدفع تحويل", "تحويل"}:
            prep["payment_method"] = "bank_transfer"
            prep["awaiting_payment_receipt"] = True

        if text in {"تم التحويل", "دفعت"}:
            prep["payment_claim_unverified"] = True
            prep["payment_receipt_received"] = False
            prep["payment_verified"] = False

        if text in {"نعم أكد الطلب", "أكد الطلب"}:
            prep["order_confirmed"] = True
            prep["confirmation_requested"] = False

        brain["order_prep"] = prep
        meta["brain_state"] = brain
        convo.extra_metadata = meta
        db.add(convo)
        db.flush()

        try:
            from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415

            sync_nahla_wa_order(
                db,
                tenant_id=self.world.tenant.id,
                conversation=convo,
                brain_state=brain,
                order_prep=prep,
                customer=self.world.customer,
                trigger="scenario_runner",
            )
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"sync_nahla_wa_order: {exc}")

        from core.conversation_engine import StateManager  # noqa: PLC0415

        StateManager.save_message(
            db,
            self.world.phone,
            text,
            "inbound",
            conversation_id=convo.id,
            tenant_id=self.world.tenant.id,
            extra_metadata={"message_origin": "scenario_runner"},
        )
        db.commit()
        return self.refresh()

    def run_location(self, inbound: LocationInbound) -> ScenarioRunResult:
        from core.wa_address_ingestion import build_whatsapp_location_patch  # noqa: PLC0415

        db = self.world.db
        convo = db.query(Conversation).filter_by(id=self.world.conversation.id).one()
        meta = dict(convo.extra_metadata or {})
        brain = dict(meta.get("brain_state") or {})
        prep = dict(brain.get("order_prep") or build_order_prep())
        location = {
            "latitude": inbound.lat,
            "longitude": inbound.lng,
            "name": inbound.name,
            "address": inbound.address,
        }
        prep.update(build_whatsapp_location_patch(location))
        brain["order_prep"] = prep
        meta["brain_state"] = brain
        convo.extra_metadata = meta
        db.add(convo)

        from core.conversation_engine import StateManager  # noqa: PLC0415

        StateManager.save_message(
            db,
            self.world.phone,
            f"[location:{inbound.lat},{inbound.lng}]",
            "inbound",
            conversation_id=convo.id,
            tenant_id=self.world.tenant.id,
            extra_metadata={
                "message_origin": "scenario_runner",
                "normalized_inbound": {
                    "source_type": "location",
                    "location": location,
                },
            },
        )
        db.commit()
        return self.refresh()

    def run_catalog(self, inbound: CatalogSelectionInbound) -> ScenarioRunResult:
        db = self.world.db
        convo = db.query(Conversation).filter_by(id=self.world.conversation.id).one()
        meta = {
            "source_type": "catalog_order",
            "catalog_id": inbound.catalog_id,
            "product_items": inbound.product_items,
            "item_count": len(inbound.product_items),
        }
        try:
            from core.wa_catalog_order_immediate_draft import (  # noqa: PLC0415
                persist_catalog_order_immediate_draft,
            )

            persist_catalog_order_immediate_draft(
                db,
                tenant_id=self.world.tenant.id,
                conversation=convo,
                inbound_metadata=meta,
                customer=self.world.customer,
                phone=self.world.phone_e164,
                source_message_key=self._next_msg_id(),
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"catalog_draft: {exc}")
        return self.refresh()

    async def _run_webhook_text(self, text: str, inbound_metadata: Optional[Dict[str, Any]] = None) -> None:
        from routers.whatsapp_webhook import _handle_merchant_message  # noqa: PLC0415

        brain = MagicMock()
        brain.process = AsyncMock(return_value={"reply": None, "buttons": [], "skipped": True})

        def _track_process(*_args, **_kwargs):
            self.llm_calls += 1
            return brain.process.return_value

        brain.process.side_effect = _track_process

        with patch("core.billing.has_billing_access", return_value=True), patch(
            "modules.ai.brain.pipeline.get_brain",
            return_value=brain,
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
        ):
            await _handle_merchant_message(
                phone_id=self.phone_id,
                to=self.world.phone,
                text=text,
                tenant_id=self.world.tenant.id,
                db=self.world.db,
                inbound_metadata=inbound_metadata,
                wa_msg_id=self._next_msg_id(),
                wa_message_ts=datetime.now(timezone.utc),
            )

    def run_webhook_text(self, text: str, inbound_metadata: Optional[Dict[str, Any]] = None) -> ScenarioRunResult:
        with self.fake_sender.patch():
            try:
                run_async(self._run_webhook_text(text, inbound_metadata))
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"webhook: {exc}")
        return self.refresh()

    def run(self, steps: Sequence[InboundEvent]) -> ScenarioRunResult:
        with self.fake_sender.patch():
            for step in steps:
                try:
                    if isinstance(step, TextInbound):
                        if self.use_webhook:
                            run_async(self._run_webhook_text(step.text))
                        else:
                            self.run_deterministic_text(step.text)
                    elif isinstance(step, LocationInbound):
                        self.run_location(step)
                    elif isinstance(step, CatalogSelectionInbound):
                        self.run_catalog(step)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(str(exc))
        return self.refresh()

    def run_automation_emitter(
        self,
        emitter: Callable[..., int],
        *,
        now: Optional[datetime] = None,
    ) -> ScenarioRunResult:
        with self.fake_sender.patch():
            try:
                emitted = emitter(self.world.db, self.world.tenant.id, now=now)
                self.decision_trace.append({"automation_emitted": emitted})
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"automation: {exc}")
        return self.refresh()

    def seed_prep_on_conversation(self, prep: Dict[str, Any]) -> None:
        convo = self.world.db.query(Conversation).filter_by(id=self.world.conversation.id).one()
        attach_brain_state(convo, prep)
        self.world.db.add(convo)
        self.world.db.commit()

    def order_count(self) -> int:
        return len(list_orders(self.world.db, self.world.tenant.id))

    def commerce_bundle(self) -> Dict[str, Any]:
        from core.active_order_context import load_commerce_bundle  # noqa: PLC0415

        convo = self.world.db.query(Conversation).filter_by(id=self.world.conversation.id).one()
        return load_commerce_bundle(dict(convo.extra_metadata or {}))

    def run_inbound_only(self, text: str) -> ScenarioRunResult:
        """Persist inbound text without order-bridge side effects."""
        from core.conversation_engine import StateManager  # noqa: PLC0415

        db = self.world.db
        convo = db.query(Conversation).filter_by(id=self.world.conversation.id).one()
        StateManager.save_message(
            db,
            self.world.phone,
            (text or "").strip(),
            "inbound",
            conversation_id=convo.id,
            tenant_id=self.world.tenant.id,
            extra_metadata={"message_origin": "scenario_runner_inbound_only"},
        )
        db.commit()
        return self.refresh()
