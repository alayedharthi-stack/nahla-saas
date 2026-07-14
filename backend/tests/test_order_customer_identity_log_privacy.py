"""Privacy-safe logging for A1 order-customer identity paths."""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from services.order_customer_identity_logging import (
    log_identity_sync_event,
    log_identity_sync_failure,
)

_BANNED_TOKENS = (
    "SCUST-PII",
    "CUST-999",
    "order_id",
    "external_customer_ref",
    "salla_customer_id",
)


def test_log_identity_sync_event_no_pii(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="nahla.order_customer_identity"):
        log_identity_sync_event(
            event="external_identity_linked",
            tenant_id=42,
            order_source_kind="external_provider",
            external_identity_link_state="verified",
            customer_link_state="unlinked",
            link_outcome="linked",
            matched_via="tier_a_external_store_id+channel",
        )
    blob = caplog.text
    for token in _BANNED_TOKENS:
        assert token not in blob


def test_log_identity_sync_failure_no_order_or_ref_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="nahla.order_customer_identity"):
        log_identity_sync_failure(
            tenant_id=42,
            ingest_source="store_sync.order_webhook",
            exception_class="IntegrityError",
            link_outcome="exception",
        )
    blob = caplog.text.lower()
    assert "order_id=" not in blob
    assert "external_customer_ref=" not in blob
    assert "customer_id=" not in blob


def test_safe_read_contracts_exclude_pii_fields() -> None:
    from services.order_customer_identity_read_contract import (
        SafeExternalProfileSourceHistoryProof,
        SafeInternalCustomerSourceHistoryProof,
    )

    ext_fields = set(SafeExternalProfileSourceHistoryProof.__dataclass_fields__)
    int_fields = set(SafeInternalCustomerSourceHistoryProof.__dataclass_fields__)
    for banned in ("customer_id", "external_customer_ref", "order_id", "profile_id"):
        assert banned not in ext_fields
        assert banned not in int_fields
