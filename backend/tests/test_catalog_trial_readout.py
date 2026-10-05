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

    def __init__(self, *, waba="WABA-900", catalog="CAT-900", business="BM-900", perm="granted", live=None, linked=True,
                 debug_scopes=None, catalogs_error=None, phone="PN-900", on_biz_app=None,
                 debug_type="USER", debug_granular=None, debug_user_id=None):
        self.waba, self.catalog, self.business, self.perm = waba, catalog, business, perm
        self.debug_type, self.debug_granular, self.debug_user_id = debug_type, debug_granular, debug_user_id
        self.live = live or {}
        self.linked = linked
        self.debug_scopes = debug_scopes   # None -> /debug_token answers with an error
        self.catalogs_error = catalogs_error   # e.g. "smb" -> /{waba}/product_catalogs answers 400 code 10
        self.phone, self.on_biz_app = phone, on_biz_app
        self.calls = []

    def _route(self, method, url, params):
        self.calls.append((method, url))
        if method != "GET":
            raise AssertionError(f"readout must not {method} {url}")
        if url.endswith("/debug_token"):
            if self.debug_scopes is None:
                return _Resp(400, {"error": {"code": 190, "message": "invalid app token"}})
            data = {"is_valid": True, "type": self.debug_type, "app_id": "APP-900",
                    "scopes": list(self.debug_scopes), "granular_scopes": list(self.debug_granular or [])}
            if self.debug_user_id:
                data["user_id"] = self.debug_user_id
            return _Resp(200, {"data": data})
        if url.endswith("/me/permissions"):
            if self.perm == "error":
                return _Resp(400, {"error": {"code": 100, "message": "unsupported"}})
            rows = [{"permission": "whatsapp_business_management", "status": "granted"},
                    {"permission": "business_management", "status": "granted"}]
            if self.perm is not None:
                rows.insert(0, {"permission": "catalog_management", "status": self.perm})
            return _Resp(200, {"data": rows})
        if url.endswith(f"/{self.waba}/product_catalogs"):
            if self.catalogs_error == "smb":
                return _Resp(400, {"error": {"code": 10, "type": "OAuthException",
                                             "message": "(#10) This operation can not be performed on SMB business type"}})
            if self.catalogs_error == "other":
                return _Resp(500, {"error": {"code": 2, "message": "An unexpected error has occurred"}})
            return _Resp(200, {"data": [{"id": self.catalog, "name": "متجر تجريبي عام"}] if self.linked else []})
        if url.endswith(f"/{self.phone}"):
            if self.on_biz_app is None:
                return _Resp(400, {"error": {"code": 100, "message": "unsupported field"}})
            return _Resp(200, {"id": self.phone, "is_on_biz_app": self.on_biz_app, "platform_type": "CLOUD_API"})
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
        assert g["waba_catalogs"]["verdict"] == "catalog_linked_to_waba"
        assert [c["id"] for c in g["waba_catalogs"]["catalogs"]] == ["CAT-900"]
        assert g["waba_catalogs"]["stamped_catalog_is_linked"] is True
        assert g["waba_owner_business"]["business_id"] == "BM-900"
        assert g["token_catalog_management"]["interpretation"]["catalog_management_on_token"] == "granted"
        assert g["catalogs"][0]["catalog_id"] == "CAT-900" and g["catalogs"][0]["business_id"] == "BM-900"
        assert g["catalog_business_matches_waba_owner"]["CAT-900"] is True
        assert all(m == "GET" for m, _ in graph.calls)
        assert "catalog_enabled" in report["missing_requirements"]
        assert not any(m.startswith("waba_catalog_link") or m.startswith("token_catalog_management")
                       for m in report["missing_requirements"])
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
        perm = g["token_catalog_management"]
        assert perm["raw"]["me_permissions"]["catalog_management_status"] == "declined"
        assert perm["interpretation"]["catalog_management_on_token"] == "not_on_token"
        assert perm["interpretation"]["cause"] == "undetermined_from_token_alone"
        causes = {c["cause"]: c["evidence"] for c in perm["interpretation"]["possible_causes"]}
        assert causes["merchant_declined_at_authorization"] == "confirmed"
        assert "token_catalog_management:not_on_token" in report["missing_requirements"]
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


