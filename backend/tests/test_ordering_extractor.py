"""Tests for deterministic ordering slot extraction (customer name regression)."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from modules.ai.brain.execution.orders import (  # noqa: E402
    _merge_message_details,
)
from modules.ai.brain.intent.ordering_extractor import (  # noqa: E402
    _strip_numbered_list_prefix,
    extract_ordering_slots,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402


def test_strip_numbered_list_prefix_arabic_western() -> None:
    assert _strip_numbered_list_prefix("1- جميل العتيبي") == "جميل العتيبي"
    assert _strip_numbered_list_prefix("2. أحمد محمد") == "أحمد محمد"
    assert _strip_numbered_list_prefix("١- سارة") == "سارة"


def test_extract_name_from_numbered_multiline_with_national_address() -> None:
    message = "\n".join([
        "1- جميل العتيبي",
        "2- الرياض",
        "3- RIYD1234",
    ])
    slots = extract_ordering_slots(message)
    assert slots.get("customer_first_name") == "جميل"
    assert slots.get("customer_last_name") == "العتيبي"
    assert slots.get("customer_name") == "جميل العتيبي"
    assert slots.get("short_address_code") == "RIYD1234"
    assert slots.get("city") == "الرياض"


def test_merge_message_details_fallback_when_intent_slots_empty() -> None:
    message = "1- جميل العتيبي\nالرياض\nRIYD1234"
    prep = OrderPreparationState()
    _merge_message_details(prep, {}, message)
    assert prep.customer_first_name == "جميل"
    assert prep.customer_last_name == "العتيبي"
    assert prep.short_address_code == "RIYD1234"
    assert prep.city == "الرياض"


def test_merge_message_details_does_not_overwrite_real_name() -> None:
    prep = OrderPreparationState(
        customer_first_name="جميل",
        customer_last_name="العتيبي",
    )
    _merge_message_details(
        prep,
        {"customer_first_name": "966551308005", "customer_last_name": ""},
        "",
    )
    assert prep.customer_first_name == "جميل"
    assert prep.customer_last_name == "العتيبي"


def test_merge_message_details_upgrades_empty_to_extracted_name() -> None:
    message = "1- جميل العتيبي"
    prep = OrderPreparationState(customer_first_name="")
    _merge_message_details(prep, {}, message)
    assert prep.customer_first_name == "جميل"
    assert prep.customer_last_name == "العتيبي"
