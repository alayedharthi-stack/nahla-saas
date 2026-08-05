"""Read-only Knowledge Base source provenance operator.

Prove tenant KB data provenance from the database without browser sessions,
DevTools, or outbound HTTP. SELECT-only — no writes, commits, or migrations.

Run in production (example)::

    railway run -e production -s Postgres -- \\
        python -m scripts.operators.knowledge_source_provenance tenant 1

Local::

    python -m scripts.operators.knowledge_source_provenance --help
    python -m scripts.operators.knowledge_source_provenance tenant <TENANT_ID>
    python -m scripts.operators.knowledge_source_provenance inventory
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators.knowledge_source_provenance_contract import (  # noqa: E402
    API_SURFACE_MAP,
    CODE_COMMAND_INVALID,
    CODE_DATABASE_ERROR,
    CODE_DATABASE_URL_MISSING,
    CODE_TENANT_NOT_FOUND,
    DIAGNOSTIC_GAP_NOTE,
    MERCHANT_CONTEXT_POLICY_KEYS,
    REPORT_SCHEMA_VERSION,
    REQUIRED_TENANT_REPORT_KEYS,
    compute_divergence,
    snapshot_has_nonempty_policy_or_shipping,
    store_settings_field_lengths,
    store_settings_has_policy_or_faq,
    text_length,
)


def emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit_error(code: str, *, detail: str | None = None) -> int:
    body: dict[str, Any] = {
        "ok": False,
        "code": code,
        "report_schema_version": REPORT_SCHEMA_VERSION,
    }
    if detail:
        body["detail"] = detail
    emit(body)
    return 1


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    """Prefer public URL when both are set.

    ``railway run -s Postgres`` injects private ``DATABASE_URL`` (``*.railway.internal``)
    and ``DATABASE_PUBLIC_URL``. Operator laptops cannot resolve the private host, so
    public must win for reproducible local diagnostics.
    """
    env = env or os.environ
    for key in ("DATABASE_PUBLIC_URL", "DATABASE_URL"):
        url = (env.get(key) or "").strip()
        if url:
            return url
    raise ValueError(CODE_DATABASE_URL_MISSING)


def create_readonly_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        poolclass=NullPool,
        pool_pre_ping=True,
    )


def _table_exists(conn: Connection, table_name: str) -> bool:
    return table_name in inspect(conn).get_table_names()


def _scalar_count(conn: Connection, sql: str, params: Mapping[str, Any] | None = None) -> int:
    value = conn.execute(text(sql), params or {}).scalar_one()
    return int(value or 0)


def _fetch_tenant_row(conn: Connection, tenant_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT id, name FROM tenants WHERE id = :tenant_id"),
        {"tenant_id": tenant_id},
    ).mappings().first()
    return dict(row) if row else None


def _coerce_json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _fetch_settings_row(conn: Connection, tenant_id: int) -> dict[str, Any]:
    if not _table_exists(conn, "tenant_settings"):
        return {}
    row = conn.execute(
        text(
            """
            SELECT ai_settings, store_settings
            FROM tenant_settings
            WHERE tenant_id = :tenant_id
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().first()
    return dict(row) if row else {}


