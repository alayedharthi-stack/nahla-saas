"""Regression: low-trust proposed hints must not demote official display names."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.customer_identity_resolver import (  # noqa: E402
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
    STATUS_VERIFIED,
    SOURCE_CUSTOMER_MESSAGE,
    apply_customer_name,
    display_name_for_customer,
    read_customer_identity,
)
from core.customer_name_extractor import extract_high_confidence_name  # noqa: E402
from core.customer_name_validator import (  # noqa: E402
    is_deictic_or_conversational_name_phrase,
    validate_customer_name,
)


def _customer(
    *,
    name: str = "",
    status: str = "",
    source: str = SOURCE_CUSTOMER_MESSAGE,
    proposed: str = "",
    extra: dict | None = None,
) -> SimpleNamespace:
    meta = dict(extra or {})
    if status:
        meta["customer_name_status"] = status
    if source:
        meta["customer_name_source"] = source
    if proposed:
        meta["proposed_name"] = proposed
    return SimpleNamespace(name=name or None, extra_metadata=meta)


class TestOfficialNameNotDowngradedByProposed:
    @pytest.mark.parametrize("status", (STATUS_CUSTOMER_ENTERED, STATUS_VERIFIED))
    def test_known_official_name_not_downgraded(self, status: str) -> None:
        customer = _customer(
            name="سلطان القرني",
            status=status,
            source=SOURCE_CUSTOMER_MESSAGE,
        )
        before_meta = dict(customer.extra_metadata)

        changed = apply_customer_name(customer, "هذا انت", source="whatsapp_inbound")

        assert changed is False
        assert customer.name == "سلطان القرني"
        assert customer.extra_metadata.get("customer_name_status") == status
        assert customer.extra_metadata.get("proposed_name") == before_meta.get("proposed_name")
        assert display_name_for_customer(customer, phone_fallback="+966560734889") == "سلطان القرني"

    def test_media_style_candidate_rejected_and_no_downgrade(self) -> None:
        customer = _customer(
            name="سلطان القرني",
            status=STATUS_VERIFIED,
            source="salla_order",
        )
        assert validate_customer_name("هذا انت").valid is False
        apply_customer_name(customer, "هذا انت", source="whatsapp_inbound")
        assert customer.name == "سلطان القرني"
        assert customer.extra_metadata.get("customer_name_status") == STATUS_VERIFIED
        assert display_name_for_customer(customer, phone_fallback="+966560734889") == "سلطان القرني"


class TestDisplayPrefersStoredNameOverProposed:
    def test_legacy_demoted_status_still_shows_stored_name(self) -> None:
        customer = _customer(
            name="سلطان القرني",
            status=STATUS_PROPOSED,
            proposed="هذا انت",
            source="whatsapp_inbound",
        )
        snap = read_customer_identity(customer)
        assert snap.display_name == "سلطان القرني"
        assert display_name_for_customer(customer, phone_fallback="+966560734889") == "سلطان القرني"


class TestUnknownCustomerRejectsDeicticProposed:
    def test_deictic_phrase_not_valid_name(self) -> None:
        assert is_deictic_or_conversational_name_phrase("هذا انت") is True
        assert validate_customer_name("هذا انت").valid is False

    def test_unknown_customer_rejects_whatsapp_hint(self) -> None:
        customer = _customer(name="", status="", source="")
        apply_customer_name(customer, "هذا انت", source="whatsapp_inbound")
        assert not (customer.name or "").strip()
        assert display_name_for_customer(customer, phone_fallback="+966560734889") == "+966560734889"

    def test_extractor_rejects_meet_you_deictic(self) -> None:
        assert extract_high_confidence_name("معك هذا انت") is None
        assert extract_high_confidence_name("أنا هذا انت") is None


class TestExplicitRealNameStillWorks:
    def test_explicit_name_adopted_for_unknown_customer(self) -> None:
        customer = _customer(name="", status="", source="")
        message = "اسمي سلطان القرني"
        hit = extract_high_confidence_name(message)
        assert hit is not None
        assert hit.value == "سلطان القرني"

        apply_customer_name(
            customer,
            hit.value,
            source="ai_detected_name",
            explicit_customer_entry=True,
            message_context={"message": message, "explicit_customer_entry": True},
        )
        assert customer.name == "سلطان القرني"
        assert customer.extra_metadata.get("customer_name_status") == STATUS_CUSTOMER_ENTERED
        assert display_name_for_customer(customer, phone_fallback="+966560734889") == "سلطان القرني"
