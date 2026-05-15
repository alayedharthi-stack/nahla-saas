"""
tests/test_catalog_sender.py
────────────────────────────
Phase 3 of the Meta WhatsApp Catalog feature.

Covers:
  * :func:`core.catalog.effective_retailer_id` resolution order.
  * :func:`core.catalog.is_catalog_eligible` reason vocabulary.
  * Payload shape for single + multi catalog messages (snapshot-ish).
  * Send-time eligibility short-circuits (returns ``fallback_recommended=True``).
  * Provider error / transport error → fallback recommended.
  * Happy path → success + message id extraction.
  * Section truncation respects Meta's 10/30 limits.

The sender is async and dispatches via
``services.whatsapp_platform.service.provider_send_message``. Tests
monkey-patch that one symbol so we never hit a real HTTP endpoint.

Run:
    cd backend
    python -m pytest tests/test_catalog_sender.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.catalog import (
    CatalogEligibility,
    catalog_summary,
    effective_retailer_id,
    is_catalog_eligible,
)
from services.whatsapp_platform import catalog_sender as cs


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Conn:
    """Stand-in for a WhatsAppConnection ORM row."""
    meta_catalog_id: Optional[str] = None
    catalog_enabled: bool = False
    phone_number_id: str = "PHONE1"


@dataclass
class _Product:
    """Stand-in for a Product ORM row."""
    external_id: Optional[str] = None
    meta_retailer_id: Optional[str] = None
    title: str = ""


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────────
# 1. effective_retailer_id — resolution order
# ─────────────────────────────────────────────────────────────────────────────

def test_effective_retailer_prefers_explicit_meta_id() -> None:
    p = _Product(external_id="ext-123", meta_retailer_id="meta-999")
    assert effective_retailer_id(p) == "meta-999"


def test_effective_retailer_falls_back_to_external_id() -> None:
    p = _Product(external_id="ext-123", meta_retailer_id=None)
    assert effective_retailer_id(p) == "ext-123"


def test_effective_retailer_strips_whitespace() -> None:
    p = _Product(external_id="  ext-123  ", meta_retailer_id=None)
    assert effective_retailer_id(p) == "ext-123"


def test_effective_retailer_empty_when_nothing_set() -> None:
    p = _Product(external_id=None, meta_retailer_id=None)
    assert effective_retailer_id(p) == ""


def test_effective_retailer_accepts_dict_shape() -> None:
    """The [PRODUCT:...] resolver hands us dicts, not ORM rows."""
    assert effective_retailer_id({"external_id": "ext-7"}) == "ext-7"
    assert effective_retailer_id({"meta_retailer_id": "m1", "external_id": "x"}) == "m1"
    assert effective_retailer_id({}) == ""


def test_effective_retailer_handles_none() -> None:
    assert effective_retailer_id(None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. is_catalog_eligible — reason vocabulary
# ─────────────────────────────────────────────────────────────────────────────

def test_eligibility_connection_missing() -> None:
    e = is_catalog_eligible(None)
    assert e.ok is False
    assert e.reason == "connection_missing"


def test_eligibility_catalog_disabled() -> None:
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=False)
    e = is_catalog_eligible(conn)
    assert e.ok is False
    assert e.reason == "catalog_disabled"


def test_eligibility_catalog_id_missing() -> None:
    conn = _Conn(meta_catalog_id="", catalog_enabled=True)
    e = is_catalog_eligible(conn)
    assert e.ok is False
    assert e.reason == "catalog_id_missing"


def test_eligibility_ok_without_products() -> None:
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    e = is_catalog_eligible(conn)
    assert e.ok is True
    assert e.reason == "ok"


def test_eligibility_no_retailer_id_in_products() -> None:
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    products = [_Product(external_id=None, meta_retailer_id=None)]
    e = is_catalog_eligible(conn, products=products)
    assert e.ok is False
    assert e.reason == "no_retailer_id"


def test_eligibility_empty_products_iterable() -> None:
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    e = is_catalog_eligible(conn, products=[])
    assert e.ok is False
    assert e.reason == "empty_products"


def test_eligibility_happy_path_with_products() -> None:
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    products = [_Product(external_id="ext-1"), _Product(external_id="ext-2")]
    e = is_catalog_eligible(conn, products=products)
    assert e.ok is True
    assert e.reason == "ok"


def test_catalog_summary_shapes() -> None:
    assert catalog_summary(None)["catalog_bound"] is False
    assert catalog_summary(_Conn())["reason"] == "catalog_id_missing"
    assert catalog_summary(
        _Conn(meta_catalog_id="C", catalog_enabled=False)
    )["reason"] == "catalog_disabled"
    s = catalog_summary(_Conn(meta_catalog_id="C", catalog_enabled=True))
    assert s == {
        "catalog_bound": True,
        "catalog_enabled": True,
        "meta_catalog_id": "C",
        "reason": "ok",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Payload builders — schema-shape tests
# ─────────────────────────────────────────────────────────────────────────────

def test_single_payload_minimal_shape() -> None:
    p = cs.build_single_product_payload(
        to="966555555555",
        catalog_id="CAT1",
        retailer_id="R1",
        body_text="بطاقة المنتج",
    )
    assert p["messaging_product"] == "whatsapp"
    assert p["recipient_type"] == "individual"
    assert p["to"] == "966555555555"
    assert p["type"] == "interactive"
    inter = p["interactive"]
    assert inter["type"] == "product"
    assert inter["body"]["text"] == "بطاقة المنتج"
    assert inter["action"] == {
        "catalog_id": "CAT1",
        "product_retailer_id": "R1",
    }
    assert "footer" not in inter


def test_single_payload_default_body_when_empty() -> None:
    p = cs.build_single_product_payload(
        to="966", catalog_id="C", retailer_id="R", body_text="",
    )
    assert p["interactive"]["body"]["text"], "must always populate a body"


def test_single_payload_truncates_body_and_footer() -> None:
    long_body = "ج" * (cs.MAX_BODY_LEN + 50)
    long_footer = "ف" * (cs.MAX_FOOTER_LEN + 10)
    p = cs.build_single_product_payload(
        to="966", catalog_id="C", retailer_id="R",
        body_text=long_body, footer_text=long_footer,
    )
    assert len(p["interactive"]["body"]["text"]) <= cs.MAX_BODY_LEN
    assert len(p["interactive"]["footer"]["text"]) <= cs.MAX_FOOTER_LEN


def test_multi_payload_shape() -> None:
    sections = [
        cs.CatalogSection(title="عسل السدر", retailer_ids=["R1", "R2"]),
        cs.CatalogSection(title="عسل الطلح", retailer_ids=["R3"]),
    ]
    p = cs.build_product_list_payload(
        to="966", catalog_id="CAT1", sections=sections,
        body_text="اخترلك أنسب الخيارات",
        header_text="منتجاتنا",
        footer_text="شحن سريع",
    )
    inter = p["interactive"]
    assert inter["type"] == "product_list"
    assert inter["header"] == {"type": "text", "text": "منتجاتنا"}
    assert inter["footer"]["text"] == "شحن سريع"
    assert inter["action"]["catalog_id"] == "CAT1"
    assert [s["title"] for s in inter["action"]["sections"]] == [
        "عسل السدر", "عسل الطلح",
    ]
    assert [
        item["product_retailer_id"]
        for s in inter["action"]["sections"]
        for item in s["product_items"]
    ] == ["R1", "R2", "R3"]


def test_multi_payload_drops_empty_sections() -> None:
    sections = [
        cs.CatalogSection(title="فارغ", retailer_ids=[]),
        cs.CatalogSection(title="سدر", retailer_ids=["R1"]),
        cs.CatalogSection(title="فارغ٢", retailer_ids=["", None]),  # all skipped
    ]
    p = cs.build_product_list_payload(
        to="966", catalog_id="C", sections=sections, body_text="x",
    )
    assert len(p["interactive"]["action"]["sections"]) == 1
    assert p["interactive"]["action"]["sections"][0]["title"] == "سدر"


def test_multi_payload_caps_at_30_products_across_10_sections() -> None:
    """Meta limits: 10 sections, 30 products total. Builder enforces both."""
    sections = [
        cs.CatalogSection(title=f"S{i}", retailer_ids=[f"R{i}-{j}" for j in range(5)])
        for i in range(12)   # 12 sections × 5 = 60 ids
    ]
    p = cs.build_product_list_payload(
        to="966", catalog_id="C", sections=sections, body_text="x",
    )
    secs = p["interactive"]["action"]["sections"]
    assert len(secs) <= cs.MAX_SECTIONS
    total = sum(len(s["product_items"]) for s in secs)
    assert total <= cs.MAX_PRODUCTS_TOTAL


def test_multi_payload_raises_when_no_non_empty_sections() -> None:
    with pytest.raises(ValueError):
        cs.build_product_list_payload(
            to="966", catalog_id="C",
            sections=[cs.CatalogSection(title="x", retailer_ids=[])],
            body_text="x",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Send wrappers — eligibility short-circuits
# ─────────────────────────────────────────────────────────────────────────────

def test_send_single_short_circuits_when_catalog_disabled(monkeypatch) -> None:
    sent: list = []

    async def fake_send(*args, **kwargs):
        sent.append((args, kwargs))
        return {"messages": [{"id": "wamid.OK"}]}, None

    monkeypatch.setattr(cs, "provider_send_message", fake_send)

    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=False)
    result = _run(cs.send_single_product_message(
        db=None, connection=conn,
        tenant_id=1, to="966555", phone_id="PH",
        retailer_id="R1", body_text="x",
    ))
    assert result.success is False
    assert result.fallback_recommended is True
    assert result.reason == "catalog_disabled"
    assert sent == [], "provider_send_message must NOT be called when ineligible"


def test_send_single_short_circuits_when_retailer_missing(monkeypatch) -> None:
    async def fake_send(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("provider_send_message should be skipped")

    monkeypatch.setattr(cs, "provider_send_message", fake_send)
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    result = _run(cs.send_single_product_message(
        db=None, connection=conn,
        tenant_id=1, to="966555", phone_id="PH",
        retailer_id="", body_text="x",
    ))
    assert result.success is False
    assert result.reason == "no_retailer_id"
    assert result.fallback_recommended is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. Send wrappers — provider failures
# ─────────────────────────────────────────────────────────────────────────────

def test_send_single_provider_returns_error(monkeypatch) -> None:
    async def fake_send(*args, **kwargs):
        return {"error": {"message": "bad catalog"}}, None

    monkeypatch.setattr(cs, "provider_send_message", fake_send)
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    result = _run(cs.send_single_product_message(
        db=None, connection=conn,
        tenant_id=1, to="966555", phone_id="PH",
        retailer_id="R1", body_text="x",
    ))
    assert result.success is False
    assert result.fallback_recommended is True
    assert result.reason == "provider_error"
    assert "bad catalog" in (result.error or "")


def test_send_single_transport_exception(monkeypatch) -> None:
    async def fake_send(*args, **kwargs):
        raise ConnectionError("upstream timeout")

    monkeypatch.setattr(cs, "provider_send_message", fake_send)
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    result = _run(cs.send_single_product_message(
        db=None, connection=conn,
        tenant_id=1, to="966555", phone_id="PH",
        retailer_id="R1", body_text="x",
    ))
    assert result.success is False
    assert result.fallback_recommended is True
    assert result.reason == "transport_error"
    assert "upstream timeout" in (result.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Send wrappers — happy paths
# ─────────────────────────────────────────────────────────────────────────────

def test_send_single_happy_path(monkeypatch) -> None:
    captured: dict = {}

    async def fake_send(db, conn, *, tenant_id, operation, phone_id, payload, timeout):
        captured.update(
            tenant_id=tenant_id, operation=operation,
            phone_id=phone_id, payload=payload, timeout=timeout,
        )
        return {"messages": [{"id": "wamid.XYZ"}]}, None

    monkeypatch.setattr(cs, "provider_send_message", fake_send)
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    result = _run(cs.send_single_product_message(
        db=None, connection=conn,
        tenant_id=42, to="966555", phone_id="PH",
        retailer_id="R-SDR-1", body_text="بطاقة عسل السدر",
        footer_text="شحن سريع",
    ))
    assert result.success is True
    assert result.reason == "sent"
    assert result.message_id == "wamid.XYZ"
    assert result.fallback_recommended is False
    assert captured["tenant_id"] == 42
    assert captured["operation"] == "send_catalog_product"
    assert captured["payload"]["interactive"]["type"] == "product"
    assert captured["payload"]["interactive"]["action"]["catalog_id"] == "CAT1"
    assert captured["payload"]["interactive"]["action"]["product_retailer_id"] == "R-SDR-1"


def test_send_multi_happy_path(monkeypatch) -> None:
    captured: dict = {}

    async def fake_send(db, conn, *, tenant_id, operation, phone_id, payload, timeout):
        captured["payload"] = payload
        captured["operation"] = operation
        return {"messages": [{"id": "wamid.MULTI"}]}, None

    monkeypatch.setattr(cs, "provider_send_message", fake_send)
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    sections = [
        cs.CatalogSection(title="السدر", retailer_ids=["R1", "R2"]),
        cs.CatalogSection(title="الطلح", retailer_ids=["R3"]),
    ]
    result = _run(cs.send_multi_product_message(
        db=None, connection=conn,
        tenant_id=42, to="966555", phone_id="PH",
        sections=sections, body_text="اختر النوع",
        header_text="منتجاتنا",
    ))
    assert result.success is True
    assert result.reason == "sent"
    assert result.message_id == "wamid.MULTI"
    assert captured["operation"] == "send_catalog_product_list"
    inter = captured["payload"]["interactive"]
    assert inter["type"] == "product_list"
    assert len(inter["action"]["sections"]) == 2


def test_send_multi_falls_back_when_no_valid_retailer(monkeypatch) -> None:
    async def fake_send(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("should not reach provider when all sections empty")

    monkeypatch.setattr(cs, "provider_send_message", fake_send)
    conn = _Conn(meta_catalog_id="CAT1", catalog_enabled=True)
    sections = [cs.CatalogSection(title="فارغ", retailer_ids=[])]
    result = _run(cs.send_multi_product_message(
        db=None, connection=conn,
        tenant_id=1, to="966555", phone_id="PH",
        sections=sections, body_text="x",
    ))
    assert result.success is False
    assert result.fallback_recommended is True
    assert result.reason == "no_retailer_id"


# ─────────────────────────────────────────────────────────────────────────────
# 7. products_to_section convenience
# ─────────────────────────────────────────────────────────────────────────────

def test_products_to_section_skips_products_without_retailer_id() -> None:
    products = [
        _Product(external_id="ext-1"),
        _Product(external_id=None, meta_retailer_id=None),  # skipped
        _Product(meta_retailer_id="meta-7"),
    ]
    sec = cs.products_to_section("الأكثر مبيعاً", products)
    assert sec.title == "الأكثر مبيعاً"
    assert list(sec.retailer_ids) == ["ext-1", "meta-7"]


def test_products_to_section_default_title_when_blank() -> None:
    sec = cs.products_to_section("", [_Product(external_id="x")])
    assert sec.title  # never empty — Meta requires a section title