def _fetch_snapshot_row(conn: Connection, tenant_id: int) -> dict[str, Any] | None:
    if not _table_exists(conn, "store_knowledge_snapshots"):
        return None
    row = conn.execute(
        text(
            """
            SELECT
                tenant_id,
                policy_summary,
                shipping_summary,
                last_full_sync_at
            FROM store_knowledge_snapshots
            WHERE tenant_id = :tenant_id
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().first()
    return dict(row) if row else None


def collect_section_counts(conn: Connection, tenant_id: int) -> dict[str, int]:
    if not _table_exists(conn, "merchant_knowledge_sections"):
        return {"total": 0, "active": 0, "deleted": 0}
    return {
        "total": _scalar_count(
            conn,
            "SELECT count(*) FROM merchant_knowledge_sections WHERE tenant_id = :tenant_id",
            {"tenant_id": tenant_id},
        ),
        "active": _scalar_count(
            conn,
            """
            SELECT count(*)
            FROM merchant_knowledge_sections
            WHERE tenant_id = :tenant_id
              AND is_active = true
              AND deleted_at IS NULL
            """,
            {"tenant_id": tenant_id},
        ),
        "deleted": _scalar_count(
            conn,
            """
            SELECT count(*)
            FROM merchant_knowledge_sections
            WHERE tenant_id = :tenant_id
              AND deleted_at IS NOT NULL
            """,
            {"tenant_id": tenant_id},
        ),
    }


def collect_draft_count(conn: Connection, tenant_id: int) -> int:
    if not _table_exists(conn, "merchant_knowledge_drafts"):
        return 0
    return _scalar_count(
        conn,
        "SELECT count(*) FROM merchant_knowledge_drafts WHERE tenant_id = :tenant_id",
        {"tenant_id": tenant_id},
    )


def collect_branch_counts(conn: Connection, tenant_id: int) -> dict[str, int]:
    if not _table_exists(conn, "merchant_branches"):
        return {"merchant_branches": 0, "branch_contacts": 0, "branch_escalation_steps": 0}
    branches = _scalar_count(
        conn,
        "SELECT count(*) FROM merchant_branches WHERE tenant_id = :tenant_id",
        {"tenant_id": tenant_id},
    )
    contacts = 0
    steps = 0
    if _table_exists(conn, "branch_contacts"):
        contacts = _scalar_count(
            conn,
            """
            SELECT count(*)
            FROM branch_contacts bc
            JOIN merchant_branches mb ON mb.id = bc.branch_id
            WHERE mb.tenant_id = :tenant_id
            """,
            {"tenant_id": tenant_id},
        )
    if _table_exists(conn, "branch_escalation_steps"):
        steps = _scalar_count(
            conn,
            """
            SELECT count(*)
            FROM branch_escalation_steps bes
            JOIN merchant_branches mb ON mb.id = bes.branch_id
            WHERE mb.tenant_id = :tenant_id
            """,
            {"tenant_id": tenant_id},
        )
    return {
        "merchant_branches": branches,
        "branch_contacts": contacts,
        "branch_escalation_steps": steps,
    }


def collect_sample_sections(conn: Connection, tenant_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
    if not _table_exists(conn, "merchant_knowledge_sections"):
        return []
    rows = conn.execute(
        text(
            """
            SELECT id, kind, title, length(coalesce(body, '')) AS body_len
            FROM merchant_knowledge_sections
            WHERE tenant_id = :tenant_id
              AND deleted_at IS NULL
            ORDER BY updated_at DESC NULLS LAST, id DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "limit": limit},
    ).mappings().all()
    return [
        {
            "id": int(row["id"]),
            "kind": row.get("kind"),
            "title": row.get("title"),
            "body_len": int(row.get("body_len") or 0),
        }
        for row in rows
    ]


def probe_structured_facts(db: Session, tenant_id: int) -> dict[str, Any]:
    try:
        from modules.ai.prompts.tenant_overlay import build_structured_facts_block  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"importable": False, "empty": True, "length": 0, "error_class": type(exc).__name__}
    try:
        block = build_structured_facts_block(db, tenant_id) or ""
    except Exception as exc:  # noqa: BLE001
        return {
            "importable": True,
            "empty": True,
            "length": 0,
            "error_class": type(exc).__name__,
        }
    return {
        "importable": True,
        "empty": len(block.strip()) == 0,
        "length": len(block),
    }


def probe_merchant_context(db: Session, tenant_id: int) -> dict[str, Any]:
    try:
        from core.store_knowledge import build_merchant_context  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {
            "importable": False,
            "policy_presence": {key: False for key in MERCHANT_CONTEXT_POLICY_KEYS},
            "error_class": type(exc).__name__,
        }
    try:
        context = build_merchant_context(db, tenant_id=tenant_id, product_limit=1) or {}
    except Exception as exc:  # noqa: BLE001
        return {
            "importable": True,
            "policy_presence": {key: False for key in MERCHANT_CONTEXT_POLICY_KEYS},
            "error_class": type(exc).__name__,
        }
    presence = dict(context.get("policy_presence") or {})
    return {
        "importable": True,
        "policy_presence": {
            key: bool(presence.get(key))
            for key in MERCHANT_CONTEXT_POLICY_KEYS
        },
    }


