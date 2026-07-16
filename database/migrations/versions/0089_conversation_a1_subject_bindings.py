"""0089 — conversation → A1-subject binding substrate (PR1 internal write path).

Adds tenant-scoped ``conversation_a1_subject_bindings`` with active/revoked/
superseded semantics. Enables composite FK to conversations via
``uq_conversations_tenant_id``. No AI read bridge or capability changes.

Downgrade drops binding rows and the conversations composite index when safe.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0089"
down_revision = "0087"
branch_labels = None
depends_on = None

_UQ_CONVERSATIONS_TENANT_ID = "uq_conversations_tenant_id"
_TABLE = "conversation_a1_subject_bindings"
_FK_CASB_TENANT_CONVERSATION = "fk_casb_tenant_conversation"
_FK_CASB_TENANT_INTERNAL_CUSTOMER = "fk_casb_tenant_internal_customer"
_UQ_CASB_ACTIVE = "uq_casb_tenant_conversation_active"
_IX_CASB_TENANT_CONVERSATION_STATE = "ix_casb_tenant_conversation_state"

_CHECKS = (
    ("chk_casb_binding_state", "binding_state IN ('active', 'revoked', 'superseded')"),
    (
        "chk_casb_subject_kind",
        "subject_kind IN ('nahla_internal_customer', 'external_customer_profile')",
    ),
    ("chk_casb_evidence_class", "evidence_class IN ('authoritative', 'inferred')"),
    (
        "chk_casb_binding_source",
        "binding_source IN ("
        "'wa_order_bridge_authoritative_internal', "
        "'salla_order_conversation_attestation', "
        "'provider_oauth_session'"
        ")",
    ),
    (
        "chk_casb_provenance_kind",
        "provenance_kind IN ('order', 'webhook_event', 'operator')",
    ),
    (
        "chk_casb_subject_xor",
        """
        (
            subject_kind = 'nahla_internal_customer'
            AND internal_customer_id IS NOT NULL
            AND external_customer_profile_id IS NULL
            AND identity_namespace = 'nahla_internal_order_v1'
        )
        OR (
            subject_kind = 'external_customer_profile'
            AND external_customer_profile_id IS NOT NULL
            AND internal_customer_id IS NULL
            AND identity_namespace LIKE 'external_provider_%'
        )
        """,
    ),
    (
        "chk_casb_state_revocation_timestamp",
        """
        (binding_state = 'active' AND revoked_at IS NULL)
        OR
        (binding_state IN ('revoked', 'superseded') AND revoked_at IS NOT NULL)
        """,
    ),
)


def _has_table(bind, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in set(insp.get_table_names())
    except Exception:
        return False


def _has_index(bind, table: str, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in {idx.get("name") for idx in insp.get_indexes(table)}
    except Exception:
        return False


def _has_constraint(bind, table: str, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        for kind in ("unique_constraints", "foreign_key_constraints", "check_constraints"):
            getter = getattr(insp, f"get_{kind}", None)
            if getter is None:
                continue
            for c in getter(table):
                if c.get("name") == name:
                    return True
    except Exception:
        pass
    return False


def _add_check_not_valid(bind, *, table: str, name: str, sql: str) -> None:
    if _has_constraint(bind, table, name):
        return
    op.execute(sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({sql}) NOT VALID"))


def _add_fk_not_valid(bind, *, table: str, name: str, sql: str) -> None:
    if _has_constraint(bind, table, name):
        return
    op.execute(sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} {sql} NOT VALID"))


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_index(bind, "conversations", _UQ_CONVERSATIONS_TENANT_ID):
        op.create_index(
            _UQ_CONVERSATIONS_TENANT_ID,
            "conversations",
            ["tenant_id", "id"],
            unique=True,
        )

    if not _has_table(bind, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("conversation_id", sa.Integer(), nullable=False),
            sa.Column("subject_kind", sa.String(), nullable=False),
            sa.Column("identity_namespace", sa.String(), nullable=False),
            sa.Column("internal_customer_id", sa.Integer(), nullable=True),
            sa.Column(
                "external_customer_profile_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column("binding_state", sa.String(), nullable=False),
            sa.Column("evidence_class", sa.String(), nullable=False),
            sa.Column("binding_source", sa.String(), nullable=False),
            sa.Column("provenance_kind", sa.String(), nullable=False),
            sa.Column("provenance_id", sa.String(), nullable=False),
            sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_casb_tenant_conversation",
            _TABLE,
            ["tenant_id", "conversation_id"],
        )
        op.create_index(
            _IX_CASB_TENANT_CONVERSATION_STATE,
            _TABLE,
            ["tenant_id", "conversation_id", "binding_state"],
        )
        if not _has_index(bind, _TABLE, _UQ_CASB_ACTIVE):
            op.create_index(
                _UQ_CASB_ACTIVE,
                _TABLE,
                ["tenant_id", "conversation_id"],
                unique=True,
                postgresql_where=sa.text("binding_state = 'active'"),
            )

    if not _has_index(bind, _TABLE, _IX_CASB_TENANT_CONVERSATION_STATE):
        # PR1 creates a new table; ordinary transactional CREATE INDEX matches
        # the expand-phase migration strategy and avoids a concurrent index
        # operation inside Alembic's transaction.
        op.create_index(
            _IX_CASB_TENANT_CONVERSATION_STATE,
            _TABLE,
            ["tenant_id", "conversation_id", "binding_state"],
        )

    for name, sql in _CHECKS:
        _add_check_not_valid(bind, table=_TABLE, name=name, sql=sql)

    _add_fk_not_valid(
        bind,
        table=_TABLE,
        name=_FK_CASB_TENANT_CONVERSATION,
        sql=(
            "FOREIGN KEY (tenant_id, conversation_id) "
            "REFERENCES conversations (tenant_id, id)"
        ),
    )
    _add_fk_not_valid(
        bind,
        table=_TABLE,
        name=_FK_CASB_TENANT_INTERNAL_CUSTOMER,
        sql=(
            "FOREIGN KEY (tenant_id, internal_customer_id) "
            "REFERENCES customers (tenant_id, id)"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, _TABLE):
        op.drop_table(_TABLE)
    if _has_index(bind, "conversations", _UQ_CONVERSATIONS_TENANT_ID):
        op.drop_index(_UQ_CONVERSATIONS_TENANT_ID, table_name="conversations")
