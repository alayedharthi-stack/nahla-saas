"""A Salla coupon's discount is stored as a number, never as a money object's
string form.

Tenant 1, September 2026: a coupon issued as 5% was pushed to Salla and read
back with its amount as ``{"amount": 5, "currency": "SAR"}``; the reconcile
wrote that object's string over the numeric value, so the record said
"percentage" and "5 SAR" at once, and the AI told a customer "5 SAR". The
normaliser now unwraps the object. Offline, generic merchant payloads.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from services.store_sync import _normalise_coupon, _scalar_discount  # noqa: E402


def _raw(**fields: object) -> dict:
    raw: dict = {"id": 501, "code": "NHTEST", "type": "percentage", "status": "active", "name": "خصم"}
    raw.update(fields)
    return raw


def test_a_money_object_amount_is_stored_as_its_number() -> None:
    normalised = _normalise_coupon(_raw(amount={"amount": 5, "currency": "SAR"}))
    assert normalised["discount_type"] == "percentage"
    assert normalised["discount_value"] == "5"


def test_a_fixed_coupon_s_money_object_keeps_its_number() -> None:
    normalised = _normalise_coupon(_raw(type="fixed", amount={"amount": 20, "currency": "SAR"}))
    assert normalised["discount_type"] == "fixed"
    assert normalised["discount_value"] == "20"


def test_plain_numbers_pass_through_unchanged() -> None:
    assert _normalise_coupon(_raw(amount=15))["discount_value"] == "15"
    assert _normalise_coupon(_raw(percent=12))["discount_value"] == "12"
    assert _normalise_coupon(_raw(amount="7.5"))["discount_value"] == "7.5"


def test_a_nested_object_is_unwrapped_and_an_empty_one_reads_as_nothing() -> None:
    assert _scalar_discount({"amount": {"amount": 3, "currency": "SAR"}}) == 3
    assert _scalar_discount({"currency": "SAR"}) == ""
    assert _scalar_discount("7") == "7"
    assert _scalar_discount(None) is None
    assert _normalise_coupon(_raw(amount={"currency": "SAR"}))["discount_value"] == ""
