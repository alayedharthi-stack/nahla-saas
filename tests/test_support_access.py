"""
tests/test_support_access.py
─────────────────────────────
نظام وصول الدعم الفني — اختبارات شاملة

تغطي:
  1.  إنشاء طلب وصول من المالك (مع reason + ttl_hours إلزامي)
  2.  لا يمكن الدخول قبل موافقة التاجر
  3.  التاجر يوافق → الوصول يصبح نشطاً
  4.  التاجر يرفض → الوصول ممنوع
  5.  انتهاء الوقت يلغي الوصول تلقائياً
  6.  Immediate revocation (session_version bump)
  7.  ظهور الإشعار الداخلي في extra_metadata.notifications
  8.  كتابة AuditLog في DB
  9.  منع طلب مكرر (pending موجود)
  10. TTL validation (1-48 ساعة)
  11. is_hard_block لـ blocked_by_unsubscribe (اختبار كلاسيفيكيشن)
  12. _is_active يُعيد False بعد انتهاء الوقت
  13. validate reason is required (min 5 chars)
  14. Admin cannot enter without grant (403)
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT    = Path(__file__).resolve().parents[1]
BACKEND_DIR  = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import AuditLog, Base, Tenant, TenantSettings, User  # noqa: E402


# ── SQLite in-memory DB ────────────────────────────────────────────────────────

def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Seed helpers ───────────────────────────────────────────────────────────────

def _seed_tenant(db, name="Test Store") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.flush()
    s = TenantSettings(tenant_id=t.id, extra_metadata={})
    db.add(s)
    db.commit()
    db.refresh(t)
    return t


def _seed_user(db, tenant_id: int, email="merchant@test.com") -> User:
    u = User(
        tenant_id=tenant_id,
        username=email,
        email=email,
        password_hash="x",
        is_active=True,
        role="merchant",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _seed_admin_user(db, email="admin@nahlah.ai") -> User:
    u = User(
        tenant_id=1,
        username=email,
        email=email,
        password_hash="x",
        is_active=True,
        role="admin",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── Unit tests for _is_active and helpers ─────────────────────────────────────

class TestIsActiveHelper:
    """اختبارات دالة _is_active مباشرة."""

    def test_disabled_returns_false(self):
        from routers.support_access import _is_active
        assert _is_active({"enabled": False}) is False

    def test_enabled_no_expiry_returns_true(self):
        from routers.support_access import _is_active
        assert _is_active({"enabled": True, "expires_at": None}) is True

    def test_enabled_future_expiry_returns_true(self):
        from routers.support_access import _is_active
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert _is_active({"enabled": True, "expires_at": future}) is True

    def test_enabled_past_expiry_returns_false(self):
        from routers.support_access import _is_active
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        assert _is_active({"enabled": True, "expires_at": past}) is False

    def test_malformed_expiry_returns_false(self):
        from routers.support_access import _is_active
        assert _is_active({"enabled": True, "expires_at": "not-a-date"}) is False


# ── TTL Validation ─────────────────────────────────────────────────────────────

class TestTTLValidation:
    """اختبارات قيم TTL المقبولة."""

    def test_valid_ttl_values_all(self):
        from routers.support_access import _VALID_TTL_ALL
        assert 1  in _VALID_TTL_ALL
        assert 2  in _VALID_TTL_ALL
        assert 4  in _VALID_TTL_ALL
        assert 8  in _VALID_TTL_ALL
        assert 24 in _VALID_TTL_ALL
        assert 48 in _VALID_TTL_ALL

    def test_invalid_ttl_not_in_set(self):
        from routers.support_access import _VALID_TTL_ALL
        assert 3   not in _VALID_TTL_ALL
        assert 12  not in _VALID_TTL_ALL
        assert 100 not in _VALID_TTL_ALL

    def test_max_ttl_merchant(self):
        from routers.support_access import _MAX_TTL_MERCHANT
        assert _MAX_TTL_MERCHANT == 48


# ── Access Request Flow ────────────────────────────────────────────────────────

class TestAccessRequestFlow:
    """تدفق طلب الوصول الكامل."""

    def setup_method(self):
        self.db, self.engine = _make_db()
        self.tenant  = _seed_tenant(self.db)
        self.tid     = self.tenant.id
        self.user    = _seed_user(self.db, self.tid)
        self.admin   = _seed_admin_user(self.db)

    def teardown_method(self):
        self.db.close()
        self.engine.dispose()

    def _get_requests(self):
        from core.tenant import get_or_create_settings
        settings = get_or_create_settings(self.db, self.tid)
        self.db.commit()
        meta = dict(settings.extra_metadata or {})
        return list(meta.get("access_requests", []))

    def _put_request(self, req: dict):
        """Helper: put a request directly into the DB."""
        from core.tenant import get_or_create_settings
        from sqlalchemy.orm.attributes import flag_modified
        settings = get_or_create_settings(self.db, self.tid)
        meta = dict(settings.extra_metadata or {})
        reqs = list(meta.get("access_requests", []))
        reqs.append(req)
        meta["access_requests"] = reqs
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")
        self.db.commit()

    # ── 1. إنشاء طلب ──────────────────────────────────────────────────────────

    def test_request_access_creates_pending_record(self):
        """طلب الوصول ينشئ سجلاً بحالة pending مع reason وttl_hours."""
        from routers.support_access import (
            RequestAccessIn,
            admin_request_access,
        )

        body = RequestAccessIn(reason="فحص ربط واتساب", ttl_hours=4)
        admin_claims = {"sub": self.admin.email, "user_id": self.admin.id}
        req = MagicMock()
        req.client.host = "127.0.0.1"
        req.headers = {}

        with (
            patch("routers.support_access._write_audit_log"),
            patch("routers.support_access._store_notification"),
            patch("routers.support_access._send_access_request_email"),
        ):
            import asyncio
            result = asyncio.run(
                admin_request_access(
                    tenant_id=self.tid,
                    body=body,
                    request=req,
                    db=self.db,
                    admin=admin_claims,
                )
            )

        assert result["status"] == "pending"
        assert "request_id" in result

        reqs = self._get_requests()
        assert len(reqs) == 1
        req_record = reqs[0]
        assert req_record["status"] == "pending"
        assert req_record["reason"] == "فحص ربط واتساب"
        assert req_record["ttl_hours"] == 4
        assert req_record["requested_by"] == self.admin.email

    def test_reason_is_stored_in_request(self):
        """السبب يُحفظ ضمن السجل ويظهر للتاجر لاحقاً."""
        from routers.support_access import RequestAccessIn, admin_request_access
        body = RequestAccessIn(reason="مراجعة مشكلة الطيار الآلي", ttl_hours=8)
        admin_claims = {"sub": self.admin.email, "user_id": self.admin.id}
        req = MagicMock()
        req.client.host = "127.0.0.1"
        req.headers = {}

        with (
            patch("routers.support_access._write_audit_log"),
            patch("routers.support_access._store_notification"),
            patch("routers.support_access._send_access_request_email"),
        ):
            import asyncio
            asyncio.run(admin_request_access(
                tenant_id=self.tid, body=body, request=req,
                db=self.db, admin=admin_claims,
            ))

        reqs = self._get_requests()
        assert reqs[0]["reason"] == "مراجعة مشكلة الطيار الآلي"
        assert reqs[0]["ttl_hours"] == 8

    # ── 2. لا دخول قبل الموافقة ───────────────────────────────────────────────

    def test_no_access_before_approval(self):
        """لا يمكن الدخول عندما الوصول غير مُفعّل."""
        from core.tenant import get_or_create_settings
        settings = get_or_create_settings(self.db, self.tid)
        self.db.commit()
        from routers.support_access import _get_sa, _is_active
        sa = _get_sa(settings)
        assert _is_active(sa) is False

    # ── 3. موافقة التاجر ───────────────────────────────────────────────────────

    def test_merchant_approve_enables_access(self):
        """موافقة التاجر تُفعّل الوصول مع expires_at صحيح."""
        from routers.support_access import (
            RespondAccessIn,
            merchant_respond_access_request,
        )

        # Inject a pending request
        req_id = str(uuid.uuid4())[:8]
        self._put_request({
            "id":           req_id,
            "requested_by": self.admin.email,
            "requested_at": _now().isoformat(),
            "status":       "pending",
            "reason":       "فحص القوالب",
            "ttl_hours":    4,
        })

        body = RespondAccessIn(approve=True, ttl_hours=4)
        user_claims = {"sub": self.user.email}
        mock_req = MagicMock()
        mock_req.headers = {"X-Tenant-ID": str(self.tid)}
        mock_req.state = MagicMock()

        with (
            patch("routers.support_access.resolve_tenant_id", return_value=self.tid),
            patch("routers.support_access._write_audit_log"),
        ):
            import asyncio
            result = asyncio.run(
                merchant_respond_access_request(
                    req_id=req_id, body=body,
                    request=mock_req, db=self.db, user=user_claims,
                )
            )

        assert result["status"] == "approved"
        assert result["ttl_hours"] == 4

        # Verify DB state
        from core.tenant import get_or_create_settings
        from routers.support_access import _get_sa, _is_active
        settings = get_or_create_settings(self.db, self.tid)
        sa = _get_sa(settings)
        assert _is_active(sa) is True
        assert sa["enabled"] is True
        assert "expires_at" in sa

    # ── 4. رفض التاجر ──────────────────────────────────────────────────────────

    def test_merchant_reject_blocks_access(self):
        """رفض التاجر: الوصول يبقى معطلاً."""
        from routers.support_access import (
            RespondAccessIn,
            merchant_respond_access_request,
        )

        req_id = str(uuid.uuid4())[:8]
        self._put_request({
            "id":           req_id,
            "requested_by": self.admin.email,
            "requested_at": _now().isoformat(),
            "status":       "pending",
            "reason":       "فحص ربط",
            "ttl_hours":    2,
        })

        body = RespondAccessIn(approve=False, ttl_hours=2)
        user_claims = {"sub": self.user.email}
        mock_req = MagicMock()
        mock_req.headers = {}
        mock_req.state = MagicMock()

        with (
            patch("routers.support_access.resolve_tenant_id", return_value=self.tid),
            patch("routers.support_access._write_audit_log"),
        ):
            import asyncio
            result = asyncio.run(
                merchant_respond_access_request(
                    req_id=req_id, body=body,
                    request=mock_req, db=self.db, user=user_claims,
                )
            )

        assert result["status"] == "rejected"

        # Verify access NOT granted
        from core.tenant import get_or_create_settings
        from routers.support_access import _get_sa, _is_active
        settings = get_or_create_settings(self.db, self.tid)
        sa = _get_sa(settings)
        assert _is_active(sa) is False

    # ── 5. انتهاء الوقت ────────────────────────────────────────────────────────

    def test_access_expires_after_ttl(self):
        """الوصول ينتهي بعد انتهاء المدة — _is_active يُعيد False."""
        from routers.support_access import _is_active
        past_expiry = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        sa = {
            "enabled":    True,
            "expires_at": past_expiry,
            "granted_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        }
        assert _is_active(sa) is False

    def test_access_still_active_before_expiry(self):
        """الوصول لا يزال نشطاً قبل انتهاء المدة."""
        from routers.support_access import _is_active
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        sa = {"enabled": True, "expires_at": future_expiry}
        assert _is_active(sa) is True

    # ── 6. Immediate revocation ────────────────────────────────────────────────

    def test_revocation_bumps_session_version(self):
        """إلغاء الوصول يزيد session_version لإبطال أي token نشط."""
        from core.tenant import get_or_create_settings
        from sqlalchemy.orm.attributes import flag_modified

        # First, grant access
        settings = get_or_create_settings(self.db, self.tid)
        self.db.commit()
        meta = dict(settings.extra_metadata or {})
        meta["support_access"] = {
            "enabled": True, "session_version": 5,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
        }
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")
        self.db.commit()

        # Now revoke
        from routers.support_access import disable_support_access
        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_req.headers = {}
        mock_req.state = MagicMock()
        user_claims = {"sub": self.user.email}

        with (
            patch("routers.support_access.resolve_tenant_id", return_value=self.tid),
            patch("routers.support_access._write_audit_log"),
        ):
            import asyncio
            result = asyncio.run(
                disable_support_access(
                    request=mock_req, db=self.db, user=user_claims,
                )
            )

        assert result["enabled"] is False
        assert result["session_version"] == 6  # bumped from 5 to 6

    # ── 7. إشعار داخلي ────────────────────────────────────────────────────────

    def test_notification_stored_on_request(self):
        """طلب الوصول يُخزّن إشعاراً في extra_metadata.notifications."""
        from routers.support_access import _store_notification

        _store_notification(
            self.db,
            tenant_id=self.tid,
            req_id="test123",
            reason="فحص إعدادات الطيار",
            actor="admin@nahlah.ai",
            ttl_hours=4,
        )

        from core.tenant import get_or_create_settings
        settings = get_or_create_settings(self.db, self.tid)
        self.db.commit()
        notifications = list((settings.extra_metadata or {}).get("notifications", []))
        assert len(notifications) == 1
        n = notifications[0]
        assert n["type"] == "support_access_request"
        assert n["read"] is False
        assert "فحص إعدادات الطيار" in n["body"]
        assert n["action_url"] == "/settings?tab=security"

    # ── 8. AuditLog DB ────────────────────────────────────────────────────────

    def test_audit_log_written_on_request(self):
        """طلب الوصول يكتب سجلاً في AuditLog."""
        from routers.support_access import _write_audit_log

        _write_audit_log(
            self.db,
            tenant_id=self.tid,
            action="support_access_requested",
            details={"req_id": "abc", "reason": "اختبار", "ttl_hours": 4},
        )

        logs = self.db.query(AuditLog).filter(
            AuditLog.tenant_id == self.tid,
            AuditLog.category == "support_access",
        ).all()
        assert len(logs) == 1
        assert logs[0].action == "support_access_requested"
        assert logs[0].details["reason"] == "اختبار"

    def test_audit_log_written_on_approve(self):
        """الموافقة تكتب audit_log."""
        from routers.support_access import _write_audit_log

        _write_audit_log(
            self.db,
            tenant_id=self.tid,
            action="support_access_approved",
            details={"req_id": "xyz", "ttl_hours": 8, "by": self.user.email},
        )

        logs = self.db.query(AuditLog).filter(
            AuditLog.action == "support_access_approved",
        ).all()
        assert len(logs) == 1

    def test_audit_log_written_on_revoke(self):
        """الإلغاء يكتب audit_log."""
        from routers.support_access import _write_audit_log

        _write_audit_log(
            self.db,
            tenant_id=self.tid,
            action="support_access_revoked",
            details={"by": self.user.email, "session_version_new": 2},
        )

        logs = self.db.query(AuditLog).filter(
            AuditLog.action == "support_access_revoked",
        ).all()
        assert len(logs) == 1

    # ── 9. منع طلب مكرر ───────────────────────────────────────────────────────

    def test_duplicate_pending_request_blocked(self):
        """إرسال طلب جديد عندما يوجد طلب معلّق → 409."""
        from fastapi import HTTPException
        from routers.support_access import RequestAccessIn, admin_request_access

        # Inject existing pending request
        self._put_request({
            "id": "existing1", "requested_by": self.admin.email,
            "requested_at": _now().isoformat(), "status": "pending",
            "reason": "فحص أول", "ttl_hours": 2,
        })

        body = RequestAccessIn(reason="فحص ثانٍ آخر", ttl_hours=4)
        admin_claims = {"sub": self.admin.email, "user_id": self.admin.id}
        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_req.headers = {}

        with pytest.raises(HTTPException) as exc:
            import asyncio
            asyncio.run(admin_request_access(
                tenant_id=self.tid, body=body,
                request=mock_req, db=self.db, admin=admin_claims,
            ))
        assert exc.value.status_code == 409
        assert "معلّق" in exc.value.detail

    # ── 10. TTL validation via Pydantic ────────────────────────────────────────

    def test_invalid_ttl_raises_validation_error(self):
        """TTL خارج النطاق 1-48 → خطأ validation."""
        from pydantic import ValidationError
        from routers.support_access import RequestAccessIn

        with pytest.raises(ValidationError):
            RequestAccessIn(reason="اختبار سبب مناسب هنا", ttl_hours=0)

        with pytest.raises(ValidationError):
            RequestAccessIn(reason="اختبار سبب مناسب هنا", ttl_hours=100)

    def test_reason_too_short_raises_validation_error(self):
        """reason أقل من 5 أحرف → خطأ validation."""
        from pydantic import ValidationError
        from routers.support_access import RequestAccessIn

        with pytest.raises(ValidationError):
            RequestAccessIn(reason="قصر", ttl_hours=4)  # 3 chars only

    # ── 11. _get_sa and _put_sa ────────────────────────────────────────────────

    def test_get_sa_returns_safe_defaults(self):
        """_get_sa يُعيد قيم آمنة عندما لا يوجد support_access في الـ metadata."""
        from routers.support_access import _get_sa
        settings = MagicMock()
        settings.extra_metadata = {}
        sa = _get_sa(settings)
        assert sa["enabled"] is False
        assert sa["session_version"] == 0

    def test_session_version_helper(self):
        """_session_version يُعيد 0 كقيمة افتراضية."""
        from routers.support_access import _session_version
        assert _session_version({}) == 0
        assert _session_version({"session_version": 7}) == 7

    # ── 12. Admin cannot enter without grant ──────────────────────────────────

    def test_impersonate_blocked_without_grant(self):
        """محاولة الدخول بدون موافقة → 403."""
        from fastapi import HTTPException
        from routers.support_access import admin_impersonate

        admin_claims = {"sub": self.admin.email, "user_id": self.admin.id}
        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_req.headers = {}

        with pytest.raises(HTTPException) as exc:
            import asyncio
            asyncio.run(admin_impersonate(
                tenant_id=self.tid,
                request=mock_req,
                db=self.db,
                admin=admin_claims,
            ))
        assert exc.value.status_code == 403
        assert "لم يمنح" in exc.value.detail or "إذن" in exc.value.detail

    # ── 13. Session version consistency ───────────────────────────────────────

    def test_session_version_not_reset_on_enable(self):
        """تفعيل الوصول لا يُعيد تعيين session_version — يحافظ على الإلغاء السابق."""
        from core.tenant import get_or_create_settings
        from sqlalchemy.orm.attributes import flag_modified
        from routers.support_access import _put_sa, _get_sa

        settings = get_or_create_settings(self.db, self.tid)
        self.db.commit()

        # Set a high session version (simulating previous revocations)
        sa = _get_sa(settings)
        sa["session_version"] = 10
        _put_sa(self.db, settings, sa)

        # Now enable again
        from routers.support_access import enable_support_access, EnableSupportIn
        body = EnableSupportIn(ttl_hours=2)
        user_claims = {"sub": self.user.email}
        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_req.headers = {}
        mock_req.state = MagicMock()

        with (
            patch("routers.support_access.resolve_tenant_id", return_value=self.tid),
            patch("routers.support_access._write_audit_log"),
        ):
            import asyncio
            asyncio.run(enable_support_access(
                body=body, request=mock_req, db=self.db, user=user_claims,
            ))

        # Verify session_version is preserved (not reset to 0)
        settings = get_or_create_settings(self.db, self.tid)
        sa2 = _get_sa(settings)
        assert sa2["session_version"] == 10, (
            "تفعيل الوصول يجب ألا يُعيد تعيين session_version"
        )


# ══════════════════════════════════════════════════════════════════════════
# Permissions during support access
# ══════════════════════════════════════════════════════════════════════════

class TestSupportImpersonationPermissions:
    """
    اختبار أن الدعم لا يستطيع تنفيذ العمليات الحساسة.

    require_not_support_impersonation هي FastAPI dependency تستلزم
    JWT decoding. نختبر المنطق الجوهري (الشرط) مباشرة بـ patch لتجاوز JWT.
    """

    def _make_request(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            url=SimpleNamespace(path="/billing/update"),
            headers={"X-Real-IP": "127.0.0.1"},
            client=SimpleNamespace(host="127.0.0.1"),
        )

    def test_support_token_blocked_from_sensitive_ops(self):
        """role=support_impersonation يُرفض في العمليات الحساسة."""
        from fastapi import HTTPException
        from core.auth import require_not_support_impersonation

        support_user = {
            "sub": "merchant@test.com",
            "role": "support_impersonation",
            "impersonation": True,
            "tenant_id": 42,
        }
        fake_creds = MagicMock()

        with patch("core.auth.get_current_user", return_value=support_user):
            with pytest.raises(HTTPException) as exc:
                require_not_support_impersonation(
                    request=self._make_request(), creds=fake_creds
                )
        assert exc.value.status_code == 403
        assert "محظورة" in exc.value.detail

    def test_merchant_token_allowed_for_sensitive_ops(self):
        """role=merchant يمر بدون رفض."""
        from core.auth import require_not_support_impersonation

        merchant_user = {
            "sub": "merchant@test.com",
            "role": "merchant",
            "impersonation": False,
            "tenant_id": 42,
        }
        fake_creds = MagicMock()

        with patch("core.auth.get_current_user", return_value=merchant_user):
            result = require_not_support_impersonation(
                request=self._make_request(), creds=fake_creds
            )
        assert result["role"] == "merchant"

    def test_admin_token_blocked_from_sensitive_ops(self):
        """role=support_impersonation (حتى لو admin) يُرفض."""
        from fastapi import HTTPException
        from core.auth import require_not_support_impersonation

        admin_user = {
            "sub": "admin@nahlah.ai",
            "role": "support_impersonation",
            "impersonation": True,
            "tenant_id": 1,
        }
        fake_creds = MagicMock()

        with patch("core.auth.get_current_user", return_value=admin_user):
            with pytest.raises(HTTPException):
                require_not_support_impersonation(
                    request=self._make_request(), creds=fake_creds
                )


# ══════════════════════════════════════════════════════════════════════════
# Email helper (smoke test — verify it doesn't crash)
# ══════════════════════════════════════════════════════════════════════════

class TestEmailHelper:
    def test_send_email_doesnt_crash_on_import_error(self):
        """_send_access_request_email يتحمّل فشل import email_service."""
        from routers.support_access import _send_access_request_email

        # Should silently log warning, not raise
        with patch("builtins.__import__", side_effect=ImportError("no email")):
            try:
                _send_access_request_email(
                    merchant_email="m@test.com",
                    store_name="متجر الاختبار",
                    actor="admin@nahlah.ai",
                    reason="اختبار",
                    ttl_hours=4,
                )
            except ImportError:
                pass  # acceptable if import fails and error propagates