def build_tenant_report(conn: Connection, db: Session, tenant_id: int) -> dict[str, Any]:
    tenant = _fetch_tenant_row(conn, tenant_id)
    if tenant is None:
        raise LookupError(CODE_TENANT_NOT_FOUND)

    settings_row = _fetch_settings_row(conn, tenant_id)
    ai_settings = _coerce_json_mapping(settings_row.get("ai_settings"))
    store_settings = _coerce_json_mapping(settings_row.get("store_settings"))

    manual_kb_length = text_length(ai_settings.get("manual_knowledge_base"))
    snapshot_row = _fetch_snapshot_row(conn, tenant_id)
    policy_summary = (snapshot_row or {}).get("policy_summary") or {}
    shipping_summary = (snapshot_row or {}).get("shipping_summary") or {}
    if not isinstance(policy_summary, dict):
        policy_summary = {}
    if not isinstance(shipping_summary, dict):
        shipping_summary = {}

    section_counts = collect_section_counts(conn, tenant_id)
    structured_probe = probe_structured_facts(db, tenant_id)
    merchant_probe = probe_merchant_context(db, tenant_id)

    intelligence_nonempty = store_settings_has_policy_or_faq(
        store_settings=store_settings,
        manual_knowledge_base_length=manual_kb_length,
    )
    snapshot_nonempty = snapshot_has_nonempty_policy_or_shipping(policy_summary, shipping_summary)

    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "ok": True,
        "tenant_id": int(tenant["id"]),
        "tenant_name": tenant.get("name"),
        "api_surface_map": dict(API_SURFACE_MAP),
        "counts": {
            "merchant_knowledge_sections": section_counts,
            "merchant_knowledge_drafts": collect_draft_count(conn, tenant_id),
            **collect_branch_counts(conn, tenant_id),
            "store_knowledge_snapshots": {
                "present": snapshot_row is not None,
                "last_full_sync_at": (
                    snapshot_row.get("last_full_sync_at").isoformat()
                    if snapshot_row and snapshot_row.get("last_full_sync_at") is not None
                    else None
                ),
            },
            "tenant_settings": {
                "manual_knowledge_base_length": manual_kb_length,
                "store_settings_field_lengths": store_settings_field_lengths(store_settings),
            },
        },
        "structured_facts_probe": structured_probe,
        "merchant_context_probe": merchant_probe,
        "divergence": compute_divergence(
            knowledge_hub_active_sections=section_counts["active"],
            intelligence_store_settings_has_policy_or_faq=intelligence_nonempty,
            snapshot_has_nonempty_policy_or_shipping_text=snapshot_nonempty,
            structured_facts_nonempty=not structured_probe.get("empty", True),
            manual_knowledge_base_length=manual_kb_length,
        ),
        "sample_section_ids": collect_sample_sections(conn, tenant_id),
        "diagnostic_gap_note": DIAGNOSTIC_GAP_NOTE,
    }
    missing = REQUIRED_TENANT_REPORT_KEYS - set(report)
    if missing:
        raise RuntimeError(f"report_incomplete:{','.join(sorted(missing))}")
    return report


