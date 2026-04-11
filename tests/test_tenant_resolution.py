import pytest
from fastapi import HTTPException

from core.tenant import resolve_tenant_id


class _State:
    pass


class _URL:
    def __init__(self, path: str):
        self.path = path


class _Request:
    def __init__(self, path: str = "/test"):
        self.state = _State()
        self.url = _URL(path)


def test_resolve_tenant_id_uses_jwt_claim_when_present():
    request = _Request("/secure")
    request.state.jwt_payload = {"tenant_id": 42, "sub": "u1", "role": "merchant"}
    request.state.tenant_id = None

    assert resolve_tenant_id(request) == 42


def test_resolve_tenant_id_uses_header_state_only_as_dev_fallback():
    request = _Request("/secure")
    request.state.jwt_payload = None
    request.state.tenant_id = "77"

    assert resolve_tenant_id(request) == 77


def test_resolve_tenant_id_fails_closed_when_no_scope_exists():
    request = _Request("/secure")
    request.state.jwt_payload = None
    request.state.tenant_id = None

    with pytest.raises(HTTPException) as exc:
        resolve_tenant_id(request)

    assert exc.value.status_code == 401
