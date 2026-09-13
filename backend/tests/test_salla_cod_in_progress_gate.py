"""Regression coverage for Salla storefront COD orders awaiting confirmation."""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _path in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.salla_order_fidelity import extract_salla_payment_facts  # noqa: E402
from services.store_sync import _normalise_order  # noqa: E402
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402
from store_adapters.salla_lifecycle import (  # noqa: E402
    normalize_salla_lifecycle_business_intent,
    salla_cod_requires_customer_confirmation,
)


def _live_created(order: dict) -> dict:
    return {
        **order,
        "lifecycle_observation": "live_webhook",
        "lifecycle_source_event": "order.created",
    }


def test_real_salla_shape_keeps_cod_method_separate_from_waiting_state():
    raw = {
        "id": 605882301,
        "reference_id": "ORD-GENERIC-101",
        "status": {"slug": "in_progress", "name": "قيد التنفيذ"},
        "payment": {"method": "waiting"},
        "payment_method": {"slug": "cash_on_delivery", "name": "الدفع عند الاستلام"},
        "amounts": {"total": {"amount": 174, "currency": "SAR"}},
    }

    facts = extract_salla_payment_facts(raw)
    normalized = _normalise_order(raw)

    assert facts == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }
    assert normalized["payment_method"] == "cod"
    assert normalized["payment_status"] == "waiting"
    assert normalized["is_cod"] is True


def test_waiting_method_uses_cod_payment_action_label_as_selected_method():
    raw = {
        "id": 605882302,
        "reference_id": "ORD-GENERIC-102",
        "status": {"slug": "in_progress"},
        "payment_method": "waiting",
        "payment_actions": {
            "refund_action": {"payment_method_label": "الدفع عند الاستلام"},
            "remaining_action": {"payment_method_label": "الدفع عند الاستلام"},
        },
        "accepted_payment_methods": ["bank", "cod"],
        "amounts": {"total": {"amount": 174, "currency": "SAR"}},
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }


def test_waiting_method_uses_singleton_cod_acceptance_but_not_a_mixed_list():
    singleton = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["cash_on_delivery"],
    }
    mixed = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank", "cash_on_delivery"],
    }

    assert extract_salla_payment_facts(singleton) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }
    assert extract_salla_payment_facts(mixed) == {
        "payment_method": "waiting",
        "payment_status": "waiting",
        "is_cod": False,
    }


def test_salla_waiting_bank_shape_does_not_misread_payment_action_state_label():
    raw = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank"],
        "payment_actions": {
            "refund_action": {"payment_method_label": "بإنتظار الدفع"},
            "remaining_action": {"payment_method_label": "بإنتظار الدفع"},
        },
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "bank",
        "payment_status": "waiting",
        "is_cod": False,
    }


def test_positive_salla_cod_fee_is_strong_cod_evidence():
    raw = {
        "payment_method": "waiting",
        "accepted_payment_methods": ["bank", "cod"],
        "amounts": {"cash_on_delivery": {"amount": 5, "currency": "SAR"}},
    }

    assert extract_salla_payment_facts(raw) == {
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    }


def test_salla_adapter_preserves_payment_facts_for_periodic_sync():
    raw = {
        "id": 605882303,
        "reference_id": "ORD-GENERIC-103",
        "status": {"slug": "in_progress"},
        "payment_method": "waiting",
        "accepted_payment_methods": ["cod"],
        "customer": {"name": "Customer", "mobile": "966500000000"},
        "amounts": {"total": {"amount": 174, "currency": "SAR"}},
    }

    adapter = object.__new__(SallaAdapter)
    adapter_order = adapter._normalize_order(raw, None)
    normalized = _normalise_order(adapter_order)

    assert adapter_order.payment_method == "cod"
    assert adapter_order.payment_status == "waiting"
    assert adapter_order.is_cod is True
    assert normalized["payment_method"] == "cod"
    assert normalized["payment_status"] == "waiting"
    assert normalized["is_cod"] is True


def test_live_in_progress_cod_prompts_customer_and_withholds_final_confirmation():
    normalized = _live_created({
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    })

    assert salla_cod_requires_customer_confirmation("in_progress", normalized) is True
    assert normalize_salla_lifecycle_business_intent(
        None, "in_progress", normalized
    ) == BusinessIntent.COD_CONFIRMATION


def test_generic_non_cod_in_progress_order_keeps_existing_confirmation_behavior():
    normalized = _live_created({
        "payment_method": "credit_card",
        "payment_status": "paid",
        "is_cod": False,
    })

    assert salla_cod_requires_customer_confirmation("in_progress", normalized) is False
    assert normalize_salla_lifecycle_business_intent(
        None, "in_progress", normalized
    ) == BusinessIntent.ORDER_CONFIRMED


def test_cod_under_review_is_no_longer_awaiting_customer_confirmation():
    normalized = _live_created({
        "payment_method": "cod",
        "payment_status": "waiting",
        "is_cod": True,
    })

    assert salla_cod_requires_customer_confirmation("under_review", normalized) is False
    # The button handler owns the immediate final confirmation. A later Salla
    # status webhook must not duplicate that already-sent message.
    assert normalize_salla_lifecycle_business_intent(
        "in_progress", "under_review", normalized
    ) is None
