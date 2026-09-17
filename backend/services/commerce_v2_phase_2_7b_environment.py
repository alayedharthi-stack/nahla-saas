"""Isolated Phase 2.7B acceptance environment: provision, guard, clean up.

Phase 2.7B needs merchant knowledge to exercise, and merchant knowledge is the
merchant's own confidential text.  So the acceptance fixtures are never written
into the database the public API serves: this module provisions a complete
synthetic world — tenant, catalog, knowledge, customers, conversations and one
order — into a temporary database that must first prove it holds no real data.

The guard runs before any write.  Pointed at a production database it refuses
and writes nothing, because a production database contains rows outside the
synthetic acceptance tenant.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from modules.ai.commerce_agent_v2.internal_e2e_identity import (
    INTERNAL_E2E_CHANNEL,
    InternalE2EAlias,
    internal_e2e_customer_identity,
    internal_e2e_metadata,
    metadata_matches_internal_e2e_identity,
)

ISOLATED_ACCEPTANCE_ENV = "NAHLA_P27B_ISOLATED_ACCEPTANCE"
ACCEPTANCE_TENANT_MARKER = "PHASE_2_7B_SYNTHETIC_ACCEPTANCE"
# The acceptance conversations live on the INTERNAL_E2E channel, so the channel
# name is the canonical one rather than a second spelling of it.
ACCEPTANCE_CHANNEL = INTERNAL_E2E_CHANNEL
ACCEPTANCE_FIXTURE_CONTRACT = "phase_2_7b_acceptance_fixture_v2"


def acceptance_aliases() -> tuple[InternalE2EAlias, ...]:
    """Aliases the checked-in K01-K16 matrix requires, in matrix order."""
    from services.commerce_v2_phase_2_7b_knowledge_acceptance import (
        load_knowledge_acceptance_matrix,
    )

    return load_knowledge_acceptance_matrix().required_aliases

# Fixture kinds the matrix refers to by name.
FIXTURE_STORE_POLICY = "store_policy_relevant"
FIXTURE_PRODUCT_DESCRIPTION = "product_linked_description"
FIXTURE_PRODUCT_ORIGIN = "product_linked_origin"
FIXTURE_PRODUCT_USAGE = "product_linked_usage"
FIXTURE_STALE_PRICE = "product_linked_stale_price"
FIXTURE_STALE_AVAILABILITY = "product_linked_stale_availability"
FIXTURE_HEALTH_STATEMENT = "product_linked_health_statement"
FIXTURE_IRRELEVANT = "irrelevant_section"
FIXTURE_DELETED = "deleted_section"
FIXTURE_FOREIGN = "foreign_tenant_section"


class AcceptanceEnvironmentError(RuntimeError):
    """Fail-closed guard for the isolated Phase 2.7B acceptance environment."""


def _fail(code: str) -> AcceptanceEnvironmentError:
    return AcceptanceEnvironmentError(f"phase_2_7b_env_{code}")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def _acceptance_tenant_ids(db: Any) -> list[int]:
    from models import Tenant

    return [
        int(row.id)
        for row in db.query(Tenant).filter(Tenant.name.like(f"{ACCEPTANCE_TENANT_MARKER}%")).all()
    ]


def assert_isolated_acceptance_database(
    db: Any, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Refuse to touch anything unless this database holds only acceptance data.

    Two independent conditions must hold: the operator has declared this an
    isolated acceptance database, and the database itself contains no row that
    does not belong to a Phase 2.7B synthetic tenant.  A production database
    fails the second condition on its first real tenant, before any write.
    """
    from models import (
        Conversation,
        Customer,
        MerchantKnowledgeSection,
        MessageEvent,
        Order,
        Product,
        Tenant,
    )

    values = _env(env)
    if str(values.get(ISOLATED_ACCEPTANCE_ENV, "")).strip().lower() not in {"1", "true", "yes"}:
        raise _fail("not_declared_isolated")
    acceptance_ids = _acceptance_tenant_ids(db)
    foreign_tenants = [
        int(row.id)
        for row in db.query(Tenant).all()
        if int(row.id) not in acceptance_ids
    ]
    if foreign_tenants:
        raise _fail("database_contains_foreign_tenants")
    census: dict[str, int] = {}
    for name, model in (
        ("customers", Customer),
        ("conversations", Conversation),
        ("messages", MessageEvent),
        ("orders", Order),
        ("products", Product),
        ("knowledge_sections", MerchantKnowledgeSection),
    ):
        rows = db.query(model).all()
        census[name] = len(rows)
        outside = [
            row
            for row in rows
            if int(getattr(row, "tenant_id", 0) or 0) not in acceptance_ids
        ]
        if outside:
            raise _fail(f"database_contains_foreign_rows:{name}")
    return {
        "isolated": True,
        "acceptance_tenant_ids": acceptance_ids,
        "row_census": census,
    }


