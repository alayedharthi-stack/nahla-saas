"""tests/test_google_merchant_models.py
─────────────────────────────────────
Pure (no database) checks of the dormant Google Merchant schema slice:

* the tables live on their own ``GoogleMerchantBase`` registry, never on the
  application's ``models.Base`` (startup ``create_all`` cannot build them);
* nothing in the application imports the module; the migration is
  self-contained;
* revision 0124 declares exactly the model's DDL (compiled for PostgreSQL)
  and byte-identical guard functions, chained on 0123;
* no transport, credential or live identifier appears in either file.

Every database invariant is proven on PostgreSQL in
``backend/tests/test_google_merchant_schema_pg.py``.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from services import google_merchant_models as gm  # noqa: E402

MODEL_PATH = REPO_ROOT / "backend/services/google_merchant_models.py"
MIGRATION_PATH = REPO_ROOT / "database/migrations/versions/0124_google_merchant_ledger.py"


def _migration():
    spec = importlib.util.spec_from_file_location("google_merchant_migration_0124", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ddl(table) -> list:
    dialect = postgresql.dialect()
    statements = [str(CreateTable(table).compile(dialect=dialect)).strip()]
    statements += sorted(str(CreateIndex(i).compile(dialect=dialect)).strip() for i in table.indexes)
    return statements


def test_tables_live_outside_the_application_metadata():
    from models import Base

    assert set(gm.GOOGLE_MERCHANT_TABLE_NAMES) == {"google_merchant_connections", "google_merchant_offer_ledger"}
    assert not set(gm.GOOGLE_MERCHANT_TABLE_NAMES) & set(Base.metadata.tables)
    assert gm.GoogleMerchantBase.metadata is not Base.metadata
    # Only the reference stub for the tenant foreign key besides the two tables.
    assert set(gm.GoogleMerchantBase.metadata.tables) == set(gm.GOOGLE_MERCHANT_TABLE_NAMES) | {"tenants"}


def _imported_names(source: str) -> set:
    """Every imported module path, in both ``import`` and ``from ... import`` forms.

    ``from services import google_merchant_models`` yields
    ``services.google_merchant_models``; relative imports keep their dots.
    """
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            names.add(module)
            names.update(f"{module}.{a.name}" if module else a.name for a in node.names)
    return names


def _imports(path: Path) -> set:
    # utf-8-sig strips a leading BOM; any other decode or parse error fails.
    return _imported_names(path.read_text(encoding="utf-8-sig"))


def _names_the_module(names: set) -> bool:
    return any("google_merchant_models" in name.split(".") for name in names)


@pytest.mark.parametrize("source", [
    "import services.google_merchant_models\n",
    "import services.google_merchant_models as gm\n",
    "from services.google_merchant_models import GoogleMerchantBase\n",
    "from services import google_merchant_models\n",
    "from services import (\n    other_module,\n    google_merchant_models,\n)\n",
    "from .google_merchant_models import create_google_merchant_tables\n",
])
def test_import_detector_covers_every_import_form(source):
    assert _names_the_module(_imported_names(source))


def test_a_file_with_a_leading_bom_is_parsed_not_skipped(tmp_path):
    path = tmp_path / "bom.py"
    path.write_bytes(b"\xef\xbb\xbf" + b"from services import (\n    google_merchant_models,\n)\n")
    assert _names_the_module(_imports(path))
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        _imports(broken)


def test_import_detector_ignores_text_mentions():
    assert not _names_the_module(_imported_names('"""see backend/services/google_merchant_models.py"""\n'))


def test_no_application_code_imports_the_module_and_the_migration_is_self_contained():
    offenders, scanned = [], 0
    for root in (REPO_ROOT / "backend", REPO_ROOT / "database", REPO_ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith("backend/tests/") or path == MODEL_PATH:
                continue
            scanned += 1
            if _names_the_module(_imports(path)):  # a parse failure fails the test
                offenders.append(rel)
    assert scanned > 100
    assert offenders == []
    assert not any(name.startswith(("services", "models", "backend")) for name in _imports(MIGRATION_PATH))


def test_migration_is_chained_on_0123():
    module = _migration()
    assert (module.revision, module.down_revision) == ("0124", "0123")
    assert module.branch_labels is None and module.depends_on is None
    assert module.TABLES == gm.GOOGLE_MERCHANT_TABLE_NAMES
    assert module.FUNCTIONS == gm.GOOGLE_MERCHANT_GUARD_FUNCTIONS


def test_migration_ddl_equals_model_ddl():
    connections, ledger = _migration()._definitions()
    assert _ddl(connections) == _ddl(gm.GoogleMerchantConnection.__table__)
    assert _ddl(ledger) == _ddl(gm.GoogleMerchantOfferLedger.__table__)


def test_guard_functions_are_byte_identical():
    module = _migration()
    assert module.CONNECTIONS_GUARD_SQL == gm.CONNECTIONS_GUARD_SQL
    assert module.LEDGER_GUARD_SQL == gm.LEDGER_GUARD_SQL
    # psycopg2 formats '%' when parameters are passed; the DDL carries none.
    assert "%" not in gm.CONNECTIONS_GUARD_SQL + gm.LEDGER_GUARD_SQL


def test_declared_invariants_are_present():
    connections = {c.name for c in gm.GoogleMerchantConnection.__table__.constraints}
    assert {
        "uq_google_merchant_connections_tenant", "uq_google_merchant_connections_account",
        "uq_google_merchant_connections_account_source", "uq_google_merchant_connections_scope",
        "ck_google_merchant_connections_account_id", "ck_google_merchant_connections_data_source_id",
        "ck_google_merchant_connections_bound", "ck_google_merchant_connections_credential",
    } <= connections
    ledger_table = gm.GoogleMerchantOfferLedger.__table__
    ledger = {c.name for c in ledger_table.constraints}
    assert {
        "fk_google_merchant_offer_ledger_scope", "uq_google_merchant_offer_ledger_identity",
        "ck_google_merchant_offer_ledger_op_shape", "ck_google_merchant_offer_ledger_op_snapshot_hash",
        "ck_google_merchant_offer_ledger_lease", "ck_google_merchant_offer_ledger_generations",
    } <= ledger
    identity = next(c for c in ledger_table.constraints if c.name == "uq_google_merchant_offer_ledger_identity")
    # Offer identity is independent of data source and connection.
    assert [c.name for c in identity.columns] == ["account_id", "content_language", "feed_label", "offer_id"]
    scope = next(c for c in ledger_table.constraints if c.name == "fk_google_merchant_offer_ledger_scope")
    assert (scope.match, scope.onupdate, scope.ondelete) == ("FULL", "RESTRICT", "RESTRICT")
    scope_columns = [c.name for c in scope.columns]
    assert scope_columns == ["connection_id", "tenant_id", "account_id", "data_source_id",
                             "content_language", "feed_label"]
    assert all(not ledger_table.c[name].nullable for name in scope_columns + ["offer_id"])
    assert "legacyLocal" not in MODEL_PATH.read_text(encoding="utf-8").replace("no ``legacyLocal``", "")


_FORBIDDEN_IMPORTS = ("socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "cryptography",
                      "os", "time", "asyncio", "threading", "fastapi")


@pytest.mark.parametrize("path", [MODEL_PATH, MIGRATION_PATH], ids=lambda p: p.name)
def test_no_transport_crypto_clock_or_live_identifier(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _imports(path):
        assert name.split(".")[0] not in _FORBIDDEN_IMPORTS, name
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)) and not isinstance(node.value, bool):
            text = str(node.value)
            if text == "0123456789":  # the ordered digit alphabet of the CHECK constraints
                continue
            assert not (text.isdigit() and len(text) >= 8), f"id-like literal {text!r}"
