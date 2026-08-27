"""Closed contract for normal bootstrap migration targets (multi-head aware)."""
from __future__ import annotations

from sqlalchemy import inspect, text

REPOSITORY_ALEMBIC_HEADS = frozenset({"0092", "0101", "0102"})
INTEGRATION_BOOTSTRAP_TARGET = "0093"
NORMAL_BOOTSTRAP_REVISIONS = frozenset({"0093"})
VALIDATED_STAGING_BOOTSTRAP_REVISIONS = frozenset({"0088", "0093"})
STAGING_VALIDATED_ATTACH_REVISIONS = frozenset({"0088", "0089"})
FORBIDDEN_BOOTSTRAP_LITERALS = frozenset({"head"})

COEXISTENCE_NONCE_MIGRATION_TARGET = "0101"
COEXISTENCE_NONCE_TABLE = "whatsapp_oauth_nonces"
COEXISTENCE_NONCE_REVISIONS = frozenset({"0101", "0102"})
_NONCE_UQ = "uq_whatsapp_oauth_nonces_hash"
_NONCE_IX_TENANT = "ix_whatsapp_oauth_nonces_tenant_id"
_NONCE_IX_EXPIRES = "ix_whatsapp_oauth_nonces_expires_at"
_NONCE_FK = "fk_whatsapp_oauth_nonces_tenant"


def build_normal_bootstrap_upgrade_argv(*, python_executable: str) -> list[str]:
    if INTEGRATION_BOOTSTRAP_TARGET in FORBIDDEN_BOOTSTRAP_LITERALS:
        raise ValueError("bootstrap_target_forbidden")
    return [python_executable, "-m", "alembic", "upgrade", INTEGRATION_BOOTSTRAP_TARGET]


def _alembic_versions(bind) -> set[str]:
    def _read_rows(executable) -> set[str]:
        rows = executable.execute(text("SELECT version_num FROM alembic_version"))
        return {str(row[0]) for row in rows}

    try:
        from sqlalchemy.engine import Engine

        if isinstance(bind, Engine):
            with bind.connect() as conn:
                return _read_rows(conn)
        if hasattr(bind, "execute"):
            return _read_rows(bind)
    except Exception:  # noqa: silent-ok - version table may be absent on fresh DB
        pass
    try:
        if "alembic_version" not in inspect(bind).get_table_names():
            return set()
        return _read_rows(bind)
    except Exception:
        return set()


def resolve_coexistence_nonce_migration_target(bind) -> str:
    revs = _alembic_versions(bind)
    if "0102" in revs:
        return "0102"
    if "0101" in revs:
        return "0101"
    if "0100" in revs:
        return "0102"
    return "0101"


def build_coexistence_nonce_upgrade_argv(*, python_executable: str, bind=None) -> list[str]:
    target = (
        resolve_coexistence_nonce_migration_target(bind)
        if bind is not None
        else COEXISTENCE_NONCE_MIGRATION_TARGET
    )
    if target in FORBIDDEN_BOOTSTRAP_LITERALS:
        raise ValueError("bootstrap_target_forbidden")
    return [python_executable, "-m", "alembic", "upgrade", target]


def assert_coexistence_nonce_migration_applied(bind) -> None:
    insp = inspect(bind)
    if COEXISTENCE_NONCE_TABLE not in insp.get_table_names():
        raise RuntimeError(f"missing_table:{COEXISTENCE_NONCE_TABLE}")
    revs = _alembic_versions(bind)
    if not revs.intersection(COEXISTENCE_NONCE_REVISIONS):
        raise RuntimeError("missing_nonce_revision")
    indexes = insp.get_indexes(COEXISTENCE_NONCE_TABLE)
    idx_names = {i.get("name") for i in indexes}
    uq_names = {u.get("name") for u in insp.get_unique_constraints(COEXISTENCE_NONCE_TABLE)}
    has_unique_nonce_hash = any(
        bool(i.get("unique"))
        and "nonce_hash" in (i.get("column_names") or [])
        for i in indexes
    )
    if _NONCE_UQ not in uq_names and not has_unique_nonce_hash:
        raise RuntimeError(f"missing_uq:{_NONCE_UQ}")
    for required in (_NONCE_IX_TENANT, _NONCE_IX_EXPIRES):
        if required not in idx_names:
            raise RuntimeError(f"missing_index:{required}")
    foreign_keys = insp.get_foreign_keys(COEXISTENCE_NONCE_TABLE)
    fk_names = {fk.get("name") for fk in foreign_keys}
    has_tenant_fk = any(
        fk.get("referred_table") == "tenants"
        and "tenant_id" in (fk.get("constrained_columns") or [])
        for fk in foreign_keys
    )
    if _NONCE_FK not in fk_names and not has_tenant_fk:
        raise RuntimeError(f"missing_fk:{_NONCE_FK}")


def assert_coexistence_nonce_migration_missing(bind) -> None:
    if COEXISTENCE_NONCE_TABLE in inspect(bind).get_table_names():
        raise RuntimeError(f"unexpected_table:{COEXISTENCE_NONCE_TABLE}")
