"""Post-0124 release states are exact and never authorize Google or a downgrade."""
import datetime as dt
import json
from pathlib import Path

import pytest

from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import bootstrap_migration_contract as bootstrap

ROOT = Path(__file__).resolve().parents[1]


def pilot_env():
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    return {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": "true",
            "SHOPIFY_INSTALL_PILOT_ADMISSION": json.dumps({k.PILOT_SHOP: {
                "sha256": "a" * 64, "expires_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ")}})}


def test_post_0124_states_are_separate_and_bootstrap_stays_0093():
    assert set(k.POST_INSTALL_PHASES) == {"S4_DORMANT", "S4_PILOT"}
    assert set(k.DEPLOY_PHASES) == {"S0", "S3"}
    assert bootstrap.INTEGRATION_BOOTSTRAP_TARGET == "0093"
    for name, definition in k.POST_INSTALL_PHASES.items():
        assert definition["rows"] == {"0111", "0113", "0124"}
        assert len(definition["tables"]) == 17
        assert definition["expectations_key"] == "shopify_s4"
        assert k.phase(name) == definition
        check, known = gate.check_graph(ROOT, definition)
        assert check.status == k.PASS
        for rows in (["0111", "0113", "0125"], ["0111", "0113", "0124", "0125"],
                     ["0111", "0113", "0123"], ["0111", "0113", "0124", "0124"]):
            assert k.evaluate_rows(rows, definition["rows"], known)[0] == k.FAIL


def test_dormant_rollback_refuses_every_enabled_shopify_feature():
    assert k.evaluate_flags({}, "S4_DORMANT")[0] == k.PASS
    for flag in k.SHOPIFY_FLAG_ENVS:
        assert k.evaluate_flags({flag: "true"}, "S4_DORMANT")[0] == k.FAIL


def test_pilot_requires_exact_connection_and_install_flags_and_one_shop():
    env = pilot_env()
    assert k.evaluate_flags(env, "S4_PILOT")[0] == k.PASS
    for flag in k.SHOPIFY_FLAG_ENVS:
        changed = dict(env)
        changed[flag] = "false" if flag in env else "true"
        assert k.evaluate_flags(changed, "S4_PILOT")[0] == k.FAIL

    entries = json.loads(env["SHOPIFY_INSTALL_PILOT_ADMISSION"])
    for shops in ({}, {"other.myshopify.com": entries[k.PILOT_SHOP]},
                  {**entries, "other.myshopify.com": entries[k.PILOT_SHOP]}):
        changed = {**env, "SHOPIFY_INSTALL_PILOT_ADMISSION": json.dumps(shops)}
        assert k.evaluate_flags(changed, "S4_PILOT")[0] == k.FAIL


@pytest.mark.parametrize("expiry", ["2026-10-15T12:00:00.123Z", " 2026-10-15T12:00:00Z ", 0, None])
def test_pilot_expiry_uses_runtime_exact_whole_second_utc(expiry):
    env = pilot_env()
    entries = json.loads(env["SHOPIFY_INSTALL_PILOT_ADMISSION"])
    entries[k.PILOT_SHOP]["expires_at"] = expiry
    env["SHOPIFY_INSTALL_PILOT_ADMISSION"] = json.dumps(entries)
    assert k.evaluate_flags(env, "S4_PILOT")[0] == k.FAIL


@pytest.mark.parametrize("raw", ["", "null", "[]", "{", "x" * 4097,
                                   '{"nahlah-dev.myshopify.com":{},"nahlah-dev.myshopify.com":{}}'])
def test_pilot_malformed_admission_fails_closed_without_echoing_hashes(raw):
    result = k.evaluate_flags({**pilot_env(), "SHOPIFY_INSTALL_PILOT_ADMISSION": raw}, "S4_PILOT")
    assert result[0] == k.FAIL
    assert "sha256" not in json.dumps(result[2])


def test_post_0124_expectations_cannot_fall_back_to_15_table_s3(tmp_path):
    (tmp_path / "scripts/operators").mkdir(parents=True)
    (tmp_path / k.INSTALL_EXPECTATIONS_RELATIVE_PATH).write_text(
        (ROOT / k.EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    assert gate.check_expectations(tmp_path, "S4_DORMANT")[0].status == k.FAIL