def verify_acceptance_fixtures(db: Any, tenant_id: int) -> dict[str, Any]:
    """Prove every matrix alias resolves to exactly one canonical fixture.

    This is the check the provisioner failed to make before: it wrote fixtures
    in one shape and the turn path looked them up in another.  Here the lookup
    is the canonical one, so a fixture that the run could not resolve cannot be
    reported as provisioned.  Duplicates, a wrong tenant, a stray customer_id
    and half-written metadata each fail closed rather than reaching a run.
    """
    from models import Conversation

    resolved: dict[str, int] = {}
    for alias in acceptance_aliases():
        identity = internal_e2e_customer_identity(tenant_id, alias)
        rows = (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == int(tenant_id),
                Conversation.external_id == identity,
            )
            .all()
        )
        if len(rows) != 1:
            raise _fail(f"fixture_missing_or_ambiguous:{alias}")
        conversation = rows[0]
        if conversation.customer_id is not None:
            raise _fail(f"fixture_customer_id_not_null:{alias}")
        if not metadata_matches_internal_e2e_identity(
            conversation.extra_metadata, tenant_id=tenant_id, alias=alias
        ):
            raise _fail(f"fixture_metadata_not_canonical:{alias}")
        resolved[alias] = int(conversation.id)

    # A conversation on this tenant that is not one of the resolved fixtures is
    # an unknown fixture; refuse rather than run beside it.
    known = set(resolved.values())
    strays = [
        int(row.id)
        for row in db.query(Conversation)
        .filter(Conversation.tenant_id == int(tenant_id))
        .all()
        if int(row.id) not in known
    ]
    if strays:
        raise _fail("fixture_unexpected_conversation_present")
    return {"aliases": sorted(resolved), "conversations": resolved}


