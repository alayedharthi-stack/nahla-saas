import importlib.util
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROUTES_PATH = _REPO_ROOT / "services" / "ai-orchestrator" / "api" / "routes.py"
_SPEC = importlib.util.spec_from_file_location("legacy_orchestrator_routes", _ROUTES_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
OrchestrateRequest = _MODULE.OrchestrateRequest


def test_orchestrate_request_contract_is_stable():
    req = OrchestrateRequest(
        tenant_id=7,
        customer_phone="+966500000000",
        message="مرحبا",
        conversation_id=123,
    )

    assert req.tenant_id == 7
    assert req.customer_phone == "+966500000000"
    assert req.message == "مرحبا"
    assert req.conversation_id == 123