def build_inventory_report(conn: Connection) -> dict[str, Any]:
    if not _table_exists(conn, "tenants"):
        return {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "ok": True,
            "tenant_count": 0,
            "tenants": [],
        }

    rows = conn.execute(
        text(
            """
            SELECT
                t.id AS tenant_id,
                t.name AS tenant_name,
                COALESCE(mks.section_count, 0) AS merchant_knowledge_sections,
                COALESCE(mkd.draft_count, 0) AS merchant_knowledge_drafts,
                CASE WHEN sks.tenant_id IS NULL THEN 0 ELSE 1 END AS store_knowledge_snapshot_present,
                length(coalesce(ts.ai_settings->>'manual_knowledge_base', ''))::int
                    AS manual_knowledge_base_length,
                length(coalesce(ts.store_settings->>'shipping_policy', ''))::int
                    AS shipping_policy_length,
                length(coalesce(ts.store_settings->>'payment_policy', ''))::int
                    AS payment_policy_length,
                COALESCE(jsonb_array_length(ts.store_settings->'faq_approved'), 0)::int
                    AS faq_approved_count
            FROM tenants t
            LEFT JOIN (
                SELECT tenant_id, count(*)::int AS section_count
                FROM merchant_knowledge_sections
                WHERE deleted_at IS NULL
                GROUP BY tenant_id
            ) mks ON mks.tenant_id = t.id
            LEFT JOIN (
                SELECT tenant_id, count(*)::int AS draft_count
                FROM merchant_knowledge_drafts
                GROUP BY tenant_id
            ) mkd ON mkd.tenant_id = t.id
            LEFT JOIN store_knowledge_snapshots sks ON sks.tenant_id = t.id
            LEFT JOIN tenant_settings ts ON ts.tenant_id = t.id
            WHERE COALESCE(mks.section_count, 0) > 0
               OR COALESCE(mkd.draft_count, 0) > 0
               OR sks.tenant_id IS NOT NULL
               OR length(coalesce(ts.ai_settings->>'manual_knowledge_base', '')) > 0
               OR length(coalesce(ts.store_settings->>'shipping_policy', '')) > 0
               OR length(coalesce(ts.store_settings->>'payment_policy', '')) > 0
               OR COALESCE(jsonb_array_length(ts.store_settings->'faq_approved'), 0) > 0
            ORDER BY t.id
            """
        )
    ).mappings().all()

    tenants = [
        {
            "tenant_id": int(row["tenant_id"]),
            "tenant_name": row.get("tenant_name"),
            "merchant_knowledge_sections": int(row.get("merchant_knowledge_sections") or 0),
            "merchant_knowledge_drafts": int(row.get("merchant_knowledge_drafts") or 0),
            "store_knowledge_snapshot_present": bool(row.get("store_knowledge_snapshot_present")),
            "manual_knowledge_base_length": int(row.get("manual_knowledge_base_length") or 0),
            "shipping_policy_length": int(row.get("shipping_policy_length") or 0),
            "payment_policy_length": int(row.get("payment_policy_length") or 0),
            "faq_approved_count": int(row.get("faq_approved_count") or 0),
        }
        for row in rows
    ]
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "ok": True,
        "tenant_count": len(tenants),
        "tenants": tenants,
        "diagnostic_gap_note": DIAGNOSTIC_GAP_NOTE,
    }


def run_tenant(tenant_id: int, *, env: Mapping[str, str] | None = None) -> int:
    try:
        database_url = resolve_database_url(env)
    except ValueError:
        return emit_error(CODE_DATABASE_URL_MISSING)

    try:
        engine = create_readonly_engine(database_url)
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        with engine.connect() as conn, SessionLocal() as db:
            report = build_tenant_report(conn, db, tenant_id)
    except LookupError as exc:
        return emit_error(str(exc))
    except Exception as exc:  # noqa: BLE001 - operator boundary
        return emit_error(CODE_DATABASE_ERROR, detail=type(exc).__name__)

    emit(report)
    return 0


def run_inventory(*, env: Mapping[str, str] | None = None) -> int:
    try:
        database_url = resolve_database_url(env)
    except ValueError:
        return emit_error(CODE_DATABASE_URL_MISSING)

    try:
        engine = create_readonly_engine(database_url)
        with engine.connect() as conn:
            report = build_inventory_report(conn)
    except Exception as exc:  # noqa: BLE001 - operator boundary
        return emit_error(CODE_DATABASE_ERROR, detail=type(exc).__name__)

    emit(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Knowledge Base source provenance operator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tenant_parser = subparsers.add_parser(
        "tenant",
        help="Emit JSON provenance report for one tenant",
    )
    tenant_parser.add_argument("tenant_id", type=int)

    subparsers.add_parser(
        "inventory",
        help="List tenants with any KB-like occupancy (counts/lengths only)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "tenant":
        return run_tenant(args.tenant_id)
    if args.command == "inventory":
        return run_inventory()

    return emit_error(CODE_COMMAND_INVALID)


if __name__ == "__main__":
    raise SystemExit(main())
