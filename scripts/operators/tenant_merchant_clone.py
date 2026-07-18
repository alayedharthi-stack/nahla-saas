#!/usr/bin/env python3
"""Selective merchant-plane tenant clone operator (Tenant 33 acceptance).

Default: dry-run with sanitized counts/checksums only — no PII or content values.
Apply requires archived dry-run digest, exact Alembic heads, and confirmation token.
Cleanup deletes only rows recorded in a prior clone manifest.

Never executes against production source without an additional exact confirmation
token; production execution remains blocked pending separate owner approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators import staging_migration_operator_gates as gates  # noqa: E402
from scripts.operators.tenant_merchant_clone_contract import (  # noqa: E402
    ALLOWED_TABLE_SPECS,
    ALLOWED_TABLE_NAMES,
    APPLY_CONFIRM_ENV,
    APPLY_CONFIRM_TOKEN,
    CLEANUP_CONFIRM_ENV,
    CLEANUP_CONFIRM_TOKEN,
    DENIED_TABLES,
    DRY_RUN_DIGEST_ENV,
    DRY_RUN_DIGEST_SCHEMA_VERSION,
    EXPECTED_ALEMBIC_HEADS,
    GLOBAL_STRIP_COLUMNS,
    MANIFEST_SCHEMA_VERSION,
    MASTER_ENABLE_ENV,
    PHONE_SCRUB_PLACEHOLDER,
    PRODUCTION_ENVIRONMENT_VALUE,
    PRODUCTION_IDENTITY_CLASS,
    PRODUCTION_SOURCE_CONFIRM_ENV,
    PRODUCTION_SOURCE_CONFIRM_TOKEN,
    RESET_COUNT_COLUMNS,
    SOURCE_DATABASE_URL_ENV,
    SOURCE_ENVIRONMENT_ENV,
    SOURCE_PROJECT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_VALUE,
    TARGET_ALLOWED_ENVIRONMENT_VALUES,
    TARGET_DATABASE_URL_ENV,
    TARGET_ENVIRONMENT_ENV,
    TARGET_PROJECT_ENV,
    TARGET_TEST_SLUG_MARKERS,
    TENANT_COPY_COLUMNS,
    TENANT_DENIED_COLUMNS,
)
from scripts.operators.tenant_merchant_clone_scrubber import (  # noqa: E402
    scrub_row_json_columns,
    scan_for_unhandled_forbidden_keys,
)

GateFailure = gates.GateFailure


@dataclass(frozen=True)
class CloneRequest:
    source_tenant_id: int
    target_tenant_id: int
    source_database_url: str
    target_database_url: str
    mode: str
    clone_id: str
    dry_run_digest: str | None
    manifest_path: Path | None
    env: Mapping[str, str]


def truthy_env(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes")


def emit(payload: Mapping[str, Any]) -> int:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))
    return 0


def emit_failure(*, error_class: str, stage: str) -> int:
    return emit({"outcome": "failed", "error_class": error_class, "stage": stage})


def _parse_database_url(raw_url: str) -> Any:
    return make_url(raw_url)


def validate_database_url_scheme(raw_url: str, *, stage: str) -> GateFailure | None:
    if not raw_url.strip():
        return GateFailure("database_binding_rejected", f"{stage}_database_url_missing")
    try:
        parsed = _parse_database_url(raw_url)
    except (ArgumentError, ValueError, TypeError):
        return GateFailure("database_binding_rejected", f"{stage}_database_url_malformed")
    if parsed.drivername not in gates._POSTGRES_SCHEMES:
        return GateFailure("database_binding_rejected", f"{stage}_database_scheme_rejected")
    host = (parsed.host or "").lower()
    if not host:
        return GateFailure("database_binding_rejected", f"{stage}_database_host_missing")
    if any(marker in host for marker in gates._FORBIDDEN_ENV_MARKERS):
        return GateFailure("database_binding_rejected", f"{stage}_database_host_production_marker")
    return None


def validate_source_target_distinct(request: CloneRequest) -> GateFailure | None:
    if request.source_tenant_id == request.target_tenant_id:
        return GateFailure("identity_rejected", "source_equals_target_tenant")
    if request.source_database_url.strip() == request.target_database_url.strip():
        return GateFailure("identity_rejected", "source_equals_target_database")
    return None


def validate_target_staging_identity(env: Mapping[str, str]) -> GateFailure | None:
    project = (env.get(TARGET_PROJECT_ENV) or "").strip()
    environment = (env.get(TARGET_ENVIRONMENT_ENV) or "").strip().lower()
    if not project:
        return GateFailure("identity_rejected", "target_project_missing")
    if project != STAGING_PROJECT_VALUE:
        return GateFailure("identity_rejected", "target_project_mismatch")
    if environment not in TARGET_ALLOWED_ENVIRONMENT_VALUES:
        return GateFailure("identity_rejected", "target_environment_not_experimental_staging")
    for marker in gates._FORBIDDEN_ENV_MARKERS:
        if marker in environment:
            return GateFailure("identity_rejected", "target_production_marker_detected")
    return None


def classify_source_identity(env: Mapping[str, str]) -> tuple[str, GateFailure | None]:
    project = (env.get(SOURCE_PROJECT_ENV) or "").strip()
    environment = (env.get(SOURCE_ENVIRONMENT_ENV) or "").strip().lower()
    if not project or not environment:
        return "", GateFailure("identity_rejected", "source_identity_incomplete")
    if project != STAGING_PROJECT_VALUE and project != "desirable-growth":
        return "", GateFailure("identity_rejected", "source_project_not_allowlisted")
    if environment == STAGING_ENVIRONMENT_VALUE:
        return STAGING_IDENTITY_CLASS, None
    if environment == PRODUCTION_ENVIRONMENT_VALUE:
        return PRODUCTION_IDENTITY_CLASS, None
    return "", GateFailure("identity_rejected", "source_environment_not_allowlisted")


def validate_production_source_gate(env: Mapping[str, str], source_class: str) -> GateFailure | None:
    if source_class != PRODUCTION_IDENTITY_CLASS:
        return None
    token = (env.get(PRODUCTION_SOURCE_CONFIRM_ENV) or "").strip()
    if token != PRODUCTION_SOURCE_CONFIRM_TOKEN:
        return GateFailure("confirmation_missing", "production_source_not_confirmed")
    return None


def validate_master_enable(env: Mapping[str, str], *, mode: str) -> GateFailure | None:
    if mode == "dry-run":
        return None
    if not truthy_env(env, MASTER_ENABLE_ENV):
        return GateFailure("execution_disabled", "master_enable_missing")
    return None


def validate_apply_confirmation(env: Mapping[str, str], *, mode: str) -> GateFailure | None:
    if mode != "apply":
        return None
    return gates.validate_confirmation(
        env,
        confirmation_env=APPLY_CONFIRM_ENV,
        confirmation_token=APPLY_CONFIRM_TOKEN,
    )


def validate_cleanup_confirmation(env: Mapping[str, str], *, mode: str) -> GateFailure | None:
    if mode != "cleanup":
        return None
    return gates.validate_confirmation(
        env,
        confirmation_env=CLEANUP_CONFIRM_ENV,
        confirmation_token=CLEANUP_CONFIRM_TOKEN,
    )


def validate_target_database_host(env: Mapping[str, str], target_url: str) -> GateFailure | None:
    failure = validate_database_url_scheme(target_url, stage="target")
    if failure:
        return failure
    host = (_parse_database_url(target_url).host or "").lower()
    if host != gates._ALLOWED_STAGING_DATABASE_HOST:
        return GateFailure("database_binding_rejected", "target_database_host_not_experimental_staging")
    return None


def validate_alembic_heads(conn: Connection) -> GateFailure | None:
    revisions = gates.read_alembic_revisions(conn)
    if not revisions:
        return GateFailure("wrong_revision", "alembic_version_missing")
    if revisions != EXPECTED_ALEMBIC_HEADS:
        return GateFailure("wrong_revision", "alembic_heads_mismatch_or_multi_head_drift")
    return None


def validate_target_test_markers(conn: Connection, target_tenant_id: int) -> GateFailure | None:
    row = conn.execute(
        text("SELECT name, domain FROM tenants WHERE id = :tid"),
        {"tid": target_tenant_id},
    ).mappings().first()
    if row is None:
        return GateFailure("preflight_failed", "target_tenant_missing")
    haystack = f"{row['name'] or ''} {row['domain'] or ''}".lower()
    if not any(marker in haystack for marker in TARGET_TEST_SLUG_MARKERS):
        return GateFailure("preflight_failed", "target_tenant_not_test_marked")
    return None


def validate_source_tenant_exists(conn: Connection, source_tenant_id: int) -> GateFailure | None:
    exists = conn.execute(
        text("SELECT 1 FROM tenants WHERE id = :tid"),
        {"tid": source_tenant_id},
    ).scalar()
    if not exists:
        return GateFailure("preflight_failed", "source_tenant_missing")
    return None


def connect_engine(url: str, *, read_only: bool = False) -> Engine:
    engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
    if read_only:
        with engine.connect() as conn:
            conn.execute(text("SET default_transaction_read_only = on"))
            conn.commit()
    return engine


def table_count_checksum(conn: Connection, table: str, tenant_id: int) -> str:
    count = conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).scalar_one()
    return hashlib.sha256(f"{table}:{tenant_id}:{count}".encode()).hexdigest()[:16]


def denied_domain_zero_proof(conn: Connection, tenant_id: int) -> dict[str, int]:
    proof: dict[str, int] = {}
    for table in sorted(DENIED_TABLES):
        if table not in inspect(conn).get_table_names():
            continue
        cols = {c["name"] for c in inspect(conn).get_columns(table)}
        if "tenant_id" not in cols:
            continue
        proof[table] = int(
            conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            ).scalar_one()
        )
    return proof


def _reflect_table(conn: Connection, name: str) -> Table:
    metadata = MetaData()
    return Table(name, metadata, autoload_with=conn)


def _allocate_new_id(conn: Connection, table: str) -> int:
    current_max = conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")).scalar_one()
    return int(current_max) + 1


def _copy_tenant_scalars(
    source_conn: Connection,
    target_conn: Connection,
    *,
    source_tenant_id: int,
    target_tenant_id: int,
) -> list[str]:
    row = source_conn.execute(
        text(
            "SELECT "
            + ", ".join(TENANT_COPY_COLUMNS)
            + " FROM tenants WHERE id = :tid"
        ),
        {"tid": source_tenant_id},
    ).mappings().one()
    assignments = ", ".join(f"{col} = :{col}" for col in TENANT_COPY_COLUMNS)
    params = {col: row[col] for col in TENANT_COPY_COLUMNS}
    params["tid"] = target_tenant_id
    target_conn.execute(text(f"UPDATE tenants SET {assignments} WHERE id = :tid"), params)
    return [f"tenant_scalars:{col}" for col in TENANT_COPY_COLUMNS]


def _transform_row(
    row: Mapping[str, Any],
    *,
    spec_name: str,
    spec_json_columns: Sequence[str],
    target_tenant_id: int,
    id_maps: dict[str, dict[int, int]],
    remap_fk_columns: Sequence[str],
    scrub_phone_columns: Sequence[str],
    deferred_fk_columns: Sequence[str],
) -> tuple[dict[str, Any], list[str]]:
    out = dict(row)
    transformations: list[str] = []

    if "tenant_id" in out:
        out["tenant_id"] = target_tenant_id

    if "id" in out:
        del out["id"]

    for column in GLOBAL_STRIP_COLUMNS:
        if column in out:
            out[column] = None
            transformations.append(f"strip_global:{spec_name}.{column}")

    for column in RESET_COUNT_COLUMNS:
        if column in out:
            out[column] = 0
            transformations.append(f"reset_count:{spec_name}.{column}")

    for column in scrub_phone_columns:
        if column in out and out[column]:
            out[column] = PHONE_SCRUB_PLACEHOLDER
            transformations.append(f"scrub_phone:{spec_name}.{column}")

    for column in remap_fk_columns:
        if column not in out or out[column] is None:
            continue
        parent_table = {
            "product_id": "products",
            "group_id": "product_groups",
            "variant_id": "product_variants",
            "source_product_id": "products",
            "target_product_id": "products",
            "section_id": "merchant_knowledge_sections",
            "media_id": "ai_media_library",
            "coupon_id": "coupons",
            "template_id": "whatsapp_templates",
            "branch_id": "merchant_branches",
            "contact_id": "branch_contacts",
        }.get(column)
        if not parent_table:
            raise ValueError(f"unknown_fk_remap:{spec_name}.{column}")
        old_id = int(out[column])
        out[column] = id_maps[parent_table][old_id]
        transformations.append(f"remap_fk:{spec_name}.{column}")

    for column in deferred_fk_columns:
        if column in out:
            out[column] = None

    if spec_json_columns:
        out, json_transforms = scrub_row_json_columns(out, spec_json_columns, table=spec_name)
        transformations.extend(json_transforms)

    for column in spec_json_columns:
        if column in row and row[column] is not None:
            violations = scan_for_unhandled_forbidden_keys(row[column])
            if violations:
                raise ValueError(f"forbidden_json_keys:{spec_name}.{column}")

    return out, transformations


def _insert_row(conn: Connection, table: str, row: Mapping[str, Any]) -> int:
    columns = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    sql = text(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id"
    )
    return int(conn.execute(sql, dict(row)).scalar_one())


def _delete_target_scope(conn: Connection, target_tenant_id: int) -> None:
    for spec in reversed(ALLOWED_TABLE_SPECS):
        if spec.name == "tenant_settings":
            continue
        conn.execute(
            text(f"DELETE FROM {spec.name} WHERE tenant_id = :tid"),
            {"tid": target_tenant_id},
        )


def build_plan(request: CloneRequest) -> dict[str, Any]:
    source_engine = connect_engine(request.source_database_url, read_only=True)
    target_engine = connect_engine(request.target_database_url)
    try:
        with source_engine.connect() as source_conn, target_engine.connect() as target_conn:
            for conn, label in ((source_conn, "source"), (target_conn, "target")):
                failure = validate_alembic_heads(conn)
                if failure:
                    raise ValueError(f"{label}:{failure.stage}")

            for validator, args in (
                (validate_source_tenant_exists, (source_conn, request.source_tenant_id)),
                (validate_target_test_markers, (target_conn, request.target_tenant_id)),
            ):
                failure = validator(*args)
                if failure:
                    raise ValueError(failure.stage)

            table_counts: dict[str, int] = {}
            dependency_order = [spec.name for spec in ALLOWED_TABLE_SPECS]
            transformations: list[str] = ["tenant_scalars"]

            for spec in ALLOWED_TABLE_SPECS:
                count = source_conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM {spec.name} "
                        f"WHERE {spec.tenant_column} = :tid"
                    ),
                    {"tid": request.source_tenant_id},
                ).scalar_one()
                table_counts[spec.name] = int(count)

            source_checksums = {
                table: table_count_checksum(source_conn, table, request.source_tenant_id)
                for table in ALLOWED_TABLE_NAMES
                if table != "tenant_settings"
            }
            target_before = {
                table: table_count_checksum(target_conn, table, request.target_tenant_id)
                for table in ALLOWED_TABLE_NAMES
                if table != "tenant_settings"
            }
            denied_proof = denied_domain_zero_proof(source_conn, request.source_tenant_id)

            digest_payload = {
                "schema_version": DRY_RUN_DIGEST_SCHEMA_VERSION,
                "source_tenant_id": request.source_tenant_id,
                "target_tenant_id": request.target_tenant_id,
                "table_counts": table_counts,
                "source_checksums": source_checksums,
                "dependency_order": dependency_order,
                "denied_domain_source_counts": denied_proof,
                "alembic_heads": sorted(EXPECTED_ALEMBIC_HEADS),
            }
            digest = hashlib.sha256(
                json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()

            return {
                "outcome": "planned",
                "mode": request.mode,
                "clone_id": request.clone_id,
                "schema_version": DRY_RUN_DIGEST_SCHEMA_VERSION,
                "source_tenant_id": request.source_tenant_id,
                "target_tenant_id": request.target_tenant_id,
                "dependency_order": dependency_order,
                "table_counts": table_counts,
                "source_checksums": source_checksums,
                "target_checksums_before": target_before,
                "denied_domain_source_counts": denied_proof,
                "transformations": transformations,
                "dry_run_digest": digest,
                "alembic_heads": sorted(EXPECTED_ALEMBIC_HEADS),
            }
    finally:
        source_engine.dispose()
        target_engine.dispose()


def apply_clone(request: CloneRequest) -> dict[str, Any]:
    if not request.dry_run_digest:
        raise ValueError("dry_run_digest_missing")

    source_engine = connect_engine(request.source_database_url, read_only=True)
    target_engine = connect_engine(request.target_database_url)
    id_maps: dict[str, dict[int, int]] = {spec.name: {} for spec in ALLOWED_TABLE_SPECS}
    id_maps["products"] = {}
    id_maps["product_variants"] = {}
    id_maps["product_groups"] = {}
    id_maps["merchant_knowledge_sections"] = {}
    id_maps["ai_media_library"] = {}
    id_maps["coupons"] = {}
    id_maps["whatsapp_templates"] = {}
    id_maps["merchant_branches"] = {}
    id_maps["branch_contacts"] = {}
    manifest_rows: dict[str, list[int]] = {}
    transformations: list[str] = []
    unrelated_before: dict[str, str] = {}

    try:
        with source_engine.connect() as source_conn:
            plan = build_plan(request)
            if plan["dry_run_digest"] != request.dry_run_digest:
                raise ValueError("dry_run_digest_mismatch")

            unrelated_raw = (request.env.get("NAHLA_CLONE_UNRELATED_TENANT_ID") or "").strip()
            unrelated_tenant_id = int(unrelated_raw) if unrelated_raw else None
            if unrelated_tenant_id is not None:
                with target_engine.connect() as target_conn:
                    for table in ALLOWED_TABLE_NAMES:
                        if table in inspect(target_conn).get_table_names():
                            unrelated_before[table] = table_count_checksum(
                                target_conn, table, unrelated_tenant_id
                            )

            with target_engine.begin() as target_conn:
                transformations.extend(
                    _copy_tenant_scalars(
                        source_conn,
                        target_conn,
                        source_tenant_id=request.source_tenant_id,
                        target_tenant_id=request.target_tenant_id,
                    )
                )
                _delete_target_scope(target_conn, request.target_tenant_id)

                for spec in ALLOWED_TABLE_SPECS:
                    rows = source_conn.execute(
                        text(
                            f"SELECT * FROM {spec.name} "
                            f"WHERE {spec.tenant_column} = :tid ORDER BY id"
                        ),
                        {"tid": request.source_tenant_id},
                    ).mappings().all()
                    manifest_rows[spec.name] = []
                    for row in rows:
                        transformed, row_transforms = _transform_row(
                            row,
                            spec_name=spec.name,
                            spec_json_columns=spec.json_columns,
                            target_tenant_id=request.target_tenant_id,
                            id_maps=id_maps,
                            remap_fk_columns=spec.remap_fk_columns,
                            scrub_phone_columns=spec.scrub_phone_columns,
                            deferred_fk_columns=spec.deferred_fk_columns,
                        )
                        transformations.extend(row_transforms)
                        if spec.upsert_on_tenant:
                            existing = target_conn.execute(
                                text(
                                    f"SELECT id FROM {spec.name} "
                                    f"WHERE tenant_id = :tid LIMIT 1"
                                ),
                                {"tid": request.target_tenant_id},
                            ).scalar()
                            if existing:
                                set_clause = ", ".join(
                                    f"{col} = :{col}"
                                    for col in transformed
                                    if col not in {"tenant_id"}
                                )
                                params = dict(transformed)
                                params["tid"] = request.target_tenant_id
                                target_conn.execute(
                                    text(
                                        f"UPDATE {spec.name} SET {set_clause} "
                                        f"WHERE tenant_id = :tid"
                                    ),
                                    params,
                                )
                                new_id = int(existing)
                            else:
                                new_id = _insert_row(target_conn, spec.name, transformed)
                        else:
                            old_id = int(row["id"])
                            new_id = _allocate_new_id(target_conn, spec.name)
                            transformed["id"] = new_id
                            _insert_row(target_conn, spec.name, transformed)
                            id_maps.setdefault(spec.name, {})[old_id] = new_id
                        manifest_rows[spec.name].append(new_id)

                # Backfill products.default_variant_id after variants exist.
                product_rows = source_conn.execute(
                    text(
                        "SELECT id, default_variant_id FROM products "
                        "WHERE tenant_id = :tid AND default_variant_id IS NOT NULL"
                    ),
                    {"tid": request.source_tenant_id},
                ).mappings().all()
                for product_row in product_rows:
                    old_product_id = int(product_row["id"])
                    old_variant_id = int(product_row["default_variant_id"])
                    target_conn.execute(
                        text(
                            "UPDATE products SET default_variant_id = :variant_id "
                            "WHERE id = :product_id AND tenant_id = :tid"
                        ),
                        {
                            "variant_id": id_maps["product_variants"][old_variant_id],
                            "product_id": id_maps["products"][old_product_id],
                            "tid": request.target_tenant_id,
                        },
                    )
                    transformations.append("remap_fk:products.default_variant_id")

                if unrelated_tenant_id is not None:
                    for table, checksum in unrelated_before.items():
                        after = table_count_checksum(target_conn, table, unrelated_tenant_id)
                        if after != checksum:
                            raise ValueError(f"unrelated_tenant_checksum_changed:{table}")

        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "clone_id": request.clone_id,
            "source_tenant_id": request.source_tenant_id,
            "target_tenant_id": request.target_tenant_id,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "dry_run_digest": request.dry_run_digest,
            "alembic_heads": sorted(EXPECTED_ALEMBIC_HEADS),
            "manifest_rows": manifest_rows,
            "transformations": sorted(set(transformations)),
        }
        if request.manifest_path:
            request.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            request.manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        return {"outcome": "applied", **manifest}
    finally:
        source_engine.dispose()
        target_engine.dispose()


def cleanup_clone(request: CloneRequest) -> dict[str, Any]:
    if not request.manifest_path or not request.manifest_path.is_file():
        raise ValueError("manifest_missing")
    manifest = json.loads(request.manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest_schema_mismatch")
    if manifest.get("clone_id") != request.clone_id:
        raise ValueError("clone_id_mismatch")

    target_engine = connect_engine(request.target_database_url)
    deleted: dict[str, int] = {}
    try:
        with target_engine.begin() as conn:
            for spec in reversed(ALLOWED_TABLE_SPECS):
                ids = manifest.get("manifest_rows", {}).get(spec.name) or []
                if not ids:
                    continue
                result = conn.execute(
                    text(f"DELETE FROM {spec.name} WHERE id = ANY(:ids)"),
                    {"ids": ids},
                )
                deleted[spec.name] = int(result.rowcount or 0)
        return {
            "outcome": "cleaned",
            "clone_id": request.clone_id,
            "deleted_counts": deleted,
        }
    finally:
        target_engine.dispose()


def run_gates(request: CloneRequest) -> GateFailure | None:
    for validator in (
        lambda: validate_source_target_distinct(request),
        lambda: validate_target_staging_identity(request.env),
        lambda: validate_target_database_host(request.env, request.target_database_url),
        lambda: validate_database_url_scheme(request.source_database_url, stage="source"),
        lambda: validate_master_enable(request.env, mode=request.mode),
        lambda: validate_apply_confirmation(request.env, mode=request.mode),
        lambda: validate_cleanup_confirmation(request.env, mode=request.mode),
    ):
        failure = validator()
        if failure:
            return failure

    source_class, failure = classify_source_identity(request.env)
    if failure:
        return failure
    failure = validate_production_source_gate(request.env, source_class)
    if failure:
        return failure
    return None


def build_request_from_env(
    *,
    mode: str,
    source_tenant_id: int,
    target_tenant_id: int,
    clone_id: str | None,
    dry_run_digest: str | None,
    manifest_path: Path | None,
    env: Mapping[str, str] | None = None,
) -> CloneRequest:
    env_map = dict(env or os.environ)
    source_url = (env_map.get(SOURCE_DATABASE_URL_ENV) or "").strip()
    target_url = (env_map.get(TARGET_DATABASE_URL_ENV) or "").strip()
    return CloneRequest(
        source_tenant_id=source_tenant_id,
        target_tenant_id=target_tenant_id,
        source_database_url=source_url,
        target_database_url=target_url,
        mode=mode,
        clone_id=clone_id or str(uuid.uuid4()),
        dry_run_digest=dry_run_digest or (env_map.get(DRY_RUN_DIGEST_ENV) or "").strip() or None,
        manifest_path=manifest_path,
        env=env_map,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merchant-plane tenant clone operator")
    parser.add_argument("command", choices=["dry-run", "apply", "cleanup"])
    parser.add_argument("--source-tenant-id", type=int, required=True)
    parser.add_argument("--target-tenant-id", type=int, required=True)
    parser.add_argument("--clone-id", default="")
    parser.add_argument("--dry-run-digest", default="")
    parser.add_argument("--manifest-path", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    mode = {"dry-run": "dry-run", "apply": "apply", "cleanup": "cleanup"}[args.command]
    request = build_request_from_env(
        mode=mode,
        source_tenant_id=args.source_tenant_id,
        target_tenant_id=args.target_tenant_id,
        clone_id=args.clone_id or None,
        dry_run_digest=args.dry_run_digest or None,
        manifest_path=Path(args.manifest_path) if args.manifest_path else None,
    )

    failure = run_gates(request)
    if failure:
        return emit_failure(error_class=failure.error_class, stage=failure.stage)

    try:
        if mode == "dry-run":
            return emit(build_plan(request))
        if mode == "apply":
            return emit(apply_clone(request))
        return emit(cleanup_clone(request))
    except ValueError as exc:
        return emit_failure(error_class="operator_rejected", stage=str(exc))
    except SQLAlchemyError:
        return emit_failure(error_class="database_error", stage="sqlalchemy")


if __name__ == "__main__":
    raise SystemExit(main())