def provision_knowledge_acceptance_environment(
    db: Any, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Create the full synthetic world once; calling it again changes nothing."""
    from models import (
        Conversation,
        MerchantKnowledgeSection,
        MerchantKnowledgeSectionProduct,
        Order,
        OrderShipment,
        Product,
        Tenant,
        TenantSettings,
        WhatsAppConnection,
    )

    assert_isolated_acceptance_database(db, env=env)
    tenant = (
        db.query(Tenant)
        .filter(Tenant.name == ACCEPTANCE_TENANT_MARKER)
        .one_or_none()
    )
    if tenant is not None:
        # Reprovisioning changes nothing, but it still has to prove the world
        # already there is the one a run can resolve.
        verify_acceptance_fixtures(db, int(tenant.id))
        return describe_knowledge_acceptance_environment(db, env=env)

    tenant = Tenant(name=ACCEPTANCE_TENANT_MARKER, is_active=True)
    foreign = Tenant(name=f"{ACCEPTANCE_TENANT_MARKER}_NEIGHBOUR", is_active=True)
    db.add_all([tenant, foreign])
    db.flush()
    db.add_all(
        [
            TenantSettings(tenant_id=tenant.id, ai_settings={"locale": "ar-SA"}),
            TenantSettings(tenant_id=foreign.id, ai_settings={"locale": "ar-SA"}),
            WhatsAppConnection(tenant_id=tenant.id, status="connected"),
        ]
    )
    db.flush()

    products = {
        "jacket": Product(
            tenant_id=tenant.id,
            external_id="P27B-JACKET",
            title="جاكيت شتوي",
            description="جاكيت خفيف",
            price="169",
            stock_quantity=2,
            in_stock=True,
            catalog_status="active",
            extra_metadata={
                "status": "active",
                "in_stock": True,
                "stock_qty": 2,
                "currency": "SAR",
                "product_url": "https://shop.example.invalid/products/jacket",
            },
        ),
        "skirt": Product(
            tenant_id=tenant.id,
            external_id="P27B-SKIRT",
            title="تنورة قطنية",
            description="تنورة صيفية",
            price="114",
            stock_quantity=1,
            in_stock=True,
            catalog_status="active",
            extra_metadata={
                "status": "active",
                "in_stock": True,
                "stock_qty": 1,
                "currency": "SAR",
                "sale_price": "114",
                "regular_price": "229",
            },
        ),
        "soap": Product(
            tenant_id=tenant.id,
            external_id="P27B-SOAP",
            title="صابون طبيعي",
            description="صابون يدوي",
            price="45",
            stock_quantity=0,
            in_stock=False,
            catalog_status="active",
            extra_metadata={
                "status": "active",
                "in_stock": False,
                "stock_qty": 0,
                "currency": "SAR",
            },
        ),
        "honey": Product(
            tenant_id=tenant.id,
            external_id="P27B-HONEY",
            title="عسل جبلي",
            description="عبوة نصف كيلو",
            price="249",
            stock_quantity=4,
            in_stock=True,
            catalog_status="active",
            extra_metadata={
                "status": "active",
                "in_stock": True,
                "stock_qty": 4,
                "currency": "SAR",
            },
        ),
    }
    db.add_all(list(products.values()))
    db.flush()

    sections = {
        FIXTURE_STORE_POLICY: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="custom",
            title="سياسة الاسترجاع",
            body="الاسترجاع متاح خلال سبعة أيام من الاستلام، والمنتجات محلية الصنع.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_PRODUCT_DESCRIPTION: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="product_info",
            title="وصف الجاكيت",
            body="الجاكيت مبطن بطبقة خفيفة ومناسب للأجواء المعتدلة.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_PRODUCT_ORIGIN: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="product_info",
            title="مصدر الجاكيت",
            body="مصدر هذا الجاكيت من ورشة محلية وخامته قطن مخلوط.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_PRODUCT_USAGE: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="product_info",
            title="طريقة العناية بالتنورة",
            body="تُغسل التنورة على حرارة منخفضة وتُجفف بعيدًا عن الشمس المباشرة.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_STALE_PRICE: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="product_info",
            title="نشرة قديمة عن العسل",
            body="سعر العسل الجبلي ٩٩ ريالًا حسب نشرتنا القديمة.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_STALE_AVAILABILITY: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="product_info",
            title="ملاحظة قديمة عن الصابون",
            body="الصابون الطبيعي متوفر دائمًا في الفرع الرئيسي.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_HEALTH_STATEMENT: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="product_info",
            title="بيان التاجر عن العسل",
            body="يقول التاجر إن العسل الجبلي يُستخدم تقليديًا كمُحلٍّ طبيعي.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_IRRELEVANT: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="custom",
            title="مواعيد الفرع",
            body="يفتح الفرع من العاشرة صباحًا حتى العاشرة مساءً.",
            is_active=True,
            ai_status="approved",
        ),
        FIXTURE_DELETED: MerchantKnowledgeSection(
            tenant_id=tenant.id,
            kind="product_info",
            title="بيان ملغى عن الصابون",
            body="هذا البيان أُلغي ويجب ألا يظهر للعميل إطلاقًا.",
            is_active=False,
            ai_status="approved",
        ),
        FIXTURE_FOREIGN: MerchantKnowledgeSection(
            tenant_id=foreign.id,
            kind="custom",
            title="سياسة متجر آخر",
            body="سياسة متجر مجاور لا يجوز أن تظهر في هذا المتجر إطلاقًا.",
            is_active=True,
            ai_status="approved",
        ),
    }
    db.add_all(list(sections.values()))
    db.flush()
    db.add_all(
        [
            MerchantKnowledgeSectionProduct(
                section_id=sections[FIXTURE_PRODUCT_DESCRIPTION].id,
                product_id=products["jacket"].id,
                source="manual",
            ),
            MerchantKnowledgeSectionProduct(
                section_id=sections[FIXTURE_PRODUCT_ORIGIN].id,
                product_id=products["jacket"].id,
                source="manual",
            ),
            MerchantKnowledgeSectionProduct(
                section_id=sections[FIXTURE_PRODUCT_USAGE].id,
                product_id=products["skirt"].id,
                source="manual",
            ),
            MerchantKnowledgeSectionProduct(
                section_id=sections[FIXTURE_STALE_PRICE].id,
                product_id=products["honey"].id,
                source="manual",
            ),
            MerchantKnowledgeSectionProduct(
                section_id=sections[FIXTURE_STALE_AVAILABILITY].id,
                product_id=products["soap"].id,
                source="manual",
            ),
            MerchantKnowledgeSectionProduct(
                section_id=sections[FIXTURE_HEALTH_STATEMENT].id,
                product_id=products["honey"].id,
                source="manual",
            ),
            MerchantKnowledgeSectionProduct(
                section_id=sections[FIXTURE_DELETED].id,
                product_id=products["soap"].id,
                source="manual",
            ),
        ]
    )

    # The acceptance turns are submitted through ``submit_internal_customer_turn``,
    # which resolves its conversation with ``_find_fixture``.  That resolver owns
    # the identity contract, so the fixtures are built from the same canonical
    # helpers it reads back: a conversation whose ``external_id`` is the canonical
    # identity, no ``customer_id``, and the canonical metadata block.  Phase 2.7B
    # keys are added on top of that block, never in place of it.
    conversations: dict[str, Conversation] = {}
    for alias in acceptance_aliases():
        identity = internal_e2e_customer_identity(tenant.id, alias)
        conversation = Conversation(
            tenant_id=tenant.id,
            customer_id=None,
            status="active",
            external_id=identity,
            extra_metadata={
                **internal_e2e_metadata(tenant.id, alias),
                "phase": "2.7B",
                "fixture_contract": ACCEPTANCE_FIXTURE_CONTRACT,
            },
        )
        db.add(conversation)
        db.flush()
        conversations[alias] = conversation

    primary_identity = internal_e2e_customer_identity(
        tenant.id, acceptance_aliases()[0]
    )
    order = Order(
        tenant_id=tenant.id,
        customer_id=None,
        external_id="P27B-ORDER-001",
        external_order_number="P27B-K-001",
        status="draft",
        total="249.00",
        customer_name="PHASE 2.7B SYNTHETIC CUSTOMER",
        customer_info={"name": "PHASE 2.7B SYNTHETIC CUSTOMER", "phone": primary_identity},
        line_items=[{"name": "عسل جبلي", "quantity": 1, "price": "249.00"}],
        source=ACCEPTANCE_CHANNEL,
    )
    db.add(order)
    db.flush()
    db.add(
        OrderShipment(
            tenant_id=tenant.id,
            order_id=order.id,
            status="in_transit",
            tracking_number="P27B-TRACK-001",
            recipient_name="PHASE 2.7B SYNTHETIC CUSTOMER",
            recipient_phone=primary_identity,
            extra_metadata={"synthetic": True, "test_only": True, "external_mutation_allowed": False},
        )
    )
    db.flush()
    # Refuse to commit a world the run could not resolve.
    verify_acceptance_fixtures(db, int(tenant.id))
    db.commit()
    return describe_knowledge_acceptance_environment(db, env=env)


def describe_knowledge_acceptance_environment(
    db: Any, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Report what the acceptance world currently holds, without changing it."""
    from models import (
        Conversation,
        Customer,
        MerchantKnowledgeSection,
        Order,
        Product,
        Tenant,
    )

    tenant = db.query(Tenant).filter(Tenant.name == ACCEPTANCE_TENANT_MARKER).one_or_none()
    if tenant is None:
        return {"provisioned": False, "tenant_id": None}
    ids = _acceptance_tenant_ids(db)
    sections = (
        db.query(MerchantKnowledgeSection)
        .filter(MerchantKnowledgeSection.tenant_id.in_(ids))
        .all()
    )
    return {
        "provisioned": True,
        "tenant_id": int(tenant.id),
        "acceptance_tenant_ids": sorted(ids),
        "products": {
            str(row.external_id): int(row.id)
            for row in db.query(Product).filter(Product.tenant_id == tenant.id).all()
        },
        "knowledge_sections": {
            str(row.title): {
                "id": int(row.id),
                "tenant_id": int(row.tenant_id),
                "is_active": bool(row.is_active),
            }
            for row in sections
        },
        "conversations": {
            str(row.external_id): int(row.id)
            for row in db.query(Conversation).filter(Conversation.tenant_id == tenant.id).all()
        },
        "fixture_contract": ACCEPTANCE_FIXTURE_CONTRACT,
        "resolvable_aliases": sorted(acceptance_aliases()),
        "customers": int(
            db.query(Customer).filter(Customer.tenant_id == tenant.id).count()
        ),
        "orders": int(db.query(Order).filter(Order.tenant_id == tenant.id).count()),
    }


def cleanup_knowledge_acceptance_environment(
    db: Any, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Remove the synthetic world and report exactly what was deleted."""
    from models import (
        Conversation,
        Customer,
        MerchantKnowledgeSection,
        MerchantKnowledgeSectionProduct,
        MessageEvent,
        Order,
        OrderShipment,
        Product,
        Tenant,
        TenantSettings,
        WhatsAppConnection,
    )

    assert_isolated_acceptance_database(db, env=env)
    ids = _acceptance_tenant_ids(db)
    if not ids:
        return {"deleted": {}, "acceptance_tenant_ids": [], "remaining_rows": 0}
    order_ids = [
        int(row.id) for row in db.query(Order).filter(Order.tenant_id.in_(ids)).all()
    ]
    section_ids = [
        int(row.id)
        for row in db.query(MerchantKnowledgeSection)
        .filter(MerchantKnowledgeSection.tenant_id.in_(ids))
        .all()
    ]
    deleted: dict[str, int] = {}
    deleted["order_shipments"] = (
        db.query(OrderShipment)
        .filter(OrderShipment.order_id.in_(order_ids))
        .delete(synchronize_session=False)
        if order_ids
        else 0
    )
    deleted["knowledge_section_products"] = (
        db.query(MerchantKnowledgeSectionProduct)
        .filter(MerchantKnowledgeSectionProduct.section_id.in_(section_ids))
        .delete(synchronize_session=False)
        if section_ids
        else 0
    )
    for name, model, column in (
        ("messages", MessageEvent, MessageEvent.tenant_id),
        ("orders", Order, Order.tenant_id),
        ("conversations", Conversation, Conversation.tenant_id),
        ("customers", Customer, Customer.tenant_id),
        ("products", Product, Product.tenant_id),
        ("knowledge_sections", MerchantKnowledgeSection, MerchantKnowledgeSection.tenant_id),
        ("whatsapp_connections", WhatsAppConnection, WhatsAppConnection.tenant_id),
        ("tenant_settings", TenantSettings, TenantSettings.tenant_id),
    ):
        deleted[name] = db.query(model).filter(column.in_(ids)).delete(synchronize_session=False)
    deleted["tenants"] = db.query(Tenant).filter(Tenant.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    remaining = sum(
        db.query(model).count()
        for model in (Customer, Conversation, MessageEvent, Order, Product, MerchantKnowledgeSection)
    )
    return {
        "deleted": {key: int(value or 0) for key, value in deleted.items()},
        "acceptance_tenant_ids": sorted(ids),
        "remaining_rows": int(remaining),
    }


__all__ = [
    "ACCEPTANCE_CHANNEL",
    "ACCEPTANCE_FIXTURE_CONTRACT",
    "ACCEPTANCE_TENANT_MARKER",
    "AcceptanceEnvironmentError",
    "FIXTURE_DELETED",
    "FIXTURE_FOREIGN",
    "FIXTURE_HEALTH_STATEMENT",
    "FIXTURE_IRRELEVANT",
    "FIXTURE_PRODUCT_DESCRIPTION",
    "FIXTURE_PRODUCT_ORIGIN",
    "FIXTURE_PRODUCT_USAGE",
    "FIXTURE_STALE_AVAILABILITY",
    "FIXTURE_STALE_PRICE",
    "FIXTURE_STORE_POLICY",
    "ISOLATED_ACCEPTANCE_ENV",
    "assert_isolated_acceptance_database",
    "cleanup_knowledge_acceptance_environment",
    "describe_knowledge_acceptance_environment",
    "acceptance_aliases",
    "provision_knowledge_acceptance_environment",
    "verify_acceptance_fixtures",
]