# ── owner review on the first live readout ────────────────────────────────

@_ENT
@_READY
def test_parent_in_stock_with_all_variants_out_is_an_anomaly_and_never_a_candidate(monkeypatch):
    """A parent row that says in stock (quantity 1) while every variant row is out of stock
    is flagged and excluded; number-completeness is reported apart from scenario coverage."""
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    session, tid, _other, engine = _make_db()
    try:
        contradictory = _salla(session, tid, "600186", variants=3)
        for v in session.query(ProductVariant).filter_by(product_id=contradictory.id).all():
            v.in_stock = False; v.stock_quantity = 0
        contradictory.stock_quantity = 1; session.commit()
        inverse = _salla(session, tid, "600187", variants=2, in_stock=False)
        v = session.query(ProductVariant).filter_by(product_id=inverse.id).first(); v.in_stock = True; v.stock_quantity = 4; session.commit()
        multi_a = _salla(session, tid, "600185", variants=4)
        multi_b = _salla(session, tid, "600188", variants=6)
        multi_c = _salla(session, tid, "600190", variants=10)

        report = build_catalog_trial_readout(session, tid, candidate_count=3)
        ext = {p["external_id"]: p for p in report["products"]}
        assert ext["600186"]["anomalies"] == ["parent_in_stock_but_all_variants_out_of_stock"]
        assert ext["600186"]["variants_in_stock_count"] == 0 and ext["600186"]["in_stock"] is True
        assert ext["600187"]["anomalies"] == ["parent_out_of_stock_but_variant_in_stock"]
        sel = report["trial_candidates"]
        chosen = {c["product_id"] for c in sel["selected"]}
        assert contradictory.id not in chosen and inverse.id not in chosen
        assert chosen == {multi_a.id, multi_b.id, multi_c.id}
        assert sel["selection_complete"] is True
        # three products, but no single-variant one: the count is complete, the coverage is not
        cov = sel["scenario_coverage"]
        assert cov["coverage_complete"] is False and cov["missing_roles"] == ["single_variant"]
        assert "trial_scenario_coverage:single_variant" in report["missing_requirements"]
        assert sel["expected_meta_items"] == 4 + 6 + 10
        # publish payloads for the chosen variants carry the identities the push will use
        payloads = report["candidate_payloads"]
        assert payloads["local_payload_checks_passed"] == 20 and payloads["local_payload_checks_blocked"] == 0
        assert "pushable" not in payloads
        assert "not Meta's acceptance" in payloads["meaning"] and "not visibility in WhatsApp" in payloads["meaning"]
        assert payloads["availability_counts"] == {"in stock": 20}
        rids = {i["retailer_id"] for i in payloads["items"]}
        assert "600185-1" in rids and "600190-10" in rids
        assert all(i["item_group_id"] for i in payloads["items"])
    finally:
        session.close(); engine.dispose()


@_ENT
@_READY
def test_owner_chosen_candidate_ids_are_evaluated_not_replaced(monkeypatch):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    session, tid, _other, engine = _make_db()
    try:
        a = _salla(session, tid, "617350990", variants=5)
        b = _salla(session, tid, "792574531", variants=7)
        c = _salla(session, tid, "59407425", variants=8)
        bad = _salla(session, tid, "600186", variants=2)
        for v in session.query(ProductVariant).filter_by(product_id=bad.id).all():
            v.in_stock = False; v.stock_quantity = 0
        session.commit()
        report = build_catalog_trial_readout(session, tid, candidate_ids=[a.id, b.id, c.id, bad.id, 999999])
        sel = report["trial_candidates"]
        assert sel["mode"] == "preferred_ids"
        assert [c_["product_id"] for c_ in sel["selected"]] == [a.id, b.id, c.id]
        ev = {e["product_id"]: e for e in sel["preferred_evaluation"]}
        assert ev[bad.id]["accepted"] is False and "anomaly:parent_in_stock_but_all_variants_out_of_stock" in ev[bad.id]["reasons"]
        assert ev[999999]["reasons"] == ["not_found_for_tenant"]
        assert sel["expected_meta_items"] == 20
        assert sel["proposed_env"]["NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS"] == f"{tid}:{a.id},{tid}:{b.id},{tid}:{c.id}"
        assert sel["scenario_coverage"]["missing_roles"] == ["single_variant"]
    finally:
        session.close(); engine.dispose()


