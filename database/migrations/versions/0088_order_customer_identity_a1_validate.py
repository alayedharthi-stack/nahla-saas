"""0088 — A1-v3.7 validate phase (deferred constraints + indexes).

A1-Validate PR: targets exactly ``0087 → 0088``. Sibling branch to ``0089``;
never selected by ``alembic upgrade head`` on environments pinned to ``0089``.

- ``CREATE INDEX CONCURRENTLY`` for deferred ``orders`` indexes (autocommit blocks).
- ``VALIDATE CONSTRAINT`` for orders FK/CHECK added NOT VALID in ``0087``.
- Capability advances to ``validated`` only after every index + constraint succeeds.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None

_FK_ORDERS_TENANT_CUSTOMER = "fk_orders_tenant_customer"
_FK_ORDERS_EXTERNAL_PROFILE = "fk_orders_external_profile_connection"

_ORDER_CHECKS = (
    "chk_orders_external_no_canonical_customer",
    "chk_orders_external_profile_authoritative",
    "chk_orders_external_no_customer_link_authoritative",
    "chk_orders_nahla_internal_authoritative",
    "chk_orders_internal_no_external_authoritative",
    "chk_orders_untrusted_no_authoritative",
    "chk_orders_untrusted_kinds_no_links",
)

_ORDER_INDEXES = (
    ("ix_orders_tenant_customer_id", ["tenant_id", "customer_id"], None),
    (
        "ix_orders_tenant_external_tuple",
        ["tenant_id", "identity_namespace", "integration_connection_id", "external_customer_ref"],
        "external_customer_ref IS NOT NULL",
    ),
    ("ix_orders_tenant_order_source_kind", ["tenant_id", "order_source_kind"], None),
)

_CAPABILITY_TABLE = "order_customer_identity_capability_state"
_CAPABILITY_KEY = "order_customer_identity"
_CAPABILITY_STATE_VALIDATED = "validated"
_VALIDATION_REVISION = "0088"


def _has_index(bind, table: str, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in {idx.get("name") for idx in insp.get_indexes(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()

    for idx_name, columns, where_sql in _ORDER_INDEXES:
        if _has_index(bind, "orders", idx_name):
            continue
        with op.get_context().autocommit_block():
            if where_sql:
                op.create_index(
                    idx_name,
                    "orders",
                    columns,
                    postgresql_where=sa.text(where_sql),
                    postgresql_concurrently=True,
                )
            else:
                op.create_index(
                    idx_name,
                    "orders",
                    columns,
                    postgresql_concurrently=True,
                )

    for fk_name in (_FK_ORDERS_TENANT_CUSTOMER, _FK_ORDERS_EXTERNAL_PROFILE):
        op.execute(
            sa.text(
                f"""
                DO $validate$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{fk_name}' AND NOT convalidated
                    ) THEN
                        ALTER TABLE orders VALIDATE CONSTRAINT {fk_name};
                    END IF;
                END $validate$;
                """
            )
        )

    for chk_name in _ORDER_CHECKS:
        op.execute(
            sa.text(
                f"""
                DO $validate$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{chk_name}' AND NOT convalidated
                    ) THEN
                        ALTER TABLE orders VALIDATE CONSTRAINT {chk_name};
                    END IF;
                END $validate$;
                """
            )
        )

    op.execute(
        sa.text(
            f"""
            UPDATE {_CAPABILITY_TABLE}
            SET state = '{_CAPABILITY_STATE_VALIDATED}',
                validation_revision = '{_VALIDATION_REVISION}',
                updated_at = now()
            WHERE capability_key = '{_CAPABILITY_KEY}'
              AND state = 'expand'
              AND validation_revision IS NULL
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.execute(
        sa.text(
            f"""
            UPDATE {_CAPABILITY_TABLE}
            SET state = 'expand',
                validation_revision = NULL,
                updated_at = now()
            WHERE capability_key = '{_CAPABILITY_KEY}'
              AND validation_revision = '{_VALIDATION_REVISION}'
            """
        )
    )

    for idx_name, _columns, _where in reversed(_ORDER_INDEXES):
        if _has_index(bind, "orders", idx_name):
            with op.get_context().autocommit_block():
                op.drop_index(idx_name, table_name="orders", postgresql_concurrently=True)
