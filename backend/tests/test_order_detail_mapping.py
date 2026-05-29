"""Tests for internal order detail customer/product display mapping."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from modules.ai.brain.execution.orders import (  # noqa: E402
    _filter_missing_phone_if_known,
    _missing_checkout_fields,
    _seed_checkout_state,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402
from routers.orders import _resolve_customer_display  # noqa: E402
from services.nahla_order_bridge import _customer_payload  # noqa: E402


def test_resolve_customer_display_prefers_metadata_name_over_phone() -> None:
    order = SimpleNamespace(
        customer_name="0551308005",
        customer_info={"phone": "966551308005", "name": "0551308005"},
        extra_metadata={"customer_name": "سارة"},
    )
    assert _resolve_customer_display(order, {}) == "سارة"


def test_resolve_customer_display_falls_back_to_phone_when_no_name() -> None:
    order = SimpleNamespace(
        customer_name=None,
        customer_info={"phone": "966551308005"},
        extra_metadata={},
    )
    assert _resolve_customer_display(order, {}) == "966551308005"


def test_filter_missing_phone_if_known_drops_phone_slot() -> None:
    missing = _filter_missing_phone_if_known(
        ["customer_first_name", "customer_phone", "city"],
        "966551308005",
    )
    assert missing == ["customer_first_name", "city"]


def test_filter_missing_phone_if_unknown_keeps_phone_slot() -> None:
    missing = _filter_missing_phone_if_known(
        ["customer_phone", "city"],
        "",
    )
    assert missing == ["customer_phone", "city"]


def test_whatsapp_phone_seeded_and_excluded_from_missing_slots() -> None:
    prep = OrderPreparationState()
    ctx = SimpleNamespace(customer_phone="966551308005", profile={})
    _seed_checkout_state(prep, ctx)
    assert prep.customer_phone == "966551308005"

    missing = _missing_checkout_fields(prep, is_sa=True)
    missing = _filter_missing_phone_if_known(
        missing + ["customer_phone"],
        ctx.customer_phone,
    )
    assert "customer_phone" not in missing

    conv = SimpleNamespace(
        customer=SimpleNamespace(
            phone="966551308005",
            name="",
            extra_metadata={},
        ),
        extra_metadata={},
    )
    _, info = _customer_payload(conv, prep.to_dict())
    assert info["phone"] == "966551308005"
    assert info["shipping_phone"] == "966551308005"