@_ENT
@_READY
def test_graph_section_reads_waba_catalogs_without_a_stamped_id_and_reports_permission_not_requested(monkeypatch):
    """`missing_catalog_id` from the legacy link status never proved the WABA has no catalog:
    the readout lists WABA-linked catalogs directly, checks their owner against the expected
    Business Manager, and tells a never-requested permission apart from a declined one."""
    session, tid, _other, engine = _make_db(catalog_enabled=False, catalog_id=None)
    try:
        a = _salla(session, tid, "617350990", variants=2)
        graph = FakeGraph(perm=None, business="2138142656950660", live={})
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph), \
             patch("services.meta_catalog_reconcile.select_catalog_graph_token", lambda *a, **k: {"token": SECRET}):
            report = build_catalog_trial_readout(session, tid, include_graph=True, client=graph,
                                                 candidate_ids=[a.id], expected_business_id="2138142656950660")
        _no_token_anywhere(report)
        g = report["graph"]
        assert g["waba_link_legacy"]["error"] == "missing_catalog_id"           # the old answer
        assert g["waba_catalogs"]["verdict"] == "catalog_linked_to_waba"       # the real answer
        assert g["waba_catalogs"]["stamped_catalog_is_linked"] is None
        assert g["waba_owner_business"]["matches_expected"] is True
        assert g["catalog_business_matches_waba_owner"]["CAT-900"] is True
        perm = g["token_catalog_management"]
        assert perm["raw"]["me_permissions"]["catalog_management_status"] == "absent"
        assert perm["raw"]["me_permissions"]["listed_permissions"]["whatsapp_business_management"] == "granted"
        assert perm["interpretation"]["catalog_management_on_token"] == "not_on_token"
        # an absent row is a fact; its cause is not: the explicit scope is read from the deployed
        # file, the config_id's permissions and the app access level stay "unknown" until read manually
        assert perm["interpretation"]["cause"] == "undetermined_from_token_alone"
        causes = {c["cause"]: c["evidence"] for c in perm["interpretation"]["possible_causes"]}
        assert causes["explicit_oauth_scope_omits_catalog_management"] == "confirmed"   # this repo's connect flow
        assert causes["embedded_signup_config_does_not_include_catalog_management"] == "unknown"
        assert causes["app_lacks_access_level_for_catalog_management"] == "unknown"
        assert causes["merchant_declined_at_authorization"] == "unknown"   # absent ≠ not declined
        assert g["coexistence"]["verdict"] == "unproven"                     # phone read not answered here
        assert g["catalog_path_assessment"]["api_catalog_link_for_this_waba"] == "api_catalog_link_readable_for_this_waba"
        assert perm["raw"]["explicit_oauth_scope_in_deployed_code"]["requests_catalog_management"] is False
        assert "embedded_signup_configuration_permissions" in perm["interpretation"]["needs_manual_reads"]
        # candidates classified against the linked catalog even though nothing is stamped locally
        pres = g["live_items"]["against_linked_catalogs"]["CAT-900"]
        assert pres["candidates_absent"] == ["617350990-1", "617350990-2"]
        assert pres["classification"] == {"create_if_absent": 2, "match_needs_verification": 0}
        assert "needs verification" in pres["note"]
        assert "token_catalog_management:not_on_token" in report["missing_requirements"]
        assert "meta_catalog_id" in report["missing_requirements"]
        assert not any(m.startswith("waba_catalog_link") for m in report["missing_requirements"])
        assert all(m == "GET" for m, _ in graph.calls)

        # and when the WABA truly has no catalog the verdict says so, distinctly
        graph2 = FakeGraph(perm=None, business="2138142656950660", linked=False)
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph2):
            report2 = build_catalog_trial_readout(session, tid, include_graph=True, client=graph2, candidate_ids=[a.id])
        assert report2["graph"]["waba_catalogs"]["verdict"] == "no_catalog_linked_to_waba"
        assert "waba_catalog_link:none_linked" in report2["missing_requirements"]
        assert report2["graph"]["live_items"]["expected_actions_if_new_catalog"] == {"create": 2, "update": 0, "noop": 0}
    finally:
        session.close(); engine.dispose()


