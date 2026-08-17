"""0099 — simple merchant escalation authoring (platform-wide).

Adds explicit customer-visibility on contacts, permitted action / trigger
on escalation steps, and a confirmed instruction snapshot on branches.

Backfill is idempotent and tenant-scoped. No tenant-id special cases.
"""
from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def _ensure_backend_path() -> None:
    repo = Path(__file__).resolve().parents[3]
    backend = repo / "backend"
    for path in (str(repo), str(backend)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "branch_contacts", "customer_visibility"):
        op.add_column(
            "branch_contacts",
            sa.Column(
                "customer_visibility",
                sa.String(length=32),
                nullable=False,
                server_default="internal_only",
            ),
        )
    if not _has_column(bind, "branch_escalation_steps", "permitted_action"):
        op.add_column(
            "branch_escalation_steps",
            sa.Column(
                "permitted_action",
                sa.String(length=64),
                nullable=False,
                server_default="share_customer_contact",
            ),
        )
    if not _has_column(bind, "branch_escalation_steps", "trigger_condition"):
        op.add_column(
            "branch_escalation_steps",
            sa.Column(
                "trigger_condition",
                sa.String(length=64),
                nullable=False,
                server_default="sequence",
            ),
        )
    if not _has_column(bind, "merchant_branches", "escalation_instruction_text"):
        op.add_column(
            "merchant_branches",
            sa.Column("escalation_instruction_text", sa.Text(), nullable=True),
        )
    if not _has_column(bind, "merchant_branches", "escalation_policy_json"):
        op.add_column(
            "merchant_branches",
            sa.Column("escalation_policy_json", JSONB(), nullable=True),
        )

    _ensure_backend_path()
    from modules.operations.escalation_policy_migration import (  # noqa: PLC0415
        normalize_all_tenants,
    )

    session = Session(bind=bind)
    try:
        summary = normalize_all_tenants(session)
        session.commit()
        print(
            "[0099] escalation_authoring scanned=%s changed=%s"
            % (summary.get("scanned"), summary.get("changed"))
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "merchant_branches", "escalation_policy_json"):
        op.drop_column("merchant_branches", "escalation_policy_json")
    if _has_column(bind, "merchant_branches", "escalation_instruction_text"):
        op.drop_column("merchant_branches", "escalation_instruction_text")
    if _has_column(bind, "branch_escalation_steps", "trigger_condition"):
        op.drop_column("branch_escalation_steps", "trigger_condition")
    if _has_column(bind, "branch_escalation_steps", "permitted_action"):
        op.drop_column("branch_escalation_steps", "permitted_action")
    if _has_column(bind, "branch_contacts", "customer_visibility"):
        op.drop_column("branch_contacts", "customer_visibility")
