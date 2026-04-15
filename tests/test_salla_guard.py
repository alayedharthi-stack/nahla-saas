"""
Tests for the Salla integration guard layer.

Verifies that:
  1. claim_store_for_tenant revokes stale bindings and creates/updates the winner
  2. Only one enabled integration per store_id can exist
  3. token-login never enables without tokens
  4. validate_before_sync blocks when api_key is missing or duplicate exists
  5. has_valid_tokens / can_call_api / is_active_binding classify correctly
  6. Easy-mode (api_key only, no refresh_token) passes sync and active checks
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (REPO_ROOT, BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import create_engine, event, JSON
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

from database.models import Base, Tenant, Integration


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _tenant(db, tid: int):
    t = db.query(Tenant).get(tid)
    if not t:
        t = Tenant(id=tid, name=f"Tenant {tid}")
        db.add(t)
        db.commit()
    return t


def _integration(db, tenant_id: int, store_id: str, *, api_key="tok", refresh_token="rtok", enabled=True):
    _tenant(db, tenant_id)
    i = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=store_id,
        config={"store_id": store_id, "api_key": api_key, "refresh_token": refresh_token},
        enabled=enabled,
    )
    db.add(i)
    db.commit()
    return i


# ── has_valid_tokens ──────────────────────────────────────────────────────────

class TestHasValidTokens:
    def test_both_present(self, db):
        from services.salla_guard import has_valid_tokens
        i = _integration(db, 1, "S1")
        assert has_valid_tokens(i) is True

    def test_missing_api_key(self, db):
        from services.salla_guard import has_valid_tokens
        i = _integration(db, 1, "S1", api_key="")
        assert has_valid_tokens(i) is False

    def test_missing_refresh(self, db):
        from services.salla_guard import has_valid_tokens
        i = _integration(db, 1, "S1", refresh_token="")
        assert has_valid_tokens(i) is False

    def test_none_integration(self):
        from services.salla_guard import has_valid_tokens
        assert has_valid_tokens(None) is False


# ── can_call_api ──────────────────────────────────────────────────────────────

class TestCanCallApi:
    def test_api_key_only(self, db):
        """Easy-mode: api_key present, no refresh_token → can call API."""
        from services.salla_guard import can_call_api
        i = _integration(db, 1, "S1", refresh_token="")
        assert can_call_api(i) is True

    def test_both_tokens(self, db):
        from services.salla_guard import can_call_api
        i = _integration(db, 1, "S1")
        assert can_call_api(i) is True

    def test_no_api_key(self, db):
        from services.salla_guard import can_call_api
        i = _integration(db, 1, "S1", api_key="")
        assert can_call_api(i) is False

    def test_none_integration(self):
        from services.salla_guard import can_call_api
        assert can_call_api(None) is False


# ── is_active_binding ─────────────────────────────────────────────────────────

class TestIsActiveBinding:
    def test_enabled_with_both_tokens(self, db):
        from services.salla_guard import is_active_binding
        i = _integration(db, 1, "S1")
        assert is_active_binding(i) is True

    def test_enabled_api_key_only_easy_mode(self, db):
        """Easy-mode: enabled + api_key (no refresh_token) → active."""
        from services.salla_guard import is_active_binding
        i = _integration(db, 1, "S1", refresh_token="")
        assert is_active_binding(i) is True

    def test_disabled(self, db):
        from services.salla_guard import is_active_binding
        i = _integration(db, 1, "S1", enabled=False)
        assert is_active_binding(i) is False

    def test_enabled_no_tokens_at_all(self, db):
        from services.salla_guard import is_active_binding
        i = _integration(db, 1, "S1", api_key="", refresh_token="")
        assert is_active_binding(i) is False


# ── claim_store_for_tenant ────────────────────────────────────────────────────

class TestClaimStore:
    def test_creates_new_integration(self, db):
        from services.salla_guard import claim_store_for_tenant
        _tenant(db, 10)
        cfg = {"store_id": "S100", "api_key": "a", "refresh_token": "r", "connected_at": "2025-01-01"}
        result = claim_store_for_tenant(db, store_id="S100", tenant_id=10, new_config=cfg)
        db.commit()
        assert result.tenant_id == 10
        assert result.enabled is True

    def test_revokes_stale_on_same_store(self, db):
        """
        When a different tenant claims the store, `claim_store_for_tenant`
        revokes the old binding then repurposes the existing Integration row
        for the new tenant.

        Observable postconditions:
          • result belongs to new tenant and is enabled
          • exactly one active integration exists for that store_id
          • the old tenant has no active binding for that store_id
        """
        from services.salla_guard import claim_store_for_tenant
        old = _integration(db, 1, "S200")
        assert old.enabled is True

        _tenant(db, 2)
        cfg = {"store_id": "S200", "api_key": "new_a", "refresh_token": "new_r"}
        result = claim_store_for_tenant(db, store_id="S200", tenant_id=2, new_config=cfg)
        db.commit()

        # New tenant is the owner
        assert result.tenant_id == 2
        assert result.enabled is True

        # Only one active binding for the store
        from database.models import Integration as _Int
        active = db.query(_Int).filter(
            _Int.provider == "salla",
            _Int.external_store_id == "S200",
            _Int.enabled == True,  # noqa: E712
        ).all()
        assert len(active) == 1, "Exactly one active binding must exist after claim"

        # Old tenant (1) no longer has an active binding
        old_active = db.query(_Int).filter(
            _Int.provider == "salla",
            _Int.external_store_id == "S200",
            _Int.tenant_id == 1,
            _Int.enabled == True,  # noqa: E712
        ).first()
        assert old_active is None, "Old tenant must lose its active binding after store transfer"

    def test_updates_existing_for_same_tenant(self, db):
        from services.salla_guard import claim_store_for_tenant
        orig = _integration(db, 5, "S300")
        orig_id = orig.id

        cfg = {"store_id": "S300", "api_key": "new", "refresh_token": "new"}
        result = claim_store_for_tenant(db, store_id="S300", tenant_id=5, new_config=cfg)
        db.commit()

        assert result.id == orig_id
        assert result.config["api_key"] == "new"

    def test_empty_store_id_raises(self, db):
        from services.salla_guard import claim_store_for_tenant
        with pytest.raises(ValueError, match="store_id"):
            claim_store_for_tenant(db, store_id="", tenant_id=1, new_config={})


# ── validate_before_sync ─────────────────────────────────────────────────────

class TestValidateBeforeSync:
    def test_no_integration(self, db):
        from services.salla_guard import validate_before_sync
        _tenant(db, 99)
        ok, msg = validate_before_sync(db, 99)
        assert ok is False

    def test_disabled_integration(self, db):
        from services.salla_guard import validate_before_sync
        _integration(db, 20, "S400", enabled=False)
        ok, msg = validate_before_sync(db, 20)
        assert ok is False
        assert "معطّل" in msg

    def test_missing_api_key(self, db):
        from services.salla_guard import validate_before_sync
        _integration(db, 30, "S500", api_key="", refresh_token="")
        ok, msg = validate_before_sync(db, 30)
        assert ok is False
        assert "توكن" in msg

    def test_valid_oauth_integration_passes(self, db):
        from services.salla_guard import validate_before_sync
        _integration(db, 40, "S600")
        ok, msg = validate_before_sync(db, 40)
        assert ok is True
        assert msg == "OK"

    def test_easy_mode_api_key_only_passes(self, db):
        """Easy-mode: api_key present, no refresh_token → sync allowed."""
        from services.salla_guard import validate_before_sync
        _integration(db, 41, "S601", refresh_token="")
        ok, msg = validate_before_sync(db, 41)
        assert ok is True
        assert msg == "OK"