def _route_salla_reads(monkeypatch, fake):
    """Serve the readout's refresh-free Salla GETs from *fake* (no network)."""
    import services.catalog_trial_readout as readout

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            return None

    async def _http_get(url, headers, params=None):
        assert headers["Authorization"] == f"Bearer {fake.api_key}"
        path = url.split("/admin/v2", 1)[1]
        if path.endswith("/variants"):
            return _Resp({"data": await fake.get_raw_variants(path.split("/")[2])})
        return _Resp(await fake._get(path))

    monkeypatch.setattr(readout, "_salla_http_get", _http_get)


@_ENT
@_READY
def test_salla_recheck_locates_the_stock_inconsistency(monkeypatch):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    session, tid, _other, engine = _make_db()
    try:
        bad = _salla(session, tid, "600186", variants=2)
        for v in session.query(ProductVariant).filter_by(product_id=bad.id).all():
            v.in_stock = False; v.stock_quantity = 0
        bad.stock_quantity = 1; session.commit()
        stale = _salla(session, tid, "600189", variants=1)
        for v in session.query(ProductVariant).filter_by(product_id=stale.id).all():
            v.in_stock = False; v.stock_quantity = 0
        session.commit()

        class FakeSalla:
            calls = []
            api_key = "salla-read-token"
            _expires_at = None

            async def _get(self, path, params=None):
                self.calls.append(path)
                ext = path.split("/")[2]
                qty = 1 if ext == "600186" else 3
                return {"data": {"id": int(ext), "quantity": qty, "status": {"slug": "sale"}}}

            async def get_raw_variants(self, ext):
                self.calls.append(f"/products/{ext}/variants")
                if ext == "600186":
                    return [{"id": 1, "quantity": 0}, {"id": 2, "quantity": 0}]
                return [{"id": 1, "quantity": 3}]

        fake = FakeSalla()
        _route_salla_reads(monkeypatch, fake)
        report = build_catalog_trial_readout(session, tid, include_salla=True, salla_adapter=fake)
        checked = {c["external_id"]: c for c in report["salla_check"]["checked"]}
        assert checked["600186"]["verdict"] == "salla_parent_quantity_inconsistent_with_its_variants"
        assert checked["600186"]["salla"]["quantity"] == 1 and checked["600186"]["salla"]["variants_in_stock"] == 0
        assert checked["600189"]["verdict"] == "local_variant_stock_stale"
        assert all(p.startswith("/products/") for p in fake.calls)   # GET only
    finally:
        session.close(); engine.dispose()


