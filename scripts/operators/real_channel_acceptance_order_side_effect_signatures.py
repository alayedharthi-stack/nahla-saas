"""Structured order side-effect signatures for real-channel acceptance.

Replaces whole-row ``to_jsonb`` hashing with per-row critical/volatile field
groups so concurrent integration sync metadata (e.g. ``metadata.last_synced_at``)
does not false-fail the AI side-effect gate.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from scripts.operators.real_channel_conversational_acceptance_contract import (
    hash_identifier,
)

ORDER_SIDE_EFFECT_CONTRACT_VERSION = "real_channel_acceptance_order_side_effect_v1"

# Closed volatile metadata allowlist — integration sync bookkeeping only.
ORDER_VOLATILE_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "last_synced_at",
        "sync_status",
        "sync_direction",
        "synced_at",
        "salla_synced",
    }
)

MAX_CONCURRENT_SYNC_DRIFT_ROWS = 20
MAX_VOLATILE_FIELD_NAMES = 8
MAX_UNKNOWN_METADATA_KEYS = 10
MAX_CRITICAL_DIFF_ROWS = 20

_ORDER_ROW_SALT = "nahla-rca-order-row-v1"
_PII_FIELD_SALTS = {
    "external_id": "nahla-rca-order-ext-id",
    "external_order_number": "nahla-rca-order-ext-num",
    "external_customer_ref": "nahla-rca-order-ext-cust",
    "customer_name": "nahla-rca-order-cust-name",
    "checkout_url": "nahla-rca-order-checkout",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _group_hash(value: Any) -> str:
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def _redact_pii_field(field: str, value: Any) -> Any:
    if value is None:
        return None
    salt = _PII_FIELD_SALTS.get(field)
    if salt is not None:
        return hash_identifier(str(value), salt=salt)
    if field == "customer_info":
        return _group_hash(value)
    return value


def _metadata_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata")
    if raw is None:
        raw = row.get("extra_metadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def redacted_order_row_key(order_id: object) -> str:
    return hash_identifier(str(order_id), salt=_ORDER_ROW_SALT)


def _critical_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: metadata[key]
        for key in sorted(metadata)
        if key not in ORDER_VOLATILE_METADATA_KEYS
    }


def _payment_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("payment_method", "payment_status", "salla_amounts", "payment_url")
    return {key: metadata[key] for key in keys if key in metadata}


def build_order_row_signature(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build a redacted per-row signature for one tenant-scoped order row."""
    metadata = _metadata_mapping(row)
    return {
        "row_fingerprint": redacted_order_row_key(row.get("id")),
        "critical_groups": {
            "lifecycle": _group_hash(
                {
                    "status": row.get("status"),
                    "is_abandoned": row.get("is_abandoned"),
                }
            ),
            "totals": _group_hash({"total": row.get("total")}),
            "line_items": _group_hash(row.get("line_items")),
            "payment": _group_hash(_payment_metadata(metadata)),
            "customer_link": _group_hash(
                {
                    "customer_id": row.get("customer_id"),
                    "customer_link_state": row.get("customer_link_state"),
                    "customer_link_evidence_class": row.get("customer_link_evidence_class"),
                    "customer_link_source": row.get("customer_link_source"),
                    "customer_linked_at": str(row.get("customer_linked_at") or ""),
                    "external_identity_link_state": row.get("external_identity_link_state"),
                    "external_identity_evidence_class": row.get(
                        "external_identity_evidence_class"
                    ),
                    "identity_namespace": row.get("identity_namespace"),
                    "integration_connection_id": row.get("integration_connection_id"),
                    "external_customer_ref": _redact_pii_field(
                        "external_customer_ref", row.get("external_customer_ref")
                    ),
                    "external_customer_profile_id": (
                        hash_identifier(str(row.get("external_customer_profile_id") or ""), salt="nahla-rca-order-ecp")
                        if row.get("external_customer_profile_id")
                        else None
                    ),
                }
            ),
            "source_identity": _group_hash(
                {
                    "source": row.get("source"),
                    "order_source_kind": row.get("order_source_kind"),
                    "external_id": _redact_pii_field("external_id", row.get("external_id")),
                    "external_order_number": _redact_pii_field(
                        "external_order_number", row.get("external_order_number")
                    ),
                    "customer_name": _redact_pii_field("customer_name", row.get("customer_name")),
                    "customer_info": _redact_pii_field("customer_info", row.get("customer_info")),
                    "checkout_url": _redact_pii_field("checkout_url", row.get("checkout_url")),
                }
            ),
            "metadata_critical": _group_hash(_critical_metadata(metadata)),
        },
        "volatile_metadata_keys": sorted(
            key for key in metadata if key in ORDER_VOLATILE_METADATA_KEYS
        ),
    }


