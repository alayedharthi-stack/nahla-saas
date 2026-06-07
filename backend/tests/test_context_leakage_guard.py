"""Tests for context leakage guard — Phase 1 Prompt Isolation."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.postprocess.context_leakage_guard import (  # noqa: E402
    apply_context_leakage_guard,
    detect_context_leakage,
)


class TestDetectContextLeakage:
    def test_detects_platform_internal_term(self) -> None:
        leaked = detect_context_leakage("اسم الحساب: نحلة الذهبية")
        assert "نحلة الذهبية" in leaked

    def test_allows_tenant_authorized_beneficiary(self) -> None:
        leaked = detect_context_leakage(
            "اسم: مناحل العايد",
            authorized_names=["مناحل العايد"],
        )
        assert leaked == []

    def test_detects_instructor_placeholder(self) -> None:
        leaked = detect_context_leakage("اسم: {BENEFICIARY_NAME}")
        assert "{BENEFICIARY_NAME}" in leaked

    def test_clean_reply_no_leakage(self) -> None:
        assert detect_context_leakage("حياك الله 🌷 تفضل بيانات التحويل") == []


class TestApplyContextLeakageGuard:
    def test_shadow_mode_does_not_rewrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_CONTEXT_LEAKAGE_GUARD_MODE", "shadow")
        reply = "الراجحي\n0555123456\nاسم: نحلة الذهبية"
        result = apply_context_leakage_guard(
            reply=reply,
            tenant_id=33,
            conversation_id=1,
        )
        assert result.action == "would_rewrite"
        assert result.replaced is False
        assert result.reply == reply
        assert "نحلة الذهبية" in result.leaked_terms

    def test_enforce_mode_strips_platform_term(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_CONTEXT_LEAKAGE_GUARD_MODE", "enforce")
        reply = "الراجحي 🌷\n0555123456\nاسم: نحلة الذهبية"
        result = apply_context_leakage_guard(
            reply=reply,
            tenant_id=33,
            conversation_id=1,
        )
        assert result.replaced is True
        assert result.action == "rewrote"
        assert "نحلة الذهبية" not in result.reply

    def test_off_mode_allows_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_CONTEXT_LEAKAGE_GUARD_MODE", "off")
        reply = "اسم: نحلة الذهبية"
        result = apply_context_leakage_guard(reply=reply, tenant_id=33)
        assert result.action == "allowed"
        assert result.reply == reply

    def test_enforce_allows_authorized_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_CONTEXT_LEAKAGE_GUARD_MODE", "enforce")
        monkeypatch.setattr(
            "modules.ai.brain.postprocess.context_leakage_guard.load_tenant_authorized_account_names",
            lambda _db, tenant_id=None: ["مناحل العايد للعسل"],
        )
        reply = "اسم: مناحل العايد للعسل"
        result = apply_context_leakage_guard(
            reply=reply,
            tenant_id=33,
            conversation_id=1,
        )
        assert result.action == "allowed"
        assert result.reply == reply
