"""0087 — A1-v3.7 expand phase (order-customer identity).

A1-Expand PR: Alembic head stops at 0087.
- New tables + nullable order identity columns (no backfill).
- `order_customer_identity_capability_state` seeded to `expand`.
- Orders FK/CHECK added NOT VALID (no full-table validation in this release).
- No orders performance indexes (deferred to A1-Validate PR / 0088).

Downgrade drops A1 expand objects. Linkage data is not preserved.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

_UQ_CUSTOMERS_TENANT_ID = "uq_customers_tenant_id"
_UQ_INTEGRATIONS_TENANT_ID = "uq_integrations_tenant_id_id"
_UQ_ECP_IDENTITY = "uq_external_customer_profiles_identity"
_UQ_ECP_TENANT_ID_CONN = "uq_external_customer_profiles_tenant_id_connection"
_FK_ORDERS_TENANT_CUSTOMER = "fk_orders_tenant_customer"
_FK_ORDERS_EXTERNAL_PROFILE = "fk_orders_external_profile_connection"
_FK_ECP_TENANT_INTEGRATION = "fk_ecp_tenant_integration"
_FK_EXT_COV_PROFILE = "fk_ext_cov_tenant_profile"
_FK_INT_COV_TENANT_CUSTOMER = "fk_int_cov_tenant_customer"
_CAPABILITY_TABLE = "order_customer_identity_capability_state"
_CAPABILITY_KEY = "order_customer_identity"
_CAPABILITY_STATE_EXPAND = "expand"

_CHK_EXTERNAL_NO_CANONICAL = "chk_orders_external_no_canonical_customer"
_CHK_EXTERNAL_PROFILE_AUTH = "chk_orders_external_profile_authoritative"
_CHK_EXTERNAL_NO_CUST_AUTH = "chk_orders_external_no_customer_link_authoritative"
_CHK_NAHL_INTERNAL_AUTH = "chk_orders_nahla_internal_authoritative"
_CHK_INTERNAL_NO_EXT_AUTH = "chk_orders_internal_no_external_authoritative"
_CHK_UNTRUSTED_NO_AUTH = "chk_orders_untrusted_no_authoritative"
_CHK_UNTRUSTED_KINDS = "chk_orders_untrusted_kinds_no_links"

_ORDER_CHECKS = (
    (_CHK_EXTERNAL_NO_CANONICAL, """
            order_source_kind IS DISTINCT FROM 'external_provider'
            OR (
                customer_id IS NULL
                AND (customer_link_state = 'unlinked' OR customer_link_state IS NULL)
                AND customer_link_evidence_class IS NULL
            )
            """),
    (_CHK_EXTERNAL_PROFILE_AUTH, """
            NOT (
                order_source_kind = 'external_provider'
                AND external_identity_evidence_class = 'authoritative'
            )
            OR (
                external_identity_link_state = 'verified'
                AND external_customer_profile_id IS NOT NULL
                AND integration_connection_id IS NOT NULL
                AND external_customer_ref IS NOT NULL
                AND identity_namespace LIKE 'external_provider_%'
                AND customer_id IS NULL
                AND customer_link_state = 'unlinked'
                AND customer_link_evidence_class IS NULL
            )
            """),
    (_CHK_EXTERNAL_NO_CUST_AUTH, """
            order_source_kind IS DISTINCT FROM 'external_provider'
            OR customer_link_evidence_class IS DISTINCT FROM 'authoritative'
            """),
    (_CHK_NAHL_INTERNAL_AUTH, """
            NOT (
                order_source_kind = 'nahla_internal'
                AND customer_link_evidence_class = 'authoritative'
            )
            OR (
                customer_link_state = 'verified'
                AND customer_id IS NOT NULL
                AND identity_namespace = 'nahla_internal_order_v1'
                AND external_identity_link_state IS NULL
                AND external_identity_evidence_class IS NULL
                AND external_customer_profile_id IS NULL
                AND integration_connection_id IS NULL
                AND external_customer_ref IS NULL
            )
            """),
    (_CHK_INTERNAL_NO_EXT_AUTH, """
            order_source_kind IS DISTINCT FROM 'nahla_internal'
            OR external_identity_evidence_class IS DISTINCT FROM 'authoritative'
            """),
    (_CHK_UNTRUSTED_NO_AUTH, """
            order_source_kind IN ('nahla_internal', 'external_provider')
            OR (
                customer_link_evidence_class IS DISTINCT FROM 'authoritative'
                AND external_identity_evidence_class IS DISTINCT FROM 'authoritative'
            )
            """),
    (_CHK_UNTRUSTED_KINDS, """
            order_source_kind NOT IN ('whatsapp', 'manual', 'other')
            OR (
                customer_id IS NULL
                AND external_customer_profile_id IS NULL
                AND customer_link_evidence_class IS NULL
                AND external_identity_evidence_class IS NULL
                AND (customer_link_state = 'unlinked' OR customer_link_state IS NULL)
                AND (external_identity_link_state = 'unlinked' OR external_identity_link_state IS NULL)
                AND integration_connection_id IS NULL
                AND external_customer_ref IS NULL
                AND identity_namespace IS NULL
            )
            """),
)


def _has_table(bind, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in set(insp.get_table_names())
    except Exception:
        return False


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(c.get("name") == column for c in insp.get_columns(table))
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

    if not _has_index(bind, "customers", _UQ_CUSTOMERS_TENANT_ID):
        op.create_index(
            _UQ_CUSTOMERS_TENANT_ID,
            "customers",
            ["tenant_id", "id"],
            unique=True,
        )

    if not _has_index(bind, "integrations", _UQ_INTEGRATIONS_TENANT_ID):
        op.create_index(
            _UQ_INTEGRATIONS_TENANT_ID,
            "integrations",
            ["tenant_id", "id"],
            unique=True,
        )

    if not _has_table(bind, "external_customer_profiles"):
        op.create_table(
            "external_customer_profiles",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("identity_namespace", sa.String(), nullable=False),
            sa.Column("integration_connection_id", sa.Integer(), nullable=False),
            sa.Column("external_customer_ref", sa.String(), nullable=False),
            sa.Column("profile_state", sa.String(), nullable=False, server_default="active"),
            sa.Column("profile_source", sa.String(), nullable=True),
            sa.Column("demographics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("provider_snapshot_at", sa.DateTime(timezone=True), nullable=True),
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
            _UQ_ECP_IDENTITY,
            "external_customer_profiles",
            ["tenant_id", "identity_namespace", "integration_connection_id", "external_customer_ref"],
            unique=True,
        )
        op.create_index(
            _UQ_ECP_TENANT_ID_CONN,
            "external_customer_profiles",
            ["tenant_id", "id", "integration_connection_id"],
            unique=True,
        )
        op.create_index(
            "ix_ecp_tenant_integration",
            "external_customer_profiles",
            ["tenant_id", "integration_connection_id"],
        )
        if not _has_constraint(bind, "external_customer_profiles", _FK_ECP_TENANT_INTEGRATION):
            op.create_foreign_key(
                _FK_ECP_TENANT_INTEGRATION,
                "external_customer_profiles",
                "integrations",
                ["tenant_id", "integration_connection_id"],
                ["tenant_id", "id"],
            )

    order_cols = [
        ("customer_id", sa.Column("customer_id", sa.Integer(), nullable=True)),
        ("order_source_kind", sa.Column("order_source_kind", sa.String(), nullable=True)),
        ("identity_namespace", sa.Column("identity_namespace", sa.String(), nullable=True)),
        (
            "integration_connection_id",
            sa.Column("integration_connection_id", sa.Integer(), nullable=True),
        ),
        ("external_customer_ref", sa.Column("external_customer_ref", sa.String(), nullable=True)),
        (
            "external_customer_profile_id",
            sa.Column("external_customer_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
        (
            "customer_link_state",
            sa.Column("customer_link_state", sa.String(), nullable=False, server_default="unlinked"),
        ),
        (
            "customer_link_evidence_class",
            sa.Column("customer_link_evidence_class", sa.String(), nullable=True),
        ),
        ("customer_link_source", sa.Column("customer_link_source", sa.String(), nullable=True)),
        (
            "customer_linked_at",
            sa.Column("customer_linked_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "external_identity_link_state",
            sa.Column("external_identity_link_state", sa.String(), nullable=True),
        ),
        (
            "external_identity_evidence_class",
            sa.Column("external_identity_evidence_class", sa.String(), nullable=True),
        ),
    ]
    for col_name, col_def in order_cols:
        if not _has_column(bind, "orders", col_name):
            op.add_column("orders", col_def)

    _add_fk_not_valid(
        bind,
        table="orders",
        name=_FK_ORDERS_TENANT_CUSTOMER,
        sql="FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id)",
    )
    _add_fk_not_valid(
        bind,
        table="orders",
        name=_FK_ORDERS_EXTERNAL_PROFILE,
        sql=(
            "FOREIGN KEY (tenant_id, external_customer_profile_id, integration_connection_id) "
            "REFERENCES external_customer_profiles (tenant_id, id, integration_connection_id)"
        ),
    )

    if not _has_table(bind, "external_customer_profile_order_history_coverage"):
        op.create_table(
            "external_customer_profile_order_history_coverage",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column(
                "external_customer_profile_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column("identity_namespace", sa.String(), nullable=False),
            sa.Column("integration_connection_id", sa.Integer(), nullable=False),
            sa.Column("external_customer_ref", sa.String(), nullable=False),
            sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "forward_sync_health",
                sa.String(),
                nullable=False,
                server_default="stale",
            ),
            sa.Column(
                "authoritative_source_history_completeness",
                sa.String(),
                nullable=False,
                server_default="incomplete",
            ),
            sa.Column("linked_orders_in_scope_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "unmapped_orders_in_scope_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "mislinked_orders_in_scope_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
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
            sa.UniqueConstraint(
                "external_customer_profile_id",
                name="uq_ext_profile_cov_profile_id",
            ),
        )
        op.create_foreign_key(
            _FK_EXT_COV_PROFILE,
            "external_customer_profile_order_history_coverage",
            "external_customer_profiles",
            ["tenant_id", "external_customer_profile_id", "integration_connection_id"],
            ["tenant_id", "id", "integration_connection_id"],
        )

    if not _has_table(bind, "nahla_internal_customer_order_history_coverage"):
        op.create_table(
            "nahla_internal_customer_order_history_coverage",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("identity_namespace", sa.String(), nullable=False, server_default="nahla_internal_order_v1"),
            sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "forward_sync_health",
                sa.String(),
                nullable=False,
                server_default="stale",
            ),
            sa.Column(
                "authoritative_source_history_completeness",
                sa.String(),
                nullable=False,
                server_default="incomplete",
            ),
            sa.Column("linked_orders_in_scope_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "unmapped_orders_in_scope_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "mislinked_orders_in_scope_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
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
            sa.UniqueConstraint(
                "tenant_id",
                "customer_id",
                "identity_namespace",
                name="uq_int_cov_tenant_customer_ns",
            ),
        )
        op.create_foreign_key(
            _FK_INT_COV_TENANT_CUSTOMER,
            "nahla_internal_customer_order_history_coverage",
            "customers",
            ["tenant_id", "customer_id"],
            ["tenant_id", "id"],
        )

    for name, sql in _ORDER_CHECKS:
        _add_check_not_valid(bind, table="orders", name=name, sql=sql)

    if not _has_table(bind, _CAPABILITY_TABLE):
        op.create_table(
            _CAPABILITY_TABLE,
            sa.Column("capability_key", sa.String(), primary_key=True),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("validation_revision", sa.String(), nullable=True),
            sa.CheckConstraint(
                "state IN ('expand', 'validated')",
                name="chk_oci_capability_state",
            ),
        )
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_CAPABILITY_TABLE} (capability_key, state, validation_revision)
            VALUES ('{_CAPABILITY_KEY}', '{_CAPABILITY_STATE_EXPAND}', NULL)
            ON CONFLICT (capability_key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, _CAPABILITY_TABLE):
        op.drop_table(_CAPABILITY_TABLE)

    for name in (
        _CHK_UNTRUSTED_KINDS,
        _CHK_UNTRUSTED_NO_AUTH,
        _CHK_INTERNAL_NO_EXT_AUTH,
        _CHK_NAHL_INTERNAL_AUTH,
        _CHK_EXTERNAL_NO_CUST_AUTH,
        _CHK_EXTERNAL_PROFILE_AUTH,
        _CHK_EXTERNAL_NO_CANONICAL,
    ):
        if _has_constraint(bind, "orders", name):
            op.drop_constraint(name, "orders", type_="check")

    if _has_table(bind, "nahla_internal_customer_order_history_coverage"):
        op.drop_table("nahla_internal_customer_order_history_coverage")
    if _has_table(bind, "external_customer_profile_order_history_coverage"):
        op.drop_table("external_customer_profile_order_history_coverage")

    if _has_constraint(bind, "orders", _FK_ORDERS_EXTERNAL_PROFILE):
        op.drop_constraint(_FK_ORDERS_EXTERNAL_PROFILE, "orders", type_="foreignkey")
    if _has_constraint(bind, "orders", _FK_ORDERS_TENANT_CUSTOMER):
        op.drop_constraint(_FK_ORDERS_TENANT_CUSTOMER, "orders", type_="foreignkey")

    for col in (
        "external_identity_evidence_class",
        "external_identity_link_state",
        "customer_linked_at",
        "customer_link_source",
        "customer_link_evidence_class",
        "customer_link_state",
        "external_customer_profile_id",
        "external_customer_ref",
        "integration_connection_id",
        "identity_namespace",
        "order_source_kind",
        "customer_id",
    ):
        if _has_column(bind, "orders", col):
            op.drop_column("orders", col)

    if _has_table(bind, "external_customer_profiles"):
        op.drop_table("external_customer_profiles")

    if _has_index(bind, "integrations", _UQ_INTEGRATIONS_TENANT_ID):
        op.drop_index(_UQ_INTEGRATIONS_TENANT_ID, table_name="integrations")
    if _has_index(bind, "customers", _UQ_CUSTOMERS_TENANT_ID):
        op.drop_index(_UQ_CUSTOMERS_TENANT_ID, table_name="customers")