def build_order_side_effect_snapshot(
    rows: Sequence[Mapping[str, Any]],
    *,
    tenant_id: int | None = None,
) -> dict[str, Any]:
    """Aggregate tenant order side-effect snapshot from scoped SQL rows."""
    scoped_rows = [
        row
        for row in rows
        if row.get("tenant_id") is not None
        and (tenant_id is None or int(row.get("tenant_id")) == tenant_id)
    ]
    row_signatures = {
        signature["row_fingerprint"]: signature
        for row in scoped_rows
        for signature in [build_order_row_signature(row)]
    }
    order_ids = [int(row["id"]) for row in scoped_rows if row.get("id") is not None]
    return {
        "contract_version": ORDER_SIDE_EFFECT_CONTRACT_VERSION,
        "aggregate": {
            "count": len(scoped_rows),
            "max_id": max(order_ids, default=0),
            "critical_content_hash": _group_hash(
                {
                    fp: sig["critical_groups"]
                    for fp, sig in sorted(row_signatures.items())
                }
            ),
        },
        "rows": row_signatures,
    }


def _unknown_metadata_changes(
    before_meta: Mapping[str, Any], after_meta: Mapping[str, Any]
) -> list[str]:
    unknown: list[str] = []
    all_keys = sorted(set(before_meta) | set(after_meta))
    for key in all_keys:
        if key in ORDER_VOLATILE_METADATA_KEYS:
            continue
        if before_meta.get(key) != after_meta.get(key):
            unknown.append(f"metadata.{key}")
        if len(unknown) >= MAX_UNKNOWN_METADATA_KEYS:
            break
    return unknown