@_ENT
@_READY
def test_permission_facts_stay_unknown_when_graph_cannot_answer_and_granted_scopes_win(monkeypatch):
    """Unknown stays unknown: a failing /me/permissions and an unavailable /debug_token
    yield no verdict and no cause; a debug_token that lists the scope proves it is on the token."""
    session, tid, _other, engine = _make_db(catalog_id=None)
    try:
        _salla(session, tid, "500100", variants=1)
        import services.catalog_trial_readout as readout

        monkeypatch.delenv("META_APP_ID", raising=False)
        monkeypatch.delenv("META_APP_SECRET", raising=False)
        monkeypatch.setattr(readout, "_explicit_oauth_scope_requests_catalog_management",
                            lambda: {"known": False, "reason": "source_file_not_found"})
        graph = FakeGraph(perm="error", debug_scopes=None)
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph):
            report = build_catalog_trial_readout(session, tid, include_graph=True, client=graph)
        _no_token_anywhere(report)
        perm = report["graph"]["token_catalog_management"]
        assert perm["raw"]["me_permissions"]["ok"] is False
        assert perm["raw"]["me_permissions"]["catalog_management_status"] == "unknown"
        assert perm["raw"]["debug_token"]["available"] is False
        assert perm["raw"]["debug_token"]["reason"] == "app_credentials_not_in_environment"
        assert perm["interpretation"]["catalog_management_on_token"] == "unknown"
        assert perm["interpretation"]["cause"] == "undetermined_from_token_alone"
        assert all(c["evidence"] == "unknown" for c in perm["interpretation"]["possible_causes"])
        assert "token_catalog_management:unknown" in report["missing_requirements"]

        # app credentials present and debug_token lists the scope: granted even though /me/permissions failed
        monkeypatch.setenv("META_APP_ID", "APP-900")
        monkeypatch.setenv("META_APP_SECRET", "app-secret-not-to-print")
        graph2 = FakeGraph(perm="error", debug_scopes=["whatsapp_business_management", "catalog_management"])
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph2):
            report2 = build_catalog_trial_readout(session, tid, include_graph=True, client=graph2)
        perm2 = report2["graph"]["token_catalog_management"]
        assert perm2["raw"]["debug_token"]["available"] is True
        assert perm2["raw"]["debug_token"]["catalog_management_in_scopes"] is True
        assert perm2["raw"]["debug_token"]["app_id_matches_configured_app"] is True
        assert perm2["interpretation"]["catalog_management_on_token"] == "granted"
        assert "app-secret-not-to-print" not in json.dumps(report2)
        assert not any(m.startswith("token_catalog_management") for m in report2["missing_requirements"])
    finally:
        session.close(); engine.dispose()


