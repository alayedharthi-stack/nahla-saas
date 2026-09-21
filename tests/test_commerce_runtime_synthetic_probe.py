"""The synthetic probe's pure parts, offline: expectations, output hygiene,
scripted answers and the usage contract. The end-to-end run on PostgreSQL is
``tests/commerce_reliability/test_commerce_runtime_synthetic_probe_pg.py``.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.operators import commerce_runtime_synthetic_probe as probe  # noqa: E402


def _seed() -> probe.Seed:
    return probe.Seed(tenant_id=7, connection_id=3, customer_id=11, conversation_id=5,
                      product_ids=(21, 22, 23, 24), section_id=1, coupon_id=9, campaign_coupon_id=10,
                      store_host="demo-probe.example-store.sa")


# ── Expectations ─────────────────────────────────────────────────────────────


def test_a_listing_case_is_met_only_when_links_reach_the_wire_unchanged() -> None:
    case = next(c for c in probe.CASES if c.name == "listing_with_links")
    met = probe.evaluate(case, {
        "replied": True, "transport_calls": 1, "replay_transport_calls": 0,
        "tools_called": "search_products", "evidence_refs": "catalog:product:21,catalog:product:22",
        "sanitizer_blocked_lines": 0, "wire_equals_reserved_intent": True,
        "wire_hosts": ["demo-probe.example-store.sa"],
    })
    assert all(met.values()), met
    rewritten = probe.evaluate(case, {
        "replied": True, "transport_calls": 1, "replay_transport_calls": 0,
        "tools_called": "search_products", "evidence_refs": "catalog:product:21",
        "sanitizer_blocked_lines": 1, "wire_equals_reserved_intent": False, "wire_hosts": [],
    })
    assert rewritten["links_reached_the_wire_unchanged"] is False
    assert rewritten["nothing_blocked_on_the_wire"] is False


def test_a_second_send_on_replay_fails_the_case() -> None:
    case = next(c for c in probe.CASES if c.name == "greeting")
    met = probe.evaluate(case, {"replied": True, "transport_calls": 1, "replay_transport_calls": 1,
                                "tools_called": "", "evidence_refs": "", "sanitizer_blocked_lines": 0})
    assert met["not_answered_twice"] is False and met["answered_once"] is True


def test_the_coupon_case_requires_a_coupon_reference() -> None:
    case = next(c for c in probe.CASES if c.name == "coupon")
    met = probe.evaluate(case, {"replied": True, "transport_calls": 1, "replay_transport_calls": 0,
                                "tools_called": "list_shareable_promotions",
                                "evidence_refs": "promotion:offer:3", "sanitizer_blocked_lines": 0})
    assert met["evidence_cited"] is True and met["coupon_evidence_cited"] is False


# ── Output hygiene ───────────────────────────────────────────────────────────


def test_secret_values_and_scrub_never_let_a_configured_secret_through(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-verysecretvalue")
    monkeypatch.setenv("NAHLA_SYNTHETIC_PROBE_ADMIN_DSN", "postgresql://nahla:pw12345@db.internal:5432/postgres")
    monkeypatch.setenv("HARMLESS_FLAG", "true")
    secrets = probe.secret_values()
    assert "sk-ant-verysecretvalue" in secrets
    assert "postgresql://nahla:pw12345@db.internal:5432/postgres" in secrets
    assert "true" not in secrets
    line = probe.scrub("key=sk-ant-verysecretvalue dsn=postgresql://nahla:pw12345@db.internal:5432/postgres "
                       "other=postgresql://u:otherpw@h/db", secrets)
    assert "sk-ant-verysecretvalue" not in line and "pw12345" not in line
    assert "otherpw" not in line and "postgresql://u:<redacted>@h/db" in line


def test_emit_prints_one_scrubbed_json_line(capsys, monkeypatch) -> None:
    probe.emit(probe.RESULT_PREFIX, {"case": "x", "note": "token=abc123secret"}, ["abc123secret"])
    out = capsys.readouterr().out.strip()
    assert out.startswith(probe.RESULT_PREFIX)
    assert json.loads(out[len(probe.RESULT_PREFIX):]) == {"case": "x", "note": "token=<redacted>"}


# ── Scripted answers ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", probe.CASES, ids=[c.name for c in probe.CASES])
def test_every_case_has_scripted_answers_ending_in_one_reply(case: probe.Case) -> None:
    from core.commerce_runtime import agent_provider as ap

    answers = probe.scripted_answers(case, _seed())
    assert answers and all(a["status"] == "ok" for a in answers)
    last_blocks = answers[-1]["blocks"]
    assert len(last_blocks) == 1 and last_blocks[0]["name"] == ap.REPLY_TOOL_NAME
    reply = last_blocks[0]["input"]
    assert isinstance(reply["claims_commerce_facts"], bool)
    if reply["claims_commerce_facts"]:
        assert reply["evidence_refs"], "a commerce claim must cite evidence"
    for tool in case.expect_tools:
        assert any(b["name"] == tool for a in answers for b in a["blocks"]), tool


def test_the_scripted_listing_carries_a_store_link_and_an_image_link_per_product() -> None:
    case = next(c for c in probe.CASES if c.name == "listing_with_links")
    text_body = probe.scripted_answers(case, _seed())[-1]["blocks"][0]["input"]["text"]
    assert text_body.count("https://demo-probe.example-store.sa/products/") == 4
    assert text_body.count("https://demo-probe.example-store.sa/images/") == 4


# ── Usage contract ───────────────────────────────────────────────────────────


def test_the_probe_refuses_to_run_without_an_admin_dsn(monkeypatch, capsys) -> None:
    monkeypatch.delenv(probe.ADMIN_DSN_ENV, raising=False)
    assert probe.main(["--provider", "scripted"]) == probe.EXIT_USAGE
    assert probe.ADMIN_DSN_ENV in capsys.readouterr().err


def test_the_probe_never_chooses_a_model_by_itself(monkeypatch, capsys) -> None:
    monkeypatch.setenv(probe.ADMIN_DSN_ENV, "postgresql://u:p@localhost:1/postgres")
    monkeypatch.delenv(probe.MODEL_ENV, raising=False)
    assert probe.main(["--provider", "anthropic"]) == probe.EXIT_USAGE
    assert probe.MODEL_ENV in capsys.readouterr().err


def test_an_unknown_case_name_is_a_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv(probe.ADMIN_DSN_ENV, "postgresql://u:p@localhost:1/postgres")
    assert probe.main(["--provider", "scripted", "--cases", "nope"]) == probe.EXIT_USAGE
    assert "nope" in capsys.readouterr().err


# ── Measuring the sanitiser ──────────────────────────────────────────────────


def test_the_capture_hears_a_disabled_sanitiser_logger_and_restores_it() -> None:
    """September 2026: alembic's ``fileConfig``, run earlier in the same process
    by another suite's migration, had disabled the sanitiser's logger, and the
    probe reported zero audit lines for a listing the sanitiser had audited."""
    logger = logging.getLogger(probe.SANITIZER_LOGGER)
    previous_level, previously_disabled = logger.level, logger.disabled
    logger.setLevel(logging.WARNING)
    logger.disabled = True
    try:
        with probe.capturing_sanitizer_log() as capture:
            logger.info("[OUTBOUND_URL_AUDIT] tenant=7 to=+********01 url_count=4 hosts=demo-probe.example-store.sa")
            logger.warning("[EXTERNAL_RESEARCH_BLOCKED] tenant=7 marker=sources_header")
            logger.info("an unrelated line")
        assert len(capture.audit) == 1 and len(capture.blocked) == 1
        assert logger.disabled is True and logger.level == logging.WARNING
        assert capture not in logger.handlers
    finally:
        logger.setLevel(previous_level)
        logger.disabled = previously_disabled


def test_the_probe_s_migration_names_no_config_file_so_env_py_installs_no_logging() -> None:
    """``env.py`` runs ``fileConfig`` only when a config file is named; that call
    disables every logger created before it, the sanitiser's included."""
    dsn = "postgresql://user:secret@127.0.0.1:5433/nahla_probe"
    cfg = probe.alembic_config(dsn)
    assert cfg.config_file_name is None
    assert cfg.get_main_option("script_location") == str(probe.APP_ROOT / "database" / "migrations")
    assert cfg.get_main_option("sqlalchemy.url") == dsn
