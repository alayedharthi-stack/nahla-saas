"""Regression tests for real-channel acceptance order side-effect signatures."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators.real_channel_acceptance_order_side_effect_signatures import (  # noqa: E402
    ORDER_VOLATILE_METADATA_KEYS,
    build_order_side_effect_snapshot,
    compare_order_rows_with_metadata,
    detect_concurrent_sync_drift,
    evaluate_order_side_effect_gate,
    extract_volatile_metadata_by_row,
    redacted_order_row_key,
)


def _order_row(
    *,
    order_id: int = 10,
    tenant_id: int = 1,
    status: str = "pending",
    total: str = "120.00",
    line_items: list | None = None,
    metadata: dict | None = None,
    customer_id: int | None = 5,
) -> dict:
    return {
        "id": order_id,
        "tenant_id": tenant_id,
        "status": status,
        "total": total,
        "line_items": line_items or [{"sku": "SKU-1", "qty": 1}],
        "source": "whatsapp",
        "is_abandoned": False,
        "external_id": "ext-100",
        "external_order_number": "ORD-100",
        "customer_id": customer_id,
        "customer_name": "Generic Customer",
        "customer_info": {"city": "الرياض"},
        "checkout_url": "https://example.test/checkout",
        "metadata": metadata
        or {
            "payment_method": "cod",
            "payment_status": "pending",
            "last_synced_at": "2026-07-22T17:35:00+00:00",
        },
        "order_source_kind": "whatsapp",
        "identity_namespace": None,
        "integration_connection_id": None,
        "external_customer_ref": None,
        "external_customer_profile_id": None,
        "customer_link_state": "linked",
        "customer_link_evidence_class": "verified",
        "customer_link_source": "conversation",
        "customer_linked_at": "2026-07-22T17:00:00+00:00",
        "external_identity_link_state": None,
        "external_identity_evidence_class": None,
    }


def test_only_last_synced_at_change_records_concurrent_drift_not_failure() -> None:
    before = [_order_row()]
    after = [
        _order_row(
            metadata={
                "payment_method": "cod",
                "payment_status": "pending",
                "last_synced_at": "2026-07-22T17:39:18+00:00",
            }
        )
    ]

    result = compare_order_rows_with_metadata(before, after)

    assert result["ai_side_effect_detected"] is False
    assert result["blockers"] == []
    assert len(result["concurrent_sync_drift"]) == 1
    assert result["concurrent_sync_drift"][0]["volatile_fields"] == ["metadata.last_synced_at"]
    assert result["concurrent_sync_drift"][0]["actor_evidence"] == "concurrent_integration_sync"


@pytest.mark.parametrize(
    ("mutator", "expected_blocker"),
    [
        (lambda row: {**row, "status": "paid"}, "order_critical_field_changed"),
        (lambda row: {**row, "total": "999.00"}, "order_critical_field_changed"),
        (
            lambda row: {
                **row,
                "line_items": [{"sku": "SKU-2", "qty": 2}],
            },
            "order_critical_field_changed",
        ),
        (
            lambda row: {
                **row,
                "metadata": {
                    **row["metadata"],
                    "payment_status": "paid",
                },
            },
            "order_critical_field_changed",
        ),
        (lambda row: {**row, "customer_id": 99}, "order_critical_field_changed"),
    ],
)
def test_critical_order_changes_fail_closed(mutator, expected_blocker) -> None:
    before = [_order_row()]
    after = [mutator(copy.deepcopy(before[0]))]

    result = compare_order_rows_with_metadata(before, after)

    assert result["ai_side_effect_detected"] is True
    assert expected_blocker in result["blockers"]
    assert result["critical_diffs"]


def test_new_order_row_fails_side_effect_gate() -> None:
    before = [_order_row(order_id=10)]
    second = _order_row(order_id=11)
    second["external_id"] = "ext-101"
    second["external_order_number"] = "ORD-101"
    after = before + [second]

    result = compare_order_rows_with_metadata(before, after)

    assert result["ai_side_effect_detected"] is True
    assert "order_row_created" in result["blockers"]
    assert "order_max_id_increased" in result["blockers"]


def test_unknown_metadata_key_change_fails_closed() -> None:
    before = [_order_row()]
    after = [
        _order_row(
            metadata={
                **before[0]["metadata"],
                "unexpected_operator_flag": True,
            }
        )
    ]

    result = compare_order_rows_with_metadata(before, after)

    assert result["ai_side_effect_detected"] is True
    assert "order_unknown_metadata_changed" in result["blockers"]


def test_cross_tenant_rows_are_ignored_in_snapshot() -> None:
    tenant_one = [_order_row(order_id=10, tenant_id=1)]
    tenant_two_only = [_order_row(order_id=99, tenant_id=2, status="other-tenant")]

    snapshot = build_order_side_effect_snapshot(tenant_one + tenant_two_only, tenant_id=1)

    assert snapshot["aggregate"]["count"] == 1
    assert redacted_order_row_key(10) in snapshot["rows"]
    assert redacted_order_row_key(99) not in snapshot["rows"]

    armed = {
        "snapshot": build_order_side_effect_snapshot(tenant_one, tenant_id=1),
        "volatile_metadata_by_row": extract_volatile_metadata_by_row(tenant_one),
        "metadata_keys_by_row": {
            redacted_order_row_key(10): sorted(tenant_one[0]["metadata"])
        },
    }
    gate = evaluate_order_side_effect_gate(armed=armed, after_rows=tenant_one)

    assert gate["ai_side_effect_detected"] is False
    assert gate["blockers"] == []


def test_evaluate_gate_detects_unknown_metadata_added_after_arm() -> None:
    before = [_order_row()]
    armed = {
        "snapshot": build_order_side_effect_snapshot(before),
        "volatile_metadata_by_row": extract_volatile_metadata_by_row(before),
        "metadata_keys_by_row": {
            redacted_order_row_key(10): sorted(before[0]["metadata"])
        },
    }
    after = [
        _order_row(
            metadata={
                **before[0]["metadata"],
                "new_sync_marker": "x",
            }
        )
    ]

    gate = evaluate_order_side_effect_gate(armed=armed, after_rows=after)

    assert gate["ai_side_effect_detected"] is True
    assert "order_unknown_metadata_changed" in gate["blockers"]


def test_detect_concurrent_sync_drift_is_bounded() -> None:
    before = [_order_row(order_id=idx, metadata={"last_synced_at": "t0"}) for idx in range(1, 4)]
    armed_volatile = extract_volatile_metadata_by_row(before)
    after = [
        _order_row(order_id=idx, metadata={"last_synced_at": f"t{idx}"}) for idx in range(1, 4)
    ]

    drift = detect_concurrent_sync_drift(
        armed_volatile_by_row=armed_volatile,
        after_rows=after,
    )

    assert len(drift) == 3
    assert all(item["volatile_fields"] == ["metadata.last_synced_at"] for item in drift)


def test_snapshot_evidence_contains_no_raw_order_ids_or_pii() -> None:
    row = _order_row()
    signature = build_order_side_effect_snapshot([row])["rows"][
        redacted_order_row_key(row["id"])
    ]

    serialized = str(signature)
    assert "ext-100" not in serialized
    assert "ORD-100" not in serialized
    assert "Generic Customer" not in serialized
    assert str(row["id"]) not in serialized
    assert "sha256:" in signature["row_fingerprint"]


def test_volatile_allowlist_is_closed() -> None:
    assert "last_synced_at" in ORDER_VOLATILE_METADATA_KEYS
    assert "payment_status" not in ORDER_VOLATILE_METADATA_KEYS
