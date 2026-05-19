"""0065 — TOTP 2FA tables (Phase 2A Sprint 1).

Revision ID: 0065
Revises:    0064

Why this migration exists
─────────────────────────
Phase 2A introduces Time-based One-Time Password (TOTP) two-factor
authentication. Two new tables are needed:

1. ``user_totp`` — one row per user that has enabled 2FA.
   * ``secret_enc``     — Fernet-encrypted TOTP shared secret. The
     plaintext base32 secret is NEVER stored. Encryption key comes
     from the ``TOTP_ENC_KEY`` env (Fernet key, base64 urlsafe).
   * ``confirmed_at``   — set on the first successful OTP verification
     during enrolment. NULL means "secret generated but never proven"
     and the row is treated as not-enabled (the API guards against
     this; the column also lets ops audit dropped enrolments).
   * ``last_used_at``   — last successful OTP/recovery verification,
     for the dashboard "active session" surface.
   * ``failed_attempts`` / ``locked_until`` — soft lock after repeated
     wrong OTPs; rate-limit is enforced at the router layer too.

2. ``user_recovery_codes`` — 10 single-use bcrypt-hashed codes per
   user. ``used_at`` marks consumption; rows are NEVER deleted on use
   (audit trail). Regeneration deletes-and-recreates the full set.

Idempotency
───────────
Same pattern as 0061: every create/index is guarded by an inspector
check, so re-running on a populated DB is a safe no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    # ── user_totp ─────────────────────────────────────────────────────────
    if not _has_table(bind, "user_totp"):
        op.create_table(
            "user_totp",
            sa.Column("user_id", sa.Integer(), primary_key=True),
            sa.Column("secret_enc", sa.LargeBinary(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "failed_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], ondelete="CASCADE",
            ),
        )

    # ── user_recovery_codes ───────────────────────────────────────────────
    if not _has_table(bind, "user_recovery_codes"):
        op.create_table(
            "user_recovery_codes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("code_hash", sa.String(length=255), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], ondelete="CASCADE",
            ),
        )

    if not _has_index(bind, "user_recovery_codes", "ix_user_recovery_codes_user_id"):
        op.create_index(
            "ix_user_recovery_codes_user_id",
            "user_recovery_codes",
            ["user_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_index(bind, "user_recovery_codes", "ix_user_recovery_codes_user_id"):
        op.drop_index("ix_user_recovery_codes_user_id", "user_recovery_codes")

    if _has_table(bind, "user_recovery_codes"):
        op.drop_table("user_recovery_codes")
    if _has_table(bind, "user_totp"):
        op.drop_table("user_totp")
