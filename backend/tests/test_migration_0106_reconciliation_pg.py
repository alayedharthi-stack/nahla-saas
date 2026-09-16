"""PostgreSQL proof: migration 0106 is safe whether or not
``commerce_agent_v2_shadow_runs`` already exists.

Production startup pins ``alembic upgrade 0093`` and materializes new ORM
tables through ``Base.metadata.create_all`` (``backend/main.py``). The
``CommerceAgentV2ShadowRun`` model has shipped in every production build
since 0106 was written, so the table exists physically while
``alembic_version`` is still ``0105`` — exactly the production state that
made the original bare ``op.create_table`` fail with ``DuplicateTable``
and block the whole ``0105 → 0107`` path.

Proven on real PostgreSQL, ephemeral databases only:

* State A — table absent: ``0105 → 0106`` creates it; ``0105 → 0107``
  creates it and ``customer_name_provenance``.
* State B — table present via ``Base.metadata.create_all`` with live rows:
  ``0105 → 0107`` succeeds with no DROP / re-create, the relation OID is
  unchanged, every row survives byte-for-byte, the resulting schema is
  identical to State A (columns, types, nullability, server defaults,
  primary key, foreign keys, indexes), ``alembic_version`` ends at ``0107``
  and ``customer_name_provenance`` is created by 0107.
* Negative control: the unguarded ``create_table`` really fails in State B.
* Partial drift (columns / index / FK lost, NULLs in NOT NULL columns) is
  reconciled additively where a safe fill value exists; required columns
  with no safe fill value (``tenant_id``, ``conversation_id``,
  ``sdk_trace_id``, ``model``, ``status``) are restored / left NULLABLE and
  never forced NOT NULL, so that shape ends nullable there rather than at
  exact State A parity.
* Already-at-0106 and already-at-0107 re-runs are no-ops; downgrade path.
* 0098 (tenant-settings normalization) never executes on this path.

Runs when a PostgreSQL DSN is available (see
``legacy_migration_drift_postgres_fixtures.connect_engine``); REQUIRED
under ``MIGRATION_0106_PG_REQUIRED=1`` or
``LEGACY_MIG_PG_INTEGRATION_REQUIRED=1``. Never touches Production and
never invokes ``head``.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_DATABASE = _REPO / "database"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    downgrade_alembic,
    drop_ephemeral_database,
    run_alembic,
)
from models import CommerceAgentV2ShadowRun  # noqa: E402

TABLE = "commerce_agent_v2_shadow_runs"
PROVENANCE = "customer_name_provenance"
MIGRATION_SOURCE = _DATABASE / "migrations" / "versions" / "0106_commerce_agent_v2_shadow_runs.py"

EXPECTED_COLUMNS = (
    "id", "tenant_id", "conversation_id", "sdk_trace_id", "model", "status",
    "structured_output", "tool_trace", "guardrail_results",
    "latency_ms", "input_tokens", "output_tokens", "total_tokens",
    "failure_reason", "created_at",
)
NOT_NULL_COLUMNS = (
    "id", "tenant_id", "conversation_id", "sdk_trace_id", "model", "status",
    "latency_ms", "input_tokens", "output_tokens", "total_tokens", "created_at",
)
SERVER_DEFAULTED = ("latency_ms", "input_tokens", "output_tokens", "total_tokens", "created_at")
INDEXES = (
    ("ix_commerce_v2_shadow_tenant_created", ("tenant_id", "created_at")),
    ("ix_commerce_v2_shadow_conversation_created", ("conversation_id", "created_at")),
    ("ix_commerce_agent_v2_shadow_runs_sdk_trace_id", ("sdk_trace_id",)),
)


# ── infrastructure ─────────────────────────────────────────────────────

def _pg_required() -> bool:
    return (
        (os.getenv("MIGRATION_0106_PG_REQUIRED") or "").strip() == "1"
        or (os.getenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED") or "").strip() == "1"
    )


@pytest.fixture(scope="module")
def admin_engine() -> Iterator[Engine]:
    try:
        engine = connect_engine()
    except pytest.skip.Exception as exc:
        if _pg_required():
            pytest.fail(f"PostgreSQL required for 0106 reconciliation tests: {exc}")
        raise
    if engine.dialect.name != "postgresql":
        pytest.fail("0106 reconciliation tests require PostgreSQL, not SQLite")
    try:
        yield engine
    finally:
        engine.dispose()


def _ephemeral(admin: Engine) -> tuple[str, Engine]:
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    return db_name, engine


def _versions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {str(r[0]) for r in conn.execute(text("SELECT version_num FROM alembic_version"))}


def _table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _oid(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT '{TABLE}'::regclass::oid")).scalar())


def _shape(engine: Engine) -> dict[str, Any]:
    """Everything about the table's physical shape, inspector-derived."""
    insp = inspect(engine)
    return {
        "columns": [
            (c["name"], str(c["type"]), bool(c["nullable"]), c.get("default"))
            for c in insp.get_columns(TABLE)
        ],
        "pk": list(insp.get_pk_constraint(TABLE)["constrained_columns"]),
        "fks": sorted(
            (fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in insp.get_foreign_keys(TABLE)
        ),
        "indexes": sorted(
            (ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in insp.get_indexes(TABLE)
        ),
    }


def _assert_required_shape(shape: dict[str, Any]) -> None:
    """The contract every post-0106 database must satisfy."""
    by_name = {name: (typ, nullable, default) for name, typ, nullable, default in shape["columns"]}
    assert set(by_name) == set(EXPECTED_COLUMNS), sorted(by_name)
    for name in NOT_NULL_COLUMNS:
        assert by_name[name][1] is False, f"{name} must be NOT NULL"
    for name in EXPECTED_COLUMNS:
        if name not in NOT_NULL_COLUMNS:
            assert by_name[name][1] is True, f"{name} must be nullable"
    for name in ("latency_ms", "input_tokens", "output_tokens", "total_tokens"):
        assert str(by_name[name][2]).strip("'") == "0", (name, by_name[name][2])
    assert "now()" in str(by_name["created_at"][2])
    assert by_name["structured_output"][0] == "JSONB"
    assert by_name["sdk_trace_id"][0] == "VARCHAR(64)"
    assert by_name["model"][0] == "VARCHAR(128)"
    assert by_name["status"][0] == "VARCHAR(32)"
    assert by_name["failure_reason"][0] == "VARCHAR(240)"
    assert shape["pk"] == ["id"]
    fk_targets = {(cols, ref_table, ref_cols) for _name, cols, ref_table, ref_cols in shape["fks"]}
    assert (("tenant_id",), "tenants", ("id",)) in fk_targets
    assert (("conversation_id",), "conversations", ("id",)) in fk_targets
    for index_name, columns in INDEXES:
        assert (index_name, columns, False) in shape["indexes"], index_name


def _seed_parent_rows(engine: Engine) -> tuple[int, int]:
    """A tenant and a conversation to hang shadow runs off (raw SQL, no ORM defaults)."""
    with engine.begin() as conn:
        tenant_id = conn.execute(
            text("INSERT INTO tenants (name, is_active) VALUES (:n, true) RETURNING id"),
            {"n": f"t-0106-{os.urandom(3).hex()}"},
        ).scalar()
        customer_id = conn.execute(
            text(
                "INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
                "VALUES (:t, '+966500000106', '+966500000106', 'محمد أحمد') RETURNING id"
            ),
            {"t": tenant_id},
        ).scalar()
        conversation_id = conn.execute(
            text(
                "INSERT INTO conversations (tenant_id, customer_id, status) "
                "VALUES (:t, :c, 'open') RETURNING id"
            ),
            {"t": tenant_id, "c": customer_id},
        ).scalar()
    return int(tenant_id), int(conversation_id)


def _seed_state_b_rows(engine: Engine) -> list[dict[str, Any]]:
    """Real shadow-run rows written through the ORM onto the create_all table."""
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    tenant_id, conversation_id = _seed_parent_rows(engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=True, expire_on_commit=False)()
    try:
        db.add_all([
            CommerceAgentV2ShadowRun(
                tenant_id=tenant_id, conversation_id=conversation_id,
                sdk_trace_id="trace-0106-a", model="claude-sonnet-5", status="completed",
                structured_output={"reply": "أهلاً بك", "intent": "greeting"},
                tool_trace=[{"tool": "lookup_products", "ok": True}],
                guardrail_results={"passed": True},
                latency_ms=812, input_tokens=1200, output_tokens=140, total_tokens=1340,
                failure_reason=None,
                created_at=datetime(2026, 9, 15, 9, 30, tzinfo=timezone.utc),
            ),
            CommerceAgentV2ShadowRun(
                tenant_id=tenant_id, conversation_id=conversation_id,
                sdk_trace_id="trace-0106-b", model="claude-sonnet-5", status="failed",
                structured_output=None, tool_trace=None, guardrail_results=None,
                latency_ms=0, input_tokens=0, output_tokens=0, total_tokens=0,
                failure_reason="timeout",
                created_at=datetime(2026, 9, 15, 9, 31, tzinfo=timezone.utc),
            ),
        ])
        db.commit()
    finally:
        db.close()
    return _raw_rows(engine)


def _raw_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {TABLE} ORDER BY id")).mappings()]


def _state_b(admin: Engine) -> tuple[str, Engine]:
    """0105 schema + the ORM table built by ``Base.metadata.create_all`` — the production shape."""
    db_name, engine = _ephemeral(admin)
    run_alembic(engine, "0105")
    assert TABLE not in _table_names(engine)
    CommerceAgentV2ShadowRun.__table__.create(engine)  # what backend/main.py's create_all does
    assert TABLE in _table_names(engine)
    assert _versions(engine) == {"0105"}
    return db_name, engine


def _planned_upgrade_path(current: str, target: str) -> list[str]:
    """The revisions Alembic will execute for ``upgrade <target>`` from ``current``,
    oldest first, straight from the script directory (what production's
    ``alembic upgrade 0107`` from ``0105`` will run)."""
    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    prev = os.getcwd()
    try:
        os.chdir(_DATABASE)  # script_location in alembic.ini is relative
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        return [rev.revision for rev in reversed(list(script.iterate_revisions(target, current)))]
    finally:
        os.chdir(prev)


class _Forbid0098:
    """Fails loudly if 0098's tenant-settings normalization is ever invoked
    during an upgrade (0098 imports it lazily inside ``upgrade()``)."""

    def __enter__(self) -> "_Forbid0098":
        import core.tenant_config_hygiene as hygiene  # noqa: PLC0415

        self._module = hygiene
        self._orig = hygiene.normalize_all_tenant_settings

        def _boom(*_a, **_k):
            raise AssertionError("0098 normalize_all_tenant_settings must never execute on this path")

        hygiene.normalize_all_tenant_settings = _boom  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        self._module.normalize_all_tenant_settings = self._orig  # type: ignore[assignment]


# ── source contract ────────────────────────────────────────────────────

def test_migration_source_contract() -> None:
    source = MIGRATION_SOURCE.read_text(encoding="utf-8")
    assert 'revision = "0106"' in source
    assert 'down_revision = "0105"' in source
    assert "has_table" in source
    upgrade_src = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "upgrade head" not in upgrade_src
    assert "drop_table" not in upgrade_src
    assert "drop_column" not in upgrade_src
    assert "DELETE FROM" not in source.upper().replace(" ", "")
    # 0107 still chains on 0106 — the revision graph is untouched.
    src_0107 = (_DATABASE / "migrations" / "versions" / "0107_customer_name_provenance.py").read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "0106"' in src_0107


# ── State A: table absent ──────────────────────────────────────────────

def test_state_a_clean_0105_to_0106(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0105")
        assert _versions(engine) == {"0105"}
        assert TABLE not in _table_names(engine)
        run_alembic(engine, "0106")
        assert _versions(engine) == {"0106"}
        assert TABLE in _table_names(engine)
        _assert_required_shape(_shape(engine))
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_state_a_clean_0105_to_0107_runs_only_0106_and_0107(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0105")
        assert TABLE not in _table_names(engine)
        assert PROVENANCE not in _table_names(engine)
        assert _planned_upgrade_path("0105", "0107") == ["0106", "0107"]
        with _Forbid0098():
            run_alembic(engine, "0107")
        assert _versions(engine) == {"0107"}
        assert TABLE in _table_names(engine)
        assert PROVENANCE in _table_names(engine)
        _assert_required_shape(_shape(engine))
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_state_a_downgrade_0106_to_0105_drops_table(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0106")
        downgrade_alembic(engine, "0105")
        assert _versions(engine) == {"0105"}
        assert TABLE not in _table_names(engine)
        run_alembic(engine, "0106")  # and forward again
        assert _versions(engine) == {"0106"}
        _assert_required_shape(_shape(engine))
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


# ── Negative control: the unguarded create_table really breaks State B ──

def test_control_unguarded_create_table_fails_when_create_all_already_ran(admin_engine: Engine) -> None:
    """The pre-reconciliation 0106 body (bare ``op.create_table``) raises
    ``DuplicateTable`` on the create_all shape — the hazard is real."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("migration_0106_under_test", MIGRATION_SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    db_name, engine = _state_b(admin_engine)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                with pytest.raises(ProgrammingError) as raised:
                    module._create_fresh()
            assert "already exists" in str(raised.value)
            conn.rollback()
        assert _versions(engine) == {"0105"}
        assert TABLE in _table_names(engine)
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


# ── State B: table exists via Base.metadata.create_all ─────────────────

def test_state_b_create_all_then_0105_to_0107_preserves_rows_and_reaches_parity(admin_engine: Engine) -> None:
    """The exact production state: alembic 0105, shadow-runs table built by
    create_all (with live rows), provenance table absent."""
    a_name, a_engine = _ephemeral(admin_engine)
    b_name, b_engine = _state_b(admin_engine)
    try:
        run_alembic(a_engine, "0107")
        state_a = _shape(a_engine)

        # create_all shape: no server defaults (ORM defaults are Python-side).
        before = _shape(b_engine)
        defaults_before = {name: d for name, _t, _n, d in before["columns"] if name in SERVER_DEFAULTED}
        assert all(d is None for d in defaults_before.values()), defaults_before

        rows_before = _seed_state_b_rows(b_engine)
        assert len(rows_before) == 2
        oid_before = _oid(b_engine)
        assert PROVENANCE not in _table_names(b_engine)

        assert _planned_upgrade_path("0105", "0107") == ["0106", "0107"]
        with _Forbid0098():
            run_alembic(b_engine, "0107")

        assert _versions(b_engine) == {"0107"}
        assert _oid(b_engine) == oid_before, "table must not be dropped and re-created"
        assert _raw_rows(b_engine) == rows_before, "existing rows must survive unchanged"
        assert PROVENANCE in _table_names(b_engine), "0107 must still create customer_name_provenance"

        state_b = _shape(b_engine)
        _assert_required_shape(state_b)
        assert state_b == state_a, "State B must end in exactly the State A schema"
    finally:
        a_engine.dispose()
        b_engine.dispose()
        drop_ephemeral_database(admin_engine, a_name)
        drop_ephemeral_database(admin_engine, b_name)


def test_state_b_reconciled_table_accepts_raw_inserts_with_server_defaults(admin_engine: Engine) -> None:
    db_name, engine = _state_b(admin_engine)
    try:
        _seed_state_b_rows(engine)
        run_alembic(engine, "0106")
        tenant_id, conversation_id = _seed_parent_rows(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {TABLE} (tenant_id, conversation_id, sdk_trace_id, model, status) "
                    "VALUES (:t, :c, 'trace-raw', 'claude-sonnet-5', 'completed')"
                ),
                {"t": tenant_id, "c": conversation_id},
            )
            raw = conn.execute(
                text(
                    f"SELECT latency_ms, input_tokens, output_tokens, total_tokens, created_at "
                    f"FROM {TABLE} WHERE sdk_trace_id = 'trace-raw'"
                )
            ).mappings().one()
        assert (raw["latency_ms"], raw["input_tokens"], raw["output_tokens"], raw["total_tokens"]) == (0, 0, 0, 0)
        assert raw["created_at"] is not None

        # Foreign keys are enforced on the reconciled table.
        with pytest.raises(Exception) as raised:  # noqa: B017 — IntegrityError wrapped by the driver
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"INSERT INTO {TABLE} (tenant_id, conversation_id, sdk_trace_id, model, status) "
                        "VALUES (:t, 999999999, 'trace-bad-fk', 'm', 's')"
                    ),
                    {"t": tenant_id},
                )
        assert "violates foreign key constraint" in str(raised.value)
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_state_b_partial_drift_is_reconciled_additively(admin_engine: Engine) -> None:
    """A create_all table that has since lost columns / index / FK (older ORM
    shape) is completed, not rebuilt; rows keep their values and NULLs in
    newly NOT NULL columns get the defaults."""
    db_name, engine = _state_b(admin_engine)
    try:
        rows_before = _seed_state_b_rows(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_commerce_agent_v2_shadow_runs_sdk_trace_id"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_conversation_id_fkey"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN failure_reason"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN total_tokens"))
            # Dropping created_at also drops both (…, created_at) indexes.
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN created_at"))
            conn.execute(text(f"ALTER TABLE {TABLE} ALTER COLUMN latency_ms DROP NOT NULL"))
            conn.execute(text(f"UPDATE {TABLE} SET latency_ms = NULL WHERE sdk_trace_id = 'trace-0106-b'"))
        assert {ix["name"] for ix in inspect(engine).get_indexes(TABLE)} == set()
        oid_before = _oid(engine)

        run_alembic(engine, "0106")

        assert _versions(engine) == {"0106"}
        shape = _shape(engine)
        _assert_required_shape(shape)
        assert _oid(engine) == oid_before
        rows_after = _raw_rows(engine)
        assert len(rows_after) == len(rows_before)
        for before, after in zip(rows_before, rows_after):
            # Untouched columns keep their exact values …
            for key, value in before.items():
                if key in {"failure_reason", "total_tokens", "created_at", "latency_ms"}:
                    continue
                assert after[key] == value, key
            # … and the reconciled ones carry the migration's defaults, never NULL.
            assert after["total_tokens"] == 0
            assert after["created_at"] is not None
            assert after["failure_reason"] is None
            assert after["latency_ms"] == (0 if before["sdk_trace_id"] == "trace-0106-b" else before["latency_ms"])
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


NO_SAFE_FILL_COLUMNS = ("tenant_id", "conversation_id", "sdk_trace_id", "model", "status")


def _assert_shape_except_nullable(shape: dict[str, Any], nullable_allowed: set[str]) -> None:
    """Required shape, except that the named no-safe-fill columns may be nullable."""
    by_name = {name: (typ, nullable, default) for name, typ, nullable, default in shape["columns"]}
    assert set(by_name) == set(EXPECTED_COLUMNS), sorted(by_name)
    for name in NOT_NULL_COLUMNS:
        if name in nullable_allowed:
            continue
        assert by_name[name][1] is False, f"{name} must be NOT NULL"
    for name in ("latency_ms", "input_tokens", "output_tokens", "total_tokens"):
        assert str(by_name[name][2]).strip("'") == "0", (name, by_name[name][2])
    assert "now()" in str(by_name["created_at"][2])
    assert shape["pk"] == ["id"]
    fk_targets = {(cols, ref_table, ref_cols) for _name, cols, ref_table, ref_cols in shape["fks"]}
    assert (("tenant_id",), "tenants", ("id",)) in fk_targets
    assert (("conversation_id",), "conversations", ("id",)) in fk_targets
    for index_name, columns in INDEXES:
        assert (index_name, columns, False) in shape["indexes"], index_name


def test_state_b_drift_missing_no_safe_fill_columns_is_restored_nullable(admin_engine: Engine) -> None:
    """Populated create_all table missing ``model`` and ``sdk_trace_id`` (no
    safe fill value): the upgrade must still succeed, re-add them NULLABLE
    (existing rows keep NULL there — no data is invented), keep every other
    row value, and still restore all defaults, FKs and indexes. Exact
    State A parity is documented as out of reach for this shape."""
    a_name, a_engine = _ephemeral(admin_engine)
    db_name, engine = _state_b(admin_engine)
    try:
        run_alembic(a_engine, "0107")
        state_a = _shape(a_engine)

        rows_before = _seed_state_b_rows(engine)
        with engine.begin() as conn:
            # Dropping sdk_trace_id also drops its index; dropping created_at
            # would drop the other two — keep created_at so this test isolates
            # the no-safe-fill behaviour.
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN model"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN sdk_trace_id"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_tenant_id_fkey"))
        oid_before = _oid(engine)
        assert {ix["name"] for ix in inspect(engine).get_indexes(TABLE)} == {
            "ix_commerce_v2_shadow_tenant_created", "ix_commerce_v2_shadow_conversation_created",
        }

        with _Forbid0098():
            run_alembic(engine, "0107")

        assert _versions(engine) == {"0107"}
        assert _oid(engine) == oid_before
        assert PROVENANCE in _table_names(engine)
        shape = _shape(engine)
        _assert_shape_except_nullable(shape, {"model", "sdk_trace_id"})
        by_name = {name: nullable for name, _t, nullable, _d in shape["columns"]}
        assert by_name["model"] is True, "no safe fill → restored nullable"
        assert by_name["sdk_trace_id"] is True, "no safe fill → restored nullable"
        # Everything that CAN be reconciled is: defaults, both FKs, all three indexes.
        assert {ix[0] for ix in shape["indexes"]} == {name for name, _c in INDEXES}
        assert {fk[2] for fk in shape["fks"]} == {"tenants", "conversations"}
        # The only differences from State A are the two nullable flags.
        a_cols = {name: (typ, nullable, default) for name, typ, nullable, default in state_a["columns"]}
        b_cols = {name: (typ, nullable, default) for name, typ, nullable, default in shape["columns"]}
        for name in EXPECTED_COLUMNS:
            if name in {"model", "sdk_trace_id"}:
                assert a_cols[name][0] == b_cols[name][0] and a_cols[name][2] == b_cols[name][2], name
            else:
                assert a_cols[name] == b_cols[name], name
        assert shape["fks"] == state_a["fks"] and shape["indexes"] == state_a["indexes"] and shape["pk"] == state_a["pk"]

        rows_after = _raw_rows(engine)
        assert len(rows_after) == len(rows_before)
        for before, after in zip(rows_before, rows_after):
            for key, value in before.items():
                if key in {"model", "sdk_trace_id"}:
                    assert after[key] is None, key  # restored empty, never invented
                else:
                    assert after[key] == value, key
    finally:
        a_engine.dispose()
        engine.dispose()
        drop_ephemeral_database(admin_engine, a_name)
        drop_ephemeral_database(admin_engine, db_name)


def test_state_b_drift_nullable_no_safe_fill_column_with_nulls_stays_nullable(admin_engine: Engine) -> None:
    """A populated table whose ``status`` is nullable and actually holds NULLs:
    the upgrade must not force NOT NULL (it would fail) and must not invent
    a value; everything else is reconciled."""
    db_name, engine = _state_b(admin_engine)
    try:
        rows_before = _seed_state_b_rows(engine)
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {TABLE} ALTER COLUMN status DROP NOT NULL"))
            conn.execute(text(f"UPDATE {TABLE} SET status = NULL WHERE sdk_trace_id = 'trace-0106-b'"))
            conn.execute(text("DROP INDEX ix_commerce_v2_shadow_tenant_created"))
        oid_before = _oid(engine)
        rows_drifted = _raw_rows(engine)

        with _Forbid0098():
            run_alembic(engine, "0107")

        assert _versions(engine) == {"0107"}
        assert _oid(engine) == oid_before
        shape = _shape(engine)
        _assert_shape_except_nullable(shape, {"status"})
        assert {name: n for name, _t, n, _d in shape["columns"]}["status"] is True
        assert _raw_rows(engine) == rows_drifted, "rows untouched; NULL status not invented"
        assert len(rows_before) == len(rows_drifted)
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


# ── Re-runs are no-ops ─────────────────────────────────────────────────

def test_already_at_0106_is_a_noop(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0106")
        shape = _shape(engine)
        oid = _oid(engine)
        assert _planned_upgrade_path("0106", "0106") == []
        run_alembic(engine, "0106")
        assert _versions(engine) == {"0106"}
        assert _shape(engine) == shape and _oid(engine) == oid
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_already_at_0107_is_a_noop(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0107")
        shape = _shape(engine)
        assert _planned_upgrade_path("0107", "0107") == []
        run_alembic(engine, "0107")
        assert _versions(engine) == {"0107"}
        assert _shape(engine) == shape
        assert PROVENANCE in _table_names(engine)
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)
