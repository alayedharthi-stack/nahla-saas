"""
Salla Merchant AI — Layer 3 Human Dialogue Review (live LLM).

Requires OPENAI_API_KEY for real Luna compose. CI skips via ``layer3_llm`` mark.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from tests.salla_acceptance.layer3_provider import (  # noqa: E402
    layer3_blocker_reason,
    openai_key_present,
    resolve_layer3_llm_config,
)
from tests.salla_acceptance.run_layer3_dialogue import (  # noqa: E402
    RESULTS_PATH,
    _write_blocker_report,
    main as run_layer3_main,
    prove_one_live_compose_turn,
)
from tests.salla_acceptance.layer3_sessions import (  # noqa: E402
    all_layer3_sessions,
    session_customer_message_total,
)

pytestmark = pytest.mark.layer3_llm


@pytest.fixture(scope="module")
def _require_openai_key():
    if not openai_key_present():
        reason = layer3_blocker_reason()
        _write_blocker_report(reason)
        pytest.skip(reason)


@pytest.fixture(scope="module")
def world():
    from tests.commerce_scenario_fixtures import make_scenario_db  # noqa: E402
    from tests.salla_acceptance.fixtures import seed_dual_tenant_world  # noqa: E402

    db, _engine = make_scenario_db()
    w = seed_dual_tenant_world(db)
    yield w
    db.close()


@pytest.fixture(scope="module", autouse=True)
def _ofv2_env_safe():
    mp = pytest.MonkeyPatch()
    mp.delenv("ORDER_FLOW_V2_ENFORCE_TENANTS", raising=False)
    mp.delenv("ORDER_FLOW_V2_DISABLED_TENANTS", raising=False)
    mp.setenv("ORDER_FLOW_V2_ENABLED", "false")
    mp.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    mp.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
    mp.setenv("ALLOW_PREMIUM_MODEL", "false")
    yield
    mp.undo()


def test_layer3_openai_key_gate():
    """Document blocker when OPENAI_API_KEY absent — no fake live scores."""
    if openai_key_present():
        cfg = resolve_layer3_llm_config()
        assert cfg is not None
        assert cfg.provider == "openai_compatible"
    else:
        reason = layer3_blocker_reason()
        assert "OPENAI_API_KEY absent" in reason
        report = _write_blocker_report(reason)
        assert report["critical_count"] >= 1
        assert report["ready_for_internal_live_test"] is False


def test_layer3_session_catalog_size():
    sessions = all_layer3_sessions()
    assert len(sessions) >= 20
    assert session_customer_message_total() >= 150


@pytest.mark.skipif(
    not openai_key_present(),
    reason="OPENAI_API_KEY not set — Layer3 live Luna compose blocked",
)
def test_layer3_live_compose_one_turn(world, _require_openai_key):
    """Prove one live Luna turn before full suite (TestLivePriceTurnProof pattern)."""
    proof = prove_one_live_compose_turn(world)
    print(
        f"\nLIVE COMPOSE: provider={proof['provider']} model={proof['model']} "
        f"reply_len={proof['reply_len']} compose_source={proof['compose_source']}"
    )
    assert proof["ok"], (
        f"Live compose failed: reply_len={proof['reply_len']} "
        f"compose_calls={proof['compose_calls']}"
    )
    assert proof["reply_len"] > 5
    assert proof["provider"] == "openai_compatible" or "openai" in proof["provider"]


@pytest.mark.skipif(
    not openai_key_present(),
    reason="OPENAI_API_KEY not set — skip full Layer3 suite",
)
def test_layer3_full_dialogue_suite(_require_openai_key):
    """Run full Layer3 suite and write LAYER3_ACCEPTANCE_RESULTS.json."""
    exit_code = run_layer3_main()
    assert RESULTS_PATH.is_file()
    report = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    assert report.get("sessions_total", 0) >= 20
    assert report.get("live_compose_proven") is True
    assert exit_code == 0, f"Layer3 suite exit={exit_code} critical={report.get('critical_count')}"
