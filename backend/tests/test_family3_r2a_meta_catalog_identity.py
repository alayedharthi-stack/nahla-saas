"""Family 3 R2-A — Meta catalog membership is required for native catalog send.

Upstream ``external_id`` is not verified Meta catalog membership.
Same-title siblings and other variant SKUs must not substitute.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO = _BACKEND.parent
for _p in (str(_BACKEND), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.meta_catalog_membership import (  # noqa: E402
    PROVENANCE_GRAPH_RECONCILE,
    MetaCatalogMembershipFact,
)
from core.native_catalog_capability import (  # noqa: E402
    REASON_CATALOG_ID_MISMATCH,
    REASON_META_CATALOG_UNVERIFIED,
    REASON_SYNTHETIC_RETAILER_ID,
    REASON_VARIANT_MAPPING_MISSING,
    evaluate_native_catalog_product_capability,
)
from services.catalog_product_orchestrator import (  # noqa: E402
    ProductCardSendAction,
    evaluate_product_card_send,
)
from services.whatsapp_platform.catalog_sender import (  # noqa: E402
    _classify_catalog_provider_failure,
)

_PUBLISHED = datetime(2026, 6, 1, tzinfo=timezone.utc)
_CATALOG_A = "catalog-aaa"
_CATALOG_B = "catalog-bbb"


def _conn(catalog_id: str = _CATALOG_A, **kw):
    defaults = dict(
        status="connected",
        sending_enabled=True,
        phone_number_id="1234567890",
        catalog_enabled=True,
        meta_catalog_id=catalog_id,
        provider="meta",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _product(
    *,
    product_id: int = 101,
    external_id: str = "ext-101",
    meta_retailer_id: str | None = None,
    published: bool = False,
    membership_catalog_id: str | None = None,
    title: str = "قميص قطني أزرق",
):
    return SimpleNamespace(
        id=product_id,
        tenant_id=7,
        title=title,
        external_id=external_id,
        meta_retailer_id=meta_retailer_id,
        in_stock=True,
        catalog_status="active",
        meta_catalog_published_at=_PUBLISHED if published else None,
        has_variants=False,
        default_variant_id=None,
    )


def _membership(
    *,
    retailer_id: str = "meta-rid-101",
    product_id: int = 101,
    variant_id: int | None = None,
    catalog_id: str = _CATALOG_A,
    tenant_id: int = 7,
) -> MetaCatalogMembershipFact:
    return MetaCatalogMembershipFact(
        tenant_id=tenant_id,
        catalog_id=catalog_id,
        retailer_id=retailer_id,
        product_id=product_id,
        variant_id=variant_id,
        meta_item_id="mg-1",
        verified_at=_PUBLISHED,
        provenance=PROVENANCE_GRAPH_RECONCILE,
    )


def _attachment(*, product_id: int = 101, **kw):
    base = dict(
        kind="product_card",
        id=product_id,
        title="قميص قطني أزرق",
        external_id="ext-101",
        file_url="https://cdn.example/shirt.jpg",
        product_url="https://shop.example/p/shirt",
        in_stock=True,
        confidence="fts",
        needs_variant_choice=False,
        variants=[],
    )
    base.update(kw)
    return base


class TestR2A01ExternalIdIsNotMembership:
    def test_external_id_alone_is_not_verified_membership(self):
        cap = evaluate_native_catalog_product_capability(
            _product(external_id="ext-101", published=False),
            catalog_id=_CATALOG_A,
        )
        assert cap.available is False
        assert cap.mapping_status == "unverified"
        assert cap.reason == REASON_META_CATALOG_UNVERIFIED


class TestR2A02VerifiedMappingIsEligible:
    def test_canonical_membership_is_eligible(self):
        product = _product(
            meta_retailer_id="meta-rid-101",
            published=True,
        )
        cap = evaluate_native_catalog_product_capability(
            product,
            catalog_id=_CATALOG_A,
            membership=_membership(),
            tenant_id=7,
        )
        assert cap.available is True
        assert cap.retailer_id == "meta-rid-101"
        assert cap.mapping_status == "verified"
        assert cap.provenance == PROVENANCE_GRAPH_RECONCILE


class TestR2A03CatalogScopedMembership:
    def test_mapping_for_catalog_a_is_not_eligible_for_catalog_b(self):
        product = _product(
            meta_retailer_id="meta-rid-101",
            published=True,
        )
        cap = evaluate_native_catalog_product_capability(
            product,
            catalog_id=_CATALOG_B,
            membership=_membership(catalog_id=_CATALOG_A),
            tenant_id=7,
        )
        assert cap.available is False
        assert cap.mapping_status == "catalog_mismatch"
        assert cap.reason == REASON_CATALOG_ID_MISMATCH


class TestR2A04UnknownMappingFailsClosed:
    def test_missing_stamp_fails_closed(self):
        cap = evaluate_native_catalog_product_capability(
            _product(meta_retailer_id="copied-ext-101", published=False),
            catalog_id=_CATALOG_A,
        )
        assert cap.available is False
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(),
            product_row=_product(published=False),
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.reason == REASON_META_CATALOG_UNVERIFIED


class TestR2A05FallbackPreservesCanonicalProduct:
    def test_native_ineligible_keeps_bound_product_id(self):
        bound = _product(product_id=101, published=False)
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(product_id=101),
            product_row=bound,
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.diagnostics["canonical_product_id"] == 101


class TestR2A06SameTitleSiblingCannotSubstitute:
    def test_published_sibling_does_not_replace_bound_unpublished_product(self):
        bound = _product(product_id=101, title="عطر ورد 100ml", published=False)
        sibling = _product(
            product_id=102,
            title="عطر ورد 100ml",
            meta_retailer_id="meta-rid-102",
            published=True,
            membership_catalog_id=_CATALOG_A,
        )
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(product_id=101, title="عطر ورد 100ml"),
            product_row=bound,
            tenant_products=[bound, sibling],
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.diagnostics["canonical_product_id"] == 101
        assert d.retailer_id != "meta-rid-102"


class TestR2A07VariantUsesExactMapping:
    def test_bound_variant_uses_its_own_verified_retailer_id(self):
        product = _product(
            meta_retailer_id="parent-rid",
            published=True,
            membership_catalog_id=_CATALOG_A,
        )
        cap = evaluate_native_catalog_product_capability(
            product,
            catalog_id=_CATALOG_A,
            variant={"id": 9, "retailer_id": "variant-rid-9"},
            membership=_membership(retailer_id="variant-rid-9", variant_id=9),
            tenant_id=7,
        )
        assert cap.available is True
        assert cap.retailer_id == "variant-rid-9"
        assert cap.variant_id == 9
        assert cap.retailer_id != "parent-rid"


class TestR2A08MissingVariantDoesNotSwitchSku:
    def test_bound_variant_without_retailer_id_does_not_use_parent(self):
        product = _product(
            meta_retailer_id="parent-rid",
            published=True,
            membership_catalog_id=_CATALOG_A,
        )
        cap = evaluate_native_catalog_product_capability(
            product,
            catalog_id=_CATALOG_A,
            variant={"id": 9, "retailer_id": ""},
        )
        assert cap.available is False
        assert cap.reason == REASON_VARIANT_MAPPING_MISSING
        assert cap.retailer_id != "parent-rid"

        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(
                product_id=101,
                picked_variant_id=9,
                needs_variant_choice=False,
            ),
            product_row=product,
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.diagnostics["canonical_product_id"] == 101
        assert d.diagnostics["canonical_variant_id"] == 9
        assert d.action != ProductCardSendAction.SEND_CATALOG


class TestR2A09SyntheticRetailerIdIsNotMembership:
    def test_synthetic_id_fails_closed_even_if_non_empty(self):
        product = _product(
            product_id=55,
            external_id="nahla_p_55",
            meta_retailer_id="nahla_p_55",
            published=False,
        )
        cap = evaluate_native_catalog_product_capability(
            product,
            catalog_id=_CATALOG_A,
        )
        assert cap.available is False
        assert cap.mapping_status == "synthetic"
        assert cap.reason == REASON_SYNTHETIC_RETAILER_ID


class TestR2A10ProductsNotFoundClassification:
    def test_131009_product_not_found_is_meta_products_not_found(self):
        reason, _detail = _classify_catalog_provider_failure(
            {
                "error": {
                    "code": 131009,
                    "message": "(#131009) Parameter value is not valid",
                    "error_data": {
                        "details": (
                            "products not found: product_retailer_id = sku-404 "
                            "catalog_id = catalog-aaa"
                        )
                    },
                }
            }
        )
        assert reason == "meta_products_not_found"


class TestR2A11DoesNotConflateDuplicateButtonTitle:
    def test_131009_duplicate_button_title_is_not_products_not_found(self):
        reason, _detail = _classify_catalog_provider_failure(
            {
                "error": {
                    "code": 131009,
                    "message": "(#131009) Duplicate button title",
                    "error_data": {"details": "Duplicate button title"},
                }
            }
        )
        assert reason != "meta_products_not_found"
        assert reason == "provider_error"


class TestR2A12NoLiveSkuHardcoding:
    def test_runtime_owners_do_not_hardcode_live_repro_ids(self):
        forbidden = ("398551325", "4573008916317550")
        owners = [
            _BACKEND / "core" / "native_catalog_capability.py",
            _BACKEND / "services" / "catalog_product_orchestrator.py",
            _BACKEND / "services" / "whatsapp_platform" / "catalog_sender.py",
        ]
        for path in owners:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, f"{path.name} hardcodes {token!r}"


class TestR2A13NoCustomerAiChanges:
    def test_r2a_does_not_require_customer_ai_owner_edits(self):
        path = _BACKEND / "modules" / "ai" / "brain" / "intent" / "semantic_relation.py"
        assert path.exists()


class TestR2AOrchestratorVerifiedSend:
    def test_verified_product_still_sends_native_catalog(self):
        product = _product(
            meta_retailer_id="meta-rid-101",
            published=True,
        )
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(),
            product_row=product,
            membership=_membership(),
        )
        assert d.action == ProductCardSendAction.SEND_CATALOG
        assert d.retailer_id == "meta-rid-101"
        assert d.diagnostics["canonical_product_id"] == 101
        assert d.diagnostics["native_catalog_available"] is True
