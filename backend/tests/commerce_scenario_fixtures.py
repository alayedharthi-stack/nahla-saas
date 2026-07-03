"""
commerce_scenario_fixtures.py
─────────────────────────────
Shared SQLite test DB + seed helpers for AI commerce scenario tests.

Policy: seed neutral merchants/products/customers for platform regressions.
See AGENTS.md 「Generic Commerce Regression Tests」. Truth comes from persisted
Customer / CustomerAddress / order state — not from inbound phrase matching alone.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    Base,
    Conversation,
    Customer,
    CustomerAddress,
    MerchantKnowledgeSection,
    MessageEvent,
    Order,
    OrderShipment,
    Product,
    SmartAutomation,
    Tenant,
    TenantSettings,
    WhatsAppConnection,
)


DEFAULT_PHONE = "966500000001"
DEFAULT_PHONE_E164 = "+966500000001"


def make_scenario_db() -> Tuple[Session, Any]:
    """In-memory SQLite DB with JSONB columns shimmed to JSON."""
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal(), engine


def seed_tenant(
    db: Session,
    *,
    name: str = "Scenario Store",
    store_ai_enabled: bool = True,
) -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.flush()
    settings = TenantSettings(
        tenant_id=tenant.id,
        ai_settings={"store_ai_enabled": store_ai_enabled},
        whatsapp_settings={
            "phone_number_id": "PH_SCENARIO",
            "phone_number": DEFAULT_PHONE_E164,
        },
        store_settings={"store_name": name},
    )
    db.add(settings)
    conn = WhatsAppConnection(
        tenant_id=tenant.id,
        status="connected",
        provider="360dialog",
        phone_number_id="PH_SCENARIO",
        phone_number=DEFAULT_PHONE_E164,
        access_token="test-token",
        sending_enabled=True,
    )
    db.add(conn)
    db.commit()
    db.refresh(tenant)
    return tenant


def seed_customer(
    db: Session,
    tenant_id: int,
    *,
    phone: str = DEFAULT_PHONE_E164,
    name: str = "",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Customer:
    normalized = phone.lstrip("+")
    customer = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=normalized,
        name=name,
        extra_metadata=extra_metadata or {},
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def seed_conversation(
    db: Session,
    tenant_id: int,
    customer_id: int,
    *,
    status: str = "open",
    ai_paused: bool = False,
    ai_paused_reason: str | None = None,
    handoff_active: bool = False,
    brain_state: Optional[Dict[str, Any]] = None,
) -> Conversation:
    meta: Dict[str, Any] = {}
    if brain_state is not None:
        meta["brain_state"] = brain_state
    convo = Conversation(
        tenant_id=tenant_id,
        customer_id=customer_id,
        status=status,
        ai_paused=ai_paused,
        ai_paused_reason=ai_paused_reason,
        handoff_active=handoff_active,
        extra_metadata=meta or None,
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo


def seed_product(
    db: Session,
    tenant_id: int,
    *,
    title: str = "عسل طلح",
    external_id: str = "sku-talh-half",
    price: str = "120",
    meta_retailer_id: str | None = None,
) -> Product:
    product = Product(
        tenant_id=tenant_id,
        title=title,
        external_id=external_id,
        meta_retailer_id=meta_retailer_id or external_id,
        price=price,
        in_stock=True,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def seed_customer_address(
    db: Session,
    tenant_id: int,
    customer_id: int,
    *,
    lat: str = "21.3891",
    lng: str = "39.8579",
    city: str = "مكة",
    address_text: str = "حي العزيزية",
    saudi_national_address: str = "",
) -> CustomerAddress:
    addr = CustomerAddress(
        tenant_id=tenant_id,
        customer_id=customer_id,
        lat=lat,
        lng=lng,
        city=city,
        address_text=address_text,
        saudi_national_address=saudi_national_address or None,
        google_maps_link=f"https://maps.google.com/?q={lat},{lng}",
        whatsapp_location={"latitude": lat, "longitude": lng},
    )
    db.add(addr)
    db.commit()
    db.refresh(addr)
    return addr


def seed_order(
    db: Session,
    tenant_id: int,
    *,
    status: str = "draft",
    external_id: str = "nahla-wa-1-1",
    external_order_number: str = "NHL-1001",
    line_items: Optional[List[Dict[str, Any]]] = None,
    customer_info: Optional[Dict[str, Any]] = None,
    source: str = "whatsapp",
    extra_metadata: Optional[Dict[str, Any]] = None,
    checkout_url: Optional[str] = None,
) -> Order:
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    meta = {
        "created_via": "nahla_order_bridge",
        "origin": "whatsapp_ai",
        "created_at": created,
        **(extra_metadata or {}),
    }
    order = Order(
        tenant_id=tenant_id,
        external_id=external_id,
        external_order_number=external_order_number,
        status=status,
        source=source,
        line_items=line_items or [{"title": "عسل طلح", "quantity": 2, "unit_price": "120"}],
        customer_info=customer_info or {"phone": DEFAULT_PHONE_E164},
        customer_name="فايز الصبحي",
        checkout_url=checkout_url,
        extra_metadata=meta,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def seed_shipment(
    db: Session,
    tenant_id: int,
    order_id: int,
    *,
    tracking_number: str = "TRK123456",
    provider: str = "smsa",
    status: str = "shipped",
) -> OrderShipment:
    shipment = OrderShipment(
        tenant_id=tenant_id,
        order_id=order_id,
        provider=provider,
        status=status,
        tracking_number=tracking_number,
    )
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment


def seed_abandoned_draft_automation(db: Session, tenant_id: int) -> SmartAutomation:
    from core.automation_triggers import AutomationTrigger  # noqa: PLC0415

    auto = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="abandoned_order_draft",
        trigger_event=AutomationTrigger.WA_ORDER_DRAFT_REMINDER_DUE.value,
        name="WA draft reminders",
        enabled=True,
        config={"delay_minutes": 60, "language": "ar"},
    )
    db.add(auto)
    db.commit()
    db.refresh(auto)
    return auto


def seed_post_delivery_review_automation(
    db: Session,
    tenant_id: int,
    *,
    delay_hours: int = 24,
    enabled: bool = True,
) -> SmartAutomation:
    from core.automation_triggers import AutomationTrigger  # noqa: PLC0415

    auto = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="post_delivery_review",
        trigger_event=AutomationTrigger.POST_DELIVERY_REVIEW_REQUEST_DUE.value,
        name="Post-delivery review",
        enabled=enabled,
        config={
            "delay_hours": delay_hours,
            "service_key": "post_delivery",
            "nahla_source_key": "review_request",
            "language": "ar",
        },
    )
    db.add(auto)
    db.commit()
    db.refresh(auto)
    return auto


def seed_review_request_template(db: Session, tenant_id: int) -> "WhatsAppTemplate":
    from models import WhatsAppTemplate  # noqa: PLC0415

    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name="review_request",
        language="ar",
        category="MARKETING",
        status="APPROVED",
        service_key="post_delivery",
        nahla_source_key="review_request",
        is_active=True,
        components=[{"type": "BODY", "text": "يسعدنا تقييم تجربتك معنا."}],
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


def enable_tenant_autopilot(db: Session, tenant_id: int) -> None:
    settings = db.query(TenantSettings).filter_by(tenant_id=tenant_id).one()
    ai = dict(settings.ai_settings or {})
    ai["autopilot_enabled"] = True
    settings.ai_settings = ai
    db.add(settings)
    db.commit()


def build_order_prep(
    *,
    product_id: str = "sku-talh-half",
    quantity: int = 2,
    customer_phone: str = DEFAULT_PHONE,
    customer_first_name: str = "",
    customer_last_name: str = "",
    payment_method: str = "",
    line_items: Optional[List[Dict[str, Any]]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    prep: Dict[str, Any] = {
        "product_id": product_id,
        "quantity": quantity,
        "customer_phone": customer_phone,
        "customer_first_name": customer_first_name,
        "customer_last_name": customer_last_name,
        "line_items": line_items or [
            {
                "title": "عسل طلح نصف كيلو",
                "quantity": quantity,
                "unit_price": "120",
                "product_id": product_id,
            }
        ],
    }
    if payment_method:
        prep["payment_method"] = payment_method
    prep.update(extra)
    return prep


def attach_brain_state(convo: Conversation, order_prep: Dict[str, Any]) -> None:
    meta = dict(convo.extra_metadata or {})
    brain = dict(meta.get("brain_state") or {})
    brain["order_prep"] = order_prep
    meta["brain_state"] = brain
    convo.extra_metadata = meta


@dataclass
class ScenarioWorld:
    db: Session
    tenant: Tenant
    customer: Customer
    conversation: Conversation
    phone: str = DEFAULT_PHONE
    phone_e164: str = DEFAULT_PHONE_E164
    product: Optional[Product] = None
    order: Optional[Order] = None
    address: Optional[CustomerAddress] = None
    shipment: Optional[OrderShipment] = None
    extras: Dict[str, Any] = field(default_factory=dict)


def persona_new_customer(db: Session, *, store_ai_enabled: bool = True) -> ScenarioWorld:
    tenant = seed_tenant(db, store_ai_enabled=store_ai_enabled)
    customer = seed_customer(db, tenant.id, name="")
    convo = seed_conversation(db, tenant.id, customer.id)
    product = seed_product(db, tenant.id)
    return ScenarioWorld(
        db=db,
        tenant=tenant,
        customer=customer,
        conversation=convo,
        product=product,
    )


def persona_returning_with_address(db: Session) -> ScenarioWorld:
    world = persona_new_customer(db)
    world.customer.name = "فايز الصبحي"
    world.db.add(world.customer)
    world.address = seed_customer_address(
        db, world.tenant.id, world.customer.id,
    )
    attach_brain_state(
        world.conversation,
        build_order_prep(
            customer_first_name="فايز",
            customer_last_name="الصبحي",
            google_maps_url=world.address.google_maps_link,
            latitude=float(world.address.lat),
            longitude=float(world.address.lng),
            delivery_address_status="accepted",
        ),
    )
    world.db.add(world.conversation)
    world.db.commit()
    return world


def persona_draft_order(db: Session) -> ScenarioWorld:
    world = persona_new_customer(db)
    prep = build_order_prep(
        customer_first_name="فايز",
        customer_last_name="الصبحي",
    )
    attach_brain_state(world.conversation, prep)
    world.db.add(world.conversation)
    world.db.commit()
    return world


def seed_knowledge_section(
    db: Session,
    tenant_id: int,
    *,
    kind: str = "faq",
    title: str = "",
    body: str = "",
    priority: int = 10,
) -> MerchantKnowledgeSection:
    section = MerchantKnowledgeSection(
        tenant_id=tenant_id,
        kind=kind,
        title=title,
        body=body,
        priority=priority,
        is_active=True,
        source="manual",
        ai_status="approved",
    )
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


def seed_honey_kb_bundle(db: Session, tenant_id: int) -> Dict[str, MerchantKnowledgeSection]:
    """Curated KB rows for FAQ / origin / availability scenario tests."""
    sections = {
        "natural_honey": seed_knowledge_section(
            db,
            tenant_id,
            kind="faq",
            title="هل العسل طبيعي؟",
            body="عسلنا طبيعي 100% بدون إضافات أو تسخين.",
        ),
        "talh_origin": seed_knowledge_section(
            db,
            tenant_id,
            kind="product_info",
            title="منشأ عسل الطلح",
            body="عسل الطلح يُقطف من جبال عسير ومناطق الطلح المعروفة.",
        ),
        "sidr_availability": seed_knowledge_section(
            db,
            tenant_id,
            kind="quick_update",
            title="توفر السدر",
            body="عسل السدر متوفر حالياً — الكمية محدودة.",
        ),
    }
    return sections


def build_commerce_bundle_from_order(
    order: Order,
    shipment: Optional[OrderShipment] = None,
    *,
    order_status: str = "shipped",
    shipping_status: str = "shipped",
) -> Dict[str, Any]:
    """Structured commerce bundle for tracking/shipment guard probes."""
    reference = str(order.external_order_number or order.external_id or order.id)
    ctx: Dict[str, Any] = {
        "order_id": reference,
        "external_id": order.external_id,
        "order_status": order_status,
        "raw_order_status": order_status,
        "shipping_status": shipping_status,
        "tracking_number": None,
        "tracking_url": None,
        "shipping_provider": None,
        "confirmed_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "product_summary": "عسل طلح",
    }
    if shipment is not None:
        ctx["tracking_number"] = shipment.tracking_number
        ctx["tracking_url"] = f"https://track.test/{shipment.tracking_number}"
        ctx["shipping_provider"] = shipment.provider
    return {
        "active_order_id": str(order.id),
        "active_order_context": ctx,
        "recent_order_ids": [str(order.id)],
    }


def attach_commerce_bundle(
    convo: Conversation,
    bundle: Dict[str, Any],
) -> None:
    meta = dict(convo.extra_metadata or {})
    meta.update(bundle)
    convo.extra_metadata = meta


def attach_shipped_commerce_context(
    convo: Conversation,
    order: Order,
    shipment: OrderShipment,
) -> None:
    attach_commerce_bundle(
        convo,
        build_commerce_bundle_from_order(order, shipment),
    )


def attach_delivered_commerce_context(
    convo: Conversation,
    order: Order,
) -> None:
    attach_commerce_bundle(
        convo,
        build_commerce_bundle_from_order(
            order,
            order_status="delivered",
            shipping_status="delivered",
        ),
    )


def persona_shipped_order(db: Session) -> ScenarioWorld:
    world = persona_returning_with_address(db)
    ext_id = f"nahla-wa-{world.tenant.id}-{world.conversation.id}"
    world.order = seed_order(
        db,
        world.tenant.id,
        status="shipped",
        external_id=ext_id,
        external_order_number="NHL-7788",
        customer_info={"phone": world.phone_e164, "name": "فايز الصبحي"},
    )
    world.shipment = seed_shipment(db, world.tenant.id, world.order.id)
    attach_shipped_commerce_context(world.conversation, world.order, world.shipment)
    world.db.add(world.conversation)
    world.db.commit()
    world.extras["kb_sections"] = seed_honey_kb_bundle(db, world.tenant.id)
    return world


def persona_delivered_order(db: Session) -> ScenarioWorld:
    world = persona_returning_with_address(db)
    ext_id = f"nahla-wa-{world.tenant.id}-{world.conversation.id}"
    delivered_at = datetime.now(timezone.utc) - timedelta(days=3)
    world.order = seed_order(
        db,
        world.tenant.id,
        status="delivered",
        external_id=ext_id,
        external_order_number="NHL-9900",
        customer_info={"phone": world.phone_e164, "name": "فايز الصبحي"},
        extra_metadata={
            "delivered_at": delivered_at.isoformat(),
            "review_request_sent": False,
        },
    )
    attach_delivered_commerce_context(world.conversation, world.order)
    prep = build_order_prep(
        customer_first_name="فايز",
        customer_last_name="الصبحي",
        order_status="delivered",
    )
    attach_brain_state(world.conversation, prep)
    world.db.add(world.conversation)
    world.db.commit()
    return world


def persona_kb_inquiry(db: Session) -> ScenarioWorld:
    """New customer with seeded honey FAQ/KB — no active order."""
    world = persona_new_customer(db)
    world.extras["kb_sections"] = seed_honey_kb_bundle(db, world.tenant.id)
    return world


def persona_paused_conversation(db: Session) -> ScenarioWorld:
    world = persona_new_customer(db)
    world.conversation.ai_paused = True
    world.conversation.ai_paused_reason = "manual_pause"
    world.db.add(world.conversation)
    world.db.commit()
    return world


def persona_store_ai_off(db: Session) -> ScenarioWorld:
    return persona_new_customer(db, store_ai_enabled=False)


def list_inbound_messages(db: Session, tenant_id: int, conversation_id: int) -> List[MessageEvent]:
    return (
        db.query(MessageEvent)
        .filter_by(tenant_id=tenant_id, conversation_id=conversation_id, direction="inbound")
        .order_by(MessageEvent.id.asc())
        .all()
    )


def list_orders(db: Session, tenant_id: int) -> List[Order]:
    return db.query(Order).filter_by(tenant_id=tenant_id).order_by(Order.id.asc()).all()
