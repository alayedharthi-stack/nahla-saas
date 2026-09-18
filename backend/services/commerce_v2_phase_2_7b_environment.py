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
# The two directions the model's session reads back as conversation history.
# The reset boundary clears exactly these and nothing else, so a run's own
# control row — also a MessageEvent on this conversation — survives.
ACCEPTANCE_TURN_DIRECTIONS = ("internal_e2e_inbound", "internal_e2e_outbound")

# Which product each knowledge fixture is attached to, and the title it is
# stored under.  The v2 matrix binds a case to a fixture; this is how that
# binding is checked against the database before the run spends a case.
FIXTURE_SECTION_TITLES = {
    "store_policy_relevant": "سياسة الاسترجاع",
    "product_linked_description": "وصف الجاكيت",
    "product_linked_origin": "مصدر الجاكيت",
    "product_linked_usage": "طريقة العناية بالتنورة",
    "product_linked_stale_price": "نشرة قديمة عن العسل",
    "product_linked_stale_availability": "ملاحظة قديمة عن الصابون",
    "product_linked_health_statement": "بيان التاجر عن العسل",
    "irrelevant_section": "مواعيد الفرع",
    "deleted_section": "بيان ملغى عن الصابون",
    "foreign_tenant_section": "سياسة متجر آخر",
}
FIXTURE_PRODUCT_SKUS = {
    "product_linked_description": "P27B-JACKET",
    "product_linked_origin": "P27B-JACKET",
    "product_linked_usage": "P27B-SKIRT",
    "product_linked_stale_price": "P27B-HONEY",
    "product_linked_stale_availability": "P27B-SOAP",
    "product_linked_health_statement": "P27B-HONEY",
    "deleted_section": "P27B-SOAP",
}
DELETED_FIXTURES = frozenset({"deleted_section"})
FOREIGN_FIXTURES = frozenset({"foreign_tenant_section"})


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


def verify_case_bindings(db: Any, tenant_id: int, matrix: Any) -> dict[str, Any]:
    """Fail closed unless every v2 case can actually reach what it declares.

    Run 1 spent sixteen cases against fixtures several of them could never
    touch: the usage section was on the skirt while the turn resolved the
    jacket, the stale price was on the honey, the deleted section on the soap.
    Nothing checked, so the run reported failures that were really unreachable
    expectations.  This refuses to start instead.
    """
    from models import (
        MerchantKnowledgeSection,
        MerchantKnowledgeSectionProduct,
        Product,
        Tenant,
    )

    products = {
        str(row.external_id): row
        for row in db.query(Product).filter(Product.tenant_id == int(tenant_id)).all()
    }
    sections = {
        str(row.title): row
        for row in db.query(MerchantKnowledgeSection)
        .filter(MerchantKnowledgeSection.tenant_id == int(tenant_id))
        .all()
    }
    foreign_ids = [
        int(row.id)
        for row in db.query(Tenant).all()
        if int(row.id) != int(tenant_id)
    ]
    foreign_sections = {
        str(row.title): row
        for row in db.query(MerchantKnowledgeSection)
        .filter(MerchantKnowledgeSection.tenant_id.in_(foreign_ids))
        .all()
    } if foreign_ids else {}

    bound: dict[str, Any] = {}
    for case in matrix.cases:
        sku = case.target_product_sku
        fixture = case.required_section_fixture
        entry: dict[str, Any] = {"thread": case.thread, "reset": case.reset_thread_before}
        product = None
        if sku:
            product = products.get(sku)
            if product is None:
                raise _fail(f"binding_target_product_missing:{case.case_id}:{sku}")
            entry["product_id"] = int(product.id)
            entry["product_sku"] = sku
        if fixture:
            title = FIXTURE_SECTION_TITLES.get(fixture)
            section = sections.get(title or "")
            if section is None:
                raise _fail(f"binding_section_missing:{case.case_id}:{fixture}")
            if int(section.tenant_id) != int(tenant_id):
                raise _fail(f"binding_section_wrong_tenant:{case.case_id}:{fixture}")
            entry["section_id"] = int(section.id)
            entry["section_fixture"] = fixture
            expected_sku = FIXTURE_PRODUCT_SKUS.get(fixture)
            if expected_sku:
                if expected_sku != sku:
                    raise _fail(
                        f"binding_section_not_linked_to_target:{case.case_id}:{fixture}"
                    )
                links = {
                    int(row.product_id)
                    for row in db.query(MerchantKnowledgeSectionProduct)
                    .filter(MerchantKnowledgeSectionProduct.section_id == int(section.id))
                    .all()
                }
                if product is None or int(product.id) not in links:
                    raise _fail(
                        f"binding_section_link_missing:{case.case_id}:{fixture}"
                    )
        for forbidden in case.forbidden_section_fixtures:
            title = FIXTURE_SECTION_TITLES.get(forbidden)
            if forbidden in FOREIGN_FIXTURES:
                row = foreign_sections.get(title or "")
                if row is None:
                    raise _fail(f"binding_foreign_fixture_missing:{case.case_id}")
                entry.setdefault("forbidden_section_ids", []).append(int(row.id))
            else:
                row = sections.get(title or "")
                if row is None:
                    raise _fail(f"binding_forbidden_fixture_missing:{case.case_id}")
                if forbidden in DELETED_FIXTURES and bool(row.is_active):
                    raise _fail(f"binding_deleted_fixture_still_active:{case.case_id}")
                entry.setdefault("forbidden_section_ids", []).append(int(row.id))
        if case.knowledge_fault_mode:
            entry["knowledge_fault_mode"] = case.knowledge_fault_mode
        bound[case.case_id] = entry
    return {"contract_version": matrix.contract_version, "cases": bound}


def reset_acceptance_thread(
    db: Any, *, tenant_id: int, conversation_id: int, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Clear one synthetic conversation's stored turns, and prove it is clear.

    This is the reset boundary the v2 matrix relies on.  It has to delete rows,
    not just drop in-process state: the model's history comes from
    ``MessageEvent`` rows read back by ``ConversationMessageSession``, so a
    previous case's turns reach the next case's prompt until those rows are
    gone.  That is exactly how Run 1 let «هذا المنتج» keep resolving to
    whichever product an earlier case had surfaced.

    Refuses outside the isolated acceptance database, and only ever touches the
    one synthetic conversation it is given.
    """
    from models import MessageEvent

    assert_isolated_acceptance_database(db, env=env)

    def _turns() -> Any:
        return db.query(MessageEvent).filter(
            MessageEvent.tenant_id == int(tenant_id),
            MessageEvent.conversation_id == int(conversation_id),
            MessageEvent.direction.in_(ACCEPTANCE_TURN_DIRECTIONS),
        )

    before = _turns().count()
    deleted = _turns().delete(synchronize_session=False)
    db.commit()
    remaining = _turns().count()
    if remaining:
        raise _fail("thread_reset_incomplete")
    return {
        "conversation_id": int(conversation_id),
        "messages_before": int(before),
        "messages_deleted": int(deleted),
        "messages_remaining": int(remaining),
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
    "ACCEPTANCE_TURN_DIRECTIONS",
    "reset_acceptance_thread",
    "verify_case_bindings",
    "provision_knowledge_acceptance_environment",
    "verify_acceptance_fixtures",
]
