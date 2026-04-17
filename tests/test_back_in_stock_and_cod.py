"""
tests/test_back_in_stock_and_cod.py
────────────────────────────────────
Coverage for two end-to-end flows shipped together:

  1. Product Back In Stock (Part A of the four-feature automation set):
      • template_library exposes back_in_stock_{ar,en} with named slots
        customer_name / store_name / product_url
      • the SmartAutomation seeded for `back_in_stock` references those
        templates and the canonical `product_back_in_stock` trigger
      • _build_template_vars resolves `product_url` correctly, including
        the synthesized `{store_url}/p/{external_id}` fallback path
      • classify_cod_reply correctly buckets confirmation/cancel words

  2. Cash-on-Delivery confirmation flow (Part B finishing touches):
      • status names are pinned to pending_confirmation → under_review
      • classify_cod_reply ignores unrelated text so we don't accidentally
        cancel the customer's order when they say "no" in another context
      • the COD service exposes the status/reply contract the webhook and
        ai_sales router rely on

These tests are deliberately pure-Python (no DB, no network). The
end-to-end DB-backed flow is exercised by the existing
`tests/test_automation_engine.py` happy-path which is unaffected here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.template_library import (  # noqa: E402
    ALLOWED_VARIABLE_SLOTS,
    DEFAULT_AUTOMATION_TEMPLATES,
    iter_template_seeds,
    numeric_var_map_for,
    required_slots_for,
)
from core.automations_seed import SEED_AUTOMATIONS  # noqa: E402
from core.automation_engine import _build_template_vars, _resolve_slot_value  # noqa: E402
from services.cod_confirmation import (  # noqa: E402
    STATUS_CANCELLED,
    STATUS_PENDING_CUSTOMER,
    STATUS_PENDING_MERCHANT,
    classify_cod_reply,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Product Back In Stock — library shape
# ─────────────────────────────────────────────────────────────────────────────

def test_back_in_stock_feature_exists_in_library() -> None:
    spec = DEFAULT_AUTOMATION_TEMPLATES.get("product_back_in_stock")
    assert spec is not None, "product_back_in_stock feature must ship in the library"
    assert spec["automation_type"] == "back_in_stock"
    assert spec["trigger_event"] == "product_back_in_stock"
    assert spec["category"] == "MARKETING"
    assert {"ar", "en"} <= set(spec["languages"].keys())


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_back_in_stock_template_uses_only_numeric_placeholders(lang: str) -> None:
    """Meta WhatsApp Cloud only accepts {{1}}, {{2}}, ..."""
    spec = DEFAULT_AUTOMATION_TEMPLATES["product_back_in_stock"]["languages"][lang]
    placeholders: set[str] = set()
    for c in spec["components"]:
        text = c.get("text") or ""
        placeholders.update(re.findall(r"\{\{[^{}]+\}\}", text))
        # Buttons can also have URL placeholders
        for btn in c.get("buttons") or []:
            placeholders.update(re.findall(r"\{\{[^{}]+\}\}", btn.get("url", "") or ""))
    for ph in placeholders:
        inner = ph.strip("{}").strip()
        assert inner.isdigit(), (
            f"back_in_stock_{lang}: placeholder {ph!r} is not numeric — Meta "
            "submission requires `{{1}}, {{2}}, ...`"
        )


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_back_in_stock_slots_are_in_allowlist(lang: str) -> None:
    spec = DEFAULT_AUTOMATION_TEMPLATES["product_back_in_stock"]["languages"][lang]
    for slot in spec["slots"]:
        assert slot in ALLOWED_VARIABLE_SLOTS, (
            f"back_in_stock_{lang}: slot '{slot}' is not in ALLOWED_VARIABLE_SLOTS"
        )


def test_back_in_stock_slot_contract() -> None:
    """The slot order is the contract the engine reads to fill {{1..N}}."""
    assert required_slots_for("back_in_stock_ar") == [
        "customer_name", "store_name", "product_url",
    ]
    assert required_slots_for("back_in_stock_en") == [
        "customer_name", "store_name", "product_url",
    ]


def test_back_in_stock_var_map_round_trip() -> None:
    var_map = numeric_var_map_for("back_in_stock_ar")
    assert var_map == {
        "{{1}}": "customer_name",
        "{{2}}": "store_name",
        "{{3}}": "product_url",
    }


def test_iter_template_seeds_includes_back_in_stock_in_each_language() -> None:
    """back_in_stock must ship in both AR and EN seeds; total count is open-ended
    as new engines (recovery/growth) keep adding default templates."""
    ar = iter_template_seeds("ar")
    en = iter_template_seeds("en")
    assert len(ar) >= 4
    assert len(en) >= 4
    assert len(ar) == len(en), "AR and EN seeds must stay symmetric"
    ar_names = {s["name"] for s in ar}
    en_names = {s["name"] for s in en}
    assert "back_in_stock_ar" in ar_names
    assert "back_in_stock_en" in en_names


# ─────────────────────────────────────────────────────────────────────────────
# 2. Product Back In Stock — seeder wiring
# ─────────────────────────────────────────────────────────────────────────────

def _seed(automation_type: str) -> Dict[str, Any]:
    for s in SEED_AUTOMATIONS:
        if s["automation_type"] == automation_type:
            return s
    raise AssertionError(f"no seed for {automation_type!r}")


def test_back_in_stock_seed_uses_library_templates() -> None:
    seed = _seed("back_in_stock")
    assert seed["trigger_event"] == "product_back_in_stock"
    assert seed["config"]["template_name"] == "back_in_stock_ar"
    assert seed["config"]["template_name_en"] == "back_in_stock_en"


def test_back_in_stock_seed_no_longer_points_at_new_arrivals() -> None:
    """
    Regression: the previous seed pointed at the generic `new_arrivals`
    template which didn't have the right slots (no product_url) so the
    automation would have shipped malformed URLs once enabled.
    """
    seed = _seed("back_in_stock")
    assert seed["config"]["template_name"] != "new_arrivals"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Variable resolution — product_url slot
# ─────────────────────────────────────────────────────────────────────────────

class _StubEvent:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.created_at = None


class _StubCustomer:
    def __init__(self, name: str):
        self.name = name
        self.id = 42


def test_build_vars_back_in_stock_resolves_product_url_from_payload() -> None:
    vars_map = _build_template_vars(
        event=_StubEvent({"product_url": "https://shop.example/p/SKU-9"}),
        customer=_StubCustomer("Lina"),
        config={},
        template_name="back_in_stock_ar",
        store_name="نحلة",
    )
    assert vars_map == {
        "{{1}}": "Lina",
        "{{2}}": "نحلة",
        "{{3}}": "https://shop.example/p/SKU-9",
    }


def test_resolve_product_url_synthesizes_when_only_external_id_present() -> None:
    """
    When the store_sync emitter couldn't bake a full product_url into the
    payload (e.g. the merchant hadn't configured store_url at the time)
    but did include the external_id + a store_url override, the resolver
    should compose the canonical {store_url}/p/{external_id} pattern.
    """
    url = _resolve_slot_value(
        slot="product_url",
        customer_name="X",
        store_name="MyStore",
        payload={"product_external_id": "SKU-7", "store_url": "https://shop.example"},
        config={},
        coupon_extras={},
    )
    assert url == "https://shop.example/p/SKU-7"


def test_resolve_product_url_returns_empty_when_nothing_known() -> None:
    """No URL data anywhere → empty string. Engine still sends; URL is blank."""
    url = _resolve_slot_value(
        slot="product_url",
        customer_name="X", store_name="MyStore",
        payload={}, config={}, coupon_extras={},
    )
    assert url == ""


def test_back_in_stock_template_render_full_round_trip() -> None:
    """
    Stitch resolver output into the template body and confirm every
    placeholder gets replaced with a real value (no orphan `{{N}}`).
    """
    vars_map = _build_template_vars(
        event=_StubEvent({
            "product_external_id": "SKU-7",
            "store_url":           "https://shop.example",
        }),
        customer=_StubCustomer("Omar"),
        config={},
        template_name="back_in_stock_ar",
        store_name="MyStore",
    )
    body = DEFAULT_AUTOMATION_TEMPLATES["product_back_in_stock"]["languages"]["ar"]["components"][0]["text"]
    rendered = body
    for ph, val in vars_map.items():
        rendered = rendered.replace(ph, val)
    assert "{{" not in rendered, f"leftover placeholder in rendered body: {rendered}"
    assert "Omar" in rendered
    assert "MyStore" in rendered
    assert "https://shop.example/p/SKU-7" in rendered


# ─────────────────────────────────────────────────────────────────────────────
# 4. ProductInterest model — schema contract
# ─────────────────────────────────────────────────────────────────────────────

def test_product_interest_model_fields_present() -> None:
    """The notify-me waitlist model must exist with the expected columns."""
    from models import ProductInterest
    cols = {c.name for c in ProductInterest.__table__.columns}
    expected = {
        "id", "tenant_id", "product_id", "customer_id",
        "customer_phone", "source", "notified", "notified_at",
        "created_at", "metadata",
    }
    assert expected <= cols, f"ProductInterest missing cols: {expected - cols}"


def test_product_has_real_stock_columns() -> None:
    """Migration 0025 added stock_quantity + in_stock as real columns."""
    from models import Product
    cols = {c.name for c in Product.__table__.columns}
    assert "stock_quantity" in cols, (
        "Product.stock_quantity must exist as a real column (not just JSONB) "
        "so back-in-stock detection can compare old-vs-new at the column level."
    )
    assert "in_stock" in cols


# ─────────────────────────────────────────────────────────────────────────────
# 5. COD confirmation flow — reply classifier
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "تأكيد الطلب ✅",
    "تأكيد الطلب",
    "تأكيد",
    "  أكد  ",
    "موافق",
    "نعم",
    "Confirm",
    "ok",
    "YES",
])
def test_classify_cod_reply_confirm(text: str) -> None:
    assert classify_cod_reply(text) == "confirm", f"expected confirm for {text!r}"


@pytest.mark.parametrize("text", [
    "إلغاء الطلب ❌",
    "الغاء الطلب",
    "إلغاء",
    "لا",
    "No",
    "cancel",
])
def test_classify_cod_reply_cancel(text: str) -> None:
    assert classify_cod_reply(text) == "cancel", f"expected cancel for {text!r}"


@pytest.mark.parametrize("text", [
    "متى يصل الطلب؟",                  # genuine question
    "كم سعر التوصيل",                   # pricing question
    "Hello",
    "I want another product",
    "",
    "   ",
    "تأكيد الطلب الجديد لو سمحت",       # contains the word but is conversational
])
def test_classify_cod_reply_unrelated_returns_none(text: str) -> None:
    """
    The webhook falls through to the AI when classify returns None. We
    must NOT match free-form messages that happen to contain the word
    "تأكيد" / "confirm" — only the literal whitelist matches.
    """
    assert classify_cod_reply(text) is None, (
        f"classify_cod_reply({text!r}) must be None — otherwise the "
        "webhook would silently consume unrelated customer messages."
    )


def test_cod_status_names_match_spec() -> None:
    """
    The user's spec says: COD orders sit in `pending_confirmation` until
    confirmed, then move to `pending_review` (or the equivalent store
    state). We use Salla's `under_review` slug as the canonical "merchant
    will review now" state. These constants are the contract every
    caller relies on — they must not drift silently.
    """
    assert STATUS_PENDING_CUSTOMER == "pending_confirmation"
    assert STATUS_PENDING_MERCHANT == "under_review"
    assert STATUS_CANCELLED == "cancelled"
