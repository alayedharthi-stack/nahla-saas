"""Nahla library import — order_summary (order_confirmation r3) only."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import JSON, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.nahla_library_order_confirmation_import import (  # noqa: E402
    MSG_ACTIVE_EXISTS_DRAFT,
    MSG_EXISTING_DRAFT,
    MSG_EXISTING_PENDING,
    MSG_LANGUAGE_AR_ONLY,
    MSG_STORE_INTEGRATION_REQUIRED,
    NahlaLibraryImportError,
    build_merchant_import_api_payload,
    find_existing_order_confirmation_library_draft,
    import_order_summary_from_library,
    inspect_whatsapp_template_schema,
    is_order_confirmation_r3_contract,
    order_summary_r3_components,
)
from core.commerce_lifecycle.order_confirmation_assets import (  # noqa: E402
    ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL,
    order_confirmation_header_public_url,
)
from core.merchant_capabilities import MerchantCapabilities  # noqa: E402
from models import WhatsAppTemplate  # noqa: E402
from services.whatsapp_templates.nahla_templates import get_template_by_key  # noqa: E402

_STORE_INTEGRATION_CAPS = MerchantCapabilities(
    has_external_store=True,
    supports_external_checkout=True,
    supports_external_coupons=True,
    supports_whatsapp_orders=False,
    supports_nahla_orders=False,
    supports_bank_transfer=False,
    supports_cod=True,
    has_whatsapp_catalog=False,
    has_external_tracking=True,
    has_nahla_tracking=False,
    has_payment_link=True,
)

_WHATSAPP_ONLY_CAPS = MerchantCapabilities(
    has_external_store=False,
    supports_external_checkout=False,
    supports_external_coupons=False,
    supports_whatsapp_orders=True,
    supports_nahla_orders=True,
    supports_bank_transfer=True,
    supports_cod=True,
    has_whatsapp_catalog=False,
    has_external_tracking=False,
    has_nahla_tracking=True,
    has_payment_link=False,
)

_MERCHANT_RESPONSE_FORBIDDEN = (
    "uq_active_lifecycle_template_null_step",
    "alembic_version",
    "supersedes_template_id_column",
    "revision_column",
    "schema_probe",
    "IntegrityError",
    "SELECT ",
    "INSERT ",
    "UPDATE ",
    "DELETE ",
    "whatsapp_templates.revision",
    "duplicate key",
    "psycopg2",
    "sqlalchemy.exc",
)


def _make_db(*models) -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for model in models:
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal(), engine


def _active_approved(db, *, tenant_id: int = 1) -> WhatsAppTemplate:
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name="nahla_order_summary_live",
        language="ar",
        category="UTILITY",
        status="APPROVED",
        components=[
            {"type": "BODY", "text": "قديم {{1}} {{2}} {{3}}"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "رابط", "url": "https://example.com/{{1}}"},
                ],
            },
        ],
        service_key="order_confirmation",
        is_active=True,
        is_hidden=False,
        step_number=None,
        revision=1,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


def _tpl_to_dict(tpl: WhatsAppTemplate) -> Dict[str, Any]:
    return {
        "id": tpl.id,
        "name": tpl.name,
        "status": tpl.status,
        "service_key": tpl.service_key,
        "nahla_source_key": tpl.nahla_source_key,
        "is_active": tpl.is_active,
        "components": tpl.components,
    }


def _assert_merchant_safe_payload(payload: Dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    for token in _MERCHANT_RESPONSE_FORBIDDEN:
        assert token.lower() not in serialized


@pytest.fixture(autouse=True)
def _enable_store_integration_for_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.merchant_capabilities.resolve_merchant_capabilities",
        lambda db, tenant_id: _STORE_INTEGRATION_CAPS,
    )


class TestOrderSummaryLibraryDefinition:
    def test_library_card_uses_r3_contract(self):
        tpl_def = get_template_by_key("order_summary")
        assert tpl_def is not None
        assert tpl_def.get("revision_contract") == "r3"
        assert is_order_confirmation_r3_contract(tpl_def["components"])
        assert "example.com" not in str(tpl_def["components"])
        header_url = tpl_def["components"][0]["example"]["header_url"]
        assert header_url == ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL
        assert "app.nahlah.ai" not in header_url

    def test_other_lifecycle_library_cards_remain_untouched(self):
        shipping = get_template_by_key("shipping_update")
        cod = get_template_by_key("cod_confirmation")
        assert shipping is not None and cod is not None
        assert shipping["service_key"] == "shipping_tracking"
        assert cod["service_key"] == "cod_confirmation"
        assert "example.com" in json.dumps(shipping["components"], ensure_ascii=False)
        assert shipping.get("revision_contract") is None
        assert cod.get("revision_contract") is None


class TestOrderConfirmationLibraryImport:
    def test_import_with_active_creates_inactive_draft_linked_to_active(self):
        db, _ = _make_db(WhatsAppTemplate)
        active = _active_approved(db)
        tpl_def = get_template_by_key("order_summary")
        outcome = import_order_summary_from_library(db, 1, tpl_def)
        draft = outcome["template"]
        assert outcome["created"] is True
        assert outcome["active_template_preserved"] is True
        assert MSG_ACTIVE_EXISTS_DRAFT in outcome["message"]
        assert draft.status == "DRAFT"
        assert draft.is_active is False
        assert draft.service_key == "order_confirmation"
        assert draft.nahla_source_key == "order_summary"
        assert is_order_confirmation_r3_contract(draft.components)
        assert int(draft.supersedes_template_id) == int(active.id)
        assert int(draft.revision) == 2
        db.refresh(active)
        assert active.is_active is True

    def test_reuses_existing_r3_draft_without_duplicate(self):
        db, _ = _make_db(WhatsAppTemplate)
        _active_approved(db)
        tpl_def = get_template_by_key("order_summary")
        first = import_order_summary_from_library(db, 1, tpl_def)
        second = import_order_summary_from_library(db, 1, tpl_def)
        assert first["created"] is True
        assert second["created"] is False
        assert second["reused_existing_draft"] is True
        assert second["message"] == MSG_EXISTING_DRAFT
        assert int(second["template"].id) == int(first["template"].id)
        count = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == 1,
                WhatsAppTemplate.nahla_source_key == "order_summary",
                WhatsAppTemplate.is_hidden.is_(False),
            )
            .count()
        )
        assert count == 1

    def test_reused_draft_matches_tenant_service_library_and_r3_not_legacy(self):
        db, _ = _make_db(WhatsAppTemplate)
        _active_approved(db, tenant_id=1)
        legacy = WhatsAppTemplate(
            tenant_id=1,
            name="nahla_order_summary_legacy",
            language="ar",
            category="UTILITY",
            status="DRAFT",
            components=[{"type": "BODY", "text": "{{1}} {{2}} {{3}}"}],
            service_key="order_confirmation",
            nahla_source_key="order_summary",
            is_active=False,
            is_hidden=False,
        )
        other_tenant_r3 = WhatsAppTemplate(
            tenant_id=2,
            name="nahla_order_summary_t2",
            language="ar",
            category="UTILITY",
            status="DRAFT",
            components=order_summary_r3_components(),
            service_key="order_confirmation",
            nahla_source_key="order_summary",
            is_active=False,
            is_hidden=False,
        )
        db.add_all([legacy, other_tenant_r3])
        db.commit()

        tpl_def = get_template_by_key("order_summary")
        created = import_order_summary_from_library(db, 1, tpl_def)
        reused = import_order_summary_from_library(db, 1, tpl_def)
        draft = reused["template"]

        assert reused["reused_existing_draft"] is True
        assert int(draft.id) == int(created["template"].id)
        assert int(draft.tenant_id) == 1
        assert draft.service_key == "order_confirmation"
        assert draft.nahla_source_key == "order_summary"
        assert is_order_confirmation_r3_contract(draft.components)
        assert not is_order_confirmation_r3_contract(legacy.components)
        assert int(draft.id) != int(legacy.id)
        assert int(draft.id) != int(other_tenant_r3.id)

    def test_find_existing_matches_r3_only(self):
        db, _ = _make_db(WhatsAppTemplate)
        legacy = WhatsAppTemplate(
            tenant_id=1,
            name="nahla_order_summary_old",
            language="ar",
            category="UTILITY",
            status="DRAFT",
            components=[{"type": "BODY", "text": "{{1}} {{2}} {{3}}"}],
            service_key="order_confirmation",
            nahla_source_key="order_summary",
            is_active=False,
            is_hidden=False,
        )
        db.add(legacy)
        db.commit()
        assert find_existing_order_confirmation_library_draft(db, 1) is None

        r3 = WhatsAppTemplate(
            tenant_id=1,
            name="nahla_order_summary_r3",
            language="ar",
            category="UTILITY",
            status="DRAFT",
            components=order_summary_r3_components(),
            service_key="order_confirmation",
            nahla_source_key="order_summary",
            is_active=False,
            is_hidden=False,
        )
        db.add(r3)
        db.commit()
        found = find_existing_order_confirmation_library_draft(db, 1)
        assert found is not None
        assert int(found.id) == int(r3.id)

    def test_schema_probe_logged_internally_not_returned(self, caplog: pytest.LogCaptureFixture):
        db, _ = _make_db(WhatsAppTemplate)
        with caplog.at_level("INFO"):
            import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        probe_logs = [
            r for r in caplog.records
            if getattr(r, "event", None) == "nahla_import_order_summary_schema_probe"
            or "NahlaImport:OC:schema_probe" in r.getMessage()
        ]
        assert probe_logs
        outcome = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        assert "schema_probe" not in outcome

    def test_merchant_api_payload_has_no_sql_or_schema_leaks(self):
        db, _ = _make_db(WhatsAppTemplate)
        _active_approved(db)
        outcome = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        payload = build_merchant_import_api_payload(outcome, template_to_dict=_tpl_to_dict)
        _assert_merchant_safe_payload(payload)

    def test_merchant_api_payload_sanitizes_import_errors(self):
        err = NahlaLibraryImportError(
            MSG_ACTIVE_EXISTS_DRAFT,
            error_code="nahla_import_lifecycle_active_conflict",
        )
        detail = err.message
        _assert_merchant_safe_payload({"detail": detail})

    def test_concurrent_imports_create_single_draft(self):
        db_factory, engine = _make_db(WhatsAppTemplate)
        session_main = db_factory
        _active_approved(session_main)
        tpl_def = get_template_by_key("order_summary")
        SessionLocal = sessionmaker(bind=engine)
        results: List[Dict[str, Any]] = [{} , {}]
        errors: List[BaseException] = []

        def _worker(idx: int) -> None:
            session = SessionLocal()
            try:
                results[idx] = import_order_summary_from_library(session, 1, tpl_def)
            except BaseException as exc:  # noqa: BLE001 — test harness
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors
        assert int(results[0]["template"].id) == int(results[1]["template"].id)
        verify = SessionLocal()
        try:
            count = (
                verify.query(WhatsAppTemplate)
                .filter(
                    WhatsAppTemplate.tenant_id == 1,
                    WhatsAppTemplate.nahla_source_key == "order_summary",
                    WhatsAppTemplate.is_hidden.is_(False),
                    WhatsAppTemplate.status == "DRAFT",
                )
                .count()
            )
            assert count == 1
            assert is_order_confirmation_r3_contract(results[0]["template"].components)
        finally:
            verify.close()

    def test_schema_probe_reads_alembic_version_via_same_session(self):
        db, engine = _make_db(WhatsAppTemplate)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0096')"))
        probe = inspect_whatsapp_template_schema(db)
        assert probe["alembic_version"] == "0096"

    def test_revision_max_plus_one_for_templates_198_and_432(self):
        db, _ = _make_db(WhatsAppTemplate)
        live = WhatsAppTemplate(
            id=198,
            tenant_id=1,
            name="nahla_order_summary_8d9f",
            language="ar",
            category="UTILITY",
            status="APPROVED",
            components=[{"type": "BODY", "text": "قديم"}],
            service_key="order_confirmation",
            is_active=True,
            is_hidden=False,
            step_number=None,
            revision=1,
        )
        pending = WhatsAppTemplate(
            id=432,
            tenant_id=1,
            name="nahla_order_confirmation_r2",
            language="ar",
            category="UTILITY",
            status="PENDING",
            components=[{"type": "BODY", "text": "قديم r2"}],
            service_key="order_confirmation",
            is_active=False,
            is_hidden=False,
            step_number=None,
            revision=2,
        )
        db.add_all([live, pending])
        db.commit()
        outcome = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        assert outcome["created"] is True
        assert int(outcome["template"].revision) == 3

    def test_reused_draft_without_active_reports_preserved_false(self):
        db, _ = _make_db(WhatsAppTemplate)
        tpl_def = get_template_by_key("order_summary")
        first = import_order_summary_from_library(db, 1, tpl_def)
        second = import_order_summary_from_library(db, 1, tpl_def)
        assert second["reused_existing_draft"] is True
        assert second["active_template_preserved"] is False
        assert second["customizable"] is True
        assert second["template_status"] == "DRAFT"
        assert int(second["template"].id) == int(first["template"].id)

    def test_reused_rejected_is_customizable(self):
        db, _ = _make_db(WhatsAppTemplate)
        rejected = WhatsAppTemplate(
            tenant_id=1,
            name="nahla_order_summary_rejected",
            language="ar",
            category="UTILITY",
            status="REJECTED",
            components=order_summary_r3_components(),
            service_key="order_confirmation",
            nahla_source_key="order_summary",
            is_active=False,
            is_hidden=False,
        )
        db.add(rejected)
        db.commit()
        outcome = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        assert outcome["reused_existing_draft"] is True
        assert outcome["template_status"] == "REJECTED"
        assert outcome["customizable"] is True

    def test_reused_pending_is_not_customizable(self):
        db, _ = _make_db(WhatsAppTemplate)
        pending = WhatsAppTemplate(
            tenant_id=1,
            name="nahla_order_summary_pending",
            language="ar",
            category="UTILITY",
            status="PENDING",
            components=order_summary_r3_components(),
            service_key="order_confirmation",
            nahla_source_key="order_summary",
            is_active=False,
            is_hidden=False,
        )
        db.add(pending)
        db.commit()
        outcome = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        assert outcome["reused_existing_draft"] is True
        assert outcome["template_status"] == "PENDING"
        assert outcome["customizable"] is False
        assert outcome["message"] == MSG_EXISTING_PENDING

    def test_rejects_non_ar_language(self):
        db, _ = _make_db(WhatsAppTemplate)
        with pytest.raises(NahlaLibraryImportError) as exc:
            import_order_summary_from_library(
                db,
                1,
                get_template_by_key("order_summary"),
                language="en",
            )
        assert exc.value.message == MSG_LANGUAGE_AR_ONLY

    def test_rejects_new_import_without_store_integration(self, monkeypatch: pytest.MonkeyPatch):
        db, _ = _make_db(WhatsAppTemplate)
        monkeypatch.setattr(
            "core.merchant_capabilities.resolve_merchant_capabilities",
            lambda db, tenant_id: _WHATSAPP_ONLY_CAPS,
        )
        with pytest.raises(NahlaLibraryImportError) as exc:
            import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        assert exc.value.message == MSG_STORE_INTEGRATION_REQUIRED
        assert exc.value.error_code == "nahla_import_store_integration_required"
        assert db.query(WhatsAppTemplate).count() == 0

    def test_reuses_existing_draft_without_store_integration(self, monkeypatch: pytest.MonkeyPatch):
        db, _ = _make_db(WhatsAppTemplate)
        first = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        monkeypatch.setattr(
            "core.merchant_capabilities.resolve_merchant_capabilities",
            lambda db, tenant_id: _WHATSAPP_ONLY_CAPS,
        )
        second = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        assert second["reused_existing_draft"] is True
        assert int(second["template"].id) == int(first["template"].id)

    def test_normalizes_language_to_ar_on_create(self):
        db, _ = _make_db(WhatsAppTemplate)
        outcome = import_order_summary_from_library(
            db,
            1,
            get_template_by_key("order_summary"),
            language=" AR ",
        )
        assert outcome["template"].language == "ar"

    def test_import_metadata_does_not_stamp_r2_url(self):
        db, _ = _make_db(WhatsAppTemplate)
        outcome = import_order_summary_from_library(db, 1, get_template_by_key("order_summary"))
        meta = outcome["template"].ai_generation_metadata or {}
        assert "header_image_url" not in meta
        assert meta.get("header_image_asset_key")

    def test_default_header_url_not_dashboard_spa(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("NAHLA_ORDER_CONFIRMATION_HEADER_URL", raising=False)
        url = order_confirmation_header_public_url()
        assert "app.nahlah.ai" not in url
        assert url.startswith("https://pub-")
