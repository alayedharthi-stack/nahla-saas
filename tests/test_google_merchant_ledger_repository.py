"""Pure boundary checks for the dormant, caller-transaction ledger adapter."""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from services import google_merchant_ledger_repository as repo


@pytest.mark.parametrize("value", [True, 0, -1, 301, "30", None])
def test_invalid_lease_rejected_without_database_access(value):
    with pytest.raises(repo.LedgerConflict, match="lease_duration_invalid"):
        repo.claim(None, None, owner="worker", attempt_id="attempt", lease_seconds=value)


@pytest.mark.parametrize("value", [True, 0, -1, 86401, "30", None])
def test_invalid_retry_rejected_without_database_access(value):
    with pytest.raises(repo.LedgerConflict, match="retry_delay_invalid"):
        repo.record_outcome(None, None, None, retry_delay_s=value)


def test_adapter_has_no_transport_credentials_engine_or_registration():
    source = (ROOT / "backend/services/google_merchant_ledger_repository.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(name.startswith(("requests", "httpx", "socket", "os", "models", "services.google_merchant_models")) for name in imports)
    calls = [n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id if isinstance(n.func, ast.Name) else "" for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not set(calls) & {"commit", "create_engine", "send", "getenv", "decrypt", "add_router", "include_router"}
