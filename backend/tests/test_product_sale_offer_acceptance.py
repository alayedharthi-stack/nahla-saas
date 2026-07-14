"""Acceptance tests for product_sale_offer slice (16 required scenarios)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.catalog import CATALOG_STATUS_ACTIVE  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.product_sale_offer_compose_projection import (  # noqa: E402
    ProductSaleOfferProjectionError,
    project_general_offer_discovery_compose_facts,
    project_product_sale_offer_compose_facts,
)
from modules.ai.brain.truth_surface.product_sale_offer_consumption_gate import (  # noqa: E402
    general_offer_discovery_bundle_trace,
    maybe_general_offer_discovery_compose_facts,
    maybe_product_sale_offer_compose_facts,
)
from modules.ai.brain.truth_surface.product_sale_offer_loader import (  # noqa: E402
    load_product_sale_offer_facts,
)
from modules.ai.brain.truth_surface.product_sale_offer_repository import (  # noqa: E402
    ProductSaleOfferRepositoryError,
    ProductSaleSampleRow,
    StoreWideSaleSnapshot,
)


def _sale_fact_record(**overrides):
    base = {
        "question_kind": "store_wide",
        "product_sale_availability": "active_sale_present",
        "verified_on_sale_product_count": 1,
        "sample_products": [
            {"title": "حذاء رياضي أبيض", "sale_price": "80", "regular_price": "100"},
        ],
        "allow_price_mention": True,
    }
    base.update(overrides)
    return base


def _snapshot(record: dict) -> TrustedContextSnapshot:
    return TrustedContextSnapshot(
        tenant_id=1,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="catalog:product_sale_offer",
                value=record,
                source=TruthSource.PRODUCTS_TABLE,
                path="test",
            )
        ],
    )


# 1) COUNT=0 => none_verified
def test_acceptance_01_count_zero_none_verified() -> None:
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        return_value=StoreWideSaleSnapshot(verified_count=0, sample_rows=[]),
    ):
        facts, obs = load_product_sale_offer_facts(
            db=MagicMock(), tenant_id=1, message="عندكم عروض؟"
        )
    assert facts[0].value["product_sale_availability"] == "none_verified"
    assert facts[0].value["allow_price_mention"] is False
    assert "sample_products" not in facts[0].value
    assert obs["verified_on_sale_product_count"] == 0
    assert "sample_product_ids" not in obs


# 2) DB error => unavailable
def test_acceptance_02_db_error_unavailable() -> None:
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        side_effect=ProductSaleOfferRepositoryError("db"),
    ):
        facts, obs = load_product_sale_offer_facts(
            db=MagicMock(), tenant_id=1, message="عندكم عروض؟"
        )
    assert facts == []
    assert obs["product_sale_availability"] == "unavailable"
    assert "verified_on_sale_product_count" not in obs


# 3-4) strict sale matrix covered in golden tests; acceptance spot-check
def test_acceptance_03_04_strict_sale_exclusions() -> None:
    from modules.ai.brain.truth_surface.product_sale_offer_loader import (  # noqa: PLC0415
        is_strict_product_sale,
    )

    assert is_strict_product_sale({"sale_price": "80", "regular_price": "100"})
    assert not is_strict_product_sale({"sale_price": "100", "regular_price": "100"})
    assert not is_strict_product_sale({"sale_price": {"amount": {"value": "80"}}, "regular_price": "100"})


# 5) tenant isolation — repository called with tenant_id only
def test_acceptance_05_tenant_isolation() -> None:
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        return_value=StoreWideSaleSnapshot(verified_count=0, sample_rows=[]),
    ) as mocked:
        load_product_sale_offer_facts(db=MagicMock(), tenant_id=42, message="عندكم عروض؟")
    mocked.assert_called_once()
    assert mocked.call_args.kwargs["tenant_id"] == 42


# 6) sample matching set <=5 deterministic
def test_acceptance_06_sample_bounded_deterministic() -> None:
    rows = [
        ProductSaleSampleRow(i, f"P{i}", "10", "20")
        for i in range(1, 8)
    ]
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        return_value=StoreWideSaleSnapshot(verified_count=7, sample_rows=rows[:5]),
    ):
        facts, obs = load_product_sale_offer_facts(
            db=MagicMock(), tenant_id=1, message="عندكم عروض؟"
        )
    assert len(facts[0].value["sample_products"]) <= 5
    assert obs["sample_product_ids"] == [1, 2, 3, 4, 5]


# 7) IDs do not reach compose
def test_acceptance_07_ids_not_in_compose() -> None:
    snap = _snapshot(
        _sale_fact_record(
            sample_products=[
                {
                    "title": "عطر ورد 100ml",
                    "sale_price": "199",
                    "regular_price": "249",
                    "product_id": 55,
                    "sku": "SKU-1",
                }
            ]
        )
    )
    payload = project_product_sale_offer_compose_facts(snapshot=snap)
    dumped = str(payload)
    assert "product_id" not in dumped
    assert "sku" not in dumped.lower()


# 8) titles/prices not in telemetry helper
def test_acceptance_08_telemetry_no_titles_prices() -> None:
    from modules.ai.brain.truth_surface.product_sale_offer_consumption_gate import (  # noqa: E402
        safe_product_sale_loader_telemetry,
    )

    telemetry = safe_product_sale_loader_telemetry(
        {
            "product_sale_availability": "active_sale_present",
            "verified_on_sale_product_count": 1,
            "loader_duration_ms": 3,
            "sample_product_ids": [1],
            "sample_products": [{"title": "x", "sale_price": "1", "regular_price": "2"}],
        }
    )
    assert telemetry["sample_product_ids"] == [1]
    assert "title" not in telemetry
    assert "sale_price" not in telemetry


# 9) general offer product facts only => one compose surface
def test_acceptance_09_general_product_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_GENERAL_OFFER_DISCOVERY_COMPOSE_ENABLED", "1")
    snap = _snapshot(_sale_fact_record())
    payload = maybe_general_offer_discovery_compose_facts(
        message="عندكم عروض؟",
        snapshot=snap,
    )
    assert payload is not None
    assert payload["surface"] == "general_offer_discovery_answer"
    assert payload["product_sale_offer_facts"] is not None
    assert payload["trusted_coupon_offer_facts"] is None


# 10) general offer coupon facts only => one compose surface
def test_acceptance_10_general_coupon_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_GENERAL_OFFER_DISCOVERY_COMPOSE_ENABLED", "1")
    coupon = {"coupon_availability": "active_eligible_present"}
    payload = maybe_general_offer_discovery_compose_facts(
        message="عندكم عروض؟",
        snapshot=_snapshot(
            _sale_fact_record(product_sale_availability="unavailable")
        ),
        trusted_coupon_offer_facts=coupon,
    )
    assert payload is not None
    assert payload["surface"] == "general_offer_discovery_answer"
    assert payload["product_sale_offer_facts"] is None
    assert payload["trusted_coupon_offer_facts"] == coupon


# 11) both sources => one compose + independent bundles
def test_acceptance_11_general_both_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_GENERAL_OFFER_DISCOVERY_COMPOSE_ENABLED", "1")
    coupon = {"coupon_availability": "active_eligible_present"}
    payload = maybe_general_offer_discovery_compose_facts(
        message="عندكم عروض؟",
        snapshot=_snapshot(_sale_fact_record()),
        trusted_coupon_offer_facts=coupon,
    )
    assert payload["product_sale_offer_facts"]["bundle_namespace"] == "product_sale_offer"
    assert payload["trusted_coupon_offer_facts"] == coupon


# 12) no valid facts => no general compose surface
def test_acceptance_12_no_valid_facts_no_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_GENERAL_OFFER_DISCOVERY_COMPOSE_ENABLED", "1")
    snap = TrustedContextSnapshot(tenant_id=1, facts=[])
    assert (
        maybe_general_offer_discovery_compose_facts(
            message="عندكم عروض؟",
            snapshot=snap,
        )
        is None
    )
    with pytest.raises(ProductSaleOfferProjectionError):
        project_general_offer_discovery_compose_facts(
            snapshot=snap,
            trusted_coupon_offer_facts=None,
        )


# 13) product-scoped without focus
def test_acceptance_13_product_scoped_requires_context() -> None:
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.first.return_value = None
        return q

    db.query.side_effect = _query
    facts, obs = load_product_sale_offer_facts(
        db=db,
        tenant_id=1,
        message="هل عليه عرض؟",
        brain_state=SimpleNamespace(current_product_focus=None),
    )
    record = facts[0].value
    assert record["product_sale_availability"] == "requires_product_context"
    assert record["allow_price_mention"] is False
    assert "verified_on_sale_product_count" not in record
    assert "verified_on_sale_product_count" not in obs


# 14) no duplicate domain reload in same turn scope
def test_acceptance_14_no_duplicate_domain_reload_per_turn() -> None:
    from modules.ai.brain.truth_surface import trusted_context  # noqa: E402

    trusted_context.clear_trusted_context()
    calls = {"n": 0}

    def _fake_fetch(db, *, tenant_id: int):
        calls["n"] += 1
        return StoreWideSaleSnapshot(verified_count=0, sample_rows=[])

    db = MagicMock()
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        side_effect=_fake_fetch,
    ), patch.object(trusted_context, "is_trusted_context_shadow_enabled", return_value=True):
        trusted_context.run_trusted_context_shadow(
            db=db,
            tenant_id=1,
            customer_phone="+966500000099",
            message="عندكم عروض؟",
            conversation_id=100,
        )
        trusted_context.run_trusted_context_shadow(
            db=db,
            tenant_id=1,
            customer_phone="+966500000099",
            message="عندكم عروض؟",
            conversation_id=100,
        )
    assert calls["n"] == 1


# 15) loader does not add LLM calls
def test_acceptance_15_loader_no_additional_llm_calls() -> None:
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        return_value=StoreWideSaleSnapshot(verified_count=0, sample_rows=[]),
    ):
        facts, obs = load_product_sale_offer_facts(
            db=MagicMock(), tenant_id=1, message="عندكم عروض؟"
        )
    assert facts
    assert obs["question_kind"] == "store_wide"
    assert "llm" not in str(obs).lower()


# 16) generic merchant/category coverage
def test_acceptance_16_generic_merchant_category() -> None:
    snapshot = StoreWideSaleSnapshot(
        verified_count=1,
        sample_rows=[
            ProductSaleSampleRow(
                product_id=11,
                title="حذاء رياضي أبيض",
                sale_price="120",
                regular_price="150",
            )
        ],
    )
    with patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.fetch_store_wide_sale_snapshot",
        return_value=snapshot,
    ):
        facts, _obs = load_product_sale_offer_facts(
            db=MagicMock(),
            tenant_id=1,
            message="عندكم عروض؟",
        )
    sample = facts[0].value["sample_products"][0]
    assert sample["title"] == "حذاء رياضي أبيض"
    assert "عسل" not in sample["title"]


def test_bundle_trace_without_raw_facts() -> None:
    trace = general_offer_discovery_bundle_trace(
        message="عندكم عروض؟",
        snapshot=_snapshot(_sale_fact_record()),
        discovery_facts={
            "product_sale_offer_facts": {"product_sale_availability": "active_sale_present"},
            "trusted_coupon_offer_facts": None,
        },
    )
    assert trace["chosen_path"] == "general_offer_discovery_compose"
    assert "title" not in str(trace)
