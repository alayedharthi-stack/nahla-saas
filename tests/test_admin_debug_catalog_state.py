"""
tests/test_admin_debug_catalog_state.py
───────────────────────────────────────
Locks the F-Catalog-1 diagnostic endpoint:

    GET /admin/debug/catalog-state?tenant_id=<n>&sample=<n>

This is the readonly tool support uses when a merchant reports
"the bot still sends image+CTA instead of a Catalog Product Card".
The Phase 4 wire-up in ``whatsapp_webhook._try_send_catalog_product``
is intentionally silent on eligibility miss — the legacy path
renders the product and the failure mode is invisible from
outside. This endpoint mirrors the exact eligibility check the
webhook performs, plus a column-presence probe for migration
0061, plus a sample of products with their resolved retailer
ids.

Why test the endpoint and not the helpers
─────────────────────────────────────────
The helpers (``effective_retailer_id`` / ``is_catalog_eligible`` /
``catalog_summary``) are already locked by ``test_catalog_sender.py``.
The endpoint is the public contract: an operator screenshots the
JSON and acts on the ``advice`` line. So we assert against the
response shape and the advice text, not against the helpers
themselves.

What's covered
──────────────
* Happy path — eligible connection + products with external_id →
  ``eligibility.ok == True``, advice points at log greps.
* ``catalog_enabled=False`` → reason=``catalog_disabled``, advice
  names the flag to flip.
* ``meta_catalog_id`` empty → reason=``catalog_id_missing``, advice
  names the field to populate.
* No connection at all → ``connection.found == False``, advice
  mentions onboarding (NOT a code change).
* Products sample with mixed retailer-id coverage → counters add up
  and the sample list reflects ``effective_retailer_id`` resolution.
* Missing migration 0061 columns → advice flags the migration
  explicitly, AND the existing ``_CRITICAL_COLUMNS`` registry
  includes all four new columns so ``db-schema-health`` catches
  the same regression.
* Tenant isolation — a row for tenant A does not bleed into
  tenant B's response.

NOT covered (intentional)
─────────────────────────
* Actual auth / JWT validation — we stub ``require_admin`` the
  same way ``test_db_schema_health.py`` does. The auth contract
  is locked by its own suite.
* Real Postgres ``information_schema`` — SQLite has no such view.
  We assert that the endpoint's ``schema`` block reports the
  correct status given a mockable execute hook.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _run(coro):
    return asyncio.run(coro)


def _make_db():
    """In-memory SQLite mirroring the rest of the suite. JSONB→JSON
    downgrade is applied during ``create_all`` and reverted right
    after so the rest of the test process behaves normally."""
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base

    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


def _admin_payload():
    """Return the dict shape ``require_admin`` produces on success.
    Mirrors the stub used in ``test_db_schema_health.py``."""
    return {"user_id": 1, "role": "admin"}


def _patch_info_schema_columns(db, present_columns):
    """Patch ``db.execute`` so ``information_schema.columns`` probes
    succeed deterministically on SQLite (which has no such view).

    Only intercepts the probe queries — every other ``execute`` call
    (ORM-driven inserts, selects, joins, ...) flows through to the
    real session with all original args/kwargs intact, so ORM
    behaviour is undisturbed.

    ``present_columns`` is a set of ``(table, column)`` tuples that
    should report "present". Any other pair returns "missing"."""
    original_execute = db.execute

    def _patched_execute(sql, *args, **kwargs):
        sql_text = str(sql).lower()
        if "information_schema.columns" in sql_text:
            # First positional arg (or ``parameters`` kwarg) carries
            # ``{"t": ..., "c": ...}`` from the endpoint.
            params = (args[0] if args else kwargs.get("parameters")) or {}
            t = params.get("t") or params.get("table_name")
            c = params.get("c") or params.get("column_name")

            class _R:
                def __init__(self, value):
                    self._v = value
                def first(self):
                    return self._v
                def __iter__(self):
                    return iter([self._v] if self._v else [])

            return _R(("present",) if (t, c) in present_columns else None)
        return original_execute(sql, *args, **kwargs)

    db.execute = _patched_execute  # type: ignore[assignment]
    return db


def _seed_tenant(db, *, tenant_id=42):
    from models import Tenant
    t = Tenant(id=tenant_id, name=f"tenant-{tenant_id}")
    db.add(t); db.commit()
    return t


def _seed_connection(
    db,
    *,
    tenant_id=42,
    catalog_enabled=True,
    meta_catalog_id="cat_123456",
    phone_number_id="100543193146977",
):
    from models import WhatsAppConnection
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        provider="meta",
        connection_type="direct",
        status="connected",
        phone_number_id=phone_number_id,
        access_token="fake_token",
        catalog_enabled=catalog_enabled,
        meta_catalog_id=meta_catalog_id,
    )
    db.add(conn); db.commit()
    return conn


def _seed_product(
    db,
    *,
    tenant_id=42,
    title,
    external_id=None,
    meta_retailer_id=None,
):
    from models import Product
    p = Product(
        tenant_id=tenant_id,
        title=title,
        external_id=external_id,
        meta_retailer_id=meta_retailer_id,
        price="50.00",
    )
    db.add(p); db.commit()
    return p


def _all_columns_present():
    """The four columns added by migration 0061."""
    return {
        ("whatsapp_connections", "meta_catalog_id"),
        ("whatsapp_connections", "catalog_enabled"),
        ("products", "meta_retailer_id"),
        ("products", "meta_catalog_published_at"),
    }


def _invoke_handler(*, db, tenant_id=42, sample=5):
    """Call the async endpoint handler directly, mirroring the
    pattern used in ``test_admin_debug_inbound_trace.py``."""
    from routers.admin_debug import admin_debug_catalog_state
    return _run(
        admin_debug_catalog_state(
            tenant_id=tenant_id,
            sample=sample,
            db=db,
            _admin=_admin_payload(),
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Critical columns registry — migration 0061 entries are present
# ─────────────────────────────────────────────────────────────────────────────

class TestCriticalColumnsRegistry:
    """Migration 0061's columns MUST be in ``_CRITICAL_COLUMNS`` so the
    existing ``GET /admin/debug/db-schema-health`` endpoint catches a
    regression the same way it catches missing 0054 columns."""

    def test_phase4_columns_registered(self):
        from routers.admin_debug import _CRITICAL_COLUMNS
        names = {(c["table"], c["column"]) for c in _CRITICAL_COLUMNS}
        assert ("whatsapp_connections", "meta_catalog_id")           in names
        assert ("whatsapp_connections", "catalog_enabled")           in names
        assert ("products",             "meta_retailer_id")          in names
        assert ("products",             "meta_catalog_published_at") in names

    def test_phase4_columns_have_added_by_0061(self):
        from routers.admin_debug import _CRITICAL_COLUMNS
        phase4 = [
            c for c in _CRITICAL_COLUMNS
            if (c["table"], c["column"]) in {
                ("whatsapp_connections", "meta_catalog_id"),
                ("whatsapp_connections", "catalog_enabled"),
                ("products", "meta_retailer_id"),
                ("products", "meta_catalog_published_at"),
            }
        ]
        assert len(phase4) == 4
        assert all(c["added_by"] == "0061" for c in phase4)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Endpoint shape contract
# ─────────────────────────────────────────────────────────────────────────────

class TestResponseShape:
    """Every response — happy or sad — must include the same top-level
    keys so operator tooling never has to optional-chain."""

    def test_all_top_level_keys_present(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        _seed_product(db, title="عسل سمر", external_id="EXT-1")
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)

        for key in [
            "tenant_id",
            "connection",
            "eligibility",
            "schema",
            "products_sample",
            "products_sample_retailer_id_coverage",
            "advice",
        ]:
            assert key in body, f"missing key: {key}"

    def test_phone_id_is_masked_to_last_four_digits(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, phone_number_id="100543193146977")
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)

        # The endpoint exposes only the last 4 digits.
        assert body["connection"]["phone_id_tail"] == "6977"
        # The full id never appears anywhere in the response.
        import json
        blob = json.dumps(body)
        assert "100543193146977" not in blob

    def test_connection_status_is_surfaced(self):
        """``status`` is a closed enum (not_connected | pending |
        connected | error | disconnected | needs_reauth) — surfaced
        verbatim so operators can correlate with the channel state."""
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)  # status defaults to "connected"
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)
        assert body["connection"]["status"] == "connected"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Eligibility outcomes drive the advice
# ─────────────────────────────────────────────────────────────────────────────

class TestEligibilityAdvice:
    """The ``advice`` string is what an operator copies into their
    next action. We pin the deterministic mapping here."""

    def test_happy_path_ok_with_logs_hint(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, catalog_enabled=True, meta_catalog_id="cat_abc")
        _seed_product(db, title="عسل سمر", external_id="EXT-1")
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)

        assert body["eligibility"]["ok"] is True
        assert body["eligibility"]["reason"] == "ok"
        # The advice for the OK case should point operators at the
        # actual logs to chase a tail failure, not at a config change.
        assert "[CATALOG_SEND_FAILED]" in body["advice"]
        # No misleading "fix this flag" text on the happy path.
        assert "catalog_enabled=false" not in body["advice"]

    def test_catalog_disabled_advice_names_the_flag(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, catalog_enabled=False, meta_catalog_id="cat_abc")
        _seed_product(db, title="عسل سمر", external_id="EXT-1")
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)

        assert body["eligibility"]["ok"] is False
        assert body["eligibility"]["reason"] == "catalog_disabled"
        assert "catalog_enabled" in body["advice"]
        assert body["connection"]["catalog_enabled"] is False

    def test_catalog_id_missing_advice_names_the_field(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db, catalog_enabled=True, meta_catalog_id="")
        _seed_product(db, title="عسل سمر", external_id="EXT-1")
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)

        assert body["eligibility"]["ok"] is False
        assert body["eligibility"]["reason"] == "catalog_id_missing"
        assert "meta_catalog_id" in body["advice"]
        assert body["connection"]["meta_catalog_id"] is None

    def test_connection_missing_returns_safe_block(self):
        """No WhatsAppConnection at all — the endpoint must NOT 500.
        It returns the same shape with ``connection.found=False`` and
        an advice line that names the onboarding step."""
        db = _make_db()
        _seed_tenant(db)
        # Note: no _seed_connection call.
        _seed_product(db, title="عسل سمر", external_id="EXT-1")
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)

        assert body["connection"]["found"] is False
        assert body["eligibility"]["ok"] is False
        assert body["eligibility"]["reason"] == "connection_missing"
        assert "WhatsAppConnection" in body["advice"] or "onboarding" in body["advice"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Products sample + retailer-id coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestProductsSample:
    def test_sample_respects_limit_and_resolves_retailer_id(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        # 3 products: 2 with external_id, 1 with explicit meta_retailer_id.
        _seed_product(db, title="P1", external_id="EXT-1")
        _seed_product(db, title="P2", external_id="EXT-2")
        _seed_product(db, title="P3", external_id=None, meta_retailer_id="META-3")
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db, sample=2)

        # sample=2 → we get at most 2 rows back.
        assert len(body["products_sample"]) == 2
        # The first two products were seeded with external_id only,
        # so effective_retailer_id should mirror those values.
        retailer_ids = {p["effective_retailer_id"] for p in body["products_sample"]}
        assert retailer_ids == {"EXT-1", "EXT-2"}

    def test_coverage_counters_match_sample(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        # 2 with retailer, 2 without — sample size 4 covers all of them.
        _seed_product(db, title="P1", external_id="EXT-1")
        _seed_product(db, title="P2", external_id="EXT-2")
        _seed_product(db, title="P3", external_id=None, meta_retailer_id=None)
        _seed_product(db, title="P4", external_id="", meta_retailer_id=None)
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db, sample=4)

        cov = body["products_sample_retailer_id_coverage"]
        assert cov["with_retailer_id"]    == 2
        assert cov["without_retailer_id"] == 2

    def test_meta_retailer_id_takes_precedence_over_external_id(self):
        """Mirrors ``effective_retailer_id``: when meta_retailer_id is
        set, it wins over external_id."""
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        _seed_product(
            db,
            title="P-override",
            external_id="EXT-1",
            meta_retailer_id="META-OVERRIDE",
        )
        _patch_info_schema_columns(db, _all_columns_present())

        body = _invoke_handler(db=db)

        sample = body["products_sample"]
        assert len(sample) == 1
        assert sample[0]["effective_retailer_id"] == "META-OVERRIDE"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Schema-probe + advice when migration didn't apply
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaProbe:
    """When the SQLite test rig is told that none of the migration 0061
    columns are present, the schema block reports them all as
    "missing" and the advice flips to "run the migration"."""

    def test_all_missing_routes_advice_to_migration(self):
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        _seed_product(db, title="عسل سمر", external_id="EXT-1")
        # Empty set → every probe returns None → "missing".
        _patch_info_schema_columns(db, present_columns=set())

        body = _invoke_handler(db=db)

        for key in [
            "whatsapp_connections.meta_catalog_id",
            "whatsapp_connections.catalog_enabled",
            "products.meta_retailer_id",
            "products.meta_catalog_published_at",
        ]:
            assert body["schema"][key] == "missing", key
        # Advice must call out the missing migration so the operator
        # doesn't try to flip flags on columns that don't exist yet.
        assert "0061" in body["advice"]

    def test_some_present_some_missing_still_flags_migration(self):
        """Partial-apply scenarios are rare but possible after a
        crash mid-upgrade. The advice still routes to the migration
        because at least one expected column is absent."""
        db = _make_db()
        _seed_tenant(db)
        _seed_connection(db)
        _seed_product(db, title="عسل سمر", external_id="EXT-1")
        present = {
            ("whatsapp_connections", "meta_catalog_id"),
            ("whatsapp_connections", "catalog_enabled"),
            # products.* columns missing.
        }
        _patch_info_schema_columns(db, present_columns=present)

        body = _invoke_handler(db=db)

        assert body["schema"]["whatsapp_connections.meta_catalog_id"] == "present"
        assert body["schema"]["products.meta_retailer_id"]            == "missing"
        assert "0061" in body["advice"]


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tenant isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestTenantIsolation:
    def test_other_tenant_data_does_not_bleed_in(self):
        db = _make_db()
        _seed_tenant(db, tenant_id=42)
        _seed_tenant(db, tenant_id=99)
        # Tenant 42: enabled. Tenant 99: disabled, must NOT affect 42.
        _seed_connection(db, tenant_id=42, catalog_enabled=True,  meta_catalog_id="A")
        _seed_connection(db, tenant_id=99, catalog_enabled=False, meta_catalog_id="")
        _seed_product(db, tenant_id=42, title="P-42", external_id="EXT-42")
        _seed_product(db, tenant_id=99, title="P-99", external_id=None)
        _patch_info_schema_columns(db, _all_columns_present())

        body42 = _invoke_handler(db=db, tenant_id=42)
        body99 = _invoke_handler(db=db, tenant_id=99)

        assert body42["eligibility"]["ok"] is True
        assert body99["eligibility"]["ok"] is False
        # Sample lists are tenant-scoped.
        titles42 = [p["title"] for p in body42["products_sample"]]
        titles99 = [p["title"] for p in body99["products_sample"]]
        assert "P-42" in titles42 and "P-99" not in titles42
        assert "P-99" in titles99 and "P-42" not in titles99