@_ENT
@_READY
def test_graph_error_on_waba_catalogs_leaves_the_link_unproven_and_names_the_business_type_restriction(monkeypatch):
    """`GET /{waba}/product_catalogs` → HTTP 400 code 10 "SMB business type": the link is *unproven*
    (never "none linked"), no create/update split is claimed, the restriction is named apart from a
    permission, coexistence is read from the phone, and the assessment never says that
    catalog_management alone would lift the block."""
    session, tid, _other, engine = _make_db(catalog_enabled=False, catalog_id=None)
    try:
        conn = session.query(WhatsAppConnection).filter_by(tenant_id=tid).one()
        conn.extra_metadata = {"connection_mode": "coexistence"}; session.commit()
        a = _salla(session, tid, "617350990", variants=2)
        graph = FakeGraph(perm=None, business="2138142656950660", catalogs_error="smb", on_biz_app=True)
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph):
            report = build_catalog_trial_readout(session, tid, include_graph=True, client=graph,
                                                 candidate_ids=[a.id], expected_business_id="2138142656950660")
        _no_token_anywhere(report)
        g = report["graph"]
        wc = g["waba_catalogs"]
        assert wc["ok"] is False and wc["http_status"] == 400 and wc["count"] is None
        assert wc["verdict"] == "unproven_graph_error"
        assert wc["error_class"]["class"] == "business_type_restriction" and wc["error_class"]["meta_code"] == 10
        assert "refusal of this operation only" in wc["error_class"]["note"]
        # the failed read is not turned into "no catalog": nothing is linked *or* unlinked here
        li = g["live_items"]
        assert li["skipped"] == "waba_catalog_link_unproven_graph_error"
        assert "expected_actions_if_new_catalog" not in li
        assert li["conditional_actions"]["if_no_catalog_is_linked"] == {"create": 2, "update": 0, "noop": 0}
        assert li["conditional_actions"]["if_a_catalog_is_linked"] == "per_identity_presence_read_required"
        # coexistence is a Graph fact, read GET-only from the phone number
        assert g["coexistence"] == {"local_connection_mode": "coexistence", "is_on_biz_app": True,
                                    "platform_type": "CLOUD_API", "graph_ok": True,
                                    "verdict": "coexistence_confirmed_by_graph"}
        assert "GET /PN-900?fields=is_on_biz_app,platform_type" in g["reads"]
        pa = g["catalog_path_assessment"]
        assert pa["api_catalog_link_for_this_waba"] == "current_read_refused_with_business_type_message"
        assert pa["catalog_exists_for_this_waba"] == "unproven"
        assert pa["final_cause_of_refusal"] == "unknown" and pa["alternative_paths"] == "unknown_until_manual_reads"
        assert pa["would_catalog_management_alone_lift_the_block"] == "unproven"
        assert pa["tenant_verdict"] == "conditional"
        assert any(c.startswith("coexistence.is_on_biz_app") for c in pa["conditional_on"])
        assert any(c.startswith("meta_dashboard_reads") for c in pa["conditional_on"])
        docs = {d["kind"]: d for d in pa["documentation"]}
        # the official table is carried with its columns and the two questions kept apart
        table = docs["official_page_table"]
        assert len(table["columns_per_excerpt"]) == 3
        assert table["row_business_tools_per_excerpt"]["change_to_business_app_feature_after_onboarding"] == "No change"
        assert table["row_business_tools_per_excerpt"]["supported_on_cloud_api"] == "Not supported"
        assert "persists" in next(k for k in table["reading"] if "persists" in k)
        # the circulating sentence is never attributed to Meta's Limitations section
        assert "NOT to be cited as Meta's Limitations section" in docs["unattributed_excerpt"]["attribution"]
        assert all("verif" in d["verification"] for d in pa["documentation"])
        assert "whatsapp_manager:business_portfolio_that_owns_the_waba_and_its_type" in pa["needs_manual_reads"]
        missing = report["missing_requirements"]
        assert "waba_catalog_link:unproven" in missing
        assert "waba_catalog_link:none_linked" not in missing
        assert "graph_refused_product_catalogs:business_type_message" in missing
        assert "coexistence:api_catalog_path_unproven" in missing
        assert all(m == "GET" for m, _ in graph.calls)

        # an unrelated Graph failure is also "unproven", but carries no business-type claim
        graph2 = FakeGraph(perm=None, catalogs_error="other", on_biz_app=False)
        with patch("services.meta_catalog_linking._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_import._select_graph_token", lambda conn: {"token": SECRET, "token_source": "merchant_meta_oauth"}), \
             patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph2):
            report2 = build_catalog_trial_readout(session, tid, include_graph=True, client=graph2, candidate_ids=[a.id])
        g2 = report2["graph"]
        assert g2["waba_catalogs"]["verdict"] == "unproven_graph_error"
        assert g2["waba_catalogs"]["error_class"]["class"] == "graph_error"
        assert g2["live_items"]["skipped"] == "waba_catalog_link_unproven_graph_error"
        assert g2["coexistence"]["verdict"] == "not_on_business_app_per_graph"
        assert g2["catalog_path_assessment"]["api_catalog_link_for_this_waba"] == "unproven"
        assert g2["catalog_path_assessment"]["final_cause_of_refusal"] == "n/a"
        assert g2["catalog_path_assessment"]["documentation"] == []
        assert "graph_refused_product_catalogs:business_type_message" not in report2["missing_requirements"]
        assert "coexistence:api_catalog_path_unproven" not in report2["missing_requirements"]
    finally:
        session.close(); engine.dispose()


