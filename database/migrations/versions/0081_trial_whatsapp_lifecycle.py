"""0081 — Trial starts after WhatsApp connection + first_whatsapp_connected_at.

Revision ID: 0081
Revises:    0080
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("tenants")}
    if "first_whatsapp_connected_at" not in cols:
        op.add_column(
            "tenants",
            sa.Column("first_whatsapp_connected_at", sa.DateTime(), nullable=True),
        )

    # Data migration — correct existing tenant trial anchors.
    # Run on an independent connection so ORM/schema drift cannot poison
    # Alembic's outer revision transaction (PostgreSQL abort semantics).
    try:
        import os
        import sys

        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        backend_dir = os.path.join(repo_root, "backend")
        database_dir = os.path.join(repo_root, "database")
        for p in (repo_root, backend_dir, database_dir):
            if p not in sys.path:
                sys.path.insert(0, p)

        from sqlalchemy.orm import sessionmaker
        from core.trial_lifecycle import migrate_existing_tenant_trials

        with bind.engine.connect() as data_conn:
            data_txn = data_conn.begin()
            session = sessionmaker(bind=data_conn)()
            try:
                migrate_existing_tenant_trials(session)
                data_txn.commit()
            except Exception:
                data_txn.rollback()
                raise
            finally:
                session.close()
    except Exception as exc:
        # Column add succeeded; log and continue — operator can rerun script.
        import logging
        logging.getLogger("alembic.runtime.migration").warning(
            "0081 data migration skipped or partial: %s", exc
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("tenants")}
    if "first_whatsapp_connected_at" in cols:
        op.drop_column("tenants", "first_whatsapp_connected_at")
