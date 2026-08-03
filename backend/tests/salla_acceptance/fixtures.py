"""
Synthetic dual-tenant Salla-like stores for acceptance tests.

Generic commerce (shoes / perfume / apparel) — not honey-only.
Reuses ``commerce_scenario_fixtures`` helpers; extends product seeding for
variants, stock, URLs, and offer metadata without schema migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from models import CommercePermissions, Customer, MerchantKnowledgeSection, Order, Product, ProductVariant
from tests.commerce_scenario_fixtures import (
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_knowledge_section,
    seed_order,
    seed_shipment,
    seed_tenant,
)

TENANT_A_NAME = "متجر سلة تجريبي عام"
TENANT_B_NAME = "متجر سلة آخر"

PHONE_CUST_A = "+966500100001"
PHONE_CUST_B = "+966500100002"
PHONE_CUST_C = "+966500100003"
PHONE_CUST_D = "+966500100004"

PHONE_B_CUST_A = "+966500200001"
PHONE_B_CUST_B = "+966500200002"


@dataclass
class TenantBundle:
    tenant_id: int
    name: str
    products: Dict[str, Product] = field(default_factory=dict)
    variants: Dict[str, List[ProductVariant]] = field(default_factory=dict)
    customers: Dict[str, Customer] = field(default_factory=dict)
    conversations: Dict[str, Any] = field(default_factory=dict)
    orders: Dict[str, Order] = field(default_factory=dict)
    kb: Dict[str, MerchantKnowledgeSection] = field(default_factory=dict)
    permissions: Optional[CommercePermissions] = None


@dataclass
class DualTenantWorld:
    db: Session
    tenant_a: TenantBundle
    tenant_b: TenantBundle


def seed_product_rich(
    db: Session,
    tenant_id: int,
    *,
    title: str,
    external_id: str,
    price: str,
    in_stock: bool = True,
    stock_quantity: Optional[int] = 10,
    description: str = "",
    meta_retailer_id: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    variant_specs: Optional[List[Dict[str, Any]]] = None,
    source: str = "salla",
) -> Tuple[Product, List[ProductVariant]]:
    """Seed a parent product with optional ProductVariant rows."""
    meta = dict(extra_metadata or {})
    product = Product(
        tenant_id=tenant_id,
        title=title,
        external_id=external_id,
        meta_retailer_id=meta_retailer_id or external_id,
        price=price,
        description=description or None,
        in_stock=in_stock,
        stock_quantity=stock_quantity,
        extra_metadata=meta,
        source=source,
        catalog_status="active",
    )
    db.add(product)
    db.flush()

    variant_rows: List[ProductVariant] = []
    if variant_specs:
        for idx, spec in enumerate(variant_specs):
            vr = ProductVariant(
                tenant_id=tenant_id,
                product_id=product.id,
                salla_variant_id=str(spec.get("salla_variant_id") or f"v-{external_id}-{idx}"),
                sku=str(spec.get("sku") or f"{external_id}-{idx}"),
                retailer_id=str(spec.get("retailer_id") or f"nahla_v_{external_id}_{idx}"),
                price=str(spec.get("price") or price),
                currency="SAR",
                stock_quantity=int(spec.get("stock_quantity") or 0),
                in_stock=bool(spec.get("in_stock", True)),
                options=spec.get("options") or {},
                option_summary=str(spec.get("option_summary") or ""),
                is_default=bool(spec.get("is_default", idx == 0)),
                extra_metadata=spec.get("extra_metadata"),
            )
            db.add(vr)
            variant_rows.append(vr)
        db.flush()
        real_variants = [v for v in variant_rows if not v.is_default]
        product.has_variants = len(real_variants) > 1 or len(variant_rows) > 1
        default = next((v for v in variant_rows if v.is_default), variant_rows[0])
        product.default_variant_id = default.id
    elif not in_stock or (stock_quantity is not None and stock_quantity <= 0):
        product.has_variants = False
    else:
        default_v = ProductVariant(
            tenant_id=tenant_id,
            product_id=product.id,
            salla_variant_id=f"v-{external_id}-default",
            sku=external_id,
            retailer_id=meta_retailer_id or external_id,
            price=price,
            currency="SAR",
            stock_quantity=int(stock_quantity or 0),
            in_stock=in_stock,
            is_default=True,
        )
        db.add(default_v)
        db.flush()
        variant_rows = [default_v]
        product.default_variant_id = default_v.id

    db.add(product)
    db.commit()
    db.refresh(product)
    for vr in variant_rows:
        db.refresh(vr)
    return product, variant_rows


def _seed_permissions(
    db: Session,
    tenant_id: int,
    *,
    can_create_orders: bool,
    can_apply_coupons: bool,
    can_create_checkout_links: bool = True,
    can_send_payment_links: bool = True,
) -> CommercePermissions:
    row = CommercePermissions(
        tenant_id=tenant_id,
        can_create_orders=can_create_orders,
        can_create_checkout_links=can_create_checkout_links,
        can_send_payment_links=can_send_payment_links,
        can_apply_coupons=can_apply_coupons,
        can_auto_generate_coupons=False,
        can_cancel_orders=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_tenant_a_catalog(db: Session, tenant_id: int) -> Tuple[Dict[str, Product], Dict[str, List[ProductVariant]]]:
    products: Dict[str, Product] = {}
    variants: Dict[str, List[ProductVariant]] = {}

    p_a, v_a = seed_product_rich(
        db,
        tenant_id,
        title="حذاء رياضي أبيض",
        external_id="sku-shoe-white",
        price="249",
        description="حذاء رياضي أبيض للجري",
        extra_metadata={
            "product_url": "https://store-a.test/products/shoe-white",
            "regular_price": "249",
            "category": "أحذية",
        },
        variant_specs=[
            {
                "option_summary": "مقاس 40",
                "options": {"size": "40"},
                "price": "249",
                "stock_quantity": 5,
                "in_stock": True,
                "is_default": True,
            },
            {
                "option_summary": "مقاس 42",
                "options": {"size": "42"},
                "price": "269",
                "stock_quantity": 3,
                "in_stock": True,
            },
        ],
    )
    products["A"] = p_a
    variants["A"] = v_a

    p_b, v_b = seed_product_rich(
        db,
        tenant_id,
        title="حذاء رياضي أسود",
        external_id="sku-shoe-black",
        price="259",
        extra_metadata={"product_url": "https://store-a.test/products/shoe-black"},
        variant_specs=[
            {
                "option_summary": "مقاس 41",
                "options": {"size": "41"},
                "price": "259",
                "stock_quantity": 2,
                "in_stock": True,
                "is_default": True,
            },
            {
                "option_summary": "مقاس 43",
                "options": {"size": "43"},
                "price": "259",
                "stock_quantity": 0,
                "in_stock": False,
            },
        ],
    )
    products["B"] = p_b
    variants["B"] = v_b

    p_c, v_c = seed_product_rich(
        db,
        tenant_id,
        title="عطر ورد 100ml",
        external_id="sku-perfume-rose",
        price="180",
        extra_metadata={
            "regular_price": "180",
            "sale_price": "180",
            "product_url": "https://store-a.test/products/perfume-rose",
        },
    )
    products["C"] = p_c
    variants["C"] = v_c

    p_d, v_d = seed_product_rich(
        db,
        tenant_id,
        title="عطر خشب 100ml",
        external_id="sku-perfume-wood",
        price="149",
        extra_metadata={
            "regular_price": "199",
            "sale_price": "149",
            "has_offer": True,
            "offer_label": "خصم موسمي",
            "product_url": "https://store-a.test/products/perfume-wood",
        },
    )
    products["D"] = p_d
    variants["D"] = v_d

    p_e, v_e = seed_product_rich(
        db,
        tenant_id,
        title="قميص قطني أزرق",
        external_id="sku-shirt-blue",
        price="99",
        in_stock=False,
        stock_quantity=0,
        extra_metadata={"product_url": "https://store-a.test/products/shirt-blue"},
    )
    products["E"] = p_e
    variants["E"] = v_e

    return products, variants


def _seed_tenant_a_kb(db: Session, tenant_id: int) -> Dict[str, MerchantKnowledgeSection]:
    return {
        "shipping": seed_knowledge_section(
            db,
            tenant_id,
            kind="shipping",
            title="مدة وكلفة الشحن للرياض",
            body="الشحن للرياض 2-3 أيام عمل — 25 ريال.",
        ),
        "payment": seed_knowledge_section(
            db,
            tenant_id,
            kind="payment",
            title="طرق الدفع",
            body="نقبل مدى، فيزا، أبل باي، والدفع عند الاستلام.",
        ),
        "returns": seed_knowledge_section(
            db,
            tenant_id,
            kind="policy",
            title="الاستبدال والاسترجاع",
            body="يمكن الاستبدال خلال 7 أيام بحالة المنتج الأصلية.",
        ),
        "hours": seed_knowledge_section(
            db,
            tenant_id,
            kind="hours",
            title="ساعات العمل",
            body="من السبت إلى الخميس 10ص–10م.",
        ),
        "staff": seed_knowledge_section(
            db,
            tenant_id,
            kind="staff_contact",
            title="تواصل مع الموظفين",
            body="للدعم: واتساب الموظفين 966541111001",
        ),
        "oos_policy": seed_knowledge_section(
            db,
            tenant_id,
            kind="policy",
            title="سياسة نفاد المخزون",
            body="عند نفاد المنتج نبلغك بموعد التوفر أو نقترح بديلاً.",
        ),
    }


def _seed_tenant_b_catalog(db: Session, tenant_id: int) -> Tuple[Dict[str, Product], Dict[str, List[ProductVariant]]]:
    products: Dict[str, Product] = {}
    variants: Dict[str, List[ProductVariant]] = {}
    p1, v1 = seed_product_rich(
        db,
        tenant_id,
        title="ساعة يد فضية",
        external_id="sku-b-watch",
        price="320",
        extra_metadata={"product_url": "https://store-b.test/watch"},
    )
    products["watch"] = p1
    variants["watch"] = v1
    p2, v2 = seed_product_rich(
        db,
        tenant_id,
        title="حقيبة يد جلد",
        external_id="sku-b-bag",
        price="210",
        in_stock=False,
        stock_quantity=0,
    )
    products["bag"] = p2
    variants["bag"] = v2
    return products, variants


def _seed_tenant_b_kb(db: Session, tenant_id: int) -> Dict[str, MerchantKnowledgeSection]:
    return {
        "shipping": seed_knowledge_section(
            db,
            tenant_id,
            kind="shipping",
            title="شحن متجر ب",
            body="شحن جدة فقط — 35 ريال خلال 4 أيام.",
        ),
        "staff": seed_knowledge_section(
            db,
            tenant_id,
            kind="staff_contact",
            title="دعم متجر ب",
            body="واتساب الدعم 966542222002",
        ),
    }


def seed_dual_tenant_world(db: Session) -> DualTenantWorld:
    """Build Tenant A + Tenant B with distinct catalog, KB, orders, permissions."""
    tenant_a = seed_tenant(db, name=TENANT_A_NAME)
    tenant_b = seed_tenant(db, name=TENANT_B_NAME)

    products_a, variants_a = _seed_tenant_a_catalog(db, tenant_a.id)
    kb_a = _seed_tenant_a_kb(db, tenant_a.id)

    cust_a = seed_customer(db, tenant_a.id, phone=PHONE_CUST_A, name="أحمد سالم")
    cust_b = seed_customer(db, tenant_a.id, phone=PHONE_CUST_B, name="نورة عبدالله")
    cust_c = seed_customer(db, tenant_a.id, phone=PHONE_CUST_C, name="خالد فهد")
    cust_d = seed_customer(db, tenant_a.id, phone=PHONE_CUST_D, name="سارة محمد")

    conv_a = seed_conversation(db, tenant_a.id, cust_a.id)
    conv_b = seed_conversation(db, tenant_a.id, cust_b.id)
    conv_c = seed_conversation(db, tenant_a.id, cust_c.id)
    conv_d = seed_conversation(db, tenant_a.id, cust_d.id)

    order_a = seed_order(
        db,
        tenant_a.id,
        status="processing",
        external_id=f"nahla-a-{tenant_a.id}-1",
        external_order_number="SLL-A-1001",
        customer_info={"phone": PHONE_CUST_A, "name": "أحمد سالم"},
        line_items=[{"title": "حذاء رياضي أبيض", "quantity": 1, "unit_price": "249"}],
    )
    order_b = seed_order(
        db,
        tenant_a.id,
        status="shipped",
        external_id=f"nahla-a-{tenant_a.id}-2",
        external_order_number="SLL-A-2002",
        customer_info={"phone": PHONE_CUST_B, "name": "نورة عبدالله"},
        line_items=[{"title": "عطر ورد 100ml", "quantity": 1, "unit_price": "180"}],
    )
    seed_shipment(db, tenant_a.id, order_b.id, tracking_number="TRK-A-7788", status="shipped")
    order_c1 = seed_order(
        db,
        tenant_a.id,
        status="processing",
        external_id=f"nahla-a-{tenant_a.id}-3",
        external_order_number="SLL-A-3003",
        customer_info={"phone": PHONE_CUST_C, "name": "خالد فهد"},
    )
    order_c2 = seed_order(
        db,
        tenant_a.id,
        status="delivered",
        external_id=f"nahla-a-{tenant_a.id}-4",
        external_order_number="SLL-A-3004",
        customer_info={"phone": PHONE_CUST_C, "name": "خالد فهد"},
    )

    perms_a = _seed_permissions(
        db,
        tenant_a.id,
        can_create_orders=True,
        can_apply_coupons=True,
    )

    products_b, variants_b = _seed_tenant_b_catalog(db, tenant_b.id)
    kb_b = _seed_tenant_b_kb(db, tenant_b.id)
    cust_b_a = seed_customer(db, tenant_b.id, phone=PHONE_B_CUST_A, name="ليلى حسن")
    cust_b_b = seed_customer(db, tenant_b.id, phone=PHONE_B_CUST_B, name="عمر يوسف")
    conv_b_a = seed_conversation(db, tenant_b.id, cust_b_a.id)
    conv_b_b = seed_conversation(db, tenant_b.id, cust_b_b.id)
    order_b_only = seed_order(
        db,
        tenant_b.id,
        status="processing",
        external_id=f"nahla-b-{tenant_b.id}-1",
        external_order_number="SLL-B-9001",
        customer_info={"phone": PHONE_B_CUST_A, "name": "ليلى حسن"},
        line_items=[{"title": "ساعة يد فضية", "quantity": 1, "unit_price": "320"}],
    )
    perms_b = _seed_permissions(
        db,
        tenant_b.id,
        can_create_orders=True,
        can_apply_coupons=False,
    )

    bundle_a = TenantBundle(
        tenant_id=tenant_a.id,
        name=TENANT_A_NAME,
        products=products_a,
        variants=variants_a,
        customers={"A": cust_a, "B": cust_b, "C": cust_c, "D": cust_d},
        conversations={"A": conv_a, "B": conv_b, "C": conv_c, "D": conv_d},
        orders={
            "A": order_a,
            "B": order_b,
            "C1": order_c1,
            "C2": order_c2,
        },
        kb=kb_a,
        permissions=perms_a,
    )
    bundle_b = TenantBundle(
        tenant_id=tenant_b.id,
        name=TENANT_B_NAME,
        products=products_b,
        variants=variants_b,
        customers={"A": cust_b_a, "B": cust_b_b},
        conversations={"A": conv_b_a, "B": conv_b_b},
        orders={"A": order_b_only},
        kb=kb_b,
        permissions=perms_b,
    )
    return DualTenantWorld(db=db, tenant_a=bundle_a, tenant_b=bundle_b)


def query_kb_sections(db: Session, tenant_id: int, *, kind: str = "") -> List[MerchantKnowledgeSection]:
    q = db.query(MerchantKnowledgeSection).filter_by(tenant_id=tenant_id, is_active=True)
    if kind:
        q = q.filter_by(kind=kind)
    return q.order_by(MerchantKnowledgeSection.id.asc()).all()


__all__ = [
    "DualTenantWorld",
    "TenantBundle",
    "TENANT_A_NAME",
    "TENANT_B_NAME",
    "PHONE_CUST_A",
    "PHONE_CUST_B",
    "PHONE_CUST_C",
    "PHONE_CUST_D",
    "make_scenario_db",
    "seed_dual_tenant_world",
    "seed_product_rich",
    "query_kb_sections",
]
