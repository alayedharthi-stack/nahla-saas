"""Real PostgreSQL integration tests for product_sale_offer repository + price parity."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.product_sale_offer_loader import (  # noqa: E402
    _store_wide_from_snapshot,
    is_strict_product_sale,
    load_product_sale_offer_facts,
)
from modules.ai.brain.truth_surface.product_sale_offer_price_parse import (  # noqa: E402
    canonical_price_string,
    strict_sale_from_metadata,
)
from modules.ai.brain.truth_surface.product_sale_offer_repository import (  # noqa: E402
    canonical_prices_from_metadata,
    fetch_store_wide_sale_snapshot,
)
from tests.product_sale_offer_postgres_fixtures import (  # noqa: E402
    _TEST_TENANT_A,
    _TEST_TENANT_B,
    insert_catalog_product,
    pg_session,
    postgres_engine,
)

pytestmark = pytest.mark.usefixtures("postgres_engine")


def test_pg_scalar_string_prices_with_python_parity(pg_session) -> None:
    meta = {"sale_price": "80", "regular_price": "100"}
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="حذاء رياضي أبيض",
        metadata=meta,
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    py_sale, py_regular = canonical_prices_from_metadata(meta)
    assert snap.verified_count == 1
    assert snap.sample_rows[0].sale_price == py_sale == "80"
    assert snap.sample_rows[0].regular_price == py_regular == "100"
    assert is_strict_product_sale(meta)


def test_pg_json_number_and_object_amount_parity(pg_session) -> None:
    meta_number = {"sale_price": 59, "regular_price": 79}
    meta_object = {"sale_price": {"amount": "90"}, "regular_price": {"amount": "120"}}
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="قميص قطني أزرق",
        metadata=meta_number,
    )
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="عطر ورد 100ml",
        metadata=meta_object,
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.verified_count == 2
    by_title = {row.title: row for row in snap.sample_rows}
    n_sale, n_regular = canonical_prices_from_metadata(meta_number)
    o_sale, o_regular = canonical_prices_from_metadata(meta_object)
    assert by_title["قميص قطني أزرق"].sale_price == n_sale == "59"
    assert by_title["عطر ورد 100ml"].sale_price == o_sale == "90"


def test_pg_nested_amount_object_excluded_without_query_failure(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="منتج غير صالح",
        metadata={
            "sale_price": {"amount": {"value": "80"}},
            "regular_price": {"amount": "100"},
        },
    )
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="منتج صالح",
        metadata={"sale_price": "45", "regular_price": "60"},
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.verified_count == 1
    assert snap.sample_rows[0].title == "منتج صالح"


def test_pg_malformed_prices_do_not_raise_cast_failure(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="malformed",
        metadata={"sale_price": "abc", "regular_price": "100"},
    )
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="equal",
        metadata={"sale_price": "100", "regular_price": "100"},
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.verified_count == 0
    assert snap.sample_rows == []


def test_pg_count_zero_none_verified_loader_shape(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="no sale",
        metadata={"sale_price": "100", "regular_price": "100"},
    )
    facts, obs = load_product_sale_offer_facts(
        db=pg_session,
        tenant_id=_TEST_TENANT_A,
        message="عندكم عروض؟",
    )
    record = facts[0].value
    assert record["product_sale_availability"] == "none_verified"
    assert record["allow_price_mention"] is False
    assert "sample_products" not in record
    assert "sample_product_ids" not in obs


def test_pg_tenant_isolation(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="tenant A",
        metadata={"sale_price": "50", "regular_price": "70"},
    )
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_B,
        title="tenant B",
        metadata={"sale_price": "80", "regular_price": "100"},
    )
    snap_a = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    snap_b = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_B)
    assert snap_a.verified_count == 1
    assert snap_b.verified_count == 1
    assert snap_a.sample_rows[0].title == "tenant A"
    assert snap_b.sample_rows[0].title == "tenant B"


def test_pg_deterministic_bounded_sample(pg_session) -> None:
    for idx in range(7):
        insert_catalog_product(
            pg_session,
            tenant_id=_TEST_TENANT_A,
            title=f"منتج {idx + 1}",
            metadata={"sale_price": str(40 + idx), "regular_price": str(80 + idx)},
        )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.verified_count == 7
    assert len(snap.sample_rows) == 5
    ids = [row.product_id for row in snap.sample_rows]
    assert ids == sorted(ids)


@pytest.mark.parametrize(
    ("sale_raw", "regular_raw", "expected_sale", "expected_regular"),
    [
        ("1,200", "1,500", "1200", "1500"),
        ("  80.00  ", "  100  ", "80", "100"),
        ("1,200.50", "1,500.75", "1200.5", "1500.75"),
    ],
)
def test_pg_comma_whitespace_canonical_parity(
    pg_session,
    sale_raw: str,
    regular_raw: str,
    expected_sale: str,
    expected_regular: str,
) -> None:
    meta = {"sale_price": sale_raw, "regular_price": regular_raw}
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="تطبيع فواصل",
        metadata=meta,
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    py_sale, py_regular = strict_sale_from_metadata(meta)[:2]
    assert py_sale == expected_sale
    assert py_regular == expected_regular
    assert snap.sample_rows[0].sale_price == expected_sale
    assert snap.sample_rows[0].regular_price == expected_regular


def test_pg_integer_display_not_80_00(pg_session) -> None:
    meta = {"sale_price": "80.00", "regular_price": "100.00"}
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="تنسيق صحيح",
        metadata=meta,
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.sample_rows[0].sale_price == "80"
    assert snap.sample_rows[0].regular_price == "100"
    assert canonical_price_string("80.00") == "80"


def test_repository_post_process_uses_price_parse_module(pg_session, monkeypatch) -> None:
    calls: list[str] = []

    def _spy(norm: str) -> str | None:
        calls.append(str(norm))
        return canonical_price_string(norm)

    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.product_sale_offer_repository.normalize_extracted_price_raw",
        _spy,
    )
    meta = {"sale_price": "80.00", "regular_price": "100.00"}
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="مصدر parser",
        metadata=meta,
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.sample_rows[0].sale_price == "80"
    assert any("80" in call for call in calls)


def test_loader_product_scoped_delegates_to_price_parse(pg_session, monkeypatch) -> None:
    pid = insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="منتج مركّز",
        metadata={"sale_price": "70", "regular_price": "90"},
    )
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.strict_sale_from_metadata",
        lambda _meta: ("70", "90", True),
    )
    facts, _obs = load_product_sale_offer_facts(
        db=pg_session,
        tenant_id=_TEST_TENANT_A,
        message="هل المنتج مخفض؟",
        brain_state=type("S", (), {"current_product_focus": {"product_id": pid}})(),
    )
    assert facts[0].value["product_sale_availability"] == "active_sale_present"
    assert facts[0].value["allow_price_mention"] is True


def test_store_wide_active_sale_allows_price_mention(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="عرض فعال",
        metadata={"sale_price": "55", "regular_price": "75"},
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    facts, _obs = _store_wide_from_snapshot(
        snapshot=snap,
        question_kind="store_wide",
        started=__import__("time").perf_counter(),
    )
    record = facts[0].value
    assert record["allow_price_mention"] is True
    assert "sample_products" in record


def test_product_scoped_requires_context_no_count_fields(pg_session) -> None:
    facts, obs = load_product_sale_offer_facts(
        db=pg_session,
        tenant_id=_TEST_TENANT_A,
        message="هل عليه عرض؟",
        brain_state=type("S", (), {"current_product_focus": None})(),
    )
    record = facts[0].value
    assert record["product_sale_availability"] == "requires_product_context"
    assert record["allow_price_mention"] is False
    assert "verified_on_sale_product_count" not in record
    assert "on_sale_record_count" not in record
    assert "verified_on_sale_product_count" not in obs


def test_pg_excludes_merchant_hidden_from_store_wide_count(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="مخفي",
        metadata={"sale_price": "50", "regular_price": "70"},
        merchant_hidden_at="2026-01-01T00:00:00+00:00",
    )
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="ظاهر",
        metadata={"sale_price": "80", "regular_price": "100"},
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.verified_count == 1
    assert snap.sample_rows[0].title == "ظاهر"


def test_pg_excludes_out_of_stock_from_store_wide_count(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="غير متوفر",
        metadata={"sale_price": "50", "regular_price": "70"},
        in_stock=False,
    )
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="متوفر",
        metadata={"sale_price": "80", "regular_price": "100"},
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.verified_count == 1
    assert snap.sample_rows[0].title == "متوفر"


def test_pg_excludes_inactive_catalog_status_from_store_wide_count(pg_session) -> None:
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="مؤرشف",
        metadata={"sale_price": "50", "regular_price": "70"},
        catalog_status="archived",
    )
    insert_catalog_product(
        pg_session,
        tenant_id=_TEST_TENANT_A,
        title="نشط",
        metadata={"sale_price": "80", "regular_price": "100"},
        catalog_status="active",
    )
    snap = fetch_store_wide_sale_snapshot(pg_session, tenant_id=_TEST_TENANT_A)
    assert snap.verified_count == 1
    assert snap.sample_rows[0].title == "نشط"
