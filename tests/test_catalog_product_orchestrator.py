"""
tests/test_catalog_product_orchestrator.py
──────────────────────────────────────────
Phase A — product-card orchestrator skeleton + shared readiness.

Decision-only layer — no webhook wiring, no provider mocks.
"""
from __future__ import annotations

import copy
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    evaluate_tenant_catalog_send_readiness,
    whatsapp_commerce_diagnostics_readiness,
)
from services.catalog_product_orchestrator import (  # noqa: E402
    ProductCardSendAction,
    REASON_OK,
    REASON_RETAILER_ID_COLLISION,
    REASON_SYNTHETIC_RETAILER_ID,
    REASON_TENANT_MISMATCH,
    REASON_TENANT_NOT_SEND_READY,
    REASON_WEAK_CONFIDENCE,
    catalog_send_retailer_id,
    count_retailer_id_owners,
    evaluate_product_card_send,
    retailer_id_has_collision,
    resolve_attachment_retailer_id,
    should_attempt_catalog_send,
    weak_confidence_block_enabled,
)


def _conn(**kw):
    defaults = dict(
        status="connected",
        sending_enabled=True,
        phone_number_id="1234567890",
        catalog_enabled=True,
        meta_catalog_id="CAT-1",
        provider="meta",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _attachment(**kw):
    base = dict(
        kind="product_card",
        id=10,
        title="Honey",
        external_id="ext-10",
        file_url="https://cdn.example/h.jpg",
        product_url="https://shop.example/p",
        in_stock=True,
        confidence="fts",
    )
    base.update(kw)
    return base


@dataclass
class _Product:
    id: int
    tenant_id: int
    external_id: str = ""
    meta_retailer_id: str | None = None
    in_stock: bool = True


class TestSharedReadiness:
    def test_send_readiness_excludes_graph_token(self):
        ready = evaluate_tenant_catalog_send_readiness(_conn(access_token=""))
        assert ready.ready is True
        assert "graph_token" not in ready.checks

    def test_send_readiness_fails_without_phone_id(self):
        ready = evaluate_tenant_catalog_send_readiness(
            _conn(phone_number_id=""),
        )
        assert ready.ready is False
        assert ready.reason == "phone_number_id_missing"

    def test_diagnostics_includes_graph_token_check(self, monkeypatch):
        monkeypatch.setattr(
            "services.meta_catalog_import._select_graph_token",
            lambda c: {"token": None},
        )
        diag = whatsapp_commerce_diagnostics_readiness(
            connection=_conn(),
            catalog_id="CAT-1",
            catalog_enabled=True,
            wa_connected=True,
            with_rid=5,
        )
        keys = [c["key"] for c in diag["checks"]]
        assert "graph_token_available" in keys


class TestOrchestratorDecisions:
    def test_send_catalog_happy_path(self):
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
            tenant_products=[_Product(id=10, tenant_id=1, external_id="ext-10")],
        )
        assert d.action == ProductCardSendAction.SEND_CATALOG
        assert d.reason == REASON_OK
        assert d.retailer_id == "ext-10"
        assert d.product_ready is True

    def test_weak_confidence_blocks_by_default(self, monkeypatch):
        monkeypatch.delenv("CATALOG_WEAK_CONFIDENCE_BLOCK", raising=False)
        assert weak_confidence_block_enabled() is True
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(confidence="weak"),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.reason == REASON_WEAK_CONFIDENCE

    def test_weak_confidence_allowed_when_flag_off(self, monkeypatch):
        monkeypatch.setenv("CATALOG_WEAK_CONFIDENCE_BLOCK", "false")
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(confidence="weak"),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
            tenant_products=[_Product(id=10, tenant_id=1, external_id="ext-10")],
        )
        assert d.action == ProductCardSendAction.SEND_CATALOG

    def test_collision_always_fallback(self):
        products = [
            _Product(id=10, tenant_id=1, external_id="same-rid"),
            _Product(id=11, tenant_id=1, external_id="same-rid"),
        ]
        assert retailer_id_has_collision(products, "same-rid") is True
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(id=10, external_id="same-rid"),
            product_row=products[0],
            tenant_products=products,
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.reason == REASON_RETAILER_ID_COLLISION
        assert d.log_event == "CATALOG_RID_COLLISION"
        assert sorted(d.diagnostics["collision_owner_ids"]) == [10, 11]

    def test_synthetic_retailer_id_fallback(self):
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(external_id="nahla_p_99"),
            product_row=_Product(id=99, tenant_id=1, meta_retailer_id="nahla_p_99"),
        )
        assert d.reason == REASON_SYNTHETIC_RETAILER_ID

    def test_tenant_mismatch_fallback(self):
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(),
            product_row=_Product(id=10, tenant_id=2, external_id="ext-10"),
        )
        assert d.reason == REASON_TENANT_MISMATCH

    def test_out_of_stock_allows_catalog_with_warning(self):
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(in_stock=False),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10", in_stock=False),
            tenant_products=[_Product(id=10, tenant_id=1, external_id="ext-10")],
        )
        assert d.action == ProductCardSendAction.SEND_CATALOG
        assert d.stock_warning is True

    def test_no_image_with_url_suggests_cta_only(self):
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(file_url="", product_url="https://shop.example/p"),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
            tenant_products=[_Product(id=10, tenant_id=1, external_id="ext-10")],
        )
        assert d.action == ProductCardSendAction.FALLBACK_CTA_ONLY

    def test_variant_prompt_when_needed(self, monkeypatch):
        monkeypatch.setenv("CATALOG_VARIANT_SEND", "true")
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(
                needs_variant_choice=True,
                variants=[{"id": 1}, {"id": 2}],
            ),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
        )
        assert d.action == ProductCardSendAction.VARIANT_PROMPT

    def test_tenant_not_send_ready_when_catalog_disabled(self):
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(catalog_enabled=False),
            attachment=_attachment(),
        )
        assert d.reason == REASON_TENANT_NOT_SEND_READY


