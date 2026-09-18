"""Real 0107 persistence of admin labels without checkout identity promotion."""
import pytest

from core.customer_identity_resolver import apply_customer_name, display_name_for_customer
from models import CustomerNameProvenance
from modules.ai.brain.commerce.catalog_checkout_customer_identity import resolve_catalog_checkout_customer_identity
from test_customer_name_provenance_read_isolation_pg import (
    _seed, _session, pg_with_provenance_table,  # noqa: F401 — shared real PostgreSQL fixture
)


@pytest.mark.parametrize("label", ["مشاعل", "李雷", "José", "Ирина", "Acme Studio"])
def test_provider_display_persists_without_operational_identity(pg_with_provenance_table, label):
    with _session(pg_with_provenance_table) as db:
        first, second = _seed(db), _seed(db)
        for c, value in [(first, label), (second, "Verified Person")]:
            apply_customer_name(c, value, source="whatsapp_profile" if c is first else "shopify_sync")
        db.commit()
        for _ in range(2):
            apply_customer_name(first, label, source="whatsapp_profile")
        db.commit()
        db.expire_all()
        assert display_name_for_customer(first) == label
        assert not resolve_catalog_checkout_customer_identity(customer=first, profile={"name": label}).customer_name_known
        assert display_name_for_customer(second) == "Verified Person"
        assert resolve_catalog_checkout_customer_identity(customer=second).customer_name_known
        row = db.query(CustomerNameProvenance).filter_by(tenant_id=first.tenant_id, customer_id=first.id).one()
        assert row.profile_hint == label
        assert row.last_attempt_source == "whatsapp_profile"
        assert row.canonical_name is None
        assert db.query(CustomerNameProvenance).filter_by(customer_id=first.id).count() == 1
        apply_customer_name(first, "أحمد سالم", source="customer_message",
                            message_context={"message": "اسمي أحمد سالم", "message_id": "synthetic-proof"})
        db.commit()
        db.expire_all()
        assert row.canonical_name == "أحمد سالم"
        assert row.profile_hint == label
        assert row.authority == "CUSTOMER_SELF_REPORTED"
        assert row.evidence_ref["message_id"] == "synthetic-proof"
        assert resolve_catalog_checkout_customer_identity(customer=first).customer_name_known