@_ENT
@_READY
def test_candidate_salla_crosscheck_compares_every_chosen_variant_with_salla(monkeypatch):
    """With --include-salla the chosen products' variants are compared with Salla's current
    variants (GET only): price, stock, option label, missing rows on either side."""
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    session, tid, _other, engine = _make_db()
    try:
        dress = _salla(session, tid, "792574531", variants=3, price="250")   # فستان بثلاث مقاسات
        shoe = _salla(session, tid, "59407425", variants=2, price="180")

        class FakeSalla:
            calls = []
            api_key = "salla-read-token"
            _expires_at = None

            async def _get(self, path, params=None):
                self.calls.append(path)
                ext = path.split("/")[2]
                return {"data": {"id": int(ext), "name": "منتج تجريبي", "quantity": 5, "status": {"slug": "sale"}}}

            async def get_raw_variants(self, ext):
                self.calls.append(f"/products/{ext}/variants")
                if ext == "792574531":
                    return [
                        {"id": 1, "price": {"amount": 250.0, "currency": "SAR"}, "quantity": 3,
                         "related_option_values": [{"name": "S"}]},
                        {"id": 2, "price": {"amount": 275.0, "currency": "SAR"}, "quantity": 3,      # price differs locally
                         "related_option_values": [{"name": "M"}]},
                        {"id": 3, "price": {"amount": 250.0, "currency": "SAR"}, "quantity": 0,      # stock differs locally
                         "related_option_values": []},                                              # and no option label
                        {"id": 4, "price": {"amount": 250.0, "currency": "SAR"}, "quantity": 3},    # not present locally
                    ]
                return [{"id": 1, "price": 180, "quantity": 2, "name": "40"},
                        {"id": 2, "price": "180.00", "quantity": 1, "name": "41"}]

        fake = FakeSalla()
        _route_salla_reads(monkeypatch, fake)
        report = build_catalog_trial_readout(session, tid, include_salla=True, salla_adapter=fake,
                                             candidate_ids=[dress.id, shoe.id], candidate_count=2)
        cc = report["candidate_salla_crosscheck"]
        by_ext = {p["external_id"]: p for p in cc["products"]}
        d = by_ext["792574531"]
        verdicts = {r["salla_variant_id"]: r["verdict"] for r in d["variants"]}
        assert verdicts["1"] == "matches_salla"
        assert verdicts["2"] == "price_mismatch"
        assert verdicts["3"] == "stock_mismatch,salla_variant_has_no_option_label"
        assert d["salla_variants_missing_locally"] == ["4"] and d["verdict"] == "differences_found"
        assert {r["salla_option_label"] for r in d["variants"]} == {"S", "M", None}
        s_ = by_ext["59407425"]
        assert s_["verdict"] == "consistent_with_salla" and all(r["verdict"] == "matches_salla" for r in s_["variants"])
        assert cc["variants_checked"] == 5 and cc["variants_matching"] == 3 and cc["mismatches"] == 3
        assert "not Meta acceptance" in cc["meaning"]
        assert all(p.startswith("/products/") for p in fake.calls)   # GET only
        assert report["candidate_payloads"]["local_payload_checks_passed"] == 5
    finally:
        session.close(); engine.dispose()


def test_system_user_token_is_classified_and_its_granular_targets_recorded_without_claiming_standard_eligibility(monkeypatch):
    """A SYSTEM_USER (business-integration) token: the readout records the type class, the
    principal id and the scope -> target ids, and lists Standard-access eligibility as an
    unknown cause — it never settles it by comparing a user id with human app roles."""
    import services.catalog_trial_readout as readout

    monkeypatch.setenv("META_APP_ID", "APP-900")
    monkeypatch.setenv("META_APP_SECRET", "secret-900")
    monkeypatch.setattr(readout, "_explicit_oauth_scope_requests_catalog_management",
                        lambda: {"known": True, "requests_catalog_management": False})
    graph = FakeGraph(
        perm=None, debug_scopes=["business_management", "whatsapp_business_management", "whatsapp_business_messaging"],
        debug_type="SYSTEM_USER", debug_user_id="SU-123",
        debug_granular=[{"scope": "whatsapp_business_management", "target_ids": ["WABA-900"]},
                        {"scope": "business_management", "target_ids": ["BM-900"]}],
    )
    with patch("services.meta_catalog_linking.httpx.Client", lambda *a, **k: graph):
        perm = readout._permission_status("EAAB-merchant-token", client=graph)
    dbg = perm["raw"]["debug_token"]
    assert dbg["type"] == "SYSTEM_USER" and dbg["token_type_class"] == "system_user"
    assert dbg["principal_id"] == "SU-123"
    assert dbg["granular_scope_targets"] == {"whatsapp_business_management": ["WABA-900"], "business_management": ["BM-900"]}
    assert dbg["catalog_management_in_scopes"] is False
    assert perm["interpretation"]["catalog_management_on_token"] == "not_on_token"
    causes = {c["cause"]: c for c in perm["interpretation"]["possible_causes"]}
    assert causes["standard_access_not_usable_for_this_token_principal"]["evidence"] == "unknown"
    assert "system_user" in causes["standard_access_not_usable_for_this_token_principal"]["note"]
    assert "EAAB" not in json.dumps(perm)