def compare_order_side_effect_snapshots(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail-closed compare with bounded redacted evidence."""
    before_rows = dict(before.get("rows") or {})
    after_rows = dict(after.get("rows") or {})
    before_agg = dict(before.get("aggregate") or {})
    after_agg = dict(after.get("aggregate") or {})

    blockers: list[str] = []
    critical_diffs: list[dict[str, Any]] = []
    concurrent_sync_drift: list[dict[str, Any]] = []

    if int(after_agg.get("count") or 0) > int(before_agg.get("count") or 0):
        blockers.append("order_row_count_increased")
    if int(after_agg.get("max_id") or 0) > int(before_agg.get("max_id") or 0):
        blockers.append("order_max_id_increased")

    new_rows = sorted(set(after_rows) - set(before_rows))
    if new_rows:
        blockers.append("order_row_created")
        for row_fp in new_rows[:MAX_CRITICAL_DIFF_ROWS]:
            critical_diffs.append(
                {
                    "row_fingerprint": row_fp,
                    "change_kind": "row_created",
                }
            )

    for row_fp, before_sig in before_rows.items():
        after_sig = after_rows.get(row_fp)
        if after_sig is None:
            blockers.append("order_row_removed")
            critical_diffs.append(
                {
                    "row_fingerprint": row_fp,
                    "change_kind": "row_removed",
                }
            )
            continue

        before_groups = dict(before_sig.get("critical_groups") or {})
        after_groups = dict(after_sig.get("critical_groups") or {})
        changed_groups = sorted(
            group
            for group in set(before_groups) | set(after_groups)
            if before_groups.get(group) != after_groups.get(group)
        )
        if not changed_groups:
            continue

        blockers.append("order_critical_field_changed")
        if len(critical_diffs) < MAX_CRITICAL_DIFF_ROWS:
            critical_diffs.append(
                {
                    "row_fingerprint": row_fp,
                    "change_kind": "critical_group_changed",
                    "critical_groups": changed_groups,
                    "before_group_hashes": {
                        group: before_groups.get(group) for group in changed_groups
                    },
                    "after_group_hashes": {
                        group: after_groups.get(group) for group in changed_groups
                    },
                }
            )

    ai_side_effect_detected = any(
        blocker
        in {
            "order_row_count_increased",
            "order_max_id_increased",
            "order_row_created",
            "order_row_removed",
            "order_critical_field_changed",
        }
        for blocker in blockers
    )
    return {
        "contract_version": ORDER_SIDE_EFFECT_CONTRACT_VERSION,
        "ai_side_effect_detected": ai_side_effect_detected,
        "blockers": sorted(set(blockers)),
        "critical_diffs": critical_diffs,
        "concurrent_sync_drift": concurrent_sync_drift,
        "aggregate": {
            "before": {
                "count": before_agg.get("count"),
                "max_id": before_agg.get("max_id"),
                "critical_content_hash": before_agg.get("critical_content_hash"),
            },
            "after": {
                "count": after_agg.get("count"),
                "max_id": after_agg.get("max_id"),
                "critical_content_hash": after_agg.get("critical_content_hash"),
            },
        },
    }


def compare_order_rows_with_metadata(
    before_rows: Sequence[Mapping[str, Any]],
    after_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Row-aware compare that can classify volatile-only metadata drift."""
    before_snapshot = build_order_side_effect_snapshot(before_rows)
    after_snapshot = build_order_side_effect_snapshot(after_rows)
    result = compare_order_side_effect_snapshots(before_snapshot, after_snapshot)

    before_by_fp = {
        redacted_order_row_key(row.get("id")): row for row in before_rows if row.get("id") is not None
    }
    after_by_fp = {
        redacted_order_row_key(row.get("id")): row for row in after_rows if row.get("id") is not None
    }

    refined_blockers = set(result["blockers"])
    refined_critical = [
        diff
        for diff in result["critical_diffs"]
        if diff.get("change_kind") != "critical_group_changed"
    ]
    before_volatile = extract_volatile_metadata_by_row(before_rows)
    after_volatile = extract_volatile_metadata_by_row(after_rows)
    refined_drift = detect_concurrent_sync_drift(
        armed_volatile_by_row=before_volatile,
        after_rows=after_rows,
    )

    for row_fp in sorted(set(before_by_fp) & set(after_by_fp)):
        before_meta = _metadata_mapping(before_by_fp[row_fp])
        after_meta = _metadata_mapping(after_by_fp[row_fp])
        unknown_changes = _unknown_metadata_changes(before_meta, after_meta)

        before_sig = before_snapshot["rows"][row_fp]
        after_sig = after_snapshot["rows"][row_fp]
        changed_groups = sorted(
            group
            for group in set(before_sig["critical_groups"]) | set(after_sig["critical_groups"])
            if before_sig["critical_groups"].get(group) != after_sig["critical_groups"].get(group)
        )

        volatile_item = _volatile_drift_item(
            row_fp,
            before_volatile.get(row_fp, {}),
            after_volatile.get(row_fp, {}),
        )
        if volatile_item and not changed_groups and not unknown_changes:
            continue

        if not changed_groups:
            continue

        if unknown_changes:
            refined_blockers.add("order_unknown_metadata_changed")
            if len(refined_critical) < MAX_CRITICAL_DIFF_ROWS:
                refined_critical.append(
                    {
                        "row_fingerprint": row_fp,
                        "change_kind": "unknown_metadata_changed",
                        "metadata_fields": unknown_changes,
                    }
                )
            continue

        refined_blockers.add("order_critical_field_changed")
        if len(refined_critical) < MAX_CRITICAL_DIFF_ROWS:
            refined_critical.append(
                {
                    "row_fingerprint": row_fp,
                    "change_kind": "critical_group_changed",
                    "critical_groups": changed_groups,
                    "before_group_hashes": {
                        group: before_sig["critical_groups"].get(group)
                        for group in changed_groups
                    },
                    "after_group_hashes": {
                        group: after_sig["critical_groups"].get(group)
                        for group in changed_groups
                    },
                }
            )

    ai_side_effect_detected = any(
        blocker
        in {
            "order_row_count_increased",
            "order_max_id_increased",
            "order_row_created",
            "order_row_removed",
            "order_critical_field_changed",
            "order_unknown_metadata_changed",
        }
        for blocker in refined_blockers
    )
    result["ai_side_effect_detected"] = ai_side_effect_detected
    result["blockers"] = sorted(refined_blockers)
    result["critical_diffs"] = refined_critical
    result["concurrent_sync_drift"] = refined_drift
    return result


_ORDER_SELECT_SQL = """
SELECT
    id,
    tenant_id,
    status,
    total,
    line_items,
    source,
    is_abandoned,
    external_id,
    external_order_number,
    customer_id,
    customer_name,
    customer_info,
    checkout_url,
    metadata,
    order_source_kind,
    identity_namespace,
    integration_connection_id,
    external_customer_ref,
    external_customer_profile_id,
    customer_link_state,
    customer_link_evidence_class,
    customer_link_source,
    customer_linked_at,
    external_identity_link_state,
    external_identity_evidence_class
FROM orders
WHERE tenant_id = :tenant_id
ORDER BY id ASC
"""


def _volatile_values_for_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: metadata[key]
        for key in ORDER_VOLATILE_METADATA_KEYS
        if key in metadata
    }


