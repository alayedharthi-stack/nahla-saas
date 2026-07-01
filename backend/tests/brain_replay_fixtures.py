"""
brain_replay_fixtures.py
────────────────────────
Generic tenant snapshots for BrainReplayRunner — platform-wide shapes only.

No real phone numbers or private customer data in committed fixtures.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from commerce_scenario_fixtures import (
    DEFAULT_PHONE,
    DEFAULT_PHONE_E164,
    ScenarioWorld,
    attach_brain_state,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_customer_address,
    seed_knowledge_section,
    seed_product,
    seed_tenant,
)
from models import MerchantKnowledgeSection, TenantSettings

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CANARY_SNAPSHOT_PATH = FIXTURES_DIR / "brain_replay_canary_snapshot.json"

# Neutral catalog lines used by the canary thread replay (not runtime logic).
GENERIC_PRODUCT_A = {
    "title": "250 جرام عسل سمر الحجاز البلدي إنتاج مناحلنا من شمال الطايف",
    "external_id": "86bqzca62a",
    "price": "126",
    "catalog_price": 126.0,
}
GENERIC_PRODUCT_B = {
    "title": "500 جرام العسل الصيفي أزهار جبلية من جنوب الطائف",
    "external_id": "summer-500",
    "price": "239.50",
    "catalog_price": 239.5,
}
GENERIC_CATALOG_TOTAL = 365.5


@dataclass
class PaymentFixtureVariant:
    rajhi: bool = True
    ahli: bool = True
    barcode_aliases: List[str] = field(default_factory=list)


@dataclass
class BrainReplaySnapshot:
    tenant_name: str = "متجر تجريبي عام"
    store_ai_mode: str = "test"
    shipping_kb_body: str = (
        "العسل والمنتجات الغذائية: شحن مجاني.\n"
        "باقي الأقسام (مستحضرات/هدايا): شحن توصيل 29 ريال."
    )
    products: List[Dict[str, Any]] = field(
        default_factory=lambda: [GENERIC_PRODUCT_A, GENERIC_PRODUCT_B]
    )
    payment: PaymentFixtureVariant = field(default_factory=PaymentFixtureVariant)
    saved_address: bool = False
    customer_name: str = ""

    @classmethod
    def from_json_file(cls, path: Path) -> "BrainReplaySnapshot":
        raw = json.loads(path.read_text(encoding="utf-8"))
        payment_raw = dict(raw.get("payment") or {})
        payment = PaymentFixtureVariant(
            rajhi=bool(payment_raw.get("rajhi", True)),
            ahli=bool(payment_raw.get("ahli", True)),
            barcode_aliases=list(payment_raw.get("barcode_aliases") or []),
        )
        return cls(
            tenant_name=str(raw.get("tenant_name") or "متجر تجريبي عام"),
            store_ai_mode=str(raw.get("store_ai_mode") or "test"),
            shipping_kb_body=str(raw.get("shipping_kb_body") or cls().shipping_kb_body),
            products=list(raw.get("products") or [GENERIC_PRODUCT_A, GENERIC_PRODUCT_B]),
            payment=payment,
            saved_address=bool(raw.get("saved_address", False)),
            customer_name=str(raw.get("customer_name") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_name": self.tenant_name,
            "store_ai_mode": self.store_ai_mode,
            "shipping_kb_body": self.shipping_kb_body,
            "products": self.products,
            "payment": {
                "rajhi": self.payment.rajhi,
                "ahli": self.payment.ahli,
                "barcode_aliases": self.payment.barcode_aliases,
            },
            "saved_address": self.saved_address,
            "customer_name": self.customer_name,
        }


def catalog_order_metadata(
  products: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    items = []
    for prod in products or [GENERIC_PRODUCT_A, GENERIC_PRODUCT_B]:
        items.append(
            {
                "product_retailer_id": prod["external_id"],
                "quantity": 1,
                "item_price": prod.get("catalog_price") or float(prod["price"]),
                "currency": "SAR",
            }
        )
    return {
        "source_type": "catalog_order",
        "product_items": items,
        "order": {"product_items": items},
        "type": "order",
        "inbound_normalized_type": "catalog_order",
    }


def _seed_payment_kb(
    db,
    tenant_id: int,
    *,
    rajhi: bool,
    ahli: bool,
) -> None:
    if rajhi:
        db.add(
            MerchantKnowledgeSection(
                tenant_id=tenant_id,
                kind="bank_transfer",
                title="حساب الراجحي",
                body="البنك: الراجحي\nالآيبان: SA1234567890123456789012",
                metadata_json={"bank_brand": "rajhi", "beneficiary_name": "متجر تجريبي"},
                is_active=True,
            )
        )
    if ahli:
        db.add(
            MerchantKnowledgeSection(
                tenant_id=tenant_id,
                kind="bank_transfer",
                title="حساب الأهلي",
                body="البنك: الأهلي\nالآيبان: SA0380000000608010167519",
                metadata_json={"bank_brand": "alahli", "beneficiary_name": "متجر تجريبي"},
                is_active=True,
            )
        )
    db.commit()


def build_brain_replay_world(
    db,
    snapshot: Optional[BrainReplaySnapshot] = None,
) -> ScenarioWorld:
    snap = snapshot or BrainReplaySnapshot()
    tenant = seed_tenant(
        db,
        name=snap.tenant_name,
        store_ai_enabled=False,
    )
    ts = db.query(TenantSettings).filter_by(tenant_id=tenant.id).one()
    ai = dict(ts.ai_settings or {})
    ai.update(
        {
            "store_ai_mode": snap.store_ai_mode,
            "store_ai_enabled": False,
            "ai_test_allowed_numbers": [DEFAULT_PHONE_E164],
        }
    )
    ts.ai_settings = ai
    ts.extra_metadata = {
        "payment_methods": {
            "bank_transfer_enabled": True,
            "cash_on_delivery_enabled": False,
        },
    }
    db.add(ts)

    customer = seed_customer(
        db,
        tenant.id,
        phone=DEFAULT_PHONE_E164,
        name=snap.customer_name,
    )
    convo = seed_conversation(db, tenant.id, customer.id)
    for prod in snap.products:
        seed_product(
            db,
            tenant.id,
            title=str(prod["title"]),
            external_id=str(prod["external_id"]),
            price=str(prod["price"]),
            meta_retailer_id=str(prod["external_id"]),
        )
    if snap.saved_address:
        seed_customer_address(
            db,
            tenant.id,
            customer.id,
            city="الطائف",
            address_text="حي الحلقة الغربية، 3320 ابن تميرة",
            saudi_national_address="TAPB3320",
        )
    seed_knowledge_section(
        db,
        tenant.id,
        kind="shipping_zones",
        title="سياسة الشحن",
        body=snap.shipping_kb_body,
    )
    _seed_payment_kb(
        db,
        tenant.id,
        rajhi=snap.payment.rajhi,
        ahli=snap.payment.ahli,
    )
    db.commit()
    return ScenarioWorld(
        db=db,
        tenant=tenant,
        customer=customer,
        conversation=convo,
        phone=DEFAULT_PHONE,
        phone_e164=DEFAULT_PHONE_E164,
    )


def make_brain_replay_db_and_world(
    snapshot: Optional[BrainReplaySnapshot] = None,
) -> Tuple[Any, ScenarioWorld]:
    db, _engine = make_scenario_db()
    world = build_brain_replay_world(db, snapshot)
    return db, world


def load_canary_snapshot() -> BrainReplaySnapshot:
    if CANARY_SNAPSHOT_PATH.is_file():
        return BrainReplaySnapshot.from_json_file(CANARY_SNAPSHOT_PATH)
    return BrainReplaySnapshot()


__all__ = [
    "BrainReplaySnapshot",
    "CANARY_SNAPSHOT_PATH",
    "GENERIC_CATALOG_TOTAL",
    "GENERIC_PRODUCT_A",
    "GENERIC_PRODUCT_B",
    "PaymentFixtureVariant",
    "build_brain_replay_world",
    "catalog_order_metadata",
    "load_canary_snapshot",
    "make_brain_replay_db_and_world",
]
