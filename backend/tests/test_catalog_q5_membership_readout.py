"""ق-5 read-only membership readout: SELECT-only, secret-free, identity parsing."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "operators" / "catalog_q5_membership_readout.py"


def _load():
    spec = importlib.util.spec_from_file_location("catalog_q5_membership_readout", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _statements(mod, **overrides):
    kwargs = dict(
        catalog_id="871742015873294",
        tenant_id=35,
        product_ids=[176, 177, 178, 179, 180, 181, 182],
        content_ids=["nahla_p_176", "123456"],
        link_external_ids=["123456"],
        meta_item_ids=[],
        store_marker="dev-cgcaqkpx5wgewsyv",
    )
    kwargs.update(overrides)
    return mod.build_statements(**kwargs)


def test_every_statement_is_a_select_and_passes_the_write_guard():
    mod = _load()
    sts = _statements(mod)
    assert len(sts) >= 15
    for st in sts:
        assert st["sql"].strip().upper().startswith("SELECT"), st["key"]
        mod._assert_select_only(st["sql"])


def test_write_guard_refuses_non_select():
    mod = _load()
    with pytest.raises(RuntimeError):
        mod._assert_select_only("UPDATE products SET meta_item_id = NULL")
    with pytest.raises(RuntimeError):
        mod._assert_select_only("SELECT 1;\nDELETE FROM products")


def test_no_secret_bearing_column_is_selected():
    mod = _load()
    joined = "\n".join(st["sql"].lower() for st in _statements(mod))
    for forbidden in ("access_token", "token_type", "app_secret", "password", "secret_enc"):
        assert forbidden not in joined, forbidden
    # Whole JSON columns that may carry tokens are never selected wholesale.
    for pattern in (r"\bi\.config\s*(,|$|\n)", r"\bts\.store_settings\s*(,|$|\n)", r"(?<!coalesce\()\bp\.metadata\s*(,|$|\n)",
                    r"\bw\.extra_metadata\b", r"\bw\.access_token\b", r"\bmeta_import_last_report\b"):
        assert re.search(pattern, joined) is None, pattern
    # JSON access is always key-scoped.
    assert "i.config ->> 'store_id'" in joined
    assert "ts.store_settings ->> 'store_url'" in joined


def test_render_refuses_secret_shapes():
    mod = _load()
    with pytest.raises(RuntimeError):
        mod.render({"x": "EAAtoken"}, pretty=False)
    with pytest.raises(RuntimeError):
        mod.render({"x": "postgresql://u:p@h/db"}, pretty=False)
    assert "ok" in mod.render({"x": "ok"}, pretty=False)


def test_identity_parsing_from_export_links_and_ranges():
    mod = _load()
    assert mod.parse_id_range("176-182,190") == [176, 177, 178, 179, 180, 181, 182, 190]
    links = [
        "https://salla.sa/dev-cgcaqkpx5wgewsyv/p/617350990?utm=x",
        "https://dev-cgcaqkpx5wgewsyv.salla.sa/p/792574531",
        "https://example.test/shop/59407425",
    ]
    assert mod.external_ids_from_links(links) == ["617350990", "792574531", "59407425"]
    markers = mod.store_markers_from_links(links)
    assert "dev-cgcaqkpx5wgewsyv" in markers
    assert "dev-cgcaqkpx5wgewsyv.salla.sa" in markers


def test_content_id_and_meta_item_id_are_separate_inputs():
    mod = _load()
    sts = {s["key"]: s for s in _statements(mod, content_ids=["A-1"], meta_item_ids=["99"])}
    assert sts["memberships_matching_q4_content_ids"]["params"]["content_ids"] == ["A-1"]
    assert sts["products_stamped_with_export_meta_item_ids"]["params"]["meta_item_ids"] == ["99"]
    # nahla_p_<id> identities are derived from the local product ids, not from the Content IDs.
    assert sts["claims_on_nahla_identities"]["params"]["nahla_ids"][0] == "nahla_p_176"


def test_cli_print_sql_needs_no_database_and_refuses_without_database_url(tmp_path, monkeypatch):
    cids = tmp_path / "cids.txt"
    cids.write_text("nahla_p_176\n# comment\n555\n", encoding="utf-8")
    links = tmp_path / "links.txt"
    links.write_text("https://salla.sa/dev-cgcaqkpx5wgewsyv/p/555\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin"}
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--print-sql", "--content-ids-file", str(cids), "--links-file", str(links)],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["inputs"]["store_marker"] == "dev-cgcaqkpx5wgewsyv"
    assert payload["inputs"]["link_external_ids"] == ["555"]
    assert payload["inputs"]["content_ids_count"] == 2
    assert all(s["sql"].upper().startswith("SELECT") for s in payload["statements"])

    res2 = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)
    assert res2.returncode == 2
    assert "DATABASE_URL is not set" in res2.stderr
    assert subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True).returncode == 0