def extract_volatile_metadata_by_row(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    volatile_by_row: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("id") is None:
            continue
        row_fp = redacted_order_row_key(row.get("id"))
        volatile_by_row[row_fp] = _volatile_values_for_metadata(_metadata_mapping(row))
    return volatile_by_row


def _volatile_drift_item(
    row_fp: str,
    before_values: Mapping[str, Any],
    after_values: Mapping[str, Any],
) -> dict[str, Any] | None:
    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    for key in ORDER_VOLATILE_METADATA_KEYS:
        before_has = key in before_values
        after_has = key in after_values
        if before_has and after_has and before_values[key] != after_values[key]:
            changed.append(f"metadata.{key}")
        elif not before_has and after_has:
            added.append(f"metadata.{key}")
        elif before_has and not after_has:
            removed.append(f"metadata.{key}")
    volatile_fields = sorted(added + changed + removed)[:MAX_VOLATILE_FIELD_NAMES]
    if not volatile_fields:
        return None
    return {
        "row_fingerprint": row_fp,
        "volatile_fields": volatile_fields,
        "volatile_changes": {
            "added": added[:MAX_VOLATILE_FIELD_NAMES],
            "changed": changed[:MAX_VOLATILE_FIELD_NAMES],
            "removed": removed[:MAX_VOLATILE_FIELD_NAMES],
        },
        "drift_class": "volatile_sync_metadata",
        "actor_attribution": "unverified",
    }


def detect_concurrent_sync_drift(
    *,
    armed_volatile_by_row: Mapping[str, Mapping[str, Any]],
    after_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    after_volatile = extract_volatile_metadata_by_row(after_rows)
    row_fps = sorted(set(armed_volatile_by_row) | set(after_volatile))
    drift: list[dict[str, Any]] = []
    for row_fp in row_fps:
        if len(drift) >= MAX_CONCURRENT_SYNC_DRIFT_ROWS:
            break
        item = _volatile_drift_item(
            row_fp,
            dict(armed_volatile_by_row.get(row_fp) or {}),
            dict(after_volatile.get(row_fp) or {}),
        )
        if item is not None:
            drift.append(item)
    return drift


def extract_metadata_keys_by_row(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    keys_by_row: dict[str, list[str]] = {}
    for row in rows:
        if row.get("id") is None:
            continue
        row_fp = redacted_order_row_key(row.get("id"))
        keys_by_row[row_fp] = sorted(_metadata_mapping(row))
    return keys_by_row


def fetch_tenant_order_rows(connection: Any, *, tenant_id: int) -> list[dict[str, Any]]:
    from sqlalchemy import text

    return [
        dict(row)
        for row in connection.execute(
            text(_ORDER_SELECT_SQL),
            {"tenant_id": tenant_id},
        ).mappings()
    ]


def capture_order_side_effect_arm(connection: Any, *, tenant_id: int) -> dict[str, Any]:
    rows = fetch_tenant_order_rows(connection, tenant_id=tenant_id)
    return {
        "snapshot": build_order_side_effect_snapshot(rows, tenant_id=tenant_id),
        "volatile_metadata_by_row": extract_volatile_metadata_by_row(rows),
        "metadata_keys_by_row": extract_metadata_keys_by_row(rows),
    }


def capture_order_side_effect_snapshot(connection: Any, *, tenant_id: int) -> dict[str, Any]:
    return capture_order_side_effect_arm(connection, tenant_id=tenant_id)["snapshot"]


def evaluate_order_side_effect_gate(
    *,
    armed: Mapping[str, Any],
    after_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate post-message order side effects against an armed snapshot."""
    armed_snapshot = dict(armed.get("snapshot") or {})
    armed_volatile = dict(armed.get("volatile_metadata_by_row") or {})
    armed_metadata_keys = dict(armed.get("metadata_keys_by_row") or {})

    after_snapshot = build_order_side_effect_snapshot(after_rows)
    result = compare_order_side_effect_snapshots(armed_snapshot, after_snapshot)

    after_by_fp = {
        redacted_order_row_key(row.get("id")): row for row in after_rows if row.get("id") is not None
    }
    refined_blockers = set(result["blockers"])
    refined_critical = list(result["critical_diffs"])

    for row_fp, after_row in after_by_fp.items():
        after_meta = _metadata_mapping(after_row)
        known_keys = set(armed_metadata_keys.get(row_fp) or [])
        new_keys = sorted(
            key for key in after_meta if key not in known_keys and key not in ORDER_VOLATILE_METADATA_KEYS
        )
        if new_keys:
            refined_blockers.add("order_unknown_metadata_changed")
            if len(refined_critical) < MAX_CRITICAL_DIFF_ROWS:
                refined_critical.append(
                    {
                        "row_fingerprint": row_fp,
                        "change_kind": "unknown_metadata_added",
                        "metadata_fields": [
                            f"metadata.{key}" for key in new_keys[:MAX_UNKNOWN_METADATA_KEYS]
                        ],
                    }
                )

    concurrent_sync_drift = detect_concurrent_sync_drift(
        armed_volatile_by_row=armed_volatile,
        after_rows=after_rows,
    )
    result["concurrent_sync_drift"] = concurrent_sync_drift
    result["blockers"] = sorted(refined_blockers)
    result["critical_diffs"] = refined_critical
    result["ai_side_effect_detected"] = bool(refined_blockers)
    return result