class TestOrchestratorHelpers:
    def test_resolve_attachment_retailer_id_prefers_variant_pick(self):
        att = _attachment(picked_variant_retailer_id="var-rid")
        row = _Product(id=10, tenant_id=1, external_id="parent-rid")
        assert resolve_attachment_retailer_id(att, row) == "var-rid"

    def test_count_retailer_id_owners(self):
        products = [
            _Product(id=1, tenant_id=1, meta_retailer_id="R1"),
            _Product(id=2, tenant_id=1, external_id="R1"),
            _Product(id=3, tenant_id=1, external_id="R2"),
        ]
        assert count_retailer_id_owners(products, "R1") == [1, 2]


class TestStructuredLogShape:
    def test_to_log_dict_includes_collision_fields(self):
        products = [
            _Product(id=10, tenant_id=1, external_id="dup"),
            _Product(id=11, tenant_id=1, external_id="dup"),
        ]
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(external_id="dup"),
            product_row=products[0],
            tenant_products=products,
        )
        payload = d.to_log_dict(tenant_id=1, product_id=10, confidence="fts")
        assert payload["event"] == "CATALOG_RID_COLLISION"
        assert payload["action"] == "fallback_legacy"
        assert payload["collision_count"] == 2


class TestAttachmentImmutability:
    """Pin the commerce runtime boundary — orchestrator never mutates attachments."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"confidence": "weak"},
            {"file_url": "", "product_url": "https://shop.example/p"},
            {"needs_variant_choice": True, "variants": [{"id": 1}, {"id": 2}]},
        ],
    )
    def test_evaluate_never_mutates_attachment(self, kwargs, monkeypatch):
        monkeypatch.setenv("CATALOG_VARIANT_SEND", "true")
        att = _attachment(**kwargs)
        snapshot = copy.deepcopy(att)
        evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=att,
            product_row=_Product(id=10, tenant_id=1, external_id=att["external_id"]),
            tenant_products=[_Product(id=10, tenant_id=1, external_id=att["external_id"])],
        )
        assert att == snapshot
        assert "retailer_id" not in att
        assert "meta_retailer_id" not in att

    def test_retailer_id_on_decision_not_attachment(self):
        att = _attachment()
        snapshot = copy.deepcopy(att)
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=att,
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
            tenant_products=[_Product(id=10, tenant_id=1, external_id="ext-10")],
        )
        assert att == snapshot
        assert catalog_send_retailer_id(d) == "ext-10"
        assert "retailer_id" not in att


class TestPhaseBWiringContract:
    def test_should_attempt_catalog_send_only_on_send_catalog(self):
        ok = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
            tenant_products=[_Product(id=10, tenant_id=1, external_id="ext-10")],
        )
        assert should_attempt_catalog_send(ok) is True

        weak = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(confidence="weak"),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
        )
        assert should_attempt_catalog_send(weak) is False

        collision_products = [
            _Product(id=10, tenant_id=1, external_id="dup"),
            _Product(id=11, tenant_id=1, external_id="dup"),
        ]
        coll = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(external_id="dup"),
            product_row=collision_products[0],
            tenant_products=collision_products,
        )
        assert should_attempt_catalog_send(coll) is False

    def test_fallback_cta_only_is_not_catalog_send(self):
        d = evaluate_product_card_send(
            tenant_id=1,
            connection=_conn(),
            attachment=_attachment(file_url="", product_url="https://shop.example/p"),
            product_row=_Product(id=10, tenant_id=1, external_id="ext-10"),
            tenant_products=[_Product(id=10, tenant_id=1, external_id="ext-10")],
        )
        assert d.action == ProductCardSendAction.FALLBACK_CTA_ONLY
        assert should_attempt_catalog_send(d) is False
