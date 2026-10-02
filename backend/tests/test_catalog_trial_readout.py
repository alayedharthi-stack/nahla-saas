"""Read-only trial readout: DB facts, candidate selection, Graph GET-only reads, no secrets.

Generic merchant data (متجر تجريبي عام). Asserts structure and behaviour, never
Arabic wording.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import (  # noqa: E402
    Base,
    MetaCatalogMembership,
    Product,
    ProductVariant,
    Tenant,
    WhatsAppConnection,
)
from core.catalog import OWNERSHIP_EXTERNAL_MANAGED  # noqa: E402
from services.catalog_trial_readout import build_catalog_trial_readout  # noqa: E402

SECRET = "EAAB-secret-token-do-not-print"


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeGraph:
    """GET-only Graph double; any POST is a test failure."""

    def __init__(self, *, waba="WABA-900", catalog="CAT-900", business="BM-900", perm="granted", live=None):
        self.waba, self.catalog, self.business, self.perm = waba, catalog, business, perm
        self.live = live or {}
        self.calls = []

    def _route(self, method, url, params):
        self.calls.append((method, url))
        if method != "GET":
            raise AssertionError(f"readout must not {method} {url}")
        if url.endswith("/me/permissions"):
            return _Resp(200, {"data": [{"permission": "catalog_management", "status": self.perm},
                                        {"permission": "whatsapp_business_management", "status": "granted"}]})
        if url.endswith(f"/{self.waba}/product_catalogs"):
            return _Resp(200, {"data": [{"id": self.catalog, "name": "متجر تجريبي عام"}]})
        if url.endswith(f"/{self.waba}"):
            return _Resp(200, {"id": self.waba, "owner_business_info": {"id": self.business, "name": "BM"}})
        if url.endswith(f"/{self.catalog}/products"):
            rows = [dict(v, retailer_id=k) for k, v in self.live.items()]
            return _Resp(200, {"data": rows, "paging": {}})
        if url.endswith(f"/{self.catalog}"):
            return _Resp(200, {"id": self.catalog, "name": "متجر تجريبي عام", "product_count": len(self.live),
                               "business": {"id": self.business, "name": "BM"}})
        return _Resp(404, {"error": {"code": 803, "message": "unknown"}})

    def get(self, url, params=None, headers=None):
        return self._route("GET", url, params)

    def post(self, url, data=None, headers=None, params=None):
        return self._route("POST", url, params)

    def request(self, method, url, params=None, data=None, headers=None, json=None):
        return self._route(method, url, params)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_db(*, catalog_enabled=True, catalog_id="CAT-900"):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    other = Tenant(name="متجر آخر", is_active=True)
    session.add_all([tenant, other]); session.commit()
    session.add(WhatsAppConnection(
        tenant_id=tenant.id, whatsapp_business_account_id="WABA-900", phone_number_id="PN-900",
        access_token=SECRET, meta_catalog_id=catalog_id, catalog_enabled=catalog_enabled,
        provider="meta", connection_type="embedded", extra_metadata={"meta_catalog_bind": {"ok": True}},
    ))
    session.commit()
    return session, tenant.id, other.id, engine


def _salla(session, tid, ext, *, variants=1, in_stock=True, image=True, url=True, price="120", status="sale", member=False):
    p = Product(
        tenant_id=tid, external_id=ext, title=f"قميص قطني أزرق {ext}", price=price, in_stock=in_stock,
        stock_quantity=3 if in_stock else 0, source="salla", ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        catalog_status="active", sync_status="pending",
        extra_metadata={"currency": "SAR", "status": "hidden" if status == "hidden" else "active",
                        "source_status": status,
                        **({"image_url": "https://cdn.example/shirt.jpg"} if image else {}),
                        **({"product_url": "https://store.example/p/shirt"} if url else {})},
    )
    session.add(p); session.flush()
    for i in range(variants):
        session.add(ProductVariant(tenant_id=tid, product_id=p.id, salla_variant_id=f"{i+1}", retailer_id=f"{ext}-{i+1}",
                                   price=price, currency="SAR", stock_quantity=3 if in_stock else 0, in_stock=in_stock))
        if member:
            session.add(MetaCatalogMembership(tenant_id=tid, catalog_id="CAT-900", retailer_id=f"{ext}-{i+1}",
                                              product_id=p.id, salla_variant_id=f"{i+1}", meta_item_id=f"META-{ext}-{i+1}",
                                              verified_at=datetime.now(timezone.utc), provenance="salla_variant_push"))
    session.commit()
    return p


_ENT = patch("core.plan_entitlements.get_entitlements",
             lambda *a, **k: SimpleNamespace(plan_slug="pro", is_active=True, is_blocked=False,
                                             has_feature=lambda key: key == "meta_catalog_sync"))
_READY = patch("services.whatsapp_catalog_sync.get_entitlements",
               lambda *a, **k: SimpleNamespace(has_feature=lambda key: key == "meta_catalog_sync"))


def _no_token_anywhere(report):
    blob = json.dumps(report, ensure_ascii=False, default=str)
    assert SECRET not in blob
    assert "EAAB" not in blob


@_ENT
@_READY
def test_readout_lists_products_variants_anomalies_and_proposes_candidates(monkeypatch):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS", raising=False)
    session, tid, other, engine = _make_db()
    try:
        simple = _salla(session, tid, "500100", variants=1)
        multi = _salla(session, tid, "500200", variants=3)
        third = _salla(session, tid, "500300", variants=2)
        _salla(session, tid, "500400", variants=1, in_stock=False)           # out of stock → not a candidate
        _salla(session, tid, "500500", variants=0)                           # anomaly: no variant rows
        _salla(session, tid, "500600", variants=1, image=False)              # anomaly: missing image
        _salla(session, tid, "500700", variants=1, status="hidden")          # hidden at source → not eligible
        bad = _salla(session, tid, "500800", variants=1, price="{'amount': 5}")  # anomaly: dict-text price
        _salla(session, other, "500100", variants=1)                         # another store, same Salla id

        report = build_catalog_trial_readout(session, tid, candidate_count=3)
        _no_token_anywhere(report)
        assert report["read_only"] is True and report["graph_reads_included"] is False
        assert report["tenant"]["exists"] is True
        counts = report["product_counts"]
        assert counts["total"] == 8 and counts["by_source"] == {"salla": 8}
        assert counts["hidden_at_source"] == 1
        ext = {p["external_id"]: p for p in report["products"]}
        assert ext["500200"]["variant_count"] == 3 and len(ext["500200"]["variants"]) == 3
        assert ext["500200"]["variants"][0]["retailer_id"] == "500200-1"
        assert ext["500700"]["publish_eligible"] is False and ext["500700"]["publish_rejection"] == "product_hidden_at_source"
        anomalies = {a["external_id"]: a["anomalies"] for a in report["anomalies"]}
        assert anomalies["500500"] == ["no_variant_rows"]
        assert "missing_image" in anomalies["500600"]
        assert "price_not_numeric" in anomalies[bad.external_id]
        # other tenant's rows are not in this readout
        assert all(p["product_id"] != 999 for p in report["products"])
        assert len([p for p in report["products"] if p["external_id"] == "500100"]) == 1
        # candidates: one single-variant, one multi-variant, one more in-stock with media
        sel = report["trial_candidates"]
        roles = {c["role"]: c["product_id"] for c in sel["selected"]}
        assert roles["single_variant"] == simple.id and roles["multi_variant"] == multi.id
        assert roles["in_stock_with_media"] == third.id
        assert sel["selection_complete"] is True and sel["expected_meta_items"] == 1 + 3 + 2
        env = sel["proposed_env"]
        assert env["NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS"] == str(tid)
        assert env["NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS"] == f"{tid}:{simple.id},{tid}:{multi.id},{tid}:{third.id}"
        # connection + entitlement + readiness facts, without the token
        conn = report["connection"]
        assert conn["catalog_enabled"] is True and conn["meta_catalog_id"] == "CAT-900"
        assert conn["has_merchant_access_token"] is True and conn["graph_token_source"]
        assert "meta_catalog_bind" in conn["extra_metadata_keys"]
        assert report["entitlement"]["meta_catalog_sync"] is True
        assert report["readiness"]["ready"] is True
        # without Graph reads the owner is told exactly what is still unproven
        assert report["missing_requirements"] == ["graph_reads_not_run"]
        assert report["sync_scope"]["active"] is False and report["sync_scope"]["tenant_in_scope"] is True
    finally:
        session.close(); engine.dispose()


@_ENT
@_READY
def test_readout_reports_catalog_disabled_without_claiming_no_catalog_exists(monkeypatch):
    """``catalog_disabled`` is a flag on the connection, not proof that no catalog
    exists: the readout keeps the stamped catalog id and the Graph link read."""
    session, tid, _other, engine = _make_db(catalog_enabled=False)
    try:
        _salla(session, tid, "500100", variants=1)
        graph = FakeGraph()
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph), \
             patch("services.meta_catalog_reconcile.select_catalog_graph_token", lambda *a, **k: {"token": SECRET}):
            report = build_catalog_trial_readout(session, tid, include_graph=True, client=graph)
        _no_token_anywhere(report)
        assert report["connection"]["catalog_enabled"] is False
        assert report["connection"]["meta_catalog_id"] == "CAT-900"
        assert report["readiness"]["blocker_code"] == "catalog_disabled"
        g = report["graph"]
        assert g["waba_link"]["linked_catalog_ids"] == ["CAT-900"] and g["waba_link"]["expected_catalog_linked"] is True
        assert g["waba_owner_business"]["business_id"] == "BM-900"
        assert g["token_catalog_management"]["verdict"] == "granted"
        assert g["catalogs"][0]["catalog_id"] == "CAT-900" and g["catalogs"][0]["business_id"] == "BM-900"
        assert g["catalog_business_matches_waba_owner"]["CAT-900"] is True
        assert all(m == "GET" for m, _ in graph.calls)
        assert "catalog_enabled" in report["missing_requirements"]
        assert "waba_catalog_link" not in report["missing_requirements"]
        assert "token_catalog_management" not in report["missing_requirements"]
    finally:
        session.close(); engine.dispose()


@_ENT
@_READY
def test_readout_graph_reads_classify_create_update_and_missing_permission(monkeypatch):
    session, tid, _other, engine = _make_db()
    try:
        simple = _salla(session, tid, "500100", variants=1, member=True)   # already on Meta → update/noop
        multi = _salla(session, tid, "500200", variants=2)                 # never pushed → create
        third = _salla(session, tid, "500300", variants=1)
        live = {"500100-1": {"id": "META-500100-1", "name": "قميص قطني أزرق 500100", "price": "99.00 SAR",
                             "currency": "SAR", "availability": "in stock"}}
        graph = FakeGraph(perm="declined", live=live)
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph), \
             patch("services.meta_catalog_reconcile.select_catalog_graph_token", lambda *a, **k: {"token": SECRET}):
            report = build_catalog_trial_readout(session, tid, include_graph=True, client=graph)
        _no_token_anywhere(report)
        g = report["graph"]
        assert g["token_catalog_management"]["verdict"] == "missing"
        assert "token_catalog_management" in report["missing_requirements"]
        plan = g["live_items"]["candidate_plan"]
        created = {r["retailer_id"] for r in plan["create"]}
        touched = {r["retailer_id"] for r in plan["update"] + plan["noop"]}
        assert created == {"500200-1", "500200-2", "500300-1"}
        assert touched == {"500100-1"}
        assert g["live_items"]["candidate_create"] == 3
        assert {c["product_id"] for c in report["trial_candidates"]["selected"]} == {simple.id, multi.id, third.id}
        assert all(m == "GET" for m, _ in graph.calls)
    finally:
        session.close(); engine.dispose()


def test_cli_entry_refuses_to_run_without_database_url(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "catalog_trial_readout_cli", REPO_ROOT / "backend" / "scripts" / "catalog_trial_readout.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["catalog_trial_readout", "--tenant-id", "35"])
    assert module.main() == 1
    assert "DATABASE_URL" in capsys.readouterr().err


# ── standalone bundle (runs against the deployed code version) ────────────

def _build_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_ctr_standalone", REPO_ROOT / "scripts" / "operators" / "build_catalog_trial_readout_standalone.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standalone_bundle_matches_the_service_module():
    build = _build_module()
    assert build.TARGET.read_text(encoding="utf-8") == build.build(), (
        "regenerate with: python scripts/operators/build_catalog_trial_readout_standalone.py"
    )


@_ENT
@_READY
def test_readout_falls_back_when_branch_only_modules_are_not_deployed(monkeypatch):
    """On the deployed code version the scope module and ``source_platform_status``
    do not exist yet; the readout must still produce the full DB section."""
    import services.catalog_trial_readout as readout

    monkeypatch.setitem(sys.modules, "services.whatsapp_catalog_sync_scope", None)  # import -> ImportError
    # the deployed core.catalog has no source_platform_status: force the inline fallback
    monkeypatch.setattr(readout, "_source_platform_status", readout._source_platform_status_fallback)
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", "35")
    session, tid, _other, engine = _make_db()
    try:
        _salla(session, tid, "500100", variants=1)
        _salla(session, tid, "500700", variants=1, status="hidden")
        report = build_catalog_trial_readout(session, tid)
        _no_token_anywhere(report)
        assert report["sync_scope"]["source"] == "env_fallback_scope_module_not_deployed"
        assert report["sync_scope"]["tenant_ids"] == [35] and report["sync_scope"]["tenant_in_scope"] is (tid == 35)
        ext = {p["external_id"]: p for p in report["products"]}
        assert ext["500700"]["source_status"] == "hidden" and ext["500100"]["source_status"] == "sale"
        assert report["product_counts"]["hidden_at_source"] == 1
    finally:
        session.close(); engine.dispose()


def test_standalone_cli_runs_against_a_sqlite_database_and_prints_the_ssh_command(monkeypatch, tmp_path, capsys):
    import importlib.util
    import subprocess

    build = _build_module()
    # a database file with one generic store, built with the ORM
    db_file = tmp_path / "readout.sqlite"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant); session.commit()
    session.add(WhatsAppConnection(tenant_id=tenant.id, whatsapp_business_account_id="WABA-1", phone_number_id="PN-1",
                                   access_token=SECRET, meta_catalog_id=None, catalog_enabled=False, extra_metadata={}))
    session.commit()
    _salla(session, tenant.id, "500100", variants=2)
    tid = tenant.id
    session.close(); engine.dispose()

    spec = importlib.util.spec_from_file_location("ctr_standalone", build.TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setattr(sys, "argv", ["ctr", "--tenant-id", str(tid), "--pretty"])
    with _ENT, _READY:
        rc = module.main()
    out, err = capsys.readouterr()
    assert rc == 0
    report = json.loads(out)
    _no_token_anywhere(report)
    assert report["tenant_id"] == tid and report["product_counts"]["total"] == 1
    assert report["connection"]["catalog_enabled"] is False and report["connection"]["meta_catalog_id"] is None
    assert "catalog_enabled" in report["missing_requirements"] and "meta_catalog_id" in report["missing_requirements"]
    assert "trial-readout tenant=" in err

    # the ssh one-liner carries this very file, base64-encoded, and runs it from /app
    monkeypatch.setattr(sys, "argv", ["ctr", "--print-ssh-command", "--tenant-id", "35", "--include-graph"])
    assert module.main() == 0
    cmd = capsys.readouterr().out.strip()
    assert cmd.startswith("railway ssh --environment production --service nahla-saas -- bash -lc 'echo ")
    import base64, re
    payload = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", cmd).group(1)
    assert base64.b64decode(payload) == build.TARGET.read_bytes()
    assert "cd /app && python /tmp/catalog_trial_readout_standalone.py --tenant-id 35 --include-graph" in cmd
    assert "DATABASE_URL" not in cmd and SECRET not in cmd
    # a missing DATABASE_URL is refused, not guessed
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setattr(sys, "argv", ["ctr", "--tenant-id", "35"])
    assert module.main() == 1
    # the bundle is plain python the production interpreter can compile
    assert subprocess.run([sys.executable, "-m", "py_compile", str(build.TARGET)], capture_output=True).returncode == 0
